# app/admin.py
from datetime import datetime, timedelta
from functools import wraps
from flask import Blueprint, render_template, redirect, url_for, request, flash, abort
from flask_login import login_required, current_user
from . import db
from sqlalchemy import func, case
from .models import (
    User,
    Joke,
    Comment,
    Vote,
    Category,
    ForumThread,
    ForumReply,
    SiteSettings,
    QuarantinedJoke,
    AdminNotification,
    ForumReaction,
    JokeCommentReaction,
    LoginAudit,
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
    upvote_sum = func.sum(case((Vote.value == 1, 1), else_=0))

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

    return render_template(
        "admin_dashboard.html",
        user_count=User.query.count(),
        joke_count=Joke.query.count(),
        thread_count=ForumThread.query.count(),
        notifications=notifications,
        unread_count=unread_count,
        downvote_leaderboard=downvote_leaderboard,
        dv_range=dv_range,
    )


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


@admin_bp.route("/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def delete_user(user_id):
    user = User.query.get_or_404(user_id)
    # Don’t let you delete yourself by accident like a clown
    if user.id == current_user.id:
        flash("You can’t delete your own account from the admin panel.", "danger")
        return redirect(url_for("admin.users"))
    # Wipe all their related stuff first
    Vote.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    Comment.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    ForumReply.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    ForumThread.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    Joke.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    ForumReaction.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    JokeCommentReaction.query.filter_by(user_id=user.id).delete(
        synchronize_session=False
    )
    db.session.delete(user)
    db.session.commit()
    flash("User and all their content deleted.", "success")
    return redirect(url_for("admin.users"))


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
        joke.body = request.form.get("body", "").strip()
        cat_id = request.form.get("category_id")
        joke.category_id = int(cat_id) if cat_id else None
        db.session.commit()
        flash("Joke updated.", "success")
        return redirect(url_for("admin.jokes"))

    return render_template(
        "admin_edit_joke.html",
        joke=joke,
        categories=categories,
    )


@admin_bp.route("/jokes/<int:joke_id>/delete", methods=["POST"])
@admin_required
def delete_joke(joke_id):
    joke = Joke.query.get_or_404(joke_id)
    # 1) Delete any quarantine records pointing at this joke
    QuarantinedJoke.query.filter_by(joke_id=joke.id).delete(synchronize_session=False)
    # 2) Delete votes on this joke
    Vote.query.filter_by(joke_id=joke.id).delete(synchronize_session=False)
    # 3) Delete comments on this joke + their reactions
    comments = Comment.query.filter_by(joke_id=joke.id).all()
    if comments:
        comment_ids = [c.id for c in comments]
        JokeCommentReaction.query.filter(
            JokeCommentReaction.comment_id.in_(comment_ids)
        ).delete(synchronize_session=False)

        Comment.query.filter(Comment.id.in_(comment_ids)).delete(
            synchronize_session=False
        )
    # 4) Finally delete the joke itself
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


@admin_bp.route("/categories", methods=["GET", "POST"])
@admin_required
def categories():
    if request.method == "POST":
        slug = request.form.get("slug", "").strip()
        name = request.form.get("name", "").strip()
        if slug and name:
            cat = Category(slug=slug, name=name)
            db.session.add(cat)
            db.session.commit()
            flash("Category added.", "success")
        else:
            flash("Slug and name required.", "danger")

    categories = Category.query.order_by(Category.name.asc()).all()
    return render_template("admin_categories.html", categories=categories)


@admin_bp.route("/categories/<int:cat_id>/delete", methods=["POST"])
@admin_required
def delete_category(cat_id):
    cat = Category.query.get_or_404(cat_id)

    # Null out category on jokes that used it
    Joke.query.filter_by(category_id=cat.id).update(
        {"category_id": None}, synchronize_session=False
    )

    db.session.delete(cat)
    db.session.commit()
    flash("Category deleted (affected jokes now have no category).", "success")
    return redirect(url_for("admin.categories"))


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
