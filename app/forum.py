from datetime import datetime
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort
from flask_login import login_required, current_user
from sqlalchemy import func
from . import db
from .models import (
    ForumThread,
    ForumReply,
    ForumReaction,
    REACTION_TYPES,
    User,
    ForumThreadRead,
)

forum_bp = Blueprint("forum", __name__)


@forum_bp.route("/")
def index():
    threads = ForumThread.query.order_by(ForumThread.last_activity_at.desc()).all()
    unread_thread_ids = set()
    if current_user.is_authenticated:
        now = datetime.utcnow()
        # Visiting forum page clears navbar glow
        current_user.forum_last_seen_at = now
        db.session.commit()
        thread_ids = [t.id for t in threads]
        if thread_ids:
            reads = (
                ForumThreadRead.query.filter(ForumThreadRead.user_id == current_user.id)
                .filter(ForumThreadRead.thread_id.in_(thread_ids))
                .all()
            )
            read_map = {r.thread_id: r.last_seen_at for r in reads}
            for t in threads:
                last_seen = read_map.get(t.id)
                if last_seen is None:
                    # never opened this thread -> unread if it has any activity at all
                    unread_thread_ids.add(t.id)
                elif t.last_activity_at and t.last_activity_at > last_seen:
                    unread_thread_ids.add(t.id)
    return render_template(
        "forum_index.html", threads=threads, unread_thread_ids=unread_thread_ids
    )


@forum_bp.route("/thread/new", methods=["GET", "POST"])
@login_required
def new_thread():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        body = request.form.get("body", "").strip()

        if not title or not body:
            flash("Title and body are required.", "danger")
            return redirect(url_for("forum.new_thread"))

        thread = ForumThread(title=title, body=body, user_id=current_user.id)
        thread.last_activity_at = datetime.utcnow()
        db.session.add(thread)
        db.session.commit()
        flash("Thread created.", "success")
        return redirect(url_for("forum.view_thread", thread_id=thread.id))

    return render_template("forum_new.html")


@forum_bp.route("/thread/<int:thread_id>", methods=["GET", "POST"])
def view_thread(thread_id):
    thread = ForumThread.query.get_or_404(thread_id)

    # -------------------- Handle posting a reply --------------------
    if request.method == "POST":
        if not current_user.is_authenticated:
            flash("Login to reply.", "danger")
            return redirect(url_for("auth.login"))

        body = request.form.get("body", "").strip()
        if not body:
            flash("Reply cannot be empty.", "danger")
        else:
            reply = ForumReply(
                body=body,
                user_id=current_user.id,
                thread_id=thread.id,
            )
            db.session.add(reply)

            # Bump thread activity so it glows in thread list
            thread.last_activity_at = datetime.utcnow()

            db.session.commit()
            flash("Reply posted.", "success")

        return redirect(url_for("forum.view_thread", thread_id=thread.id))

    # -------------------- Load replies --------------------
    replies = (
        ForumReply.query.filter_by(thread_id=thread.id)
        .order_by(ForumReply.created_at.asc())
        .all()
    )

    # -------------------- Reactions --------------------
    reply_ids = [r.id for r in replies]
    reaction_summary = {}  # {reply_id: {reaction_type: count}}
    user_reactions = {}  # {reply_id: reaction_type}

    if reply_ids:
        rows = (
            db.session.query(
                ForumReaction.reply_id,
                ForumReaction.reaction_type,
                func.count(ForumReaction.id),
            )
            .filter(ForumReaction.reply_id.in_(reply_ids))
            .group_by(ForumReaction.reply_id, ForumReaction.reaction_type)
            .all()
        )
        for reply_id, reaction_type, count in rows:
            reaction_summary.setdefault(reply_id, {})[reaction_type] = count

        if current_user.is_authenticated:
            rows = (
                ForumReaction.query.filter_by(user_id=current_user.id)
                .filter(ForumReaction.reply_id.in_(reply_ids))
                .all()
            )
            for fr in rows:
                user_reactions[fr.reply_id] = fr.reaction_type

    # -------------------- NEW REPLY GLOW LOGIC --------------------
    new_reply_ids = set()

    if current_user.is_authenticated:
        # Get or create per-thread read state
        tr = ForumThreadRead.query.filter_by(
            user_id=current_user.id,
            thread_id=thread.id,
        ).first()

        last_seen_before = tr.last_seen_at if tr else None

        # Any reply newer than last_seen_before should glow
        for r in replies:
            if last_seen_before is None or r.created_at > last_seen_before:
                new_reply_ids.add(r.id)

        # Update last seen timestamp to now
        now = datetime.utcnow()
        if not tr:
            tr = ForumThreadRead(
                user_id=current_user.id,
                thread_id=thread.id,
                last_seen_at=now,
            )
            db.session.add(tr)
        else:
            tr.last_seen_at = now

        db.session.commit()

    # -------------------- Render --------------------
    return render_template(
        "forum_thread.html",
        thread=thread,
        replies=replies,
        reaction_types=REACTION_TYPES,
        reaction_summary=reaction_summary,
        user_reactions=user_reactions,
        new_reply_ids=new_reply_ids,  # <-- IMPORTANT
    )


