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


class BlacklistEntry(db.Model):
    """
    Site-wide ban list for signup/login.
    kind: 'email' | 'ip' | 'username'
    value: normalized for lookup (email/username lowercased; ip as stored string)
    """

    __tablename__ = "blacklist_entries"
    __table_args__ = (
        UniqueConstraint("kind", "value", name="uq_blacklist_kind_value"),
    )

    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(20), nullable=False, index=True)
    value = db.Column(db.String(255), nullable=False, index=True)
    reason = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    source_user_id = db.Column(db.Integer, nullable=True)
    source_username = db.Column(db.String(64), nullable=True)

    created_by = db.relationship("User", foreign_keys=[created_by_id])


class PendingRegistration(db.Model):
    """Email-verification holding pen for sign-ups (user row is created only after verify)."""

    __tablename__ = "pending_registrations"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(32), nullable=False, index=True)
    email = db.Column(db.String(120), nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)

    # Short code typed on the site (e.g. 6 digits)
    code = db.Column(db.String(12), nullable=False, index=True)
    # Long secret used in the one-click email hyperlink
    token = db.Column(db.String(64), unique=True, nullable=False, index=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)
    last_sent_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    attempts = db.Column(db.Integer, default=0, nullable=False)

    def is_expired(self) -> bool:
        return datetime.utcnow() >= self.expires_at


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(32), unique=True, nullable=False, index=True)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    forum_last_seen_at = db.Column(db.DateTime, nullable=True)
    is_admin = db.Column(db.Boolean, default=False)
    is_moderator = db.Column(db.Boolean, default=False)
    needs_moderator = db.Column(db.Boolean, default=True, nullable=False)
    # If true, user cannot open/submit duplicate-removal appeals
    duplicate_appeals_banned = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    reputation = db.Column(db.Integer, default=0, nullable=False)
    ban_until = db.Column(db.DateTime, nullable=True)
    ban_reason = db.Column(db.String(255), nullable=True)
    banned_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    banned_at = db.Column(db.DateTime, nullable=True)

    banned_by = db.relationship("User", foreign_keys=[banned_by_id], remote_side="User.id")

    # Existing relationships
    jokes = db.relationship(
        "Joke", backref="author", lazy=True, foreign_keys="Joke.user_id"
    )
    comments = db.relationship("Comment", backref="author", lazy=True)
    votes = db.relationship("Vote", backref="voter", lazy=True)
    warnings = db.relationship(
        "UserWarning", foreign_keys="UserWarning.user_id", backref="user", lazy=True, cascade="all, delete-orphan"
    )

    def is_banned(self) -> bool:
        return self.ban_until is not None and self.ban_until > datetime.utcnow()

    def active_warning_count(self) -> int:
        return sum(1 for w in (self.warnings or []) if w.is_active)

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
    subcategories = db.relationship(
        "Subcategory",
        back_populates="category",
        lazy=True,
        cascade="all, delete-orphan",
        order_by="Subcategory.name",
    )


class Subcategory(db.Model):
    """Optional second-level topic under a Category (e.g. AIDS → Cure for AIDS)."""

    __tablename__ = "subcategories"
    __table_args__ = (
        UniqueConstraint("category_id", "slug", name="uq_subcategory_cat_slug"),
    )

    id = db.Column(db.Integer, primary_key=True)
    category_id = db.Column(
        db.Integer, db.ForeignKey("categories.id"), nullable=False, index=True
    )
    slug = db.Column(db.String(60), nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    sort_order = db.Column(db.Integer, default=0, nullable=False)

    category = db.relationship("Category", back_populates="subcategories")
    jokes = db.relationship("Joke", backref="subcategory", lazy=True)


class CategoryModSuggestion(db.Model):
    """
    Moderator proposals to add/remove a category or subcategory.
    Admins approve or reject on Suggested Category Mods.
    """

    __tablename__ = "category_mod_suggestions"

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    suggested_by_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=True, index=True
    )
    suggested_by_username = db.Column(db.String(80), nullable=False, default="?")

    # add | remove
    action = db.Column(db.String(16), nullable=False, index=True)
    # category | subcategory
    target_kind = db.Column(db.String(16), nullable=False, index=True)

    # For add: proposed display name (category or subcategory)
    proposed_name = db.Column(db.String(120), nullable=True)
    # For add subcategory: parent category id
    parent_category_id = db.Column(db.Integer, nullable=True, index=True)
    parent_category_name = db.Column(db.String(100), nullable=True)

    # For remove: target ids + snapshot names (ids may vanish later)
    category_id = db.Column(db.Integer, nullable=True, index=True)
    category_name = db.Column(db.String(100), nullable=True)
    subcategory_id = db.Column(db.Integer, nullable=True, index=True)
    subcategory_name = db.Column(db.String(120), nullable=True)

    note = db.Column(db.String(255), nullable=True)

    # pending | approved | rejected
    status = db.Column(db.String(16), nullable=False, default="pending", index=True)
    reviewed_at = db.Column(db.DateTime, nullable=True)
    reviewed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    review_note = db.Column(db.String(255), nullable=True)

    suggested_by = db.relationship("User", foreign_keys=[suggested_by_id])
    reviewed_by = db.relationship("User", foreign_keys=[reviewed_by_id])


