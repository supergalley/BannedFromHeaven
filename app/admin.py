# app/admin.py
from datetime import datetime, timedelta
from functools import wraps
from flask import Blueprint, render_template, redirect, url_for, request, flash, abort
from flask_login import login_required, current_user
from . import db
from sqlalchemy import func, case, or_
from .models import (
    User,
    UserProfile,
    Joke,
    Comment,
    Vote,
    Category,
    Subcategory,
    CategoryModSuggestion,
    ForumThread,
    ForumReply,
    ForumThreadRead,
    SiteSettings,
    QuarantinedJoke,
    AdminNotification,
    ForumReaction,
    JokeCommentReaction,
    LoginAudit,
    LoginEvent,
    Message,
    MessageBlock,
    BlacklistEntry,
    ArchivedJoke,
    DuplicateAppeal,
    JokeDupePair,
    DupeModerationLog,
)
import re
from .banning import (
    add_blacklist_entry,
    collect_user_ips,
    format_user_deletion_log,
    write_banning_log,
    normalize_blacklist_value,
    VALID_BLACKLIST_KINDS,
)

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)

    return wrapped


# -------- Dashboard --------


@admin_bp.route("/")
@admin_required
def dashboard():
    # Existing moderator notifications
    notifications = (
        AdminNotification.query.order_by(AdminNotification.created_at.desc())
        .limit(100)
        .all()
    )
    unread_count = AdminNotification.query.filter_by(is_read=False).count()

    # ----- Downvote leaderboard filter -----
    dv_range = request.args.get("dv_range", "all")  # '24h', 'week', 'all'

    # Conditional sums:
    downvote_sum = func.sum(case((Vote.value == -1, 1), else_=0))
    upvote_sum = func.sum(case((Vote.value > 0, 1), else_=0))

    # Base query: how many DOWNVOTES & UPVOTES each user has cast
    base_query = db.session.query(
        User.username.label("username"),
        downvote_sum.label("downvote_count"),
        upvote_sum.label("upvote_count"),
    ).join(Vote, Vote.user_id == User.id)

    # Time filter on votes
    now = datetime.utcnow()
    if dv_range == "24h":
        cutoff = now - timedelta(hours=24)
        base_query = base_query.filter(Vote.created_at >= cutoff)
    elif dv_range == "week":
        cutoff = now - timedelta(days=7)
        base_query = base_query.filter(Vote.created_at >= cutoff)
    # 'all' → no extra filter

    downvote_leaderboard = (
        base_query.group_by(User.id, User.username)
        .having(downvote_sum > 0)  # only show users who actually DOWNvoted
        .order_by(downvote_sum.desc())
        .limit(50)
        .all()
    )

    pending_appeals = DuplicateAppeal.query.filter_by(status="pending").count()
    pending_cat_mods = CategoryModSuggestion.query.filter_by(status="pending").count()
    return render_template(
        "admin_dashboard.html",
        user_count=User.query.count(),
        joke_count=Joke.query.count(),
        thread_count=ForumThread.query.count(),
        notifications=notifications,
        unread_count=unread_count,
        downvote_leaderboard=downvote_leaderboard,
        dv_range=dv_range,
        pending_appeals=pending_appeals,
        pending_cat_mods=pending_cat_mods,
    )


@admin_bp.route("/appeals")
@admin_required
def appeals():
    """List duplicate-removal appeals for admin uphold/reject."""
    status = (request.args.get("status") or "pending").strip().lower()
    if status not in ("pending", "upheld", "rejected", "all"):
        status = "pending"
    q = DuplicateAppeal.query.order_by(DuplicateAppeal.created_at.desc())
    if status != "all":
        q = q.filter_by(status=status)
    items = q.limit(200).all()
    return render_template(
        "admin_appeals.html",
        items=items,
        status=status,
        pending_count=DuplicateAppeal.query.filter_by(status="pending").count(),
    )


@admin_bp.route("/appeals/<int:appeal_id>")
@admin_required
def appeal_detail(appeal_id):
    appeal = DuplicateAppeal.query.get_or_404(appeal_id)
    archived = appeal.archived_joke
    kept = Joke.query.get(appeal.kept_joke_id) if appeal.kept_joke_id else None
    appellant = appeal.appellant
    return render_template(
        "admin_appeal_detail.html",
        appeal=appeal,
        archived=archived,
        kept=kept,
        appellant=appellant,
    )