@forum_bp.route("/thread/<int:thread_id>/edit", methods=["GET", "POST"])
@login_required
def edit_thread(thread_id):
    thread = ForumThread.query.get_or_404(thread_id)

    # only author or admin
    if not (current_user.is_admin or current_user.id == thread.user_id):
        abort(403)

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        body = request.form.get("body", "").strip()

        if not title or not body:
            flash("Title and body are required.", "danger")
            return redirect(url_for("forum.edit_thread", thread_id=thread.id))

        thread.title = title
        thread.body = body
        db.session.commit()
        flash("Thread updated.", "success")
        return redirect(url_for("forum.view_thread", thread_id=thread.id))

    return render_template("forum_edit_thread.html", thread=thread)


@forum_bp.route("/thread/<int:thread_id>/delete", methods=["POST"])
@login_required
def delete_thread(thread_id):
    thread = ForumThread.query.get_or_404(thread_id)

    # only author or admin
    if not (current_user.is_admin or current_user.id == thread.user_id):
        abort(403)

    # delete replies first so FK constraints don't cry
    ForumReply.query.filter_by(thread_id=thread.id).delete(synchronize_session=False)
    db.session.delete(thread)
    db.session.commit()

    flash("Thread deleted.", "success")
    return redirect(url_for("forum.index"))


@forum_bp.route("/reply/<int:reply_id>/edit", methods=["GET", "POST"])
@login_required
def edit_reply(reply_id):
    reply = ForumReply.query.get_or_404(reply_id)

    # only author or admin
    if not (current_user.is_admin or current_user.id == reply.user_id):
        abort(403)

    if request.method == "POST":
        body = request.form.get("body", "").strip()
        if not body:
            flash("Reply cannot be empty.", "danger")
            return redirect(url_for("forum.edit_reply", reply_id=reply.id))

        reply.body = body
        db.session.commit()
        flash("Reply updated.", "success")
        return redirect(url_for("forum.view_thread", thread_id=reply.thread_id))

    return render_template("forum_edit_reply.html", reply=reply)


@forum_bp.route("/reply/<int:reply_id>/delete", methods=["POST"])
@login_required
def delete_reply(reply_id):
    reply = ForumReply.query.get_or_404(reply_id)

    # only author or admin
    if not (current_user.is_admin or current_user.id == reply.user_id):
        abort(403)

    thread_id = reply.thread_id
    db.session.delete(reply)
    db.session.commit()

    flash("Reply deleted.", "success")
    return redirect(url_for("forum.view_thread", thread_id=thread_id))


@forum_bp.route("/reply/<int:reply_id>/react", methods=["POST"])
@login_required
def react_to_reply(reply_id):
    """Create / update / remove a reaction for the current user on a reply."""
    data = request.get_json(silent=True) or {}
    reaction_type = data.get("reaction_type")

    # Validate type
    if (
        reaction_type is not None
        and reaction_type != "none"
        and reaction_type not in REACTION_TYPES
    ):
        return {"error": "invalid_reaction"}, 400

    reply = ForumReply.query.get_or_404(reply_id)

    existing = ForumReaction.query.filter_by(
        user_id=current_user.id,
        reply_id=reply.id,
    ).first()

    # Toggle logic:
    # - "none" or same as existing = remove reaction
    # - different valid reaction = update/create
    if reaction_type == "none" or (
        existing and existing.reaction_type == reaction_type
    ):
        if existing:
            db.session.delete(existing)
            db.session.commit()
        user_reaction = None
    else:
        if not existing:
            existing = ForumReaction(
                user_id=current_user.id,
                reply_id=reply.id,
                reaction_type=reaction_type,
            )
            db.session.add(existing)
        else:
            existing.reaction_type = reaction_type
        db.session.commit()
        user_reaction = existing.reaction_type

    # Recompute counts for this reply only
    rows = (
        db.session.query(
            ForumReaction.reaction_type,
            func.count(ForumReaction.id),
        )
        .filter_by(reply_id=reply.id)
        .group_by(ForumReaction.reaction_type)
        .all()
    )
    counts = {rtype: count for rtype, count in rows}

    return {
        "reply_id": reply.id,
        "user_reaction": user_reaction,
        "counts": counts,
    }


@forum_bp.route("/reply/<int:reply_id>/reaction-users/<reaction_type>")
@login_required
def reaction_users(reply_id, reaction_type):
    """Return usernames of people who reacted with a specific emoji."""
    if reaction_type not in REACTION_TYPES:
        return {"error": "invalid_reaction"}, 400

    reply = ForumReply.query.get_or_404(reply_id)

    rows = (
        ForumReaction.query.filter_by(reply_id=reply.id, reaction_type=reaction_type)
        .join(User)
        .with_entities(User.username)
        .order_by(User.username.asc())
        .all()
    )
    usernames = [r[0] for r in rows]

    return {
        "reply_id": reply.id,
        "reaction_type": reaction_type,
        "usernames": usernames,
    }