class Joke(db.Model):
    __tablename__ = "jokes"
    id = db.Column(db.Integer, primary_key=True)
    # title now optional, we’ll auto-fill or ignore it
    title = db.Column(db.String(200), nullable=True)
    body = db.Column(db.Text, nullable=False)
    salute_to = db.Column(db.String(32), nullable=True)
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
    review_status = db.Column(db.String(20), default="approved", nullable=False, index=True)
    reviewed_at = db.Column(db.DateTime, nullable=True)
    reviewed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    reject_reasons = db.Column(db.String(255), nullable=True)
    reviewed_by = db.relationship("User", foreign_keys=[reviewed_by_id])
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey("categories.id"))
    subcategory_id = db.Column(
        db.Integer, db.ForeignKey("subcategories.id"), nullable=True, index=True
    )

    comments = db.relationship("Comment", backref="joke", lazy=True)
    votes = db.relationship("Vote", backref="joke", lazy=True)

    quarantined_by = db.relationship("User", foreign_keys=[quarantined_by_id])


class JokeDraft(db.Model):
    """
    Per-user draft for Submit Joke (text, meme, or clip).
    Media filenames point at the same upload/clip folders as live jokes;
    files are kept when the draft is published, deleted when the draft is discarded.
    """

    __tablename__ = "joke_drafts"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False, index=True
    )
    # joke | meme | clip
    joke_type = db.Column(db.String(10), nullable=False, default="joke")
    body = db.Column(db.Text, nullable=False, default="")
    salute_to = db.Column(db.String(32), nullable=True)
    category_id = db.Column(db.Integer, db.ForeignKey("categories.id"), nullable=True)
    subcategory_id = db.Column(
        db.Integer, db.ForeignKey("subcategories.id"), nullable=True
    )

    image_filename = db.Column(db.String(255), nullable=True)
    video_filename = db.Column(db.String(255), nullable=True)
    video_thumb = db.Column(db.String(255), nullable=True)
    video_duration = db.Column(db.Integer, nullable=True)
    video_size = db.Column(db.Integer, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    author = db.relationship("User", foreign_keys=[user_id])
    category = db.relationship("Category", foreign_keys=[category_id])
    subcategory = db.relationship("Subcategory", foreign_keys=[subcategory_id])

    @property
    def is_meme(self) -> bool:
        return (self.joke_type or "") == "meme"

    @property
    def is_clip(self) -> bool:
        return (self.joke_type or "") == "clip"

    @property
    def preview_title(self) -> str:
        body = (self.body or "").strip()
        if body:
            return body[:80] + ("…" if len(body) > 80 else "")
        if self.is_meme and self.image_filename:
            return "Meme draft"
        if self.is_clip and self.video_filename:
            return "Clip draft"
        return f"{(self.joke_type or 'joke').title()} draft"


class Vote(db.Model):
    __tablename__ = "votes"
    __table_args__ = (UniqueConstraint("user_id", "joke_id", name="uq_user_joke_vote"),)

    id = db.Column(db.Integer, primary_key=True)
    value = db.Column(db.Integer, nullable=False)  # +1, +2 (double up), or -1
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


class JokeCommentRead(db.Model):
    """
    When the post owner last viewed comments on their joke/meme/clip.
    No row (or never updated) ⇒ any comments from others count as unseen.
    """

    __tablename__ = "joke_comment_reads"
    __table_args__ = (
        UniqueConstraint("user_id", "joke_id", name="uq_joke_comment_read"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False, index=True
    )
    joke_id = db.Column(
        db.Integer, db.ForeignKey("jokes.id"), nullable=False, index=True
    )
    # Epoch / "never seen" sentinel not needed: missing row means unseen.
    # last_seen_at=0-equivalent is represented by no row or a very old datetime.
    last_seen_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    user = db.relationship(
        "User", backref=db.backref("joke_comment_reads", lazy="dynamic")
    )
    joke = db.relationship(
        "Joke", backref=db.backref("comment_read_states", lazy="dynamic")
    )


class JokeFollow(db.Model):
    """User following a joke/meme/clip for new-comment alerts."""

    __tablename__ = "joke_follows"
    __table_args__ = (
        UniqueConstraint("user_id", "joke_id", name="uq_joke_follow"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False, index=True
    )
    joke_id = db.Column(
        db.Integer, db.ForeignKey("jokes.id"), nullable=False, index=True
    )
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    # When the follower last opened the post; comments after this count as new.
    last_seen_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    user = db.relationship(
        "User", backref=db.backref("joke_follows", lazy="dynamic")
    )
    joke = db.relationship(
        "Joke", backref=db.backref("followers", lazy="dynamic")
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
    quoted_reply_id = db.Column(db.Integer, db.ForeignKey("forum_replies.id"), nullable=True)
    quoted_reply = db.relationship("ForumReply", remote_side=[id], foreign_keys=[quoted_reply_id   ])
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
    quoted_comment_id = db.Column(db.Integer, db.ForeignKey("comments.id"), nullable=True)
    quoted_comment = db.relationship("Comment", remote_side=[id], foreign_keys=[quoted_comment_id])


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


# Fixed list for moderator quarantine forms (checkbox values)
QUARANTINE_REASONS = (
    "Copied",
    "Legally dangerous",
    "Not a joke",
    "Personal attack",
    "Unfunny Bandwagon",
    "Wasp-esque",
)


class QuarantinedJoke(db.Model):
    __tablename__ = "quarantined_jokes"

    id = db.Column(db.Integer, primary_key=True)
    joke_id = db.Column(
        db.Integer, db.ForeignKey("jokes.id"), nullable=False, unique=True
    )

    original_title = db.Column(db.String(200), nullable=True)
    original_body = db.Column(db.Text, nullable=False)
    original_category_id = db.Column(db.Integer, nullable=True)

    # Comma-separated labels from QUARANTINE_REASONS (mandatory on new quarantines)
    reasons = db.Column(db.String(255), nullable=True)

    quarantined_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    restored_at = db.Column(db.DateTime, nullable=True)
    permanently_deleted = db.Column(db.Boolean, default=False, nullable=False)

    joke = db.relationship(
        "Joke", backref=db.backref("quarantine_record", uselist=False)
    )
    moderator = db.relationship("User")

    def reasons_list(self):
        if not self.reasons:
            return []
        return [r.strip() for r in self.reasons.split(",") if r.strip()]


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


class ModRoomMessage(db.Model):
    """Internal message/note posted by moderators/admins in the Moderators Room."""

    __tablename__ = "mod_room_messages"

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    body = db.Column(db.Text, nullable=False)
    is_pinned = db.Column(db.Boolean, default=False, nullable=False)

    author = db.relationship("User", foreign_keys=[user_id])


class UserWarning(db.Model):
    """Formal warning strike issued to a user by a moderator/admin."""

    __tablename__ = "user_warnings"

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    issuer_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    reason = db.Column(db.String(255), nullable=False)
    notes = db.Column(db.Text, nullable=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)

    issuer = db.relationship("User", foreign_keys=[issuer_id])




# ---------------------------------------------------------------------------
# Duplicate detection + soft-archive (moderator workflow)
# ---------------------------------------------------------------------------

DUPE_PAIR_STATUSES = (
    "pending",
    "resolved_delete_older",
    "resolved_delete_newer",
    "not_dupe",
)


class JokeDupePair(db.Model):
    """Candidate or resolved near-duplicate joke pair (from X4 scanner)."""

    __tablename__ = "joke_dupe_pairs"
    __table_args__ = (
        UniqueConstraint("joke_older_id", "joke_newer_id", name="uq_dupe_older_newer"),
    )

    id = db.Column(db.Integer, primary_key=True)
    # Prefer chronological order for UX (older first, newer usually deleted)
    joke_older_id = db.Column(db.Integer, nullable=False, index=True)
    joke_newer_id = db.Column(db.Integer, nullable=False, index=True)
    score = db.Column(db.Float, nullable=False, default=0.0)  # 0..1 similarity
    method = db.Column(db.String(40), nullable=False, default="fuzzy")  # exact|fuzzy|embed
    detail = db.Column(db.String(255), nullable=True)
    status = db.Column(db.String(32), nullable=False, default="pending", index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    reviewed_at = db.Column(db.DateTime, nullable=True)
    reviewed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    # Snapshot of which original joke id was archived (if any)
    archived_original_id = db.Column(db.Integer, nullable=True)
    kept_original_id = db.Column(db.Integer, nullable=True)

    reviewer = db.relationship("User", foreign_keys=[reviewed_by_id])


class DupeModerationLog(db.Model):
    """Admin-visible log of moderator de-duplication actions."""

    __tablename__ = "dupe_moderation_logs"

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    actor_username = db.Column(db.String(80), nullable=False, default="?")
    action = db.Column(db.String(40), nullable=False, index=True)
    # delete_newer | not_dupe | already_gone  (legacy: delete_older)
    pair_id = db.Column(db.Integer, nullable=True, index=True)
    joke_a_id = db.Column(db.Integer, nullable=True)  # older
    joke_b_id = db.Column(db.Integer, nullable=True)  # newer
    deleted_joke_id = db.Column(db.Integer, nullable=True)
    kept_joke_id = db.Column(db.Integer, nullable=True)
    reason = db.Column(db.String(120), nullable=True)
    # Pre-rendered plain summary (HTML built in template for safe links)
    summary = db.Column(db.Text, nullable=False, default="")

    actor = db.relationship("User", foreign_keys=[actor_id])


class ArchivedJoke(db.Model):
    """Soft-deleted joke (e.g. duplicate removal) — full row snapshot."""

    __tablename__ = "archived_jokes"

    id = db.Column(db.Integer, primary_key=True)
    # Original jokes.id (not a live FK — row is gone from jokes)
    original_joke_id = db.Column(db.Integer, nullable=False, index=True, unique=True)
    title = db.Column(db.String(200), nullable=True)
    body = db.Column(db.Text, nullable=False)
    salute_to = db.Column(db.String(32), nullable=True)
    created_at = db.Column(db.DateTime, nullable=True)
    score = db.Column(db.Integer, default=0)
    is_meme = db.Column(db.Boolean, default=False, nullable=False)
    image_filename = db.Column(db.String(255), nullable=True)
    is_clip = db.Column(db.Boolean, default=False, nullable=False)
    video_filename = db.Column(db.String(255), nullable=True)
    video_thumb = db.Column(db.String(255), nullable=True)
    video_duration = db.Column(db.Integer, nullable=True)
    video_size = db.Column(db.Integer, nullable=True)
    is_quarantined = db.Column(db.Boolean, default=False, nullable=False)
    quarantined_at = db.Column(db.DateTime, nullable=True)
    quarantined_by_id = db.Column(db.Integer, nullable=True)
    quarantine_locked = db.Column(db.Boolean, default=False, nullable=False)
    review_status = db.Column(db.String(20), nullable=True)
    reviewed_at = db.Column(db.DateTime, nullable=True)
    reviewed_by_id = db.Column(db.Integer, nullable=True)
    reject_reasons = db.Column(db.String(255), nullable=True)
    user_id = db.Column(db.Integer, nullable=False, index=True)
    category_id = db.Column(db.Integer, nullable=True)
    subcategory_id = db.Column(db.Integer, nullable=True)
    # Archive metadata
    archived_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    archived_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    archive_reason = db.Column(db.String(64), nullable=False, default="duplicate")
    related_kept_joke_id = db.Column(db.Integer, nullable=True, index=True)
    dupe_pair_id = db.Column(db.Integer, nullable=True, index=True)
    author_username = db.Column(db.String(80), nullable=True)

    archiver = db.relationship("User", foreign_keys=[archived_by_id])
    votes = db.relationship(
        "ArchivedVote",
        backref="archived_joke",
        lazy=True,
        cascade="all, delete-orphan",
        foreign_keys="ArchivedVote.archived_joke_id",
    )
    comments = db.relationship(
        "ArchivedComment",
        backref="archived_joke",
        lazy=True,
        cascade="all, delete-orphan",
        foreign_keys="ArchivedComment.archived_joke_id",
    )


class ArchivedVote(db.Model):
    __tablename__ = "archived_votes"

    id = db.Column(db.Integer, primary_key=True)
    archived_joke_id = db.Column(
        db.Integer, db.ForeignKey("archived_jokes.id"), nullable=False, index=True
    )
    original_vote_id = db.Column(db.Integer, nullable=True)
    value = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, nullable=True)
    user_id = db.Column(db.Integer, nullable=False, index=True)
    username = db.Column(db.String(80), nullable=True)


class ArchivedComment(db.Model):
    __tablename__ = "archived_comments"

    id = db.Column(db.Integer, primary_key=True)
    archived_joke_id = db.Column(
        db.Integer, db.ForeignKey("archived_jokes.id"), nullable=False, index=True
    )
    original_comment_id = db.Column(db.Integer, nullable=True, index=True)
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, nullable=True)
    user_id = db.Column(db.Integer, nullable=False, index=True)
    username = db.Column(db.String(80), nullable=True)
    quoted_comment_id = db.Column(db.Integer, nullable=True)

    reactions = db.relationship(
        "ArchivedCommentReaction",
        backref="archived_comment",
        lazy=True,
        cascade="all, delete-orphan",
        foreign_keys="ArchivedCommentReaction.archived_comment_id",
    )


class ArchivedCommentReaction(db.Model):
    __tablename__ = "archived_comment_reactions"

    id = db.Column(db.Integer, primary_key=True)
    archived_comment_id = db.Column(
        db.Integer, db.ForeignKey("archived_comments.id"), nullable=False, index=True
    )
    original_reaction_id = db.Column(db.Integer, nullable=True)
    user_id = db.Column(db.Integer, nullable=False)
    username = db.Column(db.String(80), nullable=True)
    reaction_type = db.Column(db.String(20), nullable=False)
    created_at = db.Column(db.DateTime, nullable=True)


class DuplicateAppeal(db.Model):
    """User appeal after their joke was archived as a duplicate."""

    __tablename__ = "duplicate_appeals"

    id = db.Column(db.Integer, primary_key=True)
    # Nullable so we can drop archive rows after a successful restore
    archived_joke_id = db.Column(
        db.Integer, db.ForeignKey("archived_jokes.id"), nullable=True, index=True
    )
    original_joke_id = db.Column(db.Integer, nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    kept_joke_id = db.Column(db.Integer, nullable=True, index=True)
    body = db.Column(db.Text, nullable=True)  # optional user note
    status = db.Column(
        db.String(20), nullable=False, default="pending", index=True
    )  # pending|upheld|rejected
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    reviewed_at = db.Column(db.DateTime, nullable=True)
    reviewed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    review_note = db.Column(db.String(255), nullable=True)

    archived_joke = db.relationship("ArchivedJoke", backref=db.backref("appeals", lazy=True))
    appellant = db.relationship("User", foreign_keys=[user_id])
    reviewer = db.relationship("User", foreign_keys=[reviewed_by_id])


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

    # Site-wide announcements marquee (under logo)
    marquee_enabled = db.Column(db.Boolean, default=False)
    marquee_items_json = db.Column(db.Text, nullable=True)  # JSON list of strings
    marquee_theme_json = db.Column(db.Text, nullable=True)  # JSON theme object

    DEFAULT_MARQUEE_THEME = {
        "speed": 40,  # seconds for one full loop (higher = slower)
        "direction": "left",  # left | right
        "text_color": "#ffb3e6",
        "bg_color": "#1a0a1a",
        "font_size": 15,  # px
        "font_weight": "600",
        "gap": 48,  # px between announcements
        "separator": " ··· ",
        "padding_y": 8,  # px
        "glow": True,
        "pause_on_hover": True,
    }

    @staticmethod
    def get():
        """Return the single settings row, creating it if needed."""
        settings = SiteSettings.query.get(1)
        if not settings:
            settings = SiteSettings(id=1)
            db.session.add(settings)
            db.session.commit()
        return settings

    def get_marquee_items(self) -> list:
        import json

        raw = (self.marquee_items_json or "").strip()
        if not raw:
            return []
        try:
            data = json.loads(raw)
            if not isinstance(data, list):
                return []
            return [str(x).strip() for x in data if str(x).strip()]
        except Exception:
            return []

    def set_marquee_items(self, items: list) -> None:
        import json

        clean = [str(x).strip() for x in (items or []) if str(x).strip()]
        self.marquee_items_json = json.dumps(clean, ensure_ascii=False)

    def get_marquee_theme(self) -> dict:
        import json

        theme = dict(self.DEFAULT_MARQUEE_THEME)
        raw = (self.marquee_theme_json or "").strip()
        if not raw:
            return theme
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                theme.update({k: data[k] for k in theme if k in data})
        except Exception:
            pass
        return theme

    def set_marquee_theme(self, theme: dict) -> None:
        import json

        base = dict(self.DEFAULT_MARQUEE_THEME)
        if isinstance(theme, dict):
            base.update(theme)
        self.marquee_theme_json = json.dumps(base, ensure_ascii=False)
