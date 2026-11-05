"""Atomic category edits and snapshots for the admin category editor."""
import hashlib
import json
import secrets

from sqlalchemy import func
from sqlalchemy.orm import selectinload

from . import db
from .models import Category, Subcategory, Joke, JokeDraft


class CategoryEditError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _categories():
    return Category.query.options(selectinload(Category.subcategories)).order_by(
        func.lower(Category.name), Category.id
    ).all()


def _version(categories):
    values = [
        [c.id, c.name, c.slug, [[s.id, s.name, s.slug, s.sort_order]
                               for s in sorted(c.subcategories, key=lambda s: s.id)]]
        for c in sorted(categories, key=lambda c: c.id)
    ]
    return hashlib.sha256(json.dumps(values, ensure_ascii=True).encode()).hexdigest()


def category_snapshot():
    categories = _categories()
    def counts(model, column):
        return dict(db.session.query(column, func.count(model.id)).group_by(column).all())
    posts = counts(Joke, Joke.category_id)
    drafts = counts(JokeDraft, JokeDraft.category_id)
    sub_posts = counts(Joke, Joke.subcategory_id)
    sub_drafts = counts(JokeDraft, JokeDraft.subcategory_id)
    return {
        "version": _version(categories),
        "categories": [dict(
            id=c.id, name=c.name, slug=c.slug,
            posts=posts.get(c.id, 0), drafts=drafts.get(c.id, 0),
            subcategories=[dict(id=s.id, name=s.name, slug=s.slug,
                                posts=sub_posts.get(s.id, 0), drafts=sub_drafts.get(s.id, 0))
                           for s in sorted(c.subcategories, key=lambda s: (s.name.lower(), s.id))],
        ) for c in categories],
    }


def _ids(values, label):
    if not isinstance(values, list) or any(type(value) is not int for value in values):
        raise CategoryEditError(f"Invalid {label} selection.")
    if len(set(values)) != len(values):
        raise CategoryEditError(f"Duplicate IDs in {label} selection.")
    return set(values)


