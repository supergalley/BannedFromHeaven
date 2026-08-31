"""Duplicate-pair helpers: archive joke + related rows, resolve pairs."""

from __future__ import annotations

from datetime import datetime

from flask_login import current_user
from sqlalchemy import or_

from . import db
from .models import (
    AdminNotification,
    ArchivedComment,
    ArchivedCommentReaction,
    ArchivedJoke,
    ArchivedVote,
    Comment,
    DuplicateAppeal,
    DupeModerationLog,
    Joke,
    JokeCommentReaction,
    JokeDupePair,
    Message,
    QuarantinedJoke,
    User,
    Vote,
)


def _moderator_system_user() -> User:
    """Shared inbox sender for automated mod notices (same as rejections)."""
    import secrets

    moderator = User.query.filter_by(username="Moderator").first()
    if not moderator:
        moderator = User(
            username="Moderator",
            email="moderator@bannedfromheaven.local",
            needs_moderator=False,
        )
        moderator.set_password(secrets.token_urlsafe(32))
        db.session.add(moderator)
        db.session.flush()
    return moderator


def _fmt_joke_date(dt) -> str:
    if not dt:
        return "unknown date"
    try:
        return dt.strftime("%d-%m-%Y %H%M")
    except Exception:
        return str(dt)


def notify_duplicate_removal(
    archived: ArchivedJoke,
    *,
    kept_joke_id: int | None,
) -> None:
    """
    Inbox message to the poster of the removed joke.
    From: supergalley (so replies land in admin inbox).
    """
    if not archived.user_id:
        return
    author = User.query.get(archived.user_id)
    if not author:
        return

    kept = Joke.query.get(kept_joke_id) if kept_joke_id else None
    original_date = _fmt_joke_date(kept.created_at if kept else None)
    original_text = (
        ((kept.body or "").strip() if kept else "")
        or "[no text / media post / original unavailable]"
    )
    if len(original_text) > 1200:
        original_text = original_text[:1200] + "…"

    dupe_date = _fmt_joke_date(archived.created_at)
    dupe_text = (archived.body or "").strip() or "[meme/clip / empty body]"
    if len(dupe_text) > 1200:
        dupe_text = dupe_text[:1200] + "…"

    # Inbox UI uses white-space:pre-wrap — use newlines, not HTML <br>
    body = (
        "Hi. Your joke has been identified as a duplicate.\n\n"
        f"Original Joke dated: {original_date}\n"
        f"{original_text}\n\n"
        f"Your joke dated: {dupe_date}\n"
        f"{dupe_text}\n\n"
        "Please reply to this message if you think this was a mistake.\n\n"
        "Satan"
    )

    # Prefer real admin account so replies go to supergalley
    sender = User.query.filter_by(username="supergalley").first()
    if not sender:
        sender = _moderator_system_user()

    db.session.add(
        Message(
            sender_id=sender.id,
            recipient_id=author.id,
            subject="Your joke was removed as a duplicate",
            body=body,
        )
    )


