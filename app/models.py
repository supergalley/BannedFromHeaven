# app/models.py
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import UserMixin
from sqlalchemy import UniqueConstraint
from . import db, login_manager

REACTION_TYPES = {
    "like": {"emoji": "👍", "label": "Like"},
    "love": {"emoji": "❤️", "label": "Love"},
    "care": {"emoji": "🤗", "label": "Care"},
    "haha": {"emoji": "😆", "label": "Haha"},
    "wow": {"emoji": "😮", "label": "Wow"},
    "sad": {"emoji": "😢", "label": "Sad"},
    "angry": {"emoji": "😡", "label": "Angry"},
}


def check_joke_rate_limit(user_id: int):
    settings = SiteSettings.get()
    now = datetime.utcnow()

    if settings.max_jokes_per_hour and settings.max_jokes_per_hour > 0:
        cutoff = now - timedelta(hours=1)
        hour_count = Joke.query.filter(
            Joke.user_id == user_id,
            Joke.created_at >= cutoff,
        ).count()
        if hour_count >= settings.max_jokes_per_hour:
            return (
                False,
                f"Rate limit: {settings.max_jokes_per_hour}/hour. Try again later.",
            )

    if settings.max_jokes_per_day and settings.max_jokes_per_day > 0:
        cutoff = now - timedelta(days=1)
        day_count = Joke.query.filter(
            Joke.user_id == user_id,
            Joke.created_at >= cutoff,
        ).count()
        if day_count >= settings.max_jokes_per_day:
            return (
                False,
                f"Daily limit: {settings.max_jokes_per_day}/day. Try again later.",
            )

    return True, ""


class LoginEvent(db.Model):
    __tablename__ = "login_events"

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    success = db.Column(db.Boolean, nullable=False, index=True)

    # If we can match a real user, store user_id, otherwise keep it null
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=True, index=True
    )

    # What they typed (for failed attempts this is usually all you have)
    username_entered = db.Column(db.String(150), nullable=True, index=True)

    host = db.Column(db.String(255), nullable=True, index=True)
    ip = db.Column(db.String(64), nullable=True, index=True)
    user_agent = db.Column(db.String(512), nullable=True)

    # optional: why it failed (bad password, locked, etc.)
    reason = db.Column(db.String(255), nullable=True)


class LoginAudit(db.Model):
    __tablename__ = "login_audit"

    id = db.Column(db.Integer, primary_key=True)

    # user_id can be NULL for failed logins with unknown user
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    username_or_email = db.Column(db.String(255), nullable=True)

    success = db.Column(db.Boolean, nullable=False, default=False)

    ip = db.Column(db.String(64), nullable=True)
    user_agent = db.Column(db.String(512), nullable=True)
    host = db.Column(db.String(255), nullable=True)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    user = db.relationship("User", foreign_keys=[user_id])


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(32), unique=True, nullable=False, index=True)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    forum_last_seen_at = db.Column(db.DateTime, nullable=True)
    is_admin = db.Column(db.Boolean, default=False)
    is_moderator = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    reputation = db.Column(db.Integer, default=0, nullable=False)
    ban_until = db.Column(db.DateTime, nullable=True)

    # Existing relationships
    jokes = db.relationship(
        "Joke", backref="author", lazy=True, foreign_keys="Joke.user_id"
    )
    comments = db.relationship("Comment", backref="author", lazy=True)
    votes = db.relationship("Vote", backref="voter", lazy=True)

    def is_banned(self) -> bool:
        return self.ban_until is not None and self.ban_until > datetime.utcnow()

    # NEW: profile (one-to-one)
    profile = db.relationship(
        "UserProfile",
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
    )

    # NEW: messaging relationships
    sent_messages = db.relationship(
        "Message",
        foreign_keys="Message.sender_id",
        back_populates="sender",
        lazy="dynamic",
        cascade="all, delete-orphan",
    )
    received_messages = db.relationship(
        "Message",
        foreign_keys="Message.recipient_id",
        back_populates="recipient",
        lazy="dynamic",
        cascade="all, delete-orphan",
    )

    def set_password(self, password: str):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)