@admin_bp.route("/appeals/<int:appeal_id>/uphold", methods=["POST"])
@admin_required
def appeal_uphold(appeal_id):
    from .duplicates import restore_joke_from_archive

    appeal = DuplicateAppeal.query.get_or_404(appeal_id)
    if appeal.status != "pending":
        flash("This appeal was already decided.", "info")
        return redirect(url_for("admin.appeal_detail", appeal_id=appeal.id))
    archived = appeal.archived_joke
    if not archived:
        flash("Archive record missing — cannot restore.", "danger")
        return redirect(url_for("admin.appeals"))
    try:
        # Detach appeal FK before archive row is deleted
        appeal.archived_joke_id = None
        db.session.flush()
        joke = restore_joke_from_archive(archived, actor=current_user)
    except ValueError as e:
        db.session.rollback()
        flash(str(e), "danger")
        return redirect(url_for("admin.appeal_detail", appeal_id=appeal.id))

    appeal.status = "upheld"
    appeal.reviewed_at = datetime.utcnow()
    appeal.reviewed_by_id = current_user.id
    appeal.review_note = (request.form.get("note") or "").strip()[:255] or None

    # Notify user
    mod = User.query.filter_by(username="Moderator").first()
    if mod and appeal.user_id:
        db.session.add(
            Message(
                sender_id=mod.id,
                recipient_id=appeal.user_id,
                subject="Duplicate appeal upheld — joke restored",
                body=(
                    f"Your appeal was upheld. Joke #{joke.id} has been restored "
                    f"to the site with its previous score and comments.\n\n"
                    f"View it: {url_for('main.joke_detail', joke_id=joke.id, _external=True)}"
                ),
            )
        )
    db.session.add(
        AdminNotification(
            action="Appeal upheld",
            message=(
                f"{current_user.username} upheld appeal #{appeal.id}; "
                f"restored joke #{joke.id}."
            ),
            performed_by_id=current_user.id,
            target_user_id=appeal.user_id,
            target_joke_id=joke.id,
        )
    )
    db.session.commit()
    flash(f"Appeal upheld. Joke #{joke.id} restored.", "success")
    return redirect(url_for("admin.appeals"))


@admin_bp.route("/appeals/<int:appeal_id>/reject", methods=["POST"])
@admin_required
def appeal_reject(appeal_id):
    appeal = DuplicateAppeal.query.get_or_404(appeal_id)
    if appeal.status != "pending":
        flash("This appeal was already decided.", "info")
        return redirect(url_for("admin.appeal_detail", appeal_id=appeal.id))
    appeal.status = "rejected"
    appeal.reviewed_at = datetime.utcnow()
    appeal.reviewed_by_id = current_user.id
    appeal.review_note = (request.form.get("note") or "").strip()[:255] or None

    mod = User.query.filter_by(username="Moderator").first()
    if mod and appeal.user_id:
        note = appeal.review_note or "No additional note."
        db.session.add(
            Message(
                sender_id=mod.id,
                recipient_id=appeal.user_id,
                subject="Duplicate appeal rejected",
                body=(
                    f"Your appeal regarding removed joke "
                    f"#{appeal.original_joke_id} was rejected.\n\n"
                    f"Admin note: {note}"
                ),
            )
        )
    db.session.commit()
    flash("Appeal rejected.", "success")
    return redirect(url_for("admin.appeals"))


@admin_bp.route("/appeals/ban-user/<int:user_id>", methods=["POST"])
@admin_required
def appeal_ban_user(user_id):
    user = User.query.get_or_404(user_id)
    ban = request.form.get("ban", "1") == "1"
    user.duplicate_appeals_banned = ban
    db.session.commit()
    flash(
        f"{'Blocked' if ban else 'Allowed'} duplicate appeals for {user.username}.",
        "success",
    )
    return redirect(request.referrer or url_for("admin.appeals"))


@admin_bp.route("/login-audit")
@login_required
def admin_login_audit():
    if not current_user.is_admin:
        abort(403)

    audits = LoginAudit.query.order_by(LoginAudit.created_at.desc()).limit(500).all()
    return render_template("admin_login_audit.html", audits=audits)


@admin_bp.route("/notifications/mark-read", methods=["POST"])
@admin_required
def mark_notifications_read():
    AdminNotification.query.filter_by(is_read=False).update(
        {"is_read": True},
        synchronize_session=False,
    )
    db.session.commit()
    flash("Notifications marked as read.", "success")
    return redirect(url_for("admin.dashboard"))


@admin_bp.route("/notifications/clear", methods=["POST"])
@admin_required
def clear_notifications():
    AdminNotification.query.delete()
    db.session.commit()
    flash("All notifications cleared.", "success")
    return redirect(url_for("admin.dashboard"))


# ----- Users (i, f, g, h) --------


@admin_bp.route("/users")
@admin_required
def users():
    users = User.query.order_by(User.username.asc()).all()
    return render_template("admin_users.html", users=users)