def restore_joke_from_archive(
    archived: ArchivedJoke,
    *,
    actor: User | None = None,
) -> Joke:
    """
    Recreate live joke (+ votes, comments, reactions) from archive, restore score
    and author reputation, then delete archive rows.
    Prefers restoring the original joke id when free.
    """
    actor = actor or (
        current_user if current_user and current_user.is_authenticated else None
    )
    if Joke.query.get(archived.original_joke_id):
        raise ValueError(
            f"Joke id #{archived.original_joke_id} already exists; cannot restore."
        )

    joke = Joke(
        id=archived.original_joke_id,
        title=archived.title,
        body=archived.body or "",
        salute_to=archived.salute_to,
        created_at=archived.created_at or datetime.utcnow(),
        score=0,  # rebuilt from votes below
        is_meme=bool(archived.is_meme),
        image_filename=archived.image_filename,
        is_clip=bool(archived.is_clip),
        video_filename=archived.video_filename,
        video_thumb=archived.video_thumb,
        video_duration=archived.video_duration,
        video_size=archived.video_size,
        is_quarantined=False,
        quarantined_at=None,
        quarantined_by_id=None,
        quarantine_locked=bool(archived.quarantine_locked),
        review_status=archived.review_status or "approved",
        reviewed_at=archived.reviewed_at,
        reviewed_by_id=archived.reviewed_by_id,
        reject_reasons=archived.reject_reasons,
        user_id=archived.user_id,
        category_id=archived.category_id,
        subcategory_id=getattr(archived, "subcategory_id", None),
    )
    db.session.add(joke)
    db.session.flush()

    author = User.query.get(archived.user_id)
    score = 0
    for av in list(archived.votes):
        # Skip if user already has a vote row for this joke (shouldn't)
        existing = Vote.query.filter_by(user_id=av.user_id, joke_id=joke.id).first()
        if existing:
            continue
        db.session.add(
            Vote(
                user_id=av.user_id,
                joke_id=joke.id,
                value=av.value,
                created_at=av.created_at or datetime.utcnow(),
            )
        )
        score += av.value or 0
        if author is not None:
            author.reputation = (author.reputation or 0) + (av.value or 0)

    # Prefer archived score snapshot if votes empty for some reason
    joke.score = score if archived.votes else (archived.score or 0)

    # Map original_comment_id -> new Comment for quote rebuild
    id_map: dict[int, int] = {}
    # First pass: create comments without quotes
    sorted_comments = sorted(
        list(archived.comments),
        key=lambda c: (c.created_at or datetime.min, c.id or 0),
    )
    for ac in sorted_comments:
        c = Comment(
            body=ac.body or "",
            created_at=ac.created_at or datetime.utcnow(),
            user_id=ac.user_id,
            joke_id=joke.id,
            quoted_comment_id=None,
        )
        db.session.add(c)
        db.session.flush()
        if ac.original_comment_id:
            id_map[ac.original_comment_id] = c.id
        for ar in list(ac.reactions):
            db.session.add(
                JokeCommentReaction(
                    user_id=ar.user_id,
                    comment_id=c.id,
                    reaction_type=ar.reaction_type,
                    created_at=ar.created_at or datetime.utcnow(),
                )
            )

    # Second pass: restore quote links where possible
    for ac in sorted_comments:
        if not ac.original_comment_id or not ac.quoted_comment_id:
            continue
        new_id = id_map.get(ac.original_comment_id)
        quoted_new = id_map.get(ac.quoted_comment_id)
        if new_id and quoted_new:
            c = Comment.query.get(new_id)
            if c:
                c.quoted_comment_id = quoted_new

    # Drop archive subtree (cascade votes/comments/reactions)
    db.session.delete(archived)
    return joke