class UserProfile(db.Model):
    __tablename__ = "user_profiles"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), unique=True, nullable=False
    )

    profile_picture = db.Column(db.String(255), nullable=True)
    about_me = db.Column(db.Text, nullable=True)
    facebook_url = db.Column(db.String(255), nullable=True)
    location = db.Column(db.String(120), nullable=True)
    age = db.Column(db.Integer, nullable=True)

    # New hide flags
    hide_location = db.Column(db.Boolean, nullable=True)
    hide_age = db.Column(db.Boolean, nullable=True)
    hide_facebook = db.Column(db.Boolean, nullable=True)

    user = db.relationship("User", back_populates="profile")

    # Convenience properties so templates don’t get ugly
    @property
    def show_location(self):
        # Default: hide location unless explicitly allowed
        if self.hide_location is None:
            return False
        return not self.hide_location

    @property
    def show_age(self):
        # Default: show age unless explicitly hidden
        if self.hide_age is None:
            return True
        return not self.hide_age

    @property
    def show_facebook(self):
        # Default: hide facebook unless explicitly allowed
        if self.hide_facebook is None:
            return False
        return not self.hide_facebook


class PageView(db.Model):
    __tablename__ = "page_views"
    id = db.Column(db.Integer, primary_key=True)
    page = db.Column(db.String(64), unique=True, nullable=False)
    count = db.Column(db.Integer, default=0, nullable=False)


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


class Category(db.Model):
    __tablename__ = "categories"

    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(50), unique=True, nullable=False, index=True)
    name = db.Column(db.String(100), nullable=False)

    jokes = db.relationship("Joke", backref="category", lazy=True)


class Joke(db.Model):
    __tablename__ = "jokes"
    id = db.Column(db.Integer, primary_key=True)
    # title now optional, we’ll auto-fill or ignore it
    title = db.Column(db.String(200), nullable=True)
    body = db.Column(db.Text, nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    score = db.Column(db.Integer, default=0, index=True)

    @property
    def comment_count(self):
        # if lazy="dynamic", this is a query
        return len(self.comments)

    # NEW FIELDS
    is_meme = db.Column(db.Boolean, default=False, nullable=False)
    image_filename = db.Column(db.String(255))
    is_clip = db.Column(db.Boolean, default=False, nullable=False)
    video_filename = db.Column(db.String(255))
    video_thumb = db.Column(db.String(255))
    video_duration = db.Column(db.Integer)  # seconds
    video_size = db.Column(db.Integer)  # bytes
    # Quarantine flags
    is_quarantined = db.Column(db.Boolean, default=False, nullable=False)
    quarantined_at = db.Column(db.DateTime, nullable=True)
    quarantined_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    quarantine_locked = db.Column(db.Boolean, default=False, nullable=False)

    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey("categories.id"))

    comments = db.relationship("Comment", backref="joke", lazy=True)
    votes = db.relationship("Vote", backref="joke", lazy=True)

    quarantined_by = db.relationship("User", foreign_keys=[quarantined_by_id])


class Vote(db.Model):
    __tablename__ = "votes"
    __table_args__ = (UniqueConstraint("user_id", "joke_id", name="uq_user_joke_vote"),)

    id = db.Column(db.Integer, primary_key=True)
    value = db.Column(db.Integer, nullable=False)  # +1 or -1
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    joke_id = db.Column(db.Integer, db.ForeignKey("jokes.id"), nullable=False)