def apply_category_edits(payload):
    from .admin import _slugify_category

    if not isinstance(payload, dict) or not isinstance(payload.get("categories"), list):
        raise CategoryEditError("Submit a complete category list.")
    if not isinstance(payload.get("version"), str):
        raise CategoryEditError("Missing editor version. Reload the saved categories.")
    # Acquire the write lock before checking the snapshot, so simultaneous saves
    # cannot both pass the version check and overwrite one another.
    Category.query.update({Category.id: Category.id}, synchronize_session=False)
    current = _categories()
    if payload["version"] != _version(current):
        raise CategoryEditError(
            "Categories changed in another session. Your edits are still here; reload saved data before trying again.", 409)
    cats = {c.id: c for c in current}
    subs = {s.id: s for c in current for s in c.subcategories}
    delete_cats = _ids(payload.get("delete_category_ids", []), "category removal")
    delete_subs = _ids(payload.get("delete_subcategory_ids", []), "subcategory removal")
    if not delete_cats <= cats.keys() or not delete_subs <= subs.keys():
        raise CategoryEditError("A selected category or subcategory no longer exists. Reload saved data.")
    plans = []
    seen_cats, seen_subs, slugs, new_keys = set(), set(), set(), set()

    def fields(row, existing, name_len, slug_len, label):
        if not isinstance(row, dict):
            raise CategoryEditError(f"Invalid {label} row.")
        name = row.get("name")
        slug = row.get("slug", "")
        if not isinstance(name, str) or not isinstance(slug, str):
            raise CategoryEditError(f"{label} name and slug must be text.")
        name, slug = name.strip(), slug.strip()
        if not name:
            raise CategoryEditError(f"{label} name is required.")
        if len(name) > name_len or len(slug) > slug_len:
            raise CategoryEditError(f"{label} names allow {name_len} characters and slugs allow {slug_len}.")
        if not slug:
            slug = existing.slug if existing else _slugify_category(name, max_len=slug_len)
        key = row.get("key")
        if not existing:
            if not isinstance(key, str) or not key or len(key) > 80 or key in new_keys:
                raise CategoryEditError("New rows require unique editor keys.")
            new_keys.add(key)
        return dict(existing=existing, key=key, name=name, slug=slug)

    for row in payload["categories"]:
        if not isinstance(row, dict):
            raise CategoryEditError("Invalid category row.")
        cid = row.get("id")
        if cid is not None and type(cid) is not int:
            raise CategoryEditError("Invalid category ID.")
        existing = cats.get(cid) if cid is not None else None
        if cid is not None:
            if not existing or cid in seen_cats or cid in delete_cats:
                raise CategoryEditError("Invalid or repeated category in the saved list.")
            seen_cats.add(cid)
        plan = fields(row, existing, 100, 50, "Category")
        if plan["slug"] in slugs:
            raise CategoryEditError(f'Category slug “{plan["slug"]}” is used more than once.')
        slugs.add(plan["slug"])
        if not isinstance(row.get("subcategories"), list):
            raise CategoryEditError(f'Include the subcategories for “{plan["name"]}”.')
        plan["subs"] = []
        sub_slugs = set()
        for child in row["subcategories"]:
            if not isinstance(child, dict):
                raise CategoryEditError("Invalid subcategory row.")
            sid = child.get("id")
            if sid is not None and type(sid) is not int:
                raise CategoryEditError("Invalid subcategory ID.")
            sub = subs.get(sid) if sid is not None else None
            if sid is not None:
                if (not sub or cid is None or sub.category_id != cid
                        or sid in seen_subs or sid in delete_subs):
                    raise CategoryEditError("A subcategory is repeated, removed, or belongs to another category.")
                seen_subs.add(sid)
            sub_plan = fields(child, sub, 120, 60, "Subcategory")
            if sub_plan["slug"] in sub_slugs:
                raise CategoryEditError(f'Subcategory slug “{sub_plan["slug"]}” is repeated in “{plan["name"]}”.')
            sub_slugs.add(sub_plan["slug"])
            plan["subs"].append(sub_plan)
        plans.append(plan)
    if seen_cats | delete_cats != cats.keys():
        raise CategoryEditError("The category list is incomplete. Use explicit removal selections or reload saved data.")
    cascading_subs = {sid for sid, sub in subs.items() if sub.category_id in delete_cats}
    if seen_subs | delete_subs | cascading_subs != subs.keys():
        raise CategoryEditError("The subcategory list is incomplete. Use explicit removal selections or reload saved data.")

    # Deleting a taxonomy row only detaches assignments; it never deletes content.
    all_deleted_subs = delete_subs | cascading_subs
    for model in (Joke, JokeDraft):
        if delete_cats:
            model.query.filter(model.category_id.in_(delete_cats)).update(
                {model.category_id: None, model.subcategory_id: None}, synchronize_session=False)
        if all_deleted_subs:
            model.query.filter(model.subcategory_id.in_(all_deleted_subs)).update(
                {model.subcategory_id: None}, synchronize_session=False)
    for sid in delete_subs - cascading_subs:
        db.session.delete(subs[sid])
    for cid in delete_cats:
        db.session.delete(cats[cid])
    db.session.flush()

    # Temporary slugs allow two existing rows to swap slugs in one atomic save.
    token = secrets.token_hex(6)
    for plan in plans:
        for item in [plan, *plan["subs"]]:
            obj = item["existing"]
            if obj and obj.slug != item["slug"]:
                obj.slug = f"edit{token}{obj.id}"
    db.session.flush()
    created_ids = {}
    for plan in plans:
        category = plan["existing"] or Category()
        category.name, category.slug = plan["name"], plan["slug"]
        if not plan["existing"]:
            db.session.add(category)
            db.session.flush()
            created_ids[plan["key"]] = category.id
        for item in plan["subs"]:
            sub = item["existing"] or Subcategory(category_id=category.id)
            sub.name, sub.slug = item["name"], item["slug"]
            if not item["existing"]:
                db.session.add(sub)
                db.session.flush()
                created_ids[item["key"]] = sub.id
    db.session.commit()
    return dict(snapshot=category_snapshot(), created_ids=created_ids, message="All category changes saved.")