@admin_bp.route("/users/<int:user_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_user(user_id):
    user = User.query.get_or_404(user_id)

    if request.method == "POST":
        user.username = request.form.get("username", "").strip()
        user.email = request.form.get("email", "").strip().lower()

        # Only full admins can change roles
        if current_user.is_admin:
            user.is_admin = bool(request.form.get("is_admin"))
            user.is_moderator = bool(request.form.get("is_moderator"))
            if not user.is_admin and not user.is_moderator:
                user.needs_moderator = bool(request.form.get("needs_moderator"))
            else:
                user.needs_moderator = False
        db.session.commit()
        flash("User updated.", "success")
        return redirect(url_for("admin.edit_user", user_id=user.id))

    return render_template("admin_edit_user.html", user=user)


@admin_bp.route("/users/<int:user_id>/ban/<period>", methods=["POST"])
@admin_required
def ban_user(user_id, period):
    user = User.query.get_or_404(user_id)
    now = datetime.utcnow()
    if period == "24h":
        user.ban_until = now + timedelta(hours=24)
        action_label = "24h ban"
    elif period == "7d":
        user.ban_until = now + timedelta(days=7)
        action_label = "7d ban"
    elif period == "30d":
        user.ban_until = now + timedelta(days=30)
        action_label = "30d ban"
    elif period == "clear":
        user.ban_until = None
        action_label = "Clear ban"
    else:
        abort(400)
    # Log notification for any non-trivial change
    note = AdminNotification(
        action=action_label,
        message=f"{current_user.username} set ban for {user.username} to '{action_label}'.",
        performed_by_id=current_user.id,
        target_user_id=user.id,
    )
    db.session.add(note)
    db.session.commit()
    flash("Ban updated.", "success")
    return redirect(url_for("admin.edit_user", user_id=user.id))


@admin_bp.route("/users/<int:user_id>/delete", methods=["GET", "POST"])
@admin_required
def delete_user(user_id):
    user = User.query.get_or_404(user_id)
    # Don’t let you delete yourself by accident like a clown
    if user.id == current_user.id:
        flash("You can’t delete your own account from the admin panel.", "danger")
        return redirect(url_for("admin.users"))

    ips = collect_user_ips(user.id)
    login_rows = (
        LoginAudit.query.filter_by(user_id=user.id)
        .order_by(LoginAudit.created_at.desc())
        .limit(100)
        .all()
    )

    if request.method == "GET":
        return render_template(
            "admin_delete_user.html",
            user=user,
            ips=ips,
            login_rows=login_rows,
        )

    # --- POST: perform delete (+ optional blacklist) ---
    bl_email = bool(request.form.get("bl_email"))
    bl_ip = bool(request.form.get("bl_ip"))
    bl_username = bool(request.form.get("bl_username"))
    bl_reason = (request.form.get("bl_reason") or "").strip()[:255]
    if not bl_reason:
        bl_reason = f"Deleted user {user.username!r} by {current_user.username}"

    uid = user.id
    admin_id = current_user.id
    username_snap = user.username
    email_snap = user.email

    # Snapshot counts for the log (before wipe)
    counts = {
        "jokes": Joke.query.filter_by(user_id=uid).count(),
        "comments": Comment.query.filter_by(user_id=uid).count(),
        "votes": Vote.query.filter_by(user_id=uid).count(),
        "forum_threads": ForumThread.query.filter_by(user_id=uid).count(),
        "forum_replies": ForumReply.query.filter_by(user_id=uid).count(),
        "forum_reactions": ForumReaction.query.filter_by(user_id=uid).count(),
        "joke_comment_reactions": JokeCommentReaction.query.filter_by(
            user_id=uid
        ).count(),
        "messages_sent_or_received": Message.query.filter(
            or_(Message.sender_id == uid, Message.recipient_id == uid)
        ).count(),
        "login_audit": LoginAudit.query.filter_by(user_id=uid).count(),
        "login_events": LoginEvent.query.filter_by(user_id=uid).count(),
        "profiles": UserProfile.query.filter_by(user_id=uid).count(),
    }

    blacklisted: dict[str, list[str]] = {"email": [], "ip": [], "username": []}

    try:
        # Blacklist first (so we keep values even if later steps fail mid-way)
        if bl_email and email_snap:
            add_blacklist_entry(
                kind="email",
                value=email_snap,
                reason=bl_reason,
                created_by_id=admin_id,
                source_user_id=uid,
                source_username=username_snap,
            )
            blacklisted["email"].append(normalize_blacklist_value("email", email_snap))
        if bl_username and username_snap:
            add_blacklist_entry(
                kind="username",
                value=username_snap,
                reason=bl_reason,
                created_by_id=admin_id,
                source_user_id=uid,
                source_username=username_snap,
            )
            blacklisted["username"].append(
                normalize_blacklist_value("username", username_snap)
            )
        if bl_ip:
            for ip in ips:
                add_blacklist_entry(
                    kind="ip",
                    value=ip,
                    reason=bl_reason,
                    created_by_id=admin_id,
                    source_user_id=uid,
                    source_username=username_snap,
                )
                blacklisted["ip"].append(ip)

        # Verbose log BEFORE content wipe (login rows still available)
        write_banning_log(
            format_user_deletion_log(
                user=user,
                deleted_by=current_user,
                ips=ips,
                blacklisted=blacklisted,
                counts=counts,
                login_rows=login_rows,
            )
        )

        # Pure SQL bulk deletes — avoid ORM session.delete(user), which tries to
        # NULL non-nullable FKs on related rows (e.g. forum_thread_reads.user_id).
        db.session.expunge(user)

        joke_ids = [
            r[0] for r in db.session.query(Joke.id).filter_by(user_id=uid).all()
        ]
        thread_ids = [
            r[0]
            for r in db.session.query(ForumThread.id).filter_by(user_id=uid).all()
        ]

        comment_ids = {
            r[0]
            for r in db.session.query(Comment.id).filter_by(user_id=uid).all()
        }
        if joke_ids:
            # Other users' comments on this user's jokes must go before the jokes
            comment_ids.update(
                r[0]
                for r in db.session.query(Comment.id)
                .filter(Comment.joke_id.in_(joke_ids))
                .all()
            )

        reply_ids = {
            r[0]
            for r in db.session.query(ForumReply.id).filter_by(user_id=uid).all()
        }
        if thread_ids:
            # Other users' replies on this user's threads must go before the threads
            reply_ids.update(
                r[0]
                for r in db.session.query(ForumReply.id)
                .filter(ForumReply.thread_id.in_(thread_ids))
                .all()
            )

        # --- Deepest dependents first (reactions, quotes, child content) ---
        if comment_ids:
            JokeCommentReaction.query.filter(
                JokeCommentReaction.comment_id.in_(comment_ids)
            ).delete(synchronize_session=False)
            Comment.query.filter(Comment.quoted_comment_id.in_(comment_ids)).update(
                {Comment.quoted_comment_id: None},
                synchronize_session=False,
            )
            Comment.query.filter(Comment.id.in_(comment_ids)).delete(
                synchronize_session=False
            )

        if reply_ids:
            ForumReaction.query.filter(
                ForumReaction.reply_id.in_(reply_ids)
            ).delete(synchronize_session=False)
            ForumReply.query.filter(ForumReply.quoted_reply_id.in_(reply_ids)).update(
                {ForumReply.quoted_reply_id: None},
                synchronize_session=False,
            )
            ForumReply.query.filter(ForumReply.id.in_(reply_ids)).delete(
                synchronize_session=False
            )

        if thread_ids:
            ForumThreadRead.query.filter(
                ForumThreadRead.thread_id.in_(thread_ids)
            ).delete(synchronize_session=False)
            ForumThread.query.filter(ForumThread.id.in_(thread_ids)).delete(
                synchronize_session=False
            )

        if joke_ids:
            Vote.query.filter(Vote.joke_id.in_(joke_ids)).delete(
                synchronize_session=False
            )
            QuarantinedJoke.query.filter(
                QuarantinedJoke.joke_id.in_(joke_ids)
            ).delete(synchronize_session=False)
            AdminNotification.query.filter(
                AdminNotification.target_joke_id.in_(joke_ids)
            ).delete(synchronize_session=False)
            Joke.query.filter(Joke.id.in_(joke_ids)).delete(synchronize_session=False)

        # --- User's activity on other people's content ---
        Vote.query.filter_by(user_id=uid).delete(synchronize_session=False)
        ForumReaction.query.filter_by(user_id=uid).delete(synchronize_session=False)
        JokeCommentReaction.query.filter_by(user_id=uid).delete(
            synchronize_session=False
        )
        ForumThreadRead.query.filter_by(user_id=uid).delete(synchronize_session=False)

        # --- Direct FKs to users.id ---
        Message.query.filter(
            or_(Message.sender_id == uid, Message.recipient_id == uid)
        ).delete(synchronize_session=False)
        MessageBlock.query.filter(
            or_(MessageBlock.blocker_id == uid, MessageBlock.blocked_id == uid)
        ).delete(synchronize_session=False)
        UserProfile.query.filter_by(user_id=uid).delete(synchronize_session=False)
        LoginAudit.query.filter_by(user_id=uid).delete(synchronize_session=False)
        LoginEvent.query.filter_by(user_id=uid).delete(synchronize_session=False)

        # performed_by_id is NOT NULL — must delete those rows
        AdminNotification.query.filter(
            or_(
                AdminNotification.performed_by_id == uid,
                AdminNotification.target_user_id == uid,
            )
        ).delete(synchronize_session=False)

        # quarantined_by_id is NOT NULL — reassign remaining records to the admin
        if admin_id != uid:
            QuarantinedJoke.query.filter_by(quarantined_by_id=uid).update(
                {QuarantinedJoke.quarantined_by_id: admin_id},
                synchronize_session=False,
            )
        else:
            QuarantinedJoke.query.filter_by(quarantined_by_id=uid).delete(
                synchronize_session=False
            )

        # Soft/nullable refs from jokes moderated/reviewed by this user
        Joke.query.filter_by(quarantined_by_id=uid).update(
            {Joke.quarantined_by_id: None},
            synchronize_session=False,
        )
        Joke.query.filter_by(reviewed_by_id=uid).update(
            {Joke.reviewed_by_id: None},
            synchronize_session=False,
        )

        # Bulk-delete the user row (no ORM relationship nulling)
        User.query.filter_by(id=uid).delete(synchronize_session=False)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        write_banning_log(
            f"USER DELETION FAILED for id={uid} username={username_snap!r}: {e}\n"
        )
        flash(
            f"Could not delete user (likely a related-record constraint): {e}",
            "danger",
        )
        return redirect(url_for("admin.users"))

    bits = [f"User {username_snap!r} and all their content deleted."]
    if blacklisted["email"] or blacklisted["ip"] or blacklisted["username"]:
        parts = []
        if blacklisted["email"]:
            parts.append("email")
        if blacklisted["ip"]:
            parts.append(f"{len(blacklisted['ip'])} IP(s)")
        if blacklisted["username"]:
            parts.append("username")
        bits.append("Blacklisted: " + ", ".join(parts) + ".")
    flash(" ".join(bits), "success")
    return redirect(url_for("admin.users"))


# -------- Blacklist management --------


@admin_bp.route("/blacklist", methods=["GET", "POST"])
@admin_required
def blacklist():
    if request.method == "POST":
        kind = (request.form.get("kind") or "").strip().lower()
        value = (request.form.get("value") or "").strip()
        reason = (request.form.get("reason") or "").strip()[:255]
        if kind not in VALID_BLACKLIST_KINDS:
            flash("Kind must be email, ip, or username.", "danger")
        elif not value:
            flash("Value is required.", "danger")
        else:
            row = add_blacklist_entry(
                kind=kind,
                value=value,
                reason=reason or None,
                created_by_id=current_user.id,
            )
            db.session.commit()
            write_banning_log(
                f"BLACKLIST ADD  {datetime.utcnow():%Y-%m-%d %H:%M:%S UTC}  "
                f"by={current_user.username}  kind={kind}  value={row.value!r}  "
                f"reason={reason!r}\n"
            )
            flash(f"Blacklisted {kind}: {row.value}", "success")
        return redirect(url_for("admin.blacklist"))

    entries = (
        BlacklistEntry.query.order_by(
            BlacklistEntry.kind.asc(), BlacklistEntry.created_at.desc()
        ).all()
    )
    return render_template("admin_blacklist.html", entries=entries)


@admin_bp.route("/blacklist/<int:entry_id>/delete", methods=["POST"])
@admin_required
def blacklist_delete(entry_id):
    row = BlacklistEntry.query.get_or_404(entry_id)
    kind, value = row.kind, row.value
    db.session.delete(row)
    db.session.commit()
    write_banning_log(
        f"BLACKLIST REMOVE  {datetime.utcnow():%Y-%m-%d %H:%M:%S UTC}  "
        f"by={current_user.username}  kind={kind}  value={value!r}\n"
    )
    flash(f"Removed blacklist {kind}: {value}", "success")
    return redirect(url_for("admin.blacklist"))


# -------- Jokes (a, b, c, d) --------


@admin_bp.route("/jokes")
@admin_required
def jokes():
    jokes = Joke.query.order_by(Joke.created_at.desc()).limit(200).all()
    return render_template("admin_jokes.html", jokes=jokes)


@admin_bp.route("/jokes/<int:joke_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_joke(joke_id):
    joke = Joke.query.get_or_404(joke_id)
    categories = Category.query.order_by(Category.name.asc()).all()

    if request.method == "POST":
        from .routes import normalize_salute_to, resolve_category_subcategory

        joke.body = request.form.get("body", "").strip()
        cat_id, sub_id = resolve_category_subcategory(
            request.form.get("category_id"),
            request.form.get("subcategory_id"),
        )
        joke.category_id = cat_id
        joke.subcategory_id = sub_id
        joke.salute_to = normalize_salute_to(
            request.form.get("salute_to", ""),
            actor_username=current_user.username,
        )
        db.session.commit()
        flash("Joke updated.", "success")
        return redirect(url_for("admin.jokes"))

    subcategories_by_category = {
        c.id: [
            {"id": s.id, "name": s.name}
            for s in sorted(c.subcategories, key=lambda x: (x.sort_order or 0, x.name or ""))
        ]
        for c in categories
    }
    return render_template(
        "admin_edit_joke.html",
        joke=joke,
        categories=categories,
        subcategories_by_category=subcategories_by_category,
    )


@admin_bp.route("/jokes/<int:joke_id>/delete", methods=["POST"])
@admin_required
def delete_joke(joke_id):
    joke = Joke.query.get_or_404(joke_id)
    # 1) Quarantine / comment-read / follow rows (joke_id is NOT NULL)
    from .models import JokeCommentRead, JokeFollow

    for row in QuarantinedJoke.query.filter_by(joke_id=joke.id).all():
        db.session.delete(row)
    for row in JokeCommentRead.query.filter_by(joke_id=joke.id).all():
        db.session.delete(row)
    for row in JokeFollow.query.filter_by(joke_id=joke.id).all():
        db.session.delete(row)
    # 2) Votes
    Vote.query.filter_by(joke_id=joke.id).delete(synchronize_session=False)
    # 3) Comment reactions then comments
    comments = Comment.query.filter_by(joke_id=joke.id).all()
    if comments:
        comment_ids = [c.id for c in comments]
        JokeCommentReaction.query.filter(
            JokeCommentReaction.comment_id.in_(comment_ids)
        ).delete(synchronize_session=False)

        Comment.query.filter(Comment.id.in_(comment_ids)).delete(
            synchronize_session=False
        )
    # 4) Soft-null admin notifications; close pending dupe pairs
    AdminNotification.query.filter_by(target_joke_id=joke.id).update(
        {AdminNotification.target_joke_id: None},
        synchronize_session=False,
    )
    pending = JokeDupePair.query.filter(
        JokeDupePair.status == "pending",
        or_(
            JokeDupePair.joke_older_id == joke.id,
            JokeDupePair.joke_newer_id == joke.id,
        ),
    ).all()
    for p in pending:
        p.status = "not_dupe"
        p.detail = ((p.detail or "") + " [auto-closed: joke deleted]").strip()
        p.reviewed_at = datetime.utcnow()
    # 5) Finally delete the joke itself
    db.session.delete(joke)
    db.session.commit()
    flash("Joke and related data deleted.", "success")
    return redirect(url_for("admin.jokes"))


# -------- Forum threads & replies (a–d again) --------
@admin_bp.route("/forum")
@admin_required
def forum_threads():
    threads = ForumThread.query.order_by(ForumThread.created_at.desc()).all()
    return render_template("admin_forum_threads.html", threads=threads)


@admin_bp.route("/forum/thread/<int:thread_id>/delete", methods=["POST"])
@admin_required
def delete_thread(thread_id):
    thread = ForumThread.query.get_or_404(thread_id)
    ForumReply.query.filter_by(thread_id=thread.id).delete(synchronize_session=False)
    db.session.delete(thread)
    db.session.commit()
    flash("Thread and replies deleted.", "success")
    return redirect(url_for("admin.forum_threads"))


@admin_bp.route("/forum/reply/<int:reply_id>/delete", methods=["POST"])
@admin_required
def delete_reply(reply_id):
    reply = ForumReply.query.get_or_404(reply_id)
    thread_id = reply.thread_id
    db.session.delete(reply)
    db.session.commit()
    flash("Reply deleted.", "success")
    return redirect(url_for("forum.view_thread", thread_id=thread_id))


# -------- Categories (j) --------


def _slugify_category(value: str, *, max_len: int = 50) -> str:
    s = (value or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "", s)  # match existing style (celebritydeath)
    if not s:
        s = re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")
        s = re.sub(r"-+", "-", s)
    return (s or "item")[:max_len]


@admin_bp.route("/categories", methods=["GET", "POST"])
@admin_required
def categories():
    if request.method == "POST":
        action = (request.form.get("action") or "add_category").strip()

        if action == "add_category":
            slug = request.form.get("slug", "").strip()
            name = request.form.get("name", "").strip()
            if not slug and name:
                slug = _slugify_category(name)
            if slug and name:
                if Category.query.filter_by(slug=slug).first():
                    flash(f"Slug “{slug}” already exists.", "danger")
                else:
                    db.session.add(Category(slug=slug, name=name))
                    db.session.commit()
                    flash("Category added.", "success")
            else:
                flash("Slug and name required.", "danger")

        elif action == "edit_category":
            cat_id = request.form.get("cat_id", type=int)
            cat = Category.query.get(cat_id) if cat_id else None
            name = (request.form.get("name") or "").strip()
            slug = (request.form.get("slug") or "").strip()
            if not cat:
                flash("Category not found.", "danger")
            elif not name:
                flash("Category name required.", "danger")
            else:
                if not slug:
                    slug = cat.slug
                other = Category.query.filter(
                    Category.slug == slug, Category.id != cat.id
                ).first()
                if other:
                    flash(f"Slug “{slug}” already used by another category.", "danger")
                else:
                    cat.name = name
                    cat.slug = slug
                    db.session.commit()
                    flash("Category updated.", "success")

        elif action == "add_subcategory":
            cat_id = request.form.get("cat_id", type=int)
            cat = Category.query.get(cat_id) if cat_id else None
            name = (request.form.get("name") or "").strip()
            slug = (request.form.get("slug") or "").strip()
            if not cat:
                flash("Category not found.", "danger")
            elif not name:
                flash("Subcategory name required.", "danger")
            else:
                if not slug:
                    slug = _slugify_category(name, max_len=60)
                exists = Subcategory.query.filter_by(
                    category_id=cat.id, slug=slug
                ).first()
                if exists:
                    flash(f"Subcategory slug “{slug}” already exists under this category.", "danger")
                else:
                    db.session.add(
                        Subcategory(category_id=cat.id, name=name, slug=slug)
                    )
                    db.session.commit()
                    flash("Subcategory added.", "success")

        elif action == "edit_subcategory":
            sub_id = request.form.get("sub_id", type=int)
            sub = Subcategory.query.get(sub_id) if sub_id else None
            name = (request.form.get("name") or "").strip()
            slug = (request.form.get("slug") or "").strip()
            if not sub:
                flash("Subcategory not found.", "danger")
            elif not name:
                flash("Subcategory name required.", "danger")
            else:
                if not slug:
                    slug = sub.slug
                other = Subcategory.query.filter(
                    Subcategory.category_id == sub.category_id,
                    Subcategory.slug == slug,
                    Subcategory.id != sub.id,
                ).first()
                if other:
                    flash(f"Slug “{slug}” already used under this category.", "danger")
                else:
                    sub.name = name
                    sub.slug = slug
                    db.session.commit()
                    flash("Subcategory updated.", "success")

        else:
            flash("Unknown action.", "danger")

        return redirect(url_for("admin.categories"))

    categories = (
        Category.query.order_by(Category.name.asc())
        .all()
    )
    return render_template("admin_categories.html", categories=categories)


@admin_bp.route("/categories/<int:cat_id>/delete", methods=["POST"])
@admin_required
def delete_category(cat_id):
    cat = Category.query.get_or_404(cat_id)

    # Null out category + subcategory on jokes that used it
    Joke.query.filter_by(category_id=cat.id).update(
        {"category_id": None, "subcategory_id": None}, synchronize_session=False
    )
    from .models import JokeDraft

    JokeDraft.query.filter_by(category_id=cat.id).update(
        {"category_id": None, "subcategory_id": None}, synchronize_session=False
    )
    # Subcategories cascade-delete via relationship; null jokes first above
    Subcategory.query.filter_by(category_id=cat.id).delete(synchronize_session=False)

    db.session.delete(cat)
    db.session.commit()
    flash("Category deleted (affected jokes now have no category).", "success")
    return redirect(url_for("admin.categories"))


@admin_bp.route("/subcategories/<int:sub_id>/delete", methods=["POST"])
@admin_required
def delete_subcategory(sub_id):
    sub = Subcategory.query.get_or_404(sub_id)
    Joke.query.filter_by(subcategory_id=sub.id).update(
        {"subcategory_id": None}, synchronize_session=False
    )
    from .models import JokeDraft

    JokeDraft.query.filter_by(subcategory_id=sub.id).update(
        {"subcategory_id": None}, synchronize_session=False
    )
    db.session.delete(sub)
    db.session.commit()
    flash("Subcategory deleted.", "success")
    return redirect(url_for("admin.categories"))


def _apply_delete_category(cat: Category) -> None:
    from .models import JokeDraft

    Joke.query.filter_by(category_id=cat.id).update(
        {"category_id": None, "subcategory_id": None}, synchronize_session=False
    )
    JokeDraft.query.filter_by(category_id=cat.id).update(
        {"category_id": None, "subcategory_id": None}, synchronize_session=False
    )
    Subcategory.query.filter_by(category_id=cat.id).delete(synchronize_session=False)
    db.session.delete(cat)


def _apply_delete_subcategory(sub: Subcategory) -> None:
    from .models import JokeDraft

    Joke.query.filter_by(subcategory_id=sub.id).update(
        {"subcategory_id": None}, synchronize_session=False
    )
    JokeDraft.query.filter_by(subcategory_id=sub.id).update(
        {"subcategory_id": None}, synchronize_session=False
    )
    db.session.delete(sub)


@admin_bp.route("/category-mod-suggestions")
@admin_required
def category_mod_suggestions():
    status = (request.args.get("status") or "pending").strip().lower()
    if status not in ("pending", "approved", "rejected", "all"):
        status = "pending"
    q = CategoryModSuggestion.query.order_by(
        CategoryModSuggestion.created_at.desc()
    )
    if status != "all":
        q = q.filter_by(status=status)
    items = q.limit(300).all()
    pending_count = CategoryModSuggestion.query.filter_by(status="pending").count()
    return render_template(
        "admin_category_mod_suggestions.html",
        items=items,
        status=status,
        pending_count=pending_count,
    )


@admin_bp.route(
    "/category-mod-suggestions/<int:sug_id>/approve", methods=["POST"]
)
@admin_required
def category_mod_suggestion_approve(sug_id):
    sug = CategoryModSuggestion.query.get_or_404(sug_id)
    if sug.status != "pending":
        flash("That suggestion was already reviewed.", "warning")
        return redirect(url_for("admin.category_mod_suggestions"))

    try:
        if sug.action == "add" and sug.target_kind == "category":
            name = (sug.proposed_name or "").strip()
            if not name:
                raise ValueError("Missing category name.")
            slug = _slugify_category(name)
            if Category.query.filter_by(slug=slug).first():
                raise ValueError(f"Category slug “{slug}” already exists.")
            if Category.query.filter(
                func.lower(Category.name) == name.lower()
            ).first():
                raise ValueError(f"Category “{name}” already exists.")
            db.session.add(Category(slug=slug, name=name))

        elif sug.action == "add" and sug.target_kind == "subcategory":
            name = (sug.proposed_name or "").strip()
            parent = (
                Category.query.get(sug.parent_category_id)
                if sug.parent_category_id
                else None
            )
            if not parent:
                raise ValueError("Parent category no longer exists.")
            if not name:
                raise ValueError("Missing subcategory name.")
            slug = _slugify_category(name, max_len=60)
            if Subcategory.query.filter_by(
                category_id=parent.id, slug=slug
            ).first():
                raise ValueError(
                    f"Subcategory “{name}” (slug {slug}) already exists under {parent.name}."
                )
            if Subcategory.query.filter(
                Subcategory.category_id == parent.id,
                func.lower(Subcategory.name) == name.lower(),
            ).first():
                raise ValueError(
                    f"Subcategory “{name}” already exists under {parent.name}."
                )
            db.session.add(
                Subcategory(category_id=parent.id, name=name, slug=slug)
            )

        elif sug.action == "remove" and sug.target_kind == "category":
            cat = (
                Category.query.get(sug.category_id)
                if sug.category_id
                else None
            )
            if not cat:
                raise ValueError(
                    f"Category “{sug.category_name or sug.category_id}” is already gone."
                )
            _apply_delete_category(cat)

        elif sug.action == "remove" and sug.target_kind == "subcategory":
            sub = (
                Subcategory.query.get(sug.subcategory_id)
                if sug.subcategory_id
                else None
            )
            if not sub:
                raise ValueError(
                    f"Subcategory “{sug.subcategory_name or sug.subcategory_id}” is already gone."
                )
            _apply_delete_subcategory(sub)

        else:
            raise ValueError("Unknown suggestion type.")

        sug.status = "approved"
        sug.reviewed_at = datetime.utcnow()
        sug.reviewed_by_id = current_user.id
        db.session.commit()
        flash("Suggestion approved and applied.", "success")
    except ValueError as e:
        db.session.rollback()
        flash(str(e), "danger")
    except Exception:
        db.session.rollback()
        flash("Failed to apply suggestion.", "danger")

    return redirect(
        url_for("admin.category_mod_suggestions", status="pending")
    )


@admin_bp.route(
    "/category-mod-suggestions/<int:sug_id>/reject", methods=["POST"]
)
@admin_required
def category_mod_suggestion_reject(sug_id):
    sug = CategoryModSuggestion.query.get_or_404(sug_id)
    if sug.status != "pending":
        flash("That suggestion was already reviewed.", "warning")
        return redirect(url_for("admin.category_mod_suggestions"))
    sug.status = "rejected"
    sug.reviewed_at = datetime.utcnow()
    sug.reviewed_by_id = current_user.id
    note = (request.form.get("review_note") or "").strip()
    sug.review_note = note[:255] if note else None
    db.session.commit()
    flash("Suggestion rejected.", "info")
    return redirect(
        url_for("admin.category_mod_suggestions", status="pending")
    )


@admin_bp.route("/quarantine")
@admin_required
def quarantine_list():
    items = (
        QuarantinedJoke.query.join(Joke, QuarantinedJoke.joke_id == Joke.id)
        .filter(QuarantinedJoke.permanently_deleted == False)
        .filter(QuarantinedJoke.restored_at.is_(None))
        .filter(Joke.is_quarantined == True)
        .order_by(QuarantinedJoke.created_at.desc())
        .all()
    )
    return render_template("admin_quarantine.html", items=items)


@admin_bp.route("/quarantine/<int:joke_id>/restore", methods=["POST"])
@admin_required
def restore_quarantined_joke(joke_id):
    q = QuarantinedJoke.query.filter_by(joke_id=joke_id).first_or_404()
    joke = q.joke

    joke.is_quarantined = False
    joke.quarantine_locked = True  # mark as "reviewed; mods can't touch"
    q.restored_at = datetime.utcnow()

    db.session.commit()
    flash("Joke restored and locked from further moderator quarantine.", "success")
    return redirect(url_for("admin.quarantine_list"))


@admin_bp.route("/quarantine/<int:joke_id>/delete", methods=["POST"])
@admin_required
def delete_quarantined_joke(joke_id):
    q = QuarantinedJoke.query.filter_by(joke_id=joke_id).first_or_404()
    joke = q.joke

    # Soft delete: keep in DB but never show it again
    joke.is_quarantined = True
    joke.quarantine_locked = True
    q.permanently_deleted = True

    db.session.commit()
    flash(
        "Joke marked as deleted. It will no longer appear in quarantine or public lists.",
        "warning",
    )
    return redirect(url_for("admin.quarantine_list"))


# -------- Settings / pinned post (e, k) --------


@admin_bp.route("/settings", methods=["GET", "POST"])
@admin_required
def settings():
    settings = SiteSettings.get()

    if request.method == "POST":
        try:
            settings.max_jokes_per_hour = int(
                request.form.get("max_jokes_per_hour") or 0
            )
            settings.max_jokes_per_day = int(request.form.get("max_jokes_per_day") or 0)
        except ValueError:
            flash("Max posts must be integers.", "danger")
            return redirect(url_for("admin.settings"))

        settings.pinned_enabled = bool(request.form.get("pinned_enabled"))
        settings.pinned_title = request.form.get("pinned_title", "").strip()
        settings.pinned_body = request.form.get("pinned_body", "").strip()

        # Seasonal effects
        settings.newyear_force_enabled = bool(request.form.get("newyear_force_enabled"))

        db.session.commit()
        flash("Settings saved.", "success")
        return redirect(url_for("admin.settings"))

    return render_template("admin_settings.html", settings=settings)


@admin_bp.route("/dupe-log")
@admin_required
def dupe_moderation_log():
    """Redirect: log lives on the duplicates moderation page."""
    return redirect(url_for("main.moderation_duplicates") + "#dupe-log")


@admin_bp.route("/announcements", methods=["GET", "POST"])
@admin_required
def announcements_marquee():
    """Manage site-wide scrolling announcements under the logo."""
    settings = SiteSettings.get()

    if request.method == "POST":
        settings.marquee_enabled = bool(request.form.get("marquee_enabled"))
        # Ordered list from the form (empty rows dropped)
        raw_items = request.form.getlist("announcement")
        settings.set_marquee_items(raw_items)

        theme = settings.get_marquee_theme()
        try:
            theme["speed"] = max(5, min(180, int(request.form.get("speed") or 40)))
        except ValueError:
            theme["speed"] = 40
        direction = (request.form.get("direction") or "left").strip().lower()
        theme["direction"] = direction if direction in ("left", "right") else "left"
        theme["text_color"] = (request.form.get("text_color") or "#ffb3e6").strip()[:32]
        theme["bg_color"] = (request.form.get("bg_color") or "#1a0a1a").strip()[:32]
        try:
            theme["font_size"] = max(10, min(48, int(request.form.get("font_size") or 15)))
        except ValueError:
            theme["font_size"] = 15
        fw = (request.form.get("font_weight") or "600").strip()
        theme["font_weight"] = fw if fw in ("400", "500", "600", "700", "800") else "600"
        try:
            theme["gap"] = max(8, min(200, int(request.form.get("gap") or 48)))
        except ValueError:
            theme["gap"] = 48
        theme["separator"] = (request.form.get("separator") or " ··· ")[:40]
        try:
            theme["padding_y"] = max(0, min(40, int(request.form.get("padding_y") or 8)))
        except ValueError:
            theme["padding_y"] = 8
        theme["glow"] = bool(request.form.get("glow"))
        theme["pause_on_hover"] = bool(request.form.get("pause_on_hover"))
        settings.set_marquee_theme(theme)

        db.session.commit()
        flash("Announcements marquee saved.", "success")
        return redirect(url_for("admin.announcements_marquee"))

    items = settings.get_marquee_items()
    if not items:
        items = [""]
    return render_template(
        "admin_announcements.html",
        settings=settings,
        items=items,
        theme=settings.get_marquee_theme(),
    )