class ForumThreadRead(db.Model):
    __tablename__ = "forum_thread_reads"
    __table_args__ = (
        UniqueConstraint("user_id", "thread_id", name="uq_forum_thread_read"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False, index=True
    )
    thread_id = db.Column(
        db.Integer, db.ForeignKey("forum_threads.id"), nullable=False, index=True
    )
    last_seen_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    user = db.relationship(
        "User", backref=db.backref("forum_thread_reads", lazy="dynamic")
    )
    thread = db.relationship(
        "ForumThread", backref=db.backref("read_states", lazy="dynamic")
    )


class ForumThread(db.Model):
    __tablename__ = "forum_threads"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    body = db.Column(db.Text, nullable=False)

    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    author = db.relationship(
        "User",
        backref=db.backref("forum_threads", lazy="dynamic"),
    )

    # 🔹 Add this relationship
    replies = db.relationship(
        "ForumReply",
        backref=db.backref("thread", lazy="joined"),
        lazy="dynamic",
        cascade="all, delete-orphan",
    )
    last_activity_at = db.Column(
        db.DateTime, default=datetime.utcnow, nullable=False, index=True
    )

    # 🔹 Optional nice helper
    @property
    def reply_count(self):
        return self.replies.count()


class ForumReply(db.Model):
    __tablename__ = "forum_replies"

    id = db.Column(db.Integer, primary_key=True)
    body = db.Column(db.Text, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    thread_id = db.Column(db.Integer, db.ForeignKey("forum_threads.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    author = db.relationship("User", backref="forum_replies")


class ForumReaction(db.Model):
    __tablename__ = "forum_reactions"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    reply_id = db.Column(db.Integer, db.ForeignKey("forum_replies.id"), nullable=False)
    reaction_type = db.Column(
        db.String(20), nullable=False
    )  # e.g. "like", "love", "haha"
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    user = db.relationship("User", backref="forum_reactions")
    reply = db.relationship("ForumReply", backref="reactions")

    __table_args__ = (
        db.UniqueConstraint("user_id", "reply_id", name="uq_forumreaction_user_reply"),
    )


class JokeCommentReaction(db.Model):
    __tablename__ = "joke_comment_reactions"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    comment_id = db.Column(db.Integer, db.ForeignKey("comments.id"), nullable=False)
    reaction_type = db.Column(db.String(20), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    user = db.relationship("User", backref="joke_comment_reactions", lazy=True)
    comment = db.relationship("Comment", backref="reactions", lazy=True)

    __table_args__ = (
        db.UniqueConstraint(
            "user_id", "comment_id", name="uq_jokecommentreaction_user_comment"
        ),
    )


class Comment(db.Model):
    __tablename__ = "comments"

    id = db.Column(db.Integer, primary_key=True)
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    joke_id = db.Column(db.Integer, db.ForeignKey("jokes.id"), nullable=False)


class Message(db.Model):
    __tablename__ = "messages"

    id = db.Column(db.Integer, primary_key=True)
    subject = db.Column(db.String(200), nullable=True)
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    read_at = db.Column(db.DateTime, nullable=True)

    deleted_by_sender = db.Column(db.Boolean, default=False, nullable=False)
    deleted_by_recipient = db.Column(db.Boolean, default=False, nullable=False)

    sender_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    recipient_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)

    sender = db.relationship(
        "User",
        foreign_keys=[sender_id],
        back_populates="sent_messages",
    )
    recipient = db.relationship(
        "User",
        foreign_keys=[recipient_id],
        back_populates="received_messages",
    )


class MessageBlock(db.Model):
    __tablename__ = "message_blocks"
    __table_args__ = (
        UniqueConstraint("blocker_id", "blocked_id", name="uq_message_block"),
    )

    id = db.Column(db.Integer, primary_key=True)
    blocker_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    blocked_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class QuarantinedJoke(db.Model):
    __tablename__ = "quarantined_jokes"

    id = db.Column(db.Integer, primary_key=True)
    joke_id = db.Column(
        db.Integer, db.ForeignKey("jokes.id"), nullable=False, unique=True
    )

    original_title = db.Column(db.String(200), nullable=True)
    original_body = db.Column(db.Text, nullable=False)
    original_category_id = db.Column(db.Integer, nullable=True)

    quarantined_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    restored_at = db.Column(db.DateTime, nullable=True)
    permanently_deleted = db.Column(db.Boolean, default=False, nullable=False)

    joke = db.relationship(
        "Joke", backref=db.backref("quarantine_record", uselist=False)
    )
    moderator = db.relationship("User")


class AdminNotification(db.Model):
    __tablename__ = "admin_notifications"

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    # short label like "24h ban", "Quarantine joke"
    action = db.Column(db.String(100), nullable=False)

    # human-readable text shown in the admin panel
    message = db.Column(db.Text, nullable=False)

    performed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    target_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    target_joke_id = db.Column(db.Integer, db.ForeignKey("jokes.id"), nullable=True)

    is_read = db.Column(db.Boolean, default=False, nullable=False)

    performed_by = db.relationship("User", foreign_keys=[performed_by_id])
    target_user = db.relationship("User", foreign_keys=[target_user_id])
    target_joke = db.relationship("Joke")


class SiteSettings(db.Model):
    __tablename__ = "site_settings"

    id = db.Column(db.Integer, primary_key=True)
    max_jokes_per_hour = db.Column(db.Integer, default=2)
    max_jokes_per_day = db.Column(db.Integer, default=10)

    pinned_enabled = db.Column(db.Boolean, default=False)
    pinned_title = db.Column(db.String(200), nullable=True)
    pinned_body = db.Column(db.Text, nullable=True)
    newyear_force_disabled = db.Column(db.Boolean, default=False)

    # Seasonal / effects toggles
    # When enabled, forces New Year effects to show regardless of date range.
    newyear_force_enabled = db.Column(db.Boolean, default=False)

    @staticmethod
    def get():
        """Return the single settings row, creating it if needed."""
        settings = SiteSettings.query.get(1)
        if not settings:
            settings = SiteSettings(id=1)
            db.session.add(settings)
            db.session.commit()
        return settings
