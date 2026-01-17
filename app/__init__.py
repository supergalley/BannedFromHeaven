# app/__init__.py
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from flask import Flask, request, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, current_user
from urllib.parse import urlsplit, urlunsplit
from werkzeug.middleware.proxy_fix import ProxyFix

LOGIN_FAIL_LIMIT = 8
LOGIN_FAIL_WINDOW = timedelta(minutes=10)
db = SQLAlchemy()
login_manager = LoginManager()


def _clean_canonical(url: str) -> str:
    """
    - strips query/fragment
    - strips default ports
    - keeps trailing slash only for site root
    """
    p = urlsplit(url)
    netloc = p.hostname or ""
    if p.port and p.port not in (80, 443):
        netloc = f"{netloc}:{p.port}"
    path = p.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return urlunsplit((p.scheme, netloc, path, "", ""))


def _env_bool(name: str, default=False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _getenv(*names, default="") -> str:
    """Return first non-empty env var from names (supports legacy + new names)."""
    for n in names:
        v = os.environ.get(n)
        if v is not None and str(v).strip() != "":
            return str(v)
    return default


def _normalize_base(url_or_host: str) -> str:
    """
    Accepts:
      - full URL: https://media.bannedfromheaven.com
      - hostname: media.bannedfromheaven.com
    Returns a clean base URL without trailing slash.
    """
    s = (url_or_host or "").strip()
    if not s:
        return ""
    s = s.rstrip("/")
    if "://" not in s:
        s = "https://" + s
    return s.rstrip("/")


def create_app():
    app = Flask(__name__)
    from datetime import timedelta

    app.config.setdefault("LOGIN_FAIL_LIMIT", 8)
    app.config.setdefault("LOGIN_FAIL_WINDOW", timedelta(minutes=10))
    # Trust Traefik headers (X-Forwarded-Proto / Host)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
    # Core secrets / turnstile
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "fallback-key-change-me")
    app.config["UPLOADS_SHARED_SECRET"] = os.environ.get("UPLOADS_SHARED_SECRET", "")
    app.config["TURNSTILE_SITE_KEY"] = os.environ.get("TURNSTILE_SITE_KEY", "")
    app.config["TURNSTILE_SECRET_KEY"] = os.environ.get("TURNSTILE_SECRET_KEY", "")
    # Cookie domains: bind to whatever host is used
    app.config["SESSION_COOKIE_DOMAIN"] = None
    app.config["REMEMBER_COOKIE_DOMAIN"] = None
    # HTTPS behind Traefik
    app.config["SESSION_COOKIE_SECURE"] = True
    app.config["REMEMBER_COOKIE_SECURE"] = True
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["REMEMBER_COOKIE_HTTPONLY"] = True
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    # Read env (support BOTH your old compose names and the new stack names)
    domain = _getenv("DOMAIN", "domain", default="").strip()
    CANONICAL_HOST = (os.environ.get("CANONICAL_HOST") or domain or "").strip().lower()

    @app.before_request
    def enforce_canonical_host_and_https():
        # Works because you already use ProxyFix(x_proto=1, x_host=1)
        if not CANONICAL_HOST:
            return
        host = (request.host.split(":")[0] or "").lower()
        scheme = request.scheme.lower()

        # prefer non-www (change if you want www instead)
        preferred = CANONICAL_HOST
        if host == f"www.{preferred}":
            target = f"https://{preferred}{request.full_path}"
            return redirect(target.rstrip("?"), code=301)
        if host != preferred:
            # Don’t accidentally redirect random hosts if you serve multiple domains here
            return
        if scheme != "https":
            target = f"https://{preferred}{request.full_path}"
            return redirect(target.rstrip("?"), code=301)

    images = _getenv("IMAGES", "images", default="/data/uploads").rstrip("/")
    clips = _getenv("CLIPS", "clips", default="/data/clips").rstrip("/")
    thumbs = _getenv(
        "THUMBS", "THUMBNAILS", "thumbnails", default="/data/clip_thumbs"
    ).rstrip("/")
    jokedb = _getenv("JOKE_DB", "JOKEDB", "jokeDB", default="/data/jokes.db")
    # DB
    if jokedb.startswith("/"):
        app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:////{jokedb.lstrip('/')}"
    else:
        app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{jokedb}"
    # FOLDERS (keep these or gunicorn cries and dies)
    app.config["UPLOAD_FOLDER"] = images
    app.config["CLIPS_FOLDER"] = clips
    app.config["CLIP_THUMBS_FOLDER"] = thumbs
    app.config["DOMAIN"] = domain
    # ---- PUBLIC ASSET HOSTS ----
    # We will serve *all public media* from ONE base:
    #   - clips:        {ASSET_BASE}/clips/<file>
    #   - thumbs:       {ASSET_BASE}/clip_thumbs/<file>
    #   - images/memes: {ASSET_BASE}/uploads/<file>
    #
    # This avoids accidental "media.{DOMAIN}" nonsense on sandbox.
    # Preferred: MEDIA_BASE (full URL) or MEDIA_DOMAIN (hostname)
    media_base_raw = _getenv("MEDIA_BASE", default="").strip()
    media_domain = _getenv("MEDIA_DOMAIN", "MEDIA_HOST", default="").strip()
    asset_base = ""
    if media_base_raw:
        asset_base = _normalize_base(media_base_raw)
    elif media_domain:
        asset_base = _normalize_base(media_domain)
    elif domain:
        # Last resort fallback only
        asset_base = _normalize_base(f"media.{domain}")
    else:
        asset_base = ""
    app.config["ASSET_BASE"] = asset_base
    # Backwards-compat variables if anything still uses them
    app.config["MEDIA_BASE"] = asset_base
    # KEEP upload_base for your future upload endpoint idea (NOT used for <img>/<video> URLs)
    upload_base_raw = _getenv("UPLOAD_BASE", default="").strip()
    upload_domain = _getenv("UPLOAD_DOMAIN", "UPLOAD_HOST", default="").strip()
    upload_base = ""
    if upload_base_raw:
        upload_base = _normalize_base(upload_base_raw)
    elif upload_domain:
        upload_base = _normalize_base(upload_domain)
    elif domain:
        upload_base = _normalize_base(f"upload.{domain}")
    else:
        upload_base = ""
    app.config["UPLOAD_BASE"] = upload_base

    # Public URL prefixes (include correct subfolder)
    # These are the ones you should use in templates.
    def _join(base: str, path: str) -> str:
        base = (base or "").rstrip("/")
        path = (path or "").lstrip("/")
        if not base:
            return (
                "/" + path
            )  # local fallback (won't work unless your app serves these routes)
        return f"{base}/{path}"

    app.config["CLIPS_URL"] = _join(asset_base, "clips")
    app.config["THUMBS_URL"] = _join(asset_base, "clip_thumbs")
    app.config["UPLOADS_URL"] = _join(asset_base, "uploads")
    # Create folders
    os.makedirs(app.config["CLIPS_FOLDER"], exist_ok=True)
    os.makedirs(app.config["CLIP_THUMBS_FOLDER"], exist_ok=True)
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    # Remember me
    app.config["REMEMBER_COOKIE_DURATION"] = timedelta(days=365)
    app.config["REMEMBER_COOKIE_REFRESH_EACH_REQUEST"] = True
    app.permanent_session_lifetime = timedelta(days=365)
    # Login manager
    login_manager.login_view = "auth.login"
    login_manager.session_protection = "strong"
    db.init_app(app)
    login_manager.init_app(app)
    # DB init + schema patch
    with app.app_context():
        from . import models  # noqa: F401

        db.create_all()
        try:
            with db.engine.begin() as conn:
                cols = {
                    row[1]
                    for row in conn.exec_driver_sql(
                        "PRAGMA table_info(site_settings)"
                    ).fetchall()
                }
                if "newyear_force_enabled" not in cols:
                    conn.exec_driver_sql(
                        "ALTER TABLE site_settings ADD COLUMN newyear_force_enabled INTEGER DEFAULT 0"
                    )
        except Exception:
            db.session.rollback()
    # Inject unread DM count

    @app.context_processor
    def inject_seo_defaults():
        # default canonical = current page WITHOUT query string
        canonical_url = _clean_canonical(request.base_url)
        # default noindex if *any* query params exist (kills duplicates)
        noindex = bool(request.args)
        return dict(canonical_url=canonical_url, noindex=noindex)

    @app.context_processor
    def inject_unread_message_count():
        from .models import Message

        unread = 0
        if current_user.is_authenticated:
            unread = Message.query.filter_by(
                recipient_id=current_user.id,
                read_at=None,
                deleted_by_recipient=False,
            ).count()
        return dict(unread_message_count=unread)

    # Seasonal flags
    @app.context_processor
    def inject_seasonal_flags():
        now = datetime.now(ZoneInfo("Europe/London"))
        show_christmas_snow = (now.month == 12 and now.day >= 1) or (
            now.month == 1 and now.day <= 6
        )
        if _env_bool("force_snow", default=False):
            show_christmas_snow = True
        try:
            from .models import SiteSettings

            settings = SiteSettings.get()
            force_newyear = bool(getattr(settings, "newyear_force_enabled", False))
        except Exception:
            force_newyear = False
        in_newyear_window = False
        entering_year = now.year + 1
        if now.month == 12:
            entering_year = now.year + 1
            if now.day == 31:
                in_newyear_window = (now.hour > 11) or (
                    now.hour == 11 and now.minute >= 30
                )
        elif now.month == 1:
            entering_year = now.year
            if now.day == 1:
                in_newyear_window = (now.hour < 23) or (
                    now.hour == 23 and now.minute <= 59
                )
        show_newyear_effects = force_newyear or in_newyear_window
        if force_newyear and not in_newyear_window:
            entering_year = now.year + 1
        return dict(
            show_christmas_snow=show_christmas_snow,
            show_newyear_effects=show_newyear_effects,
            newyear_entering_year=entering_year,
        )

    @app.context_processor
    def inject_admin_notification_flag():
        from .models import AdminNotification

        unread = 0
        if current_user.is_authenticated and getattr(current_user, "is_admin", False):
            unread = AdminNotification.query.filter_by(is_read=False).count()
        return dict(admin_unread_notifications=unread)

    @app.context_processor
    def inject_user_medals():
        from .models import User, Joke, Vote
        from sqlalchemy import func

        user_medals = {}
        try:
            now = datetime.utcnow()
            day_ago = now - timedelta(days=1)
            week_ago = now - timedelta(days=7)
            medal_icon = {1: "🥇", 2: "🥈", 3: "🥉"}
            place_label = {1: "Winner", 2: "Runner-up", 3: "3rd place"}

            def add_medals(rows, period_key, period_label):
                for rank, (user_id, _) in enumerate(rows, start=1):
                    icon = medal_icon.get(rank)
                    if not icon:
                        continue
                    tooltip = f"{period_label} – {place_label[rank]}"
                    user_medals.setdefault(user_id, {})[period_key] = {
                        "icon": icon,
                        "tooltip": tooltip,
                        "rank": rank,
                    }

            all_time_rows = (
                User.query.order_by(User.reputation.desc())
                .with_entities(User.id, User.reputation)
                .limit(3)
                .all()
            )
            add_medals(all_time_rows, "all_time", "All-Time")
            week_rows = (
                User.query.join(Joke, Joke.user_id == User.id)
                .join(Vote, Vote.joke_id == Joke.id)
                .filter(Vote.value == 1, Vote.created_at >= week_ago)
                .group_by(User.id)
                .order_by(func.sum(Vote.value).desc())
                .with_entities(User.id, func.sum(Vote.value))
                .limit(3)
                .all()
            )
            add_medals(week_rows, "week", "This Week")
            day_rows = (
                User.query.join(Joke, Joke.user_id == User.id)
                .join(Vote, Vote.joke_id == Joke.id)
                .filter(Vote.value == 1, Vote.created_at >= day_ago)
                .group_by(User.id)
                .order_by(func.sum(Vote.value).desc())
                .with_entities(User.id, func.sum(Vote.value))
                .limit(3)
                .all()
            )
            add_medals(day_rows, "today", "Today")
        except Exception:
            pass
        return dict(user_medals=user_medals)

    @app.context_processor
    def inject_asset_bases():
        return dict(
            ASSET_BASE=app.config.get("ASSET_BASE", ""),
            CLIPS_URL=app.config.get("CLIPS_URL", ""),
            THUMBS_URL=app.config.get("THUMBS_URL", ""),
            UPLOADS_URL=app.config.get("UPLOADS_URL", ""),
            # kept for compatibility:
            MEDIA_BASE=app.config.get("MEDIA_BASE", ""),
            UPLOAD_BASE=app.config.get("UPLOAD_BASE", ""),
        )

    from .routes import main_bp
    from .auth import auth_bp
    from .forum import forum_bp
    from .admin import admin_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(forum_bp, url_prefix="/forum")
    app.register_blueprint(admin_bp)

    @app.context_processor
    def inject_forum_unread_flag():
        from .models import ForumThread

        unread = False
        if current_user.is_authenticated:
            last_seen = getattr(current_user, "forum_last_seen_at", None)
            latest = (
                ForumThread.query.order_by(ForumThread.last_activity_at.desc())
                .with_entities(ForumThread.last_activity_at)
                .first()
            )
            if latest and latest[0]:
                if last_seen is None or latest[0] > last_seen:
                    unread = True
        return dict(forum_has_unread=unread)

    # Ban logic
    @app.before_request
    def block_banned_users():
        if not current_user.is_authenticated:
            return
        if not hasattr(current_user, "is_banned"):
            return
        if not current_user.is_banned():
            return
        ep = request.endpoint or ""

        if ep == "static":
            return
        if ep.startswith("admin."):
            return
        if ep.startswith("auth."):
            return
        if request.method in ("POST", "PUT", "DELETE", "PATCH"):
            flash(f"Your account is banned until {current_user.ban_until}.", "danger")
            return redirect(url_for("main.index"))
        return

    return app


# Gunicorn imports `app` from this module
app = create_app()