def archive_and_delete_joke(
    joke: Joke,
    *,
    reason: str = "duplicate",
    kept_joke_id: int | None = None,
    dupe_pair_id: int | None = None,
    actor: User | None = None,
) -> ArchivedJoke:
    """
    Copy joke + votes + comments (+ comment reactions) into archive tables,
    reverse author reputation for votes, then remove live rows.
    """
    actor = actor or (current_user if current_user and current_user.is_authenticated else None)
    author = joke.author
    author_username = author.username if author else None

    archived = ArchivedJoke(
        original_joke_id=joke.id,
        title=joke.title,
        body=joke.body or "",
        salute_to=joke.salute_to,
        created_at=joke.created_at,
        score=joke.score or 0,
        is_meme=bool(joke.is_meme),
        image_filename=joke.image_filename,
        is_clip=bool(joke.is_clip),
        video_filename=joke.video_filename,
        video_thumb=joke.video_thumb,
        video_duration=joke.video_duration,
        video_size=joke.video_size,
        is_quarantined=bool(joke.is_quarantined),
        quarantined_at=joke.quarantined_at,
        quarantined_by_id=joke.quarantined_by_id,
        quarantine_locked=bool(joke.quarantine_locked),
        review_status=joke.review_status,
        reviewed_at=joke.reviewed_at,
        reviewed_by_id=joke.reviewed_by_id,
        reject_reasons=joke.reject_reasons,
        user_id=joke.user_id,
        category_id=joke.category_id,
        subcategory_id=getattr(joke, "subcategory_id", None),
        archived_at=datetime.utcnow(),
        archived_by_id=actor.id if actor else None,
        archive_reason=reason,
        related_kept_joke_id=kept_joke_id,
        dupe_pair_id=dupe_pair_id,
        author_username=author_username,
    )
    db.session.add(archived)
    db.session.flush()  # need archived.id

    # Votes → archive + reverse reputation
    votes = Vote.query.filter_by(joke_id=joke.id).all()
    for v in votes:
        uname = None
        try:
            uname = v.voter.username if getattr(v, "voter", None) else None
        except Exception:
            uname = None
        if uname is None:
            u = User.query.get(v.user_id)
            uname = u.username if u else None
        db.session.add(
            ArchivedVote(
                archived_joke_id=archived.id,
                original_vote_id=v.id,
                value=v.value,
                created_at=v.created_at,
                user_id=v.user_id,
                username=uname,
            )
        )
        if author is not None:
            author.reputation = (author.reputation or 0) - (v.value or 0)
        db.session.delete(v)

    # Comments + reactions → archive
    comments = Comment.query.filter_by(joke_id=joke.id).all()
    comment_ids = [c.id for c in comments]
    reactions_by_comment = {}
    if comment_ids:
        for rx in JokeCommentReaction.query.filter(
            JokeCommentReaction.comment_id.in_(comment_ids)
        ).all():
            reactions_by_comment.setdefault(rx.comment_id, []).append(rx)

    for c in comments:
        uname = None
        try:
            uname = c.author.username if getattr(c, "author", None) else None
        except Exception:
            uname = None
        if uname is None:
            u = User.query.get(c.user_id)
            uname = u.username if u else None
        ac = ArchivedComment(
            archived_joke_id=archived.id,
            original_comment_id=c.id,
            body=c.body or "",
            created_at=c.created_at,
            user_id=c.user_id,
            username=uname,
            quoted_comment_id=c.quoted_comment_id,
        )
        db.session.add(ac)
        db.session.flush()
        for rx in reactions_by_comment.get(c.id, []):
            rx_user = User.query.get(rx.user_id)
            db.session.add(
                ArchivedCommentReaction(
                    archived_comment_id=ac.id,
                    original_reaction_id=rx.id,
                    user_id=rx.user_id,
                    username=rx_user.username if rx_user else None,
                    reaction_type=rx.reaction_type,
                    created_at=rx.created_at,
                )
            )
            db.session.delete(rx)
        db.session.delete(c)

    # Quarantine + comment-read + follow (joke_id NOT NULL) — ORM delete
    from .models import JokeCommentRead, JokeFollow

    for row in QuarantinedJoke.query.filter_by(joke_id=joke.id).all():
        db.session.delete(row)
    for row in JokeCommentRead.query.filter_by(joke_id=joke.id).all():
        db.session.delete(row)
    for row in JokeFollow.query.filter_by(joke_id=joke.id).all():
        db.session.delete(row)

    # Admin notifications pointing at this joke (avoid FK orphans)
    AdminNotification.query.filter_by(target_joke_id=joke.id).update(
        {AdminNotification.target_joke_id: None},
        synchronize_session=False,
    )

    # Cancel other pending dupe pairs involving this joke
    pending = JokeDupePair.query.filter(
        JokeDupePair.status == "pending",
        or_(
            JokeDupePair.joke_older_id == joke.id,
            JokeDupePair.joke_newer_id == joke.id,
        ),
    ).all()
    for p in pending:
        if dupe_pair_id and p.id == dupe_pair_id:
            continue
        p.status = "not_dupe"
        p.detail = (p.detail or "") + " [auto-closed: related joke archived]"
        p.reviewed_at = datetime.utcnow()
        if actor:
            p.reviewed_by_id = actor.id

    db.session.delete(joke)
    return archived


def _log_dupe_action(
    *,
    actor: User | None,
    action: str,
    pair: JokeDupePair,
    deleted_id: int | None = None,
    kept_id: int | None = None,
    reason: str | None = None,
) -> None:
    """Append a row for the admin de-duplication log."""
    uname = "?"
    actor_id = None
    if actor is not None:
        actor_id = getattr(actor, "id", None)
        uname = getattr(actor, "username", None) or "?"
    a, b = pair.joke_older_id, pair.joke_newer_id
    if action == "not_dupe":
        summary = (
            f"{uname} checked Joke #{a} and Joke #{b} and marked them "
            f"as not a duplicate / newer much better"
        )
        reason = reason or "Not a duplicate / newer much better"
    elif action == "already_gone":
        summary = (
            f"{uname} checked Joke #{a} and Joke #{b}; target "
            f"#{deleted_id} was already gone — pair closed"
        )
        reason = reason or "Target already gone"
    elif deleted_id is not None and kept_id is not None:
        if deleted_id == b:
            why = reason or "Newer duplicate"
        elif deleted_id == a:
            why = reason or "Older duplicate"
        else:
            why = reason or "Duplicate"
        reason = why
        summary = (
            f"{uname} checked Joke #{a} and Joke #{b} and deleted "
            f"Joke #{deleted_id} because '{why}'"
        )
    else:
        summary = f"{uname} reviewed pair #{pair.id} ({action})"
        reason = reason or action

    db.session.add(
        DupeModerationLog(
            actor_id=actor_id,
            actor_username=uname[:80],
            action=action,
            pair_id=pair.id,
            joke_a_id=a,
            joke_b_id=b,
            deleted_joke_id=deleted_id,
            kept_joke_id=kept_id,
            reason=(reason or "")[:120] or None,
            summary=summary,
        )
    )


def resolve_dupe_pair(
    pair: JokeDupePair,
    *,
    action: str,
    actor: User | None = None,
) -> str:
    """
    action: delete_newer | not_dupe
    Returns flash message.
    """
    actor = actor or current_user
    if pair.status != "pending":
        return "This pair was already reviewed."

    if action == "not_dupe":
        pair.status = "not_dupe"
        pair.reviewed_at = datetime.utcnow()
        pair.reviewed_by_id = actor.id if actor else None
        _log_dupe_action(actor=actor, action="not_dupe", pair=pair)
        db.session.commit()
        return "Marked as not a duplicate / newer much better — won't be suggested again."

    if action == "delete_newer":
        delete_id, keep_id = pair.joke_newer_id, pair.joke_older_id
        status = "resolved_delete_newer"
        reason = "Newer duplicate"
    else:
        return "Unknown action."

    joke = Joke.query.get(delete_id)
    if not joke:
        pair.status = "not_dupe"
        pair.detail = (pair.detail or "") + " [target joke already gone]"
        pair.reviewed_at = datetime.utcnow()
        pair.reviewed_by_id = actor.id if actor else None
        _log_dupe_action(
            actor=actor,
            action="already_gone",
            pair=pair,
            deleted_id=delete_id,
            kept_id=keep_id,
        )
        db.session.commit()
        return f"Joke #{delete_id} is already gone — pair closed."

    archived = archive_and_delete_joke(
        joke,
        reason="duplicate",
        kept_joke_id=keep_id,
        dupe_pair_id=pair.id,
        actor=actor,
    )
    pair.status = status
    pair.archived_original_id = delete_id
    pair.kept_original_id = keep_id
    pair.reviewed_at = datetime.utcnow()
    pair.reviewed_by_id = actor.id if actor else None
    _log_dupe_action(
        actor=actor,
        action=action,
        pair=pair,
        deleted_id=delete_id,
        kept_id=keep_id,
        reason=reason,
    )
    # Notify poster (inbox) with original + appeal link
    try:
        notify_duplicate_removal(archived, kept_joke_id=keep_id)
    except Exception:
        # Don't fail the archive if messaging breaks
        pass
    db.session.commit()
    return f"Archived joke #{delete_id} as duplicate of #{keep_id}."


def ingest_dupe_pairs(pairs: list[dict]) -> dict:
    """
    Upsert candidate pairs from the X4 scanner.
    Each item: older_id, newer_id, score, method?, detail?
    Skips not_dupe / already-resolved pairs; updates score on pending.
    """
    added = 0
    updated = 0
    skipped = 0
    for item in pairs:
        try:
            a = int(item.get("older_id") or item.get("joke_older_id"))
            b = int(item.get("newer_id") or item.get("joke_newer_id"))
            score = float(item.get("score") or 0)
        except (TypeError, ValueError):
            skipped += 1
            continue
        if a == b or a <= 0 or b <= 0:
            skipped += 1
            continue
        # Normalise chronological: lower created_at = older; fallback lower id
        ja, jb = Joke.query.get(a), Joke.query.get(b)
        if not ja or not jb:
            skipped += 1
            continue
        if ja.is_quarantined or jb.is_quarantined:
            skipped += 1
            continue
        if (ja.created_at or datetime.min) <= (jb.created_at or datetime.min):
            older_id, newer_id = ja.id, jb.id
        else:
            older_id, newer_id = jb.id, ja.id

        existing = JokeDupePair.query.filter_by(
            joke_older_id=older_id, joke_newer_id=newer_id
        ).first()
        method = (item.get("method") or "fuzzy")[:40]
        detail = (item.get("detail") or "")[:255] or None

        if existing:
            if existing.status != "pending":
                skipped += 1
                continue
            if score > (existing.score or 0):
                existing.score = score
                existing.method = method
                if detail:
                    existing.detail = detail
                updated += 1
            else:
                skipped += 1
            continue

        db.session.add(
            JokeDupePair(
                joke_older_id=older_id,
                joke_newer_id=newer_id,
                score=min(1.0, max(0.0, score)),
                method=method,
                detail=detail,
                status="pending",
            )
        )
        added += 1

    db.session.commit()
    return {"added": added, "updated": updated, "skipped": skipped}
