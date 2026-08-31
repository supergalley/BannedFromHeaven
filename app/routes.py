# app/routes.py
import base64
import bleach
import os
import secrets
import subprocess
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from flask import (
    Blueprint,
    Response,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    jsonify,
    current_app,
    send_from_directory,
    abort,
    make_response,
)
from flask_login import login_required, current_user
from functools import wraps
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from sqlalchemy import desc, or_, and_, func, case, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError
from werkzeug.utils import secure_filename
from . import db
from .models import (
    Joke,
    JokeDraft,
    Comment,
    Category,
    Subcategory,
    Vote,
    ForumThread,
    ForumReply,
    User,
    SiteSettings,
    Message,
    MessageBlock,
    PageView,
    QuarantinedJoke,
    AdminNotification,
    JokeCommentReaction,
    REACTION_TYPES,
)

MAX_JOKE_DRAFTS_PER_USER = 25
VALID_SUBMIT_TYPES = frozenset({"joke", "meme", "clip"})
SUBMIT_TYPE_COOKIE = "submit_joke_type"
SUBMIT_TYPE_MAX_AGE = 60 * 60 * 24 * 365  # 1 year
from PIL import Image, ImageOps
from io import BytesIO

# Safe HTML for admins/mods (no script/iframe/forms/event handlers)
ALLOWED_TAGS = [
    "b",
    "strong",
    "i",
    "em",
    "u",
    "s",
    "strike",
    "sub",
    "sup",
    "br",
    "hr",
    "p",
    "div",
    "span",
    "blockquote",
    "code",
    "pre",
    "ul",
    "ol",
    "li",
    "a",
    "img",
    "h1",
    "h2",
    "h3",
    "h4",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
    "mark",
]
ALLOWED_ATTRS = {
    "a": ["href", "title", "target", "rel"],
    "img": ["src", "alt", "title", "width", "height"],
    "td": ["colspan", "rowspan"],
    "th": ["colspan", "rowspan"],
    "*": ["class"],
}
ALLOWED_PROTOCOLS = [
    "http",
    "https",
    "mailto",
]  # note: /static/... has no scheme, that's fine

# Tags that never need a closer
_HTML_VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)

def get_max_clip_size_mb():
    return int(os.environ.get("MAX_CLIP_SIZE", "150"))

def get_max_clip_length_seconds():
    return int(os.environ.get("MAX_CLIP_LENGTH", "90"))

def human_clip_length(seconds):
    seconds = int(seconds)
    if seconds % 60 == 0:
        return f"{seconds // 60} min"
    return f"{seconds} seconds"

def user_can_post_html(user) -> bool:
    """Admins and moderators may use a sanitized HTML subset in jokes/comments."""
    if not user or not getattr(user, "is_authenticated", False):
        # Some callers pass a User ORM object without Flask-Login mixin flags
        pass
    if not user:
        return False
    return bool(getattr(user, "is_admin", False) or getattr(user, "is_moderator", False))


def autoclose_html_tags(html: str) -> str:
    """
    Close any still-open tags so markup cannot bleed outside its container.
    Preserves void tags; ignores malformed closer order best-effort.
    """
    if not html:
        return html
    import re

    token_re = re.compile(
        r"<!--.*?-->|</\s*([a-zA-Z][\w:-]*)\s*>|<\s*([a-zA-Z][\w:-]*)(\s[^>]*)?>",
        re.DOTALL,
    )
    stack: list[str] = []
    for m in token_re.finditer(html):
        raw = m.group(0)
        if raw.startswith("<!--"):
            continue
        if raw.startswith("</"):
            name = (m.group(1) or "").lower()
            if name in stack:
                while stack:
                    top = stack.pop()
                    if top == name:
                        break
            continue
        name = (m.group(2) or "").lower()
        rest = m.group(3) or ""
        if name in _HTML_VOID_TAGS or rest.rstrip().endswith("/"):
            continue
        stack.append(name)
    if not stack:
        return html
    return html + "".join(f"</{t}>" for t in reversed(stack))


def sanitize_admin_html(raw: str) -> str:
    """
    Sanitize staff HTML: allow useful formatting, strip scripts/handlers/etc,
    then auto-close any tags the author left open.
    """
    from bleach.sanitizer import Cleaner

    cleaner = Cleaner(
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRS,
        protocols=ALLOWED_PROTOCOLS,
        strip=True,
    )
    cleaned = cleaner.clean(raw or "")
    cleaned = autoclose_html_tags(cleaned)
    # Also prevent <a target=_blank> tabnabbing if you allow target
    cleaned = cleaned.replace(
        'target="_blank"', 'target="_blank" rel="noopener noreferrer"'
    )
    # Belt-and-braces: strip any leftover event-handler attributes bleach missed
    import re

    cleaned = re.sub(
        r"\son[a-z]+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    return cleaned


def maybe_sanitize_post_html(raw: str, user) -> str:
    """Apply staff HTML sanitization when allowed; otherwise leave plain text."""
    text = raw if raw is not None else ""
    if user_can_post_html(user):
        return sanitize_admin_html(text)
    return text


# Joke.salute_to is String(32)
SALUTE_TO_MAX_LEN = 32


def normalize_salute_to(raw, actor_username=None):
    """
    Clean a Salute To value for storage.
    - empty -> None (remove credit)
    - matches actor username -> None (block accidental self-salute / autofill)
    - matches a site user -> that username (canonical casing)
    - otherwise free text, truncated to column length
    """
    salute_to = (raw or "").strip()
    if not salute_to:
        return None
    if actor_username and salute_to.lower() == (actor_username or "").lower():
        return None
    salute_user = User.query.filter(
        func.lower(User.username) == salute_to.lower()
    ).first()
    if salute_user:
        return salute_user.username
    return salute_to[:SALUTE_TO_MAX_LEN]


main_bp = Blueprint("main", __name__)

@main_bp.app_context_processor
def inject_pending_review_count():
    if current_user.is_authenticated and (
        current_user.is_admin or current_user.is_moderator
    ):
        from .models import JokeDupePair

        return {
            "pending_review_count": Joke.query.filter_by(
                review_status="pending"
            ).count(),
            "pending_dupe_count": JokeDupePair.query.filter_by(
                status="pending"
            ).count(),
        }
    return {"pending_review_count": 0, "pending_dupe_count": 0}

def _uploads_serializer():
    return URLSafeTimedSerializer(
        current_app.config.get("SECRET_KEY", ""), salt="bfh-uploads"
    )

def _require_uploads_secret():
    expected = (current_app.config.get("UPLOADS_SHARED_SECRET") or "").strip()
    if not expected:
        abort(500, "UPLOADS_SHARED_SECRET not configured")
    got = (request.headers.get("X-BFH-Uploads-Secret") or "").strip()
    if got != expected:
        abort(403)


def process_meme_image(file_storage):
    if not file_storage or file_storage.filename == "":
        raise ValueError("Please upload an image for a meme.")

    original_name = secure_filename(file_storage.filename)
    ext = os.path.splitext(original_name)[1].lower()

    try:
        img = Image.open(file_storage.stream)
        is_gif = img.format == "GIF"
        is_animated = (
            bool(getattr(img, "is_animated", False)) and getattr(img, "n_frames", 1) > 1
        )
    except Exception:
        raise ValueError("Uploaded file is not a valid image.")

    upload_folder = current_app.config["UPLOAD_FOLDER"]
    os.makedirs(upload_folder, exist_ok=True)

    now = datetime.utcnow()
    base = now.strftime("%y%m%d %H.%M.%S")
    rand_two_digits = f"{secrets.randbelow(100):02d}"

    # -------------------------
    # Animated GIF: store AS-IS
    # -------------------------
    if is_gif and is_animated:
        filename = f"{base} {rand_two_digits}.gif"
        final_path = os.path.join(upload_folder, filename)

        # Read original bytes safely
        file_storage.stream.seek(0)
        data = file_storage.stream.read()

        # Hard size cap (2.2mb)
        if len(data) > 4400 * 2040:
            raise ValueError("Animated GIF must be under 2.2mb. Upload a smaller one.")

        with open(final_path, "wb") as f:
            f.write(data)

        return filename

    # -------------------------
    # Static GIF: convert to JPG
    # -------------------------
    if is_gif and not is_animated:
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGB")
        filename = f"{base} {rand_two_digits}.jpg"
        final_path = os.path.join(upload_folder, filename)

        img.thumbnail((1024, 1024))
        target_size = 200 * 1024
        quality = 85

        last = None
        while quality >= 40:
            buf = BytesIO()
            img.save(buf, format="JPEG", optimize=True, quality=quality)
            last = buf
            if buf.tell() <= target_size:
                break
            quality -= 5

        with open(final_path, "wb") as f:
            f.write(last.getvalue())

        return filename

    # -------------------------
    # Non-GIF: your existing JPG workflow
    # -------------------------
    img = img.convert("RGB")
    img = ImageOps.exif_transpose(img)
    img.thumbnail((1024, 1024))

    filename = f"{base} {rand_two_digits}.jpg"
    final_path = os.path.join(upload_folder, filename)

    target_size = 200 * 1024
    quality = 85
    last = None

    while quality >= 40:
        buf = BytesIO()
        img.save(buf, format="JPEG", optimize=True, quality=quality)
        last = buf
        if buf.tell() <= target_size:
            break
        quality -= 5

    with open(final_path, "wb") as f:
        f.write(last.getvalue())

    return filename


def process_clip_video(file_storage, max_seconds=None, max_mb=None):
    if max_seconds is None:
        max_seconds = get_max_clip_length_seconds()
    if max_mb is None:
        max_mb = get_max_clip_size_mb()

    if not file_storage or file_storage.filename == "":
        raise ValueError("Please upload a video clip.")

    # Basic filename safety
    original = secure_filename(file_storage.filename)
    ext = os.path.splitext(original)[1].lower()

    # Save upload to a temp file inside /data (persistent mount, also avoids /tmp perms surprises)
    temp_in = os.path.join("/data", f"upload_{secrets.token_hex(8)}{ext}")
    file_storage.save(temp_in)

    # Output filenames
    out_name = f"clip_{secrets.token_hex(12)}.mp4"
    thumb_name = f"clip_{secrets.token_hex(12)}.jpg"
    out_path = os.path.join(current_app.config["CLIPS_FOLDER"], out_name)
    thumb_path = os.path.join(current_app.config["CLIP_THUMBS_FOLDER"], thumb_name)

    # Target bitrate math:
    # 25 MB for 60s => ~3.3 Mbps total. Leave some for audio.
    max_bits = max_mb * 1024 * 1024 * 8
    target_total_bps = int(max_bits / max_seconds)  # ~3_333_333
    audio_bps = 128_000
    video_bps = max(300_000, target_total_bps - audio_bps)  # keep sane minimum

    # Encode to MP4 (H.264 + AAC), capped duration, scaled down if huge
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        temp_in,
        "-t",
        str(max_seconds),
        "-vf",
        "scale='min(1280,iw)':-2",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-b:v",
        str(video_bps),
        "-maxrate",
        str(video_bps),
        "-bufsize",
        str(video_bps * 2),
        "-c:a",
        "aac",
        "-b:a",
        str(audio_bps),
        "-movflags",
        "+faststart",
        out_path,
    ]

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as e:
        # Clean up temp input
        try:
            os.remove(temp_in)
        except:
            pass
        raise ValueError(f"Video processing failed: {e.stderr[-400:]}")

    # Generate thumbnail at 1s
    thumb_cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        "00:00:01",
        "-i",
        out_path,
        "-vframes",
        "1",
        "-q:v",
        "3",
        thumb_path,
    ]
    try:
        subprocess.run(thumb_cmd, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError:
        thumb_name = None  # not fatal

    # Final size check (hard fail if still too big)
    size_bytes = os.path.getsize(out_path)
    if size_bytes > max_mb * 1024 * 1024:
        # You can choose to re-encode more aggressively instead.
        os.remove(out_path)
        if thumb_name:
            try:
                os.remove(thumb_path)
            except:
                pass
        try:
            os.remove(temp_in)
        except:
            pass
        raise ValueError(
            "Clip is still over 25MB after encoding. Use a shorter/simpler clip."
        )

    # Duration (optional, cheap: just assume max_seconds for now)
    duration = max_seconds

    # Clean temp upload
    try:
        os.remove(temp_in)
    except:
        pass

    return out_name, thumb_name, duration, size_bytes


def _safe_unlink_upload(filename: str | None, folder_key: str) -> None:
    """Delete a media file under a known config folder if it exists."""
    if not filename:
        return
    name = os.path.basename(str(filename).strip())
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        return
    folder = current_app.config.get(folder_key) or ""
    if not folder:
        return
    path = os.path.join(folder, name)
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass


def delete_draft_media_files(draft: JokeDraft) -> None:
    """Remove draft-owned media from disk (not used after publish)."""
    if draft.image_filename:
        _safe_unlink_upload(draft.image_filename, "UPLOAD_FOLDER")
    if draft.video_filename:
        _safe_unlink_upload(draft.video_filename, "CLIPS_FOLDER")
    if draft.video_thumb:
        _safe_unlink_upload(draft.video_thumb, "CLIP_THUMBS_FOLDER")


def get_user_draft(draft_id: int, user_id: int) -> JokeDraft | None:
    if not draft_id:
        return None
    return JokeDraft.query.filter_by(id=draft_id, user_id=user_id).first()


def list_user_drafts(user_id: int):
    return (
        JokeDraft.query.filter_by(user_id=user_id)
        .order_by(JokeDraft.updated_at.desc(), JokeDraft.id.desc())
        .all()
    )


def discard_joke_draft(draft: JokeDraft, *, delete_media: bool = True) -> None:
    if delete_media:
        delete_draft_media_files(draft)
    db.session.delete(draft)


def apply_draft_media_replacement(
    draft: JokeDraft,
    *,
    joke_type: str,
    image_filename: str | None = None,
    video_filename: str | None = None,
    video_thumb: str | None = None,
    video_duration=None,
    video_size=None,
) -> None:
    """
    Attach newly processed media to a draft, removing previous files of that type.
    """
    if joke_type == "meme" and image_filename:
        if draft.image_filename and draft.image_filename != image_filename:
            _safe_unlink_upload(draft.image_filename, "UPLOAD_FOLDER")
        draft.image_filename = image_filename
        # Clear clip fields if switching type later handled separately
    elif joke_type == "clip" and video_filename:
        if draft.video_filename and draft.video_filename != video_filename:
            _safe_unlink_upload(draft.video_filename, "CLIPS_FOLDER")
        if draft.video_thumb and draft.video_thumb != video_thumb:
            _safe_unlink_upload(draft.video_thumb, "CLIP_THUMBS_FOLDER")
        draft.video_filename = video_filename
        draft.video_thumb = video_thumb
        draft.video_duration = video_duration
        draft.video_size = video_size


def set_draft_type(draft: JokeDraft, joke_type: str) -> None:
    """Switch draft type and drop media that no longer applies."""
    prev = (draft.joke_type or "joke").strip().lower()
    jt = (joke_type or "joke").strip().lower()
    if jt not in VALID_SUBMIT_TYPES:
        jt = "joke"
    if prev == jt:
        draft.joke_type = jt
        return
    if prev == "meme" and jt != "meme":
        _safe_unlink_upload(draft.image_filename, "UPLOAD_FOLDER")
        draft.image_filename = None
    if prev == "clip" and jt != "clip":
        _safe_unlink_upload(draft.video_filename, "CLIPS_FOLDER")
        _safe_unlink_upload(draft.video_thumb, "CLIP_THUMBS_FOLDER")
        draft.video_filename = None
        draft.video_thumb = None
        draft.video_duration = None
        draft.video_size = None
    draft.joke_type = jt


def moderator_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated:
            abort(403)
        if not (current_user.is_admin or getattr(current_user, "is_moderator", False)):
            abort(403)
        return view(*args, **kwargs)

    return wrapped

def approved_jokes_query():
    return Joke.query.filter_by(review_status="approved", is_quarantined=False)

def choose_best_time_range(base_query, min_results: int = 10):
    """
    Try today → week → month → all until we get at least min_results.
    Returns (query_with_filter, chosen_time_range_string).
    """
    for tr in ("today", "week", "month", "all"):
        q = apply_time_filter(base_query, tr)
        # Only need to know if there are at least min_results
        count = q.limit(min_results).count()
        if count >= min_results or tr == "all":
            return q, tr

    # Fallback, shouldn't really happen
    return base_query, "all"


def expand_time_range_if_sparse(
    base_query,
    time_range: str,
    min_results: int = 3,
):
    """
    For sparse feeds (especially clips): widen the requested window only as far
    as needed, without rewriting cookies server-side.

      today  → week  → month   (stop; no auto jump to all)
      week   → month
      month / all → unchanged

    Returns (query_with_filter, chosen_time_range).
    """
    ladders = {
        "today": ("today", "week", "month"),
        "week": ("week", "month"),
        "month": ("month",),
        "all": ("all",),
    }
    steps = ladders.get(time_range) or (time_range,)
    chosen = steps[-1]
    query = apply_time_filter(base_query, chosen)
    for tr in steps:
        q = apply_time_filter(base_query, tr)
        if q.limit(min_results).count() >= min_results:
            return q, tr
        chosen = tr
        query = q
    return query, chosen


@main_bp.route("/terms")
@main_bp.route("/rules")
@main_bp.route("/terms-and-conditions")
def terms_and_conditions():
    """Public Rules, Terms & Conditions (cookie notice / legal)."""
    return render_template(
        "terms.html",
        noindex=False,
        canonical_url=url_for("main.terms_and_conditions", _external=True),
        skip_cookie_consent=True,
        meta_description=(
            "Rules, Terms & Conditions for BannedFromHeaven.com — "
            "community guidelines, cookies, and acceptable use."
        ),
        og_title="Rules, Terms & Conditions – BannedFromHeaven.com",
    )


@main_bp.route("/robots.txt")
def robots_txt():
    # This uses ProxyFix + Traefik forwarded headers, so it should come out as https://<current-host>/sitemap.xml
    sitemap_url = url_for("main.sitemap", _external=True)

    robots = "\n".join(
        [
            "User-agent: *",
            "Allow: /",
            "",
            f"Sitemap: {sitemap_url}",
            "",
        ]
    )

    return Response(robots, mimetype="text/plain")


def apply_time_filter(query, time_range: str):
    now = datetime.utcnow()

    if time_range == "today":
        # Rolling 24 hours instead of midnight
        start = now - timedelta(hours=24)
        return query.filter(Joke.created_at >= start)

    elif time_range == "week":
        start = now - timedelta(days=7)
        return query.filter(Joke.created_at >= start)

    elif time_range == "month":
        start = now - timedelta(days=30)
        return query.filter(Joke.created_at >= start)

    # 'all' → no filter
    return query


FEED_SORT_COOKIE = "feed_sort"
FEED_SORT_MAX_AGE = 60 * 60 * 24 * 365  # 1 year
VALID_FEED_SORTS = frozenset({"best", "newest"})
FEED_PAGE_SIZE = 50
FEED_MAX_SHOW = 2000  # hard cap so "More..." can't pull the whole DB by accident


def resolve_feed_sort() -> str:
    """
    Prefer ?sort=…, else long-lived feed_sort cookie, default 'best'.
    """
    sort = (request.args.get("sort") or "").strip().lower()
    if sort not in VALID_FEED_SORTS:
        sort = (request.cookies.get(FEED_SORT_COOKIE) or "").strip().lower()
    if sort not in VALID_FEED_SORTS:
        sort = "best"
    return sort


def resolve_feed_show() -> int:
    """How many cards to show (grows by FEED_PAGE_SIZE via More...)."""
    raw = request.args.get("show")
    try:
        show = int(raw) if raw is not None else FEED_PAGE_SIZE
    except (TypeError, ValueError):
        show = FEED_PAGE_SIZE
    if show < 1:
        show = FEED_PAGE_SIZE
    return min(show, FEED_MAX_SHOW)


def apply_feed_sort(query, sort: str):
    if sort == "newest":
        return query.order_by(Joke.created_at.desc(), Joke.id.desc())
    # best: highest score, newest as tie-breaker
    return query.order_by(Joke.score.desc(), Joke.created_at.desc())


def fetch_feed_jokes(query, sort: str, show: int):
    """
    Return (jokes, has_more). Fetches show+1 rows to detect another page.
    """
    query = apply_feed_sort(query, sort)
    rows = query.limit(show + 1).all()
    has_more = len(rows) > show
    return rows[:show], has_more


def feed_vote_map(jokes):
    vote_map = {}
    if current_user.is_authenticated and jokes:
        joke_ids = [j.id for j in jokes]
        votes = Vote.query.filter(
            Vote.user_id == current_user.id,
            Vote.joke_id.in_(joke_ids),
        ).all()
        vote_map = {v.joke_id: v.value for v in votes}
    return vote_map


def feed_vote_tallies(jokes):
    """
    Per-joke counts of vote *types* (not score):
      super = value 2, up = value 1, down = value -1
    One grouped query for the whole page — safe for feed size.
    """
    empty = {"super": 0, "up": 0, "down": 0}
    if not jokes:
        return {}
    joke_ids = [j.id for j in jokes]
    out = {jid: dict(empty) for jid in joke_ids}
    rows = (
        db.session.query(
            Vote.joke_id,
            func.coalesce(func.sum(case((Vote.value == 2, 1), else_=0)), 0),
            func.coalesce(func.sum(case((Vote.value == 1, 1), else_=0)), 0),
            func.coalesce(func.sum(case((Vote.value == -1, 1), else_=0)), 0),
        )
        .filter(Vote.joke_id.in_(joke_ids))
        .group_by(Vote.joke_id)
        .all()
    )
    for jid, n_super, n_up, n_down in rows:
        out[int(jid)] = {
            "super": int(n_super or 0),
            "up": int(n_up or 0),
            "down": int(n_down or 0),
        }
    return out


def joke_vote_tallies(joke_id: int) -> dict:
    class _J:
        def __init__(self, i):
            self.id = i

    return feed_vote_tallies([_J(joke_id)]).get(
        joke_id, {"super": 0, "up": 0, "down": 0}
    )


def feed_24h_counts() -> dict:
    """Approved posts created in the last rolling 24 hours, by type."""
    cutoff = datetime.utcnow() - timedelta(hours=24)
    recent = approved_jokes_query().filter(Joke.created_at >= cutoff)
    return {
        "jokes": recent.filter_by(is_meme=False, is_clip=False).count(),
        "memes": approved_jokes_query()
        .filter(Joke.created_at >= cutoff, Joke.is_meme.is_(True))
        .count(),
        "clips": approved_jokes_query()
        .filter(Joke.created_at >= cutoff, Joke.is_clip.is_(True))
        .count(),
    }


def render_feed(template_name: str, *, sort: str, **context):
    """Render a feed page and refresh the long-lived sort cookie."""
    if "count_24h" not in context:
        context["count_24h"] = feed_24h_counts()
    # Mode-specific SEO defaults (views may still override)
    mode = context.get("mode") or "jokes"
    if "meta_description" not in context:
        if mode == "memes":
            context["meta_description"] = (
                "Sickipedia style memes and dark humour images on "
                "BannedFromHeaven.com."
            )
            context["og_title"] = (
                "Memes – Sickipedia Style Dark Humour | BannedFromHeaven.com"
            )
        elif mode == "clips":
            context["meta_description"] = (
                "Sickipedia style video clips and dark humour on "
                "BannedFromHeaven.com."
            )
            context["og_title"] = (
                "Clips – Sickipedia Style Dark Humour | BannedFromHeaven.com"
            )
        else:
            context["meta_description"] = (
                "Sickipedia style jokes, dark humour and offensive comedy on "
                "BannedFromHeaven.com."
            )
            context["og_title"] = (
                "Sickipedia Style Jokes & Dark Humour | BannedFromHeaven.com"
            )
        context["og_description"] = context["meta_description"]
    resp = make_response(render_template(template_name, sort=sort, **context))
    resp.set_cookie(
        FEED_SORT_COOKIE,
        sort,
        max_age=FEED_SORT_MAX_AGE,
        samesite="Lax",
        secure=True,
        httponly=False,
        path="/",
    )
    return resp


@main_bp.route("/jokes/<int:joke_id>/delete", methods=["POST"])
@login_required
def delete_joke(joke_id):
    joke = Joke.query.get_or_404(joke_id)

    # Only author or admin can delete
    if not (current_user.is_admin or current_user.id == joke.user_id):
        abort(403)

    author = joke.author

    # Undo reputation from all votes on this joke, then delete the vote rows.
    votes = Vote.query.filter_by(joke_id=joke.id).all()
    for v in votes:
        if author is not None:
            author.reputation = (author.reputation or 0) - (v.value or 0)
        db.session.delete(v)

    # Comment reactions first (FK to comments), then comments
    comments = Comment.query.filter_by(joke_id=joke.id).all()
    if comments:
        comment_ids = [c.id for c in comments]
        JokeCommentReaction.query.filter(
            JokeCommentReaction.comment_id.in_(comment_ids)
        ).delete(synchronize_session=False)
        Comment.query.filter(Comment.id.in_(comment_ids)).delete(
            synchronize_session=False
        )

    # Related rows with NOT NULL joke_id: must DELETE (not null FK).
    # Use ORM deletes so the identity map matches the DB (bulk delete with
    # synchronize_session=False left managed rows that SQLAlchemy then tried
    # to UPDATE joke_id=NULL → IntegrityError).
    from .models import JokeCommentRead, JokeFollow, JokeDupePair

    for row in QuarantinedJoke.query.filter_by(joke_id=joke.id).all():
        db.session.delete(row)
    for row in JokeCommentRead.query.filter_by(joke_id=joke.id).all():
        db.session.delete(row)
    for row in JokeFollow.query.filter_by(joke_id=joke.id).all():
        db.session.delete(row)

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

    deleted_id = joke.id
    db.session.delete(joke)
    db.session.commit()

    try:
        from .embed_outbox import enqueue_joke_delete

        enqueue_joke_delete(joke_id=deleted_id)
    except Exception:
        current_app.logger.exception("embed outbox delete enqueue failed for %s", deleted_id)

    flash("Post deleted.", "success")
    return redirect(url_for("main.index"))


@main_bp.route("/jokes/<int:joke_id>/edit", methods=["GET", "POST"])
@login_required
def edit_joke(joke_id):
    joke = Joke.query.get_or_404(joke_id)

    # Only author or admin can edit
    if not (current_user.is_admin or current_user.id == joke.user_id):
        abort(403)

    if request.method == "POST":
        body = request.form.get("body", "")
        body = maybe_sanitize_post_html(body, current_user)
        body = body.replace("\r\n", "\n")
        body = body.replace("\u00a0", " ")  # NBSP --> Normal Space
        body = body.lstrip()
        joke.body = body

        # Update category / subcategory if provided
        cat_id, sub_id = resolve_category_subcategory(
            request.form.get("category_id"),
            request.form.get("subcategory_id"),
        )
        joke.category_id = cat_id
        joke.subcategory_id = sub_id

        # Optionally switch joke/meme flag
        kind = request.form.get("type", "joke")
        joke.is_meme = kind == "meme"

        # Add / edit / clear Salute To (empty field removes it)
        joke.salute_to = normalize_salute_to(
            request.form.get("salute_to", ""),
            actor_username=current_user.username,
        )

        db.session.commit()
        return redirect(url_for("main.joke_detail", joke_id=joke.id))

    categories = Category.query.order_by(Category.name).all()
    return render_template(
        "edit_joke.html",
        joke=joke,
        categories=categories,
        subcategories_by_category=subcategories_by_category_map(categories),
    )

@main_bp.route("/api/users/salute-search")
@login_required
def salute_user_search():
    q = (request.args.get("q") or "").strip()
    if len(q) < 3:
        return jsonify({"ok": True, "users": []})
    users = (
        User.query
        .filter(
            User.username.ilike(f"{q}%"),
            User.id != current_user.id,  # can't salute yourself
        )
        .order_by(User.username.asc())
        .limit(10)
        .all()
    )
    return jsonify({
        "ok": True,
        "users": [u.username for u in users]
    })

@main_bp.route("/comments/<int:comment_id>/edit", methods=["GET", "POST"])
@login_required
def edit_comment(comment_id):
    comment = Comment.query.get_or_404(comment_id)

    # Only author or admin can edit
    if not (current_user.is_admin or current_user.id == comment.user_id):
        abort(403)

    if request.method == "POST":
        body = request.form.get("body", "").strip()
        body = maybe_sanitize_post_html(body, current_user)
        if not body:
            flash("Comment cannot be empty.", "danger")
            return redirect(url_for("main.edit_comment", comment_id=comment.id))

        comment.body = body
        db.session.commit()
        flash("Comment updated.", "success")
        return redirect(url_for("main.joke_detail", joke_id=comment.joke_id))

    return render_template("edit_comment.html", comment=comment)


@main_bp.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(current_app.config["UPLOAD_FOLDER"], filename)


@main_bp.route("/clips/<path:filename>/download")
def clip_download(filename):
    """Force-download clip (same-origin; works when CLIPS_URL is on media host)."""
    # Register before /clips/<path> so ".../download" is not swallowed as a filename.
    safe_name = os.path.basename(filename) or "clip.mp4"
    return send_from_directory(
        current_app.config["CLIPS_FOLDER"],
        filename,
        as_attachment=True,
        download_name=safe_name,
    )


@main_bp.route("/clips/<path:filename>")
def clip_file(filename):
    return send_from_directory(current_app.config["CLIPS_FOLDER"], filename)


@main_bp.route("/clip_thumbs/<path:filename>")
def clip_thumb(filename):
    return send_from_directory(current_app.config["CLIP_THUMBS_FOLDER"], filename)


@main_bp.route("/")
def index():
    # --- Page view counter (persistent) ---
    pv = PageView.query.filter_by(page="index").first()
    if pv is None:
        pv = PageView(page="index", count=1)
        db.session.add(pv)
    else:
        pv.count += 1
    db.session.commit()
    # Query params
    requested_range = request.args.get(
        "range", None
    )  # 'today', 'week', 'month', 'all' or None
    category_name = request.args.get("category", None)  # e.g. "Sex n Shit", "Wordplay"
    subcategory_name = request.args.get("subcategory", None)
    sort = resolve_feed_sort()
    show = resolve_feed_show()

    # Base query: all jokes (not memes)
    base_query = approved_jokes_query().filter_by(is_meme=False, is_clip=False)

    # Optional category filter (by category.name)
    if category_name:
        category = Category.query.filter_by(name=category_name).first()
        if category:
            base_query = base_query.filter_by(category_id=category.id)
    if subcategory_name:
        sub = Subcategory.query.filter_by(name=subcategory_name).first()
        if sub:
            base_query = base_query.filter_by(subcategory_id=sub.id)

    # Time range handling (unchanged logic, applied to filtered base_query)
    valid_ranges = {"today", "week", "month", "all"}
    if requested_range in valid_ranges:
        time_range = requested_range
        query = apply_time_filter(base_query, time_range)
    else:
        # No explicit range: auto-widen until we get at least 10 jokes
        query, time_range = choose_best_time_range(base_query, min_results=10)

    jokes, has_more = fetch_feed_jokes(query, sort, show)
    vote_map = feed_vote_map(jokes)
    vote_tallies = feed_vote_tallies(jokes)
    settings = SiteSettings.get()

    return render_feed(
        "index.html",
        sort=sort,
        jokes=jokes,
        time_range=time_range,
        mode="jokes",
        settings=settings,
        vote_map=vote_map,
        vote_tallies=vote_tallies,
        page_views=pv.count,
        show=show,
        has_more=has_more,
        page_size=FEED_PAGE_SIZE,
        category_name=category_name,
        subcategory_name=subcategory_name,
        canonical_url=url_for("main.index", _external=True),
        noindex=bool(request.args),
    )


@main_bp.route("/memes")
def memes():
    # Query params
    requested_range = request.args.get(
        "range", None
    )  # 'today', 'week', 'month', 'all' or None
    category_name = request.args.get("category", None)
    subcategory_name = request.args.get("subcategory", None)
    sort = resolve_feed_sort()
    show = resolve_feed_show()

    # Base query: memes only
    base_query = approved_jokes_query().filter_by(is_meme=True)

    # Optional category filter
    if category_name:
        category = Category.query.filter_by(name=category_name).first()
        if category:
            base_query = base_query.filter_by(category_id=category.id)
    if subcategory_name:
        sub = Subcategory.query.filter_by(name=subcategory_name).first()
        if sub:
            base_query = base_query.filter_by(subcategory_id=sub.id)

    # Time range logic (same as before)
    valid_ranges = {"today", "week", "month", "all"}
    if requested_range in valid_ranges:
        time_range = requested_range
        query = apply_time_filter(base_query, time_range)
    else:
        query, time_range = choose_best_time_range(base_query, min_results=10)

    jokes, has_more = fetch_feed_jokes(query, sort, show)
    vote_map = feed_vote_map(jokes)
    vote_tallies = feed_vote_tallies(jokes)
    settings = SiteSettings.get()

    return render_feed(
        "index.html",
        sort=sort,
        jokes=jokes,
        mode="memes",
        time_range=time_range,
        settings=settings,
        vote_map=vote_map,
        vote_tallies=vote_tallies,
        page_views=None,
        show=show,
        has_more=has_more,
        page_size=FEED_PAGE_SIZE,
        category_name=category_name,
        subcategory_name=subcategory_name,
    )


@main_bp.route("/clips")
def clips():
    requested_range = request.args.get("range", None)
    category_name = request.args.get("category", None)
    subcategory_name = request.args.get("subcategory", None)
    sort = resolve_feed_sort()
    show = resolve_feed_show()

    base_query = approved_jokes_query().filter_by(is_clip=True)

    if category_name:
        category = Category.query.filter_by(name=category_name).first()
        if category:
            base_query = base_query.filter_by(category_id=category.id)
    if subcategory_name:
        sub = Subcategory.query.filter_by(name=subcategory_name).first()
        if sub:
            base_query = base_query.filter_by(subcategory_id=sub.id)

    valid_ranges = {"today", "week", "month", "all"}
    # Clips are sparse: may widen the *query* today→week→month when <3 items.
    # Artificial only — sticky/URL range (sessionStorage feed_range) and the
    # dropdown stay on the user's preferred setting so Jokes/Memes are unchanged.
    clips_effective_range = None
    if requested_range in valid_ranges:
        preferred_range = requested_range
        query, effective_range = expand_time_range_if_sparse(
            base_query, preferred_range, min_results=3
        )
        time_range = preferred_range  # UI / mode links / sticky
        if effective_range != preferred_range:
            clips_effective_range = effective_range
    else:
        # No range in URL yet: pick a window with enough clips for this view only
        query, effective_range = choose_best_time_range(base_query, min_results=3)
        time_range = effective_range
        preferred_range = effective_range

    jokes, has_more = fetch_feed_jokes(query, sort, show)
    vote_map = feed_vote_map(jokes)
    vote_tallies = feed_vote_tallies(jokes)
    settings = SiteSettings.get()

    return render_feed(
        "index.html",
        sort=sort,
        jokes=jokes,
        mode="clips",
        time_range=time_range,
        clips_effective_range=clips_effective_range,
        settings=settings,
        vote_map=vote_map,
        vote_tallies=vote_tallies,
        page_views=None,
        show=show,
        has_more=has_more,
        page_size=FEED_PAGE_SIZE,
        category_name=category_name,
        subcategory_name=subcategory_name,
    )


@main_bp.route("/search")
def search():
    """Search jokes/memes by time range, date range, category/categories, score, username and keyword."""

    # Inputs
    time_range = request.args.get("range", "today")
    valid_ranges = {"today", "week", "month", "all"}
    if time_range not in valid_ranges:
        time_range = "today"

    search_type = request.args.get("type", "all")  # all|jokes|memes
    # NEW: multi-category selection (comma-separated ids)
    category_ids_raw = (request.args.get("category_ids") or "").strip()
    category_id_raw = (request.args.get("category_id") or "").strip()  # legacy
    subcategory_ids_raw = (request.args.get("subcategory_ids") or "").strip()
    username = (request.args.get("username") or "").strip()
    min_score_raw = (request.args.get("min_score") or "").strip()
    date_from_raw = (request.args.get("date_from") or "").strip()
    date_to_raw = (request.args.get("date_to") or "").strip()

    # NEW: keyword / phrase search (body/title)
    q_raw = (request.args.get("q") or "").strip()

    # NEW: sort
    sort = (request.args.get("sort") or "newest").strip()
    valid_sorts = {"newest", "oldest", "score_desc", "score_asc", "comments_desc"}
    if sort not in valid_sorts:
        sort = "newest"

    # Base query
    query = approved_jokes_query()

    if search_type == "jokes":
        query = query.filter_by(is_meme=False, is_clip=False)
    elif search_type == "memes":
        query = query.filter_by(is_meme=True)

    # Categories (multi)
    selected_category_ids = []
    if category_ids_raw:
        for part in category_ids_raw.split(","):
            part = part.strip()
            if part.isdigit():
                selected_category_ids.append(int(part))
    elif category_id_raw and category_id_raw.isdigit():
        selected_category_ids = [int(category_id_raw)]

    if selected_category_ids:
        query = query.filter(Joke.category_id.in_(selected_category_ids))

    selected_subcategory_ids = []
    if subcategory_ids_raw:
        for part in subcategory_ids_raw.split(","):
            part = part.strip()
            if part.isdigit():
                selected_subcategory_ids.append(int(part))
    if selected_subcategory_ids:
        query = query.filter(Joke.subcategory_id.in_(selected_subcategory_ids))

    # Username filter (textbox, no dropdown = fewer spite campaigns)
    if username:
        query = query.join(User, Joke.user_id == User.id).filter(
            User.username.ilike(username)
        )

    # Keyword filter (simple LIKE on title/body)
    if q_raw:
        like = f"%{q_raw}%"
        query = query.filter(or_(Joke.body.ilike(like), Joke.title.ilike(like)))

    # Score/rating filter
    if min_score_raw:
        try:
            query = query.filter(Joke.score >= int(min_score_raw))
        except ValueError:
            pass

    # Date filtering: explicit date_from/date_to wins; otherwise use quick time-range filter.
    used_explicit_dates = False
    if date_from_raw or date_to_raw:
        try:
            if date_from_raw:
                dt_from = datetime.strptime(date_from_raw, "%Y-%m-%d")
                query = query.filter(Joke.created_at >= dt_from)
            if date_to_raw:
                dt_to = datetime.strptime(date_to_raw, "%Y-%m-%d") + timedelta(days=1)
                query = query.filter(Joke.created_at < dt_to)
            used_explicit_dates = True
        except ValueError:
            used_explicit_dates = False

    if not used_explicit_dates:
        query = apply_time_filter(query, time_range)

    # Sorting
    if sort == "oldest":
        query = query.order_by(Joke.created_at.asc())
    elif sort == "score_desc":
        query = query.order_by(Joke.score.desc(), Joke.created_at.desc())
    elif sort == "score_asc":
        query = query.order_by(Joke.score.asc(), Joke.created_at.desc())
    elif sort == "comments_desc":
        # comments count via subquery; stable tiebreaker: newest first
        cc = (
            db.session.query(
                Comment.joke_id.label("joke_id"),
                func.count(Comment.id).label("comment_count"),
            )
            .group_by(Comment.joke_id)
            .subquery()
        )
        query = query.outerjoin(cc, Joke.id == cc.c.joke_id).order_by(
            func.coalesce(cc.c.comment_count, 0).desc(),
            Joke.created_at.desc(),
        )
    else:
        # newest (default)
        query = query.order_by(Joke.created_at.desc())

    # Cap to keep it fast
    results = query.limit(200).all()

    # Vote glow mapping (same as index)
    vote_map = {}
    if current_user.is_authenticated and results:
        ids = [j.id for j in results]
        votes = Vote.query.filter(
            Vote.user_id == current_user.id,
            Vote.joke_id.in_(ids),
        ).all()
        vote_map = {v.joke_id: v.value for v in votes}
    vote_tallies = feed_vote_tallies(results)

    categories = Category.query.order_by(Category.name.asc()).all()
    subcategories_by_category = subcategories_by_category_map(categories)
    # Flat list of subcats relevant to selected categories (or all if none selected)
    searchable_subcategories = []
    if selected_category_ids:
        for cid in selected_category_ids:
            for s in subcategories_by_category.get(cid, []):
                searchable_subcategories.append(
                    {"id": s["id"], "name": s["name"], "category_id": cid}
                )
    else:
        for cid, subs in subcategories_by_category.items():
            for s in subs:
                searchable_subcategories.append(
                    {"id": s["id"], "name": s["name"], "category_id": cid}
                )
    searchable_subcategories.sort(key=lambda x: (x["name"] or "").lower())

    current_args = request.args.to_dict(flat=True)

    # Category URLs for the category badge inside result cards:
    # clicking a badge should set ONLY that category (replace current category selection)
    base_args = request.args.to_dict(flat=True)
    base_args.pop("category_id", None)
    base_args.pop("category_ids", None)
    base_args.pop("subcategory_ids", None)

    category_url_map = {}
    for cat in categories:
        args = dict(base_args)
        args["category_ids"] = str(cat.id)
        category_url_map[cat.id] = url_for("main.search", **args)

    subcategory_url_map = {}
    for row in searchable_subcategories:
        args = dict(base_args)
        # Pin parent category + this subcategory for a focused result set
        parent_id = row.get("category_id")
        if parent_id:
            args["category_ids"] = str(parent_id)
        args["subcategory_ids"] = str(row["id"])
        subcategory_url_map[row["id"]] = url_for("main.search", **args)

    return render_template(
        "search.html",
        results=results,
        categories=categories,
        category_url_map=category_url_map,
        subcategory_url_map=subcategory_url_map,
        subcategories_by_category=subcategories_by_category,
        searchable_subcategories=searchable_subcategories,
        time_range=time_range,
        vote_map=vote_map,
        vote_tallies=vote_tallies,
        current_args=current_args,
        search_type=search_type,
        selected_category_ids=selected_category_ids,
        selected_subcategory_ids=selected_subcategory_ids,
        q=q_raw,
        sort=sort,
        noindex=True,
        canonical_url=url_for("main.search", _external=True),
    )


@main_bp.route("/sitecode.zip")
def sitecode_zip():
    # current_app.root_path is typically /app/app inside the container
    return send_from_directory(
        directory=current_app.root_path, path="sitecode.zip", as_attachment=True
    )


@main_bp.route("/jokes/<int:joke_id>")
def joke_detail(joke_id):
    joke = Joke.query.get_or_404(joke_id)
    if joke.review_status != "approved":
        if not current_user.is_authenticated or not (current_user.is_admin or current_user.is_moderator or current_user.id == joke.user_id):
            abort(404)
    comments = (
        Comment.query.filter_by(joke_id=joke.id)
        .order_by(desc(Comment.created_at))
        .all()
    )
    user_vote = 0
    is_following = False
    if current_user.is_authenticated:
        v = Vote.query.filter_by(user_id=current_user.id, joke_id=joke.id).first()
        if v:
            user_vote = v.value  # +1, +2, or -1
        # Owner viewing their post clears "unseen comments" glow for this joke
        if current_user.id == joke.user_id:
            try:
                from .comment_reads import mark_joke_comments_seen

                mark_joke_comments_seen(current_user.id, joke.id)
            except Exception:
                current_app.logger.exception(
                    "mark_joke_comments_seen failed joke=%s", joke.id
                )
        try:
            from .follows import is_following as _is_following
            from .follows import mark_followed_joke_seen

            is_following = _is_following(current_user.id, joke.id)
            if is_following:
                mark_followed_joke_seen(current_user.id, joke.id)
        except Exception:
            current_app.logger.exception(
                "follow seen/check failed joke=%s", joke.id
            )
    vote_tallies = {joke.id: joke_vote_tallies(joke.id)}
    # Reactions for comments on this joke
    comment_ids = [c.id for c in comments]
    reaction_summary = {}
    user_reactions = {}

    if comment_ids:
        # overall counts per comment per reaction type
        rows = (
            db.session.query(
                JokeCommentReaction.comment_id,
                JokeCommentReaction.reaction_type,
                func.count(JokeCommentReaction.id),
            )
            .filter(JokeCommentReaction.comment_id.in_(comment_ids))
            .group_by(JokeCommentReaction.comment_id, JokeCommentReaction.reaction_type)
            .all()
        )
        for comment_id, reaction_type, count in rows:
            reaction_summary.setdefault(comment_id, {})[reaction_type] = count
        if current_user.is_authenticated:
            rows = (
                JokeCommentReaction.query.filter_by(user_id=current_user.id)
                .filter(JokeCommentReaction.comment_id.in_(comment_ids))
                .all()
            )
            for jr in rows:
                user_reactions[jr.comment_id] = jr.reaction_type
    # Per-joke SEO (unique title/description; OG points at this URL)
    canon = url_for("main.joke_detail", joke_id=joke.id, _external=True)
    body_snip = seo_text_snippet(joke.body or "", 160)
    if joke.is_meme and not body_snip:
        body_snip = "Meme on BannedFromHeaven.com – sickipedia style dark humour."
    elif joke.is_clip and not body_snip:
        body_snip = "Video clip on BannedFromHeaven.com – sickipedia style dark humour."
    elif not body_snip:
        body_snip = (
            "Sickipedia style joke on BannedFromHeaven.com – dark humour and "
            "offensive comedy."
        )
    title_snip = seo_text_snippet(joke.body or joke.title or "Joke", 70) or "Joke"
    page_title = f"{title_snip} – BannedFromHeaven.com"
    # Prefer meme image for social cards when present
    og_image = None
    if joke.is_meme and joke.image_filename:
        try:
            from flask import current_app

            base = (current_app.config.get("UPLOADS_URL") or "").rstrip("/")
            if base:
                og_image = f"{base}/{joke.image_filename}"
        except Exception:
            og_image = None

    return render_template(
        "joke_detail.html",
        joke=joke,
        comments=comments,
        user_vote=user_vote,
        is_following=is_following,
        vote_tallies=vote_tallies,
        reaction_types=REACTION_TYPES,
        reaction_summary=reaction_summary,
        user_reactions=user_reactions,
        canonical_url=canon,
        noindex=False,
        meta_description=body_snip,
        og_url=canon,
        og_type="article",
        og_title=page_title,
        og_description=body_snip,
        **({"og_image": og_image} if og_image else {}),
        page_title=page_title,
    )


PROFILE_SORT_COOKIE = "profile_sort"
PROFILE_SORT_MAX_AGE = 60 * 60 * 24 * 365  # 1 year
VALID_PROFILE_SORTS = frozenset({"highest", "newest"})


def resolve_profile_sort() -> str:
    """Prefer ?sort=…, else profile_sort cookie, default highest (score)."""
    sort = (request.args.get("sort") or "").strip().lower()
    if sort not in VALID_PROFILE_SORTS:
        sort = (request.cookies.get(PROFILE_SORT_COOKIE) or "").strip().lower()
    # Accept "best" as alias in case users reuse the feed cookie value by habit
    if sort == "best":
        sort = "highest"
    if sort not in VALID_PROFILE_SORTS:
        sort = "highest"
    return sort


@main_bp.route("/user/<username>")
def user_profile(username):
    if not username or username.lower() in ("none", "null", "undefined"):
        return redirect(url_for("main.index"), code=301)
    user = User.query.filter_by(username=username).first_or_404()
    sort = resolve_profile_sort()

    # All public post types (jokes, memes, clips) — was wrongly is_meme=False only
    query = approved_jokes_query().filter_by(user_id=user.id)
    if sort == "newest":
        query = query.order_by(Joke.created_at.desc(), Joke.id.desc())
    else:
        query = query.order_by(Joke.score.desc(), Joke.created_at.desc())
    jokes = query.all()

    resp = make_response(
        render_template(
            "user_profile.html",
            profile_user=user,
            jokes=jokes,
            sort=sort,
        )
    )
    resp.set_cookie(
        PROFILE_SORT_COOKIE,
        sort,
        max_age=PROFILE_SORT_MAX_AGE,
        samesite="Lax",
        secure=True,
        httponly=False,
        path="/",
    )
    return resp


@main_bp.route("/account/profile", methods=["GET", "POST"])
@login_required
def edit_profile():
    from .models import UserProfile

    profile = current_user.profile
    if profile is None:
        profile = UserProfile(user=current_user)
        db.session.add(profile)
        db.session.commit()  # make sure it exists in DB and in the session

    if request.method == "POST":
        p = profile
        p.about_me = request.form.get("about_me", "").strip() or None
        p.location = request.form.get("location", "").strip() or None

        facebook_raw = request.form.get("facebook_url", "").strip()
        p.facebook_url = facebook_raw[:120] if facebook_raw else None

        age_raw = request.form.get("age", "").strip()
        p.age = int(age_raw) if age_raw.isdigit() else None

        # Hide checkboxes: present → checked
        p.hide_location = bool(request.form.get("hide_location"))
        p.hide_age = bool(request.form.get("hide_age"))
        p.hide_facebook = bool(request.form.get("hide_facebook"))

        file = request.files.get("profile_picture")
        if file and file.filename:
            from werkzeug.utils import secure_filename

            filename = secure_filename(f"{current_user.id}_pfp_{file.filename}")
            upload_path = os.path.join(current_app.config["UPLOAD_FOLDER"], filename)
            file.save(upload_path)
            p.profile_picture = filename

        db.session.commit()
        flash("Profile updated.", "success")
        return redirect(url_for("main.user_profile", username=current_user.username))

    return render_template("edit_profile.html", user=current_user)


@main_bp.route("/api/uploads/sign", methods=["POST"])
@login_required
def uploads_sign():
    data = request.get_json(silent=True) or {}
    joke_type = (data.get("joke_type") or "").strip().lower()
    if joke_type not in ("meme", "clip"):
        return jsonify({"ok": False, "error": "Invalid joke_type"}), 400

    token = _uploads_serializer().dumps({"uid": current_user.id, "jt": joke_type})
    return jsonify({"ok": True, "token": token})


@main_bp.route("/api/uploads/finalize", methods=["POST"])
def uploads_finalize():
    _require_uploads_secret()

    data = request.get_json(silent=True) or {}
    token = (data.get("token") or "").strip()
    if not token:
        return jsonify({"ok": False, "error": "Missing token"}), 400

    try:
        payload = _uploads_serializer().loads(token, max_age=600)
    except SignatureExpired:
        return jsonify({"ok": False, "error": "Token expired"}), 401
    except BadSignature:
        return jsonify({"ok": False, "error": "Bad token"}), 401

    user_id = int(payload.get("uid") or 0)
    joke_type = (data.get("joke_type") or payload.get("jt") or "").strip().lower()
    if joke_type not in ("meme", "clip"):
        return jsonify({"ok": False, "error": "Invalid joke_type"}), 400

    user = User.query.get(user_id)
    if not user:
        return jsonify({"ok": False, "error": "User not found"}), 404

    category_id_int, subcategory_id_int = resolve_category_subcategory(
        data.get("category_id"), data.get("subcategory_id")
    )

    body = (data.get("body") or "").strip()
    body = maybe_sanitize_post_html(body, user)
    salute_to = normalize_salute_to(
        data.get("salute_to"), actor_username=user.username
    )

    is_meme = joke_type == "meme"
    is_clip = joke_type == "clip"

    image_filename = (data.get("image_filename") or "").strip() or None
    video_filename = (data.get("video_filename") or "").strip() or None
    video_thumb = (data.get("video_thumb") or "").strip() or None
    video_duration = data.get("video_duration")
    video_size = data.get("video_size")

    if is_meme and not image_filename:
        return jsonify({"ok": False, "error": "Missing image_filename"}), 400
    if is_clip and not video_filename:
        return jsonify({"ok": False, "error": "Missing video_filename"}), 400

    as_draft = str(data.get("as_draft") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    draft_id_raw = data.get("draft_id")
    try:
        draft_id = int(draft_id_raw) if draft_id_raw not in (None, "") else None
    except (TypeError, ValueError):
        draft_id = None

    # --- Save / update draft (media already processed by upload worker) ---
    if as_draft:
        draft = None
        if draft_id:
            draft = get_user_draft(draft_id, user.id)
            if not draft:
                return jsonify({"ok": False, "error": "Draft not found"}), 404
        else:
            n = JokeDraft.query.filter_by(user_id=user.id).count()
            if n >= MAX_JOKE_DRAFTS_PER_USER:
                return (
                    jsonify(
                        {
                            "ok": False,
                            "error": (
                                f"Draft limit reached ({MAX_JOKE_DRAFTS_PER_USER}). "
                                "Delete or submit an existing draft."
                            ),
                        }
                    ),
                    400,
                )
            draft = JokeDraft(user_id=user.id, joke_type=joke_type, body="")
            db.session.add(draft)
            db.session.flush()

        set_draft_type(draft, joke_type)
        draft.body = body
        draft.salute_to = salute_to
        draft.category_id = category_id_int
        draft.subcategory_id = subcategory_id_int
        draft.updated_at = datetime.utcnow()
        apply_draft_media_replacement(
            draft,
            joke_type=joke_type,
            image_filename=image_filename,
            video_filename=video_filename,
            video_thumb=video_thumb,
            video_duration=video_duration,
            video_size=video_size,
        )
        db.session.commit()
        return jsonify({"ok": True, "draft_id": draft.id, "as_draft": True})

    if is_meme:
        title = "Meme"
    elif is_clip:
        title = body[:80] or "Clip"
    else:
        title = body[:80]

    settings = SiteSettings.get()

    if settings.max_jokes_per_hour and settings.max_jokes_per_hour > 0:
        hour_count = Joke.query.filter(
            Joke.user_id == user.id,
            Joke.created_at >= func.datetime("now", "-1 hour"),
        ).count()
        if hour_count >= settings.max_jokes_per_hour:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": f"Rate limit: {settings.max_jokes_per_hour}/hour",
                    }
                ),
                429,
            )

    if settings.max_jokes_per_day and settings.max_jokes_per_day > 0:
        day_count = Joke.query.filter(
            Joke.user_id == user.id,
            Joke.created_at >= func.datetime("now", "-1 day"),
        ).count()
        if day_count >= settings.max_jokes_per_day:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": f"Daily limit: {settings.max_jokes_per_day}/day",
                    }
                ),
                429,
            )

    joke = Joke(
        body=body,
        category_id=category_id_int,
        subcategory_id=subcategory_id_int,
        image_filename=image_filename,
        is_clip=is_clip,
        is_meme=is_meme,
        salute_to=salute_to,
        title=title,
        user_id=user.id,
        video_duration=video_duration,
        video_filename=video_filename,
        video_thumb=video_thumb,
        video_size=video_size,
    )
    joke.review_status = "pending" if user.needs_moderator else "approved"
    db.session.add(joke)
    db.session.flush()
    if joke.review_status == "approved":
        auto_vote = Vote(user_id=user.id, joke_id=joke.id, value=1)
        db.session.add(auto_vote)
        joke.score += 1
        user.reputation += 1

    # Publishing from a draft: drop draft row but keep the new media files.
    if draft_id:
        old_draft = get_user_draft(draft_id, user.id)
        if old_draft:
            # Only delete media that was not carried over into the new joke
            if (
                old_draft.image_filename
                and old_draft.image_filename != image_filename
            ):
                _safe_unlink_upload(old_draft.image_filename, "UPLOAD_FOLDER")
            if (
                old_draft.video_filename
                and old_draft.video_filename != video_filename
            ):
                _safe_unlink_upload(old_draft.video_filename, "CLIPS_FOLDER")
            if old_draft.video_thumb and old_draft.video_thumb != video_thumb:
                _safe_unlink_upload(old_draft.video_thumb, "CLIP_THUMBS_FOLDER")
            db.session.delete(old_draft)

    db.session.commit()
    return jsonify({"ok": True, "joke_id": joke.id, "review_status": joke.review_status})


def resolve_submit_joke_type() -> str:
    """
    Prefer form (POST) → explicit ?type= → 'joke'.
    No sticky cookie — media selection drives meme/clip on the submit form.
    """
    if request.method == "POST":
        jt = (request.form.get("joke_type") or "").strip().lower()
    else:
        jt = (
            request.args.get("type")
            or request.args.get("joke_type")
            or ""
        ).strip().lower()
    if jt not in VALID_SUBMIT_TYPES:
        jt = "joke"
    return jt


def render_submit_joke(categories, joke_type: str, **extra):
    """Render submit form (defaults to plain joke; clears legacy sticky type cookie)."""
    if "drafts" not in extra and current_user.is_authenticated:
        extra["drafts"] = list_user_drafts(current_user.id)
    if "draft" not in extra:
        extra["draft"] = None
    if "subcategories_by_category" not in extra:
        extra["subcategories_by_category"] = subcategories_by_category_map(categories)
    if current_user.is_authenticated:
        extra.setdefault("staff_html_ok", user_can_post_html(current_user))
    resp = make_response(
        render_template(
            "submit_joke.html",
            categories=categories,
            joke_type=joke_type,
            **extra,
        )
    )
    # Expire legacy sticky type cookie so it cannot override defaults
    resp.set_cookie(
        SUBMIT_TYPE_COOKIE,
        "",
        max_age=0,
        samesite="Lax",
        secure=True,
        httponly=False,
        path="/",
    )
    return resp


def _submit_clip_template_vars():
    return {
        "max_clip_size_mb": get_max_clip_size_mb(),
        "max_clip_length_seconds": get_max_clip_length_seconds(),
        "max_clip_length_human": human_clip_length(get_max_clip_length_seconds()),
    }


def _parse_category_id(raw):
    try:
        return int(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _parse_subcategory_id(raw):
    try:
        return int(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return None


def resolve_category_subcategory(category_raw, subcategory_raw):
    """
    Parse category_id + subcategory_id and ensure the sub belongs to the category.
    Returns (category_id|None, subcategory_id|None).
    """
    cat_id = _parse_category_id(category_raw)
    sub_id = _parse_subcategory_id(subcategory_raw)
    if not cat_id:
        return None, None
    if not Category.query.get(cat_id):
        return None, None
    if not sub_id:
        return cat_id, None
    sub = Subcategory.query.get(sub_id)
    if not sub or int(sub.category_id) != int(cat_id):
        return cat_id, None
    return cat_id, sub.id


def subcategories_by_category_map(categories=None):
    """{category_id: [{id, name}, ...]} for dependent selects / search pills."""
    if categories is None:
        categories = Category.query.order_by(Category.name.asc()).all()
    out = {}
    for c in categories:
        subs = sorted(
            list(c.subcategories or []),
            key=lambda s: ((s.sort_order or 0), (s.name or "").lower()),
        )
        out[c.id] = [{"id": s.id, "name": s.name} for s in subs]
    return out


def _save_joke_draft_from_form(
    *,
    joke_type: str,
    body: str,
    salute_to,
    category_id,
    draft: JokeDraft | None,
    subcategory_id=None,
    clear_media: bool = False,
    new_image_filename: str | None = None,
    new_video_tuple=None,
) -> JokeDraft:
    """Create or update a draft from the submit form (no rate limit)."""
    if draft is None:
        n = JokeDraft.query.filter_by(user_id=current_user.id).count()
        if n >= MAX_JOKE_DRAFTS_PER_USER:
            raise ValueError(
                f"Draft limit reached ({MAX_JOKE_DRAFTS_PER_USER}). "
                "Delete or submit an existing draft first."
            )
        draft = JokeDraft(user_id=current_user.id, joke_type=joke_type, body="")
        db.session.add(draft)
        db.session.flush()
    else:
        set_draft_type(draft, joke_type)

    draft.joke_type = joke_type if joke_type in VALID_SUBMIT_TYPES else "joke"
    draft.body = body or ""
    draft.salute_to = salute_to
    draft.category_id = category_id
    draft.subcategory_id = subcategory_id
    draft.updated_at = datetime.utcnow()

    if clear_media:
        if draft.image_filename:
            _safe_unlink_upload(draft.image_filename, "UPLOAD_FOLDER")
            draft.image_filename = None
        if draft.video_filename:
            _safe_unlink_upload(draft.video_filename, "CLIPS_FOLDER")
            draft.video_filename = None
        if draft.video_thumb:
            _safe_unlink_upload(draft.video_thumb, "CLIP_THUMBS_FOLDER")
            draft.video_thumb = None
            draft.video_duration = None
            draft.video_size = None

    if new_image_filename:
        apply_draft_media_replacement(
            draft, joke_type="meme", image_filename=new_image_filename
        )
    if new_video_tuple:
        vf, vt, vd, vs = new_video_tuple
        apply_draft_media_replacement(
            draft,
            joke_type="clip",
            video_filename=vf,
            video_thumb=vt,
            video_duration=vd,
            video_size=vs,
        )

    # Minimal content check — allow incomplete drafts but not totally empty
    has_media = bool(draft.image_filename or draft.video_filename)
    if not (draft.body or "").strip() and not has_media:
        raise ValueError("Draft needs some text or media.")

    db.session.commit()
    return draft


@main_bp.route("/submit", methods=["GET", "POST"])
@login_required
def submit_joke():
    categories = Category.query.order_by(Category.name).all()
    clip_template_vars = _submit_clip_template_vars()

    # Load draft for edit (GET) or continuing a POST
    draft_id = request.values.get("draft_id", type=int) or request.args.get(
        "draft", type=int
    )
    draft = get_user_draft(draft_id, current_user.id) if draft_id else None
    if draft_id and not draft and request.method == "GET":
        flash("Draft not found.", "warning")
        return redirect(url_for("main.submit_joke"))

    if request.method == "GET":
        if draft:
            joke_type = (
                draft.joke_type
                if draft.joke_type in VALID_SUBMIT_TYPES
                else "joke"
            )
        else:
            joke_type = resolve_submit_joke_type()
        return render_submit_joke(
            categories, joke_type, draft=draft, **clip_template_vars
        )

    # --- POST ---
    form_action = (request.form.get("form_action") or "submit").strip().lower()
    joke_type = resolve_submit_joke_type()
    category_id, subcategory_id = resolve_category_subcategory(
        request.form.get("category_id"),
        request.form.get("subcategory_id"),
    )
    body = request.form.get("body", "").strip()
    salute_to = normalize_salute_to(
        request.form.get("salute_to", ""),
        actor_username=current_user.username,
    )
    body = maybe_sanitize_post_html(body, current_user)

    # Re-bind draft from form (authoritative on POST)
    form_draft_id = request.form.get("draft_id", type=int)
    if form_draft_id:
        draft = get_user_draft(form_draft_id, current_user.id)
        if not draft:
            flash("Draft not found or already submitted.", "warning")
            return redirect(url_for("main.submit_joke"))
    else:
        draft = None

    is_meme = joke_type == "meme"
    is_clip = joke_type == "clip"
    clear_media = (request.form.get("clear_media") or "").strip() in (
        "1",
        "true",
        "yes",
        "on",
    )

    # ---------- Save draft ----------
    if form_action == "save_draft":
        new_image = None
        new_video = None
        # Optional multipart media on draft save (usually memes/clips use tus;
        # multipart still works for smaller files / no-JS).
        if is_meme and not clear_media:
            file = request.files.get("image")
            if file and file.filename:
                try:
                    new_image = process_meme_image(file)
                except ValueError as e:
                    flash(str(e), "danger")
                    return render_submit_joke(
                        categories,
                        joke_type,
                        draft=draft,
                        **clip_template_vars,
                    )
        elif is_clip and not clear_media:
            file = request.files.get("clip")
            if file and file.filename:
                try:
                    new_video = process_clip_video(file)
                except ValueError as e:
                    flash(str(e), "danger")
                    return render_submit_joke(
                        categories,
                        joke_type,
                        draft=draft,
                        **clip_template_vars,
                    )
        try:
            draft = _save_joke_draft_from_form(
                joke_type=joke_type,
                body=body,
                salute_to=salute_to,
                category_id=category_id,
                subcategory_id=subcategory_id,
                draft=draft,
                clear_media=clear_media,
                new_image_filename=new_image,
                new_video_tuple=new_video,
            )
        except ValueError as e:
            flash(str(e), "danger")
            return render_submit_joke(
                categories, joke_type, draft=draft, **clip_template_vars
            )
        flash("Draft saved.", "success")
        return redirect(url_for("main.submit_joke", draft=draft.id))

    # ---------- Publish ----------
    image_filename = None
    video_filename = None
    video_thumb = None
    video_duration = None
    video_size = None

    if is_meme:
        file = request.files.get("image")
        if file and file.filename:
            try:
                image_filename = process_meme_image(file)
            except ValueError as e:
                flash(str(e), "danger")
                return render_submit_joke(
                    categories, joke_type, draft=draft, **clip_template_vars
                )
        elif request.form.get("image_filename") and not clear_media:
            image_filename = (request.form.get("image_filename") or "").strip()
        elif draft and draft.image_filename and not clear_media:
            image_filename = draft.image_filename
        else:
            flash("Please upload a meme image (or open a draft that has one).", "danger")
            return render_submit_joke(
                categories, joke_type, draft=draft, **clip_template_vars
            )

        confirm_original = (request.form.get("confirm_original") or "").strip() in (
            "1",
            "true",
            "yes",
            "on",
        )
        if not confirm_original and image_filename:
            try:
                from .similarity import find_similar_memes

                def _load_by_ids(ids):
                    return approved_jokes_query().filter(Joke.id.in_(list(ids))).all()

                similar = find_similar_memes(
                    image_filename=image_filename,
                    limit=6,
                    min_score=0.85,
                    load_jokes_by_ids=_load_by_ids,
                )
                if similar:
                    return render_template(
                        "submit_similar.html",
                        similar=similar,
                        draft_body=body,
                        draft_joke_type="meme",
                        draft_image_filename=image_filename,
                        draft_category_id=category_id or "",
                        draft_subcategory_id=subcategory_id or "",
                        draft_salute_to=salute_to or "",
                        draft_id=draft.id if draft else "",
                        hard_block=False,
                        matched_joke_id=similar[0][0].id if similar else None,
                        matched_score=similar[0][1] if similar else None,
                    )
            except Exception:
                current_app.logger.exception("similar-meme check failed; posting anyway")

    elif is_clip:
        file = request.files.get("clip")
        if file and file.filename:
            try:
                video_filename, video_thumb, video_duration, video_size = (
                    process_clip_video(file)
                )
            except ValueError as e:
                flash(str(e), "danger")
                return render_submit_joke(
                    categories, joke_type, draft=draft, **clip_template_vars
                )
        elif draft and draft.video_filename and not clear_media:
            video_filename = draft.video_filename
            video_thumb = draft.video_thumb
            video_duration = draft.video_duration
            video_size = draft.video_size
        else:
            flash("Please upload a clip (or open a draft that has one).", "danger")
            return render_submit_joke(
                categories, joke_type, draft=draft, **clip_template_vars
            )

    else:
        if not body:
            flash("Joke text is required.", "danger")
            return render_submit_joke(
                categories, joke_type, draft=draft, **clip_template_vars
            )

        # Intermediate page: suggest possible copies before first publish.
        confirm_original = (request.form.get("confirm_original") or "").strip() in (
            "1",
            "true",
            "yes",
            "on",
        )
        try:
            from .similarity import hard_block_recent_duplicate

            hard = hard_block_recent_duplicate(
                body, actor_user_id=current_user.id
            )
        except Exception:
            current_app.logger.exception("hard_block_recent_duplicate failed")
            hard = None
        if hard:
            joke_hit, score, method = hard
            own = int(joke_hit.user_id) == int(current_user.id)
            return render_template(
                "submit_similar.html",
                similar=[(joke_hit, score, method)],
                draft_body=body,
                draft_category_id=category_id or "",
                draft_salute_to=salute_to or "",
                draft_id=draft.id if draft else "",
                hard_block=True,
                hard_block_own=own,
            )

        if not confirm_original:
            try:
                from .similarity import find_similar_jokes, find_recent_quick_dupes

                fuzzy_pool = (
                    approved_jokes_query()
                    .filter_by(is_meme=False, is_clip=False)
                    .order_by(Joke.id.desc())
                    .limit(800)
                    .all()
                )

                def _load_by_ids(ids):
                    return (
                        approved_jokes_query()
                        .filter(Joke.id.in_(list(ids)))
                        .all()
                    )

                similar = find_similar_jokes(
                    body,
                    jokes_for_fuzzy=fuzzy_pool,
                    joke_by_id={j.id: j for j in fuzzy_pool},
                    load_jokes_by_ids=_load_by_ids,
                    limit=8,
                    fuzzy_min=0.72,
                    embed_min=0.72,
                )
                for j, sc, meth in find_recent_quick_dupes(
                    body,
                    actor_user_id=current_user.id,
                    hours=48,
                    same_user_min=0.85,
                    site_exact_only=False,
                    site_min=0.92,
                ):
                    if not any(s[0].id == j.id for s in similar):
                        similar.append((j, sc, meth))
                similar.sort(key=lambda t: t[1], reverse=True)
                similar = similar[:8]
                if similar:
                    return render_template(
                        "submit_similar.html",
                        similar=similar,
                        draft_body=body,
                        draft_category_id=category_id or "",
                        draft_salute_to=salute_to or "",
                        draft_id=draft.id if draft else "",
                        hard_block=False,
                    )
            except Exception:
                current_app.logger.exception(
                    "similar-joke check failed; posting anyway"
                )

    if is_meme:
        title = "Meme"
    elif is_clip:
        title = body[:80] or "Clip"
    else:
        title = body[:80]
    settings = SiteSettings.get()

    if settings.max_jokes_per_hour and settings.max_jokes_per_hour > 0:
        hour_count = Joke.query.filter(
            Joke.user_id == current_user.id,
            Joke.created_at >= func.datetime("now", "-1 hour"),
        ).count()

        if hour_count >= settings.max_jokes_per_hour:
            flash(
                f"Rate limit: {settings.max_jokes_per_hour}/hour. Try again later.",
                "danger",
            )
            return render_submit_joke(
                categories, joke_type, draft=draft, **clip_template_vars
            )

    if settings.max_jokes_per_day and settings.max_jokes_per_day > 0:
        day_count = Joke.query.filter(
            Joke.user_id == current_user.id,
            Joke.created_at >= func.datetime("now", "-1 day"),
        ).count()

        if day_count >= settings.max_jokes_per_day:
            flash(
                f"Daily limit: {settings.max_jokes_per_day}/day. Try again later.",
                "danger",
            )
            return render_submit_joke(
                categories, joke_type, draft=draft, **clip_template_vars
            )

    joke = Joke(
        body=body,
        category_id=category_id,
        subcategory_id=subcategory_id,
        image_filename=image_filename,
        is_clip=is_clip,
        is_meme=is_meme,
        salute_to=salute_to,
        title=title,
        user_id=current_user.id,
        video_duration=video_duration,
        video_filename=video_filename,
        video_size=video_size,
        video_thumb=video_thumb,
    )

    if not is_meme and not is_clip and body:
        try:
            from .similarity import hard_block_recent_duplicate

            hard2 = hard_block_recent_duplicate(
                body, actor_user_id=current_user.id
            )
        except Exception:
            hard2 = None
        if hard2:
            joke_hit, score, method = hard2
            own = int(joke_hit.user_id) == int(current_user.id)
            return render_template(
                "submit_similar.html",
                similar=[(joke_hit, score, method)],
                draft_body=body,
                draft_category_id=category_id or "",
                draft_salute_to=salute_to or "",
                draft_id=draft.id if draft else "",
                hard_block=True,
                hard_block_own=own,
            )

    joke.review_status = "pending" if current_user.needs_moderator else "approved"
    db.session.add(joke)
    db.session.flush()

    # Auto-flag user-confirmed matches for moderator duplicate review queue
    matched_id = request.form.get("matched_joke_id", type=int)
    matched_sc = request.form.get("matched_score", type=float)
    if matched_id and matched_id != joke.id:
        try:
            from .models import JokeDupePair
            older_id = min(matched_id, joke.id)
            newer_id = max(matched_id, joke.id)
            existing_pair = JokeDupePair.query.filter_by(
                joke_older_id=older_id,
                joke_newer_id=newer_id,
            ).first()
            if not existing_pair:
                db.session.add(
                    JokeDupePair(
                        joke_older_id=older_id,
                        joke_newer_id=newer_id,
                        score=min(1.0, max(0.0, matched_sc or 0.85)),
                        method="image_embed" if is_meme else "embed",
                        status="pending",
                        detail="User confirmed original on submit",
                    )
                )
        except Exception:
            current_app.logger.exception("Failed to auto-enqueue user-confirmed duplicate pair")

    if joke.review_status == "approved":
        auto_vote = Vote(user_id=current_user.id, joke_id=joke.id, value=1)
        db.session.add(auto_vote)
        joke.score += 1
        current_user.reputation += 1
        flash("Joke submitted.", "success")
        redirect_target = url_for("main.joke_detail", joke_id=joke.id)
    else:
        flash("Submission received. It will appear after moderator approval.", "info")
        redirect_target = url_for("main.index")

    # Remove draft after successful publish; keep media that is now on the joke
    if draft is not None:
        if draft.image_filename and draft.image_filename != image_filename:
            _safe_unlink_upload(draft.image_filename, "UPLOAD_FOLDER")
        if draft.video_filename and draft.video_filename != video_filename:
            _safe_unlink_upload(draft.video_filename, "CLIPS_FOLDER")
        if draft.video_thumb and draft.video_thumb != video_thumb:
            _safe_unlink_upload(draft.video_thumb, "CLIP_THUMBS_FOLDER")
        db.session.delete(draft)

    db.session.commit()

    try:
        from .embed_outbox import enqueue_joke_upsert

        enqueue_joke_upsert(
            joke_id=joke.id,
            body=joke.body or "",
            is_meme=bool(joke.is_meme),
            is_clip=bool(joke.is_clip),
        )
    except Exception:
        current_app.logger.exception(
            "embed outbox enqueue failed for joke %s", joke.id
        )

    resp = redirect(redirect_target)
    resp.set_cookie(
        SUBMIT_TYPE_COOKIE,
        "",
        max_age=0,
        samesite="Lax",
        secure=True,
        httponly=False,
        path="/",
    )
    return resp


@main_bp.route("/submit/drafts/<int:draft_id>/delete", methods=["POST"])
@login_required
def delete_joke_draft(draft_id):
    draft = get_user_draft(draft_id, current_user.id)
    if not draft:
        flash("Draft not found.", "warning")
        return redirect(url_for("main.submit_joke"))
    discard_joke_draft(draft, delete_media=True)
    db.session.commit()
    flash("Draft deleted.", "success")
    return redirect(url_for("main.submit_joke"))

@main_bp.route("/jokes/<int:joke_id>/comment", methods=["POST"])
@login_required
def add_comment(joke_id):
    body = request.form.get("body", "").strip()
    body = maybe_sanitize_post_html(body, current_user)
    if not body:
        flash("Comment cannot be empty.", "danger")
        return redirect(url_for("main.joke_detail", joke_id=joke_id))

    quoted_comment_id = request.form.get("quoted_comment_id", type=int)
    quoted_comment = None
    if quoted_comment_id:
        quoted_comment = Comment.query.filter_by(id=quoted_comment_id, joke_id=joke_id).first()
    comment = Comment(body=body, user_id=current_user.id, joke_id=joke_id, quoted_comment_id=quoted_comment.id if quoted_comment else None)
    db.session.add(comment)
    db.session.commit()

    # Default: follow the post when commenting (checkbox on by default).
    # Unchecked checkbox is omitted from the form → no follow.
    want_follow = (request.form.get("follow") or "").strip() in ("1", "true", "yes", "on")
    if want_follow:
        try:
            from .follows import follow_joke

            follow_joke(current_user.id, joke_id)
        except Exception:
            current_app.logger.exception(
                "auto-follow after comment failed joke=%s", joke_id
            )

    return redirect(url_for("main.joke_detail", joke_id=joke_id))


@main_bp.route("/wankerboard")
@login_required
def wankerboard():
    lb_range = request.args.get("range", "24h")  # '24h', 'week', 'all'

    # Time filter
    now = datetime.utcnow()
    cutoff = None
    if lb_range == "24h":
        cutoff = now - timedelta(hours=24)
    elif lb_range == "week":
        cutoff = now - timedelta(days=7)

    # ------------------------------------------------------------
    # 1) Reputation leaderboard (net reputation earned in period)
    #    Rep is driven by Vote.value on the user's jokes
    # ------------------------------------------------------------
    rep_query = (
        db.session.query(
            User.username.label("username"),
            func.coalesce(func.sum(Vote.value), 0).label("rep_delta"),
        )
        .join(Joke, Joke.user_id == User.id)  # user is the joke author
        .join(Vote, Vote.joke_id == Joke.id)  # votes on their jokes
    )
    if cutoff:
        rep_query = rep_query.filter(Vote.created_at >= cutoff)

    rep_leaderboard = (
        rep_query.group_by(User.id, User.username)
        .having(func.coalesce(func.sum(Vote.value), 0) != 0)
        .order_by(func.sum(Vote.value).desc())
        .limit(50)
        .all()
    )

    # ------------------------------------------------------------
    # 2) Upvote leaderboard (upvotes CAST by user in period)
    # ------------------------------------------------------------
    upvote_query = (
        db.session.query(
            User.username.label("username"),
            func.count(Vote.id).label("upvote_count"),
        )
        .join(Vote, Vote.user_id == User.id)
        .filter(Vote.value > 0)  # +1 or +2 upvotes
    )
    if cutoff:
        upvote_query = upvote_query.filter(Vote.created_at >= cutoff)

    upvote_leaderboard = (
        upvote_query.group_by(User.id, User.username)
        .order_by(func.count(Vote.id).desc())
        .limit(50)
        .all()
    )

    # ------------------------------------------------------------
    # 3) Downvote leaderboard (downvotes CAST + upvotes CAST balance)
    # ------------------------------------------------------------
    downvote_sum = func.sum(case((Vote.value == -1, 1), else_=0))
    upvote_sum = func.sum(case((Vote.value > 0, 1), else_=0))

    downvote_query = db.session.query(
        User.username.label("username"),
        downvote_sum.label("downvote_count"),
        upvote_sum.label("upvote_count"),
    ).join(Vote, Vote.user_id == User.id)
    if cutoff:
        downvote_query = downvote_query.filter(Vote.created_at >= cutoff)

    downvote_leaderboard = (
        downvote_query.group_by(User.id, User.username)
        .having(downvote_sum > 0)
        .order_by(downvote_sum.desc())
        .limit(50)
        .all()
    )

    return render_template(
        "wankerboard.html",  # you can rename the file later if you want
        lb_range=lb_range,
        rep_leaderboard=rep_leaderboard,
        upvote_leaderboard=upvote_leaderboard,
        downvote_leaderboard=downvote_leaderboard,
    )


# Double-upvote (+2 hold) from this UK wall-clock time onward.
# Joke.created_at is stored as naive UTC (datetime.utcnow); compare in UTC.
# 24/07/2026 18:20 Europe/London → converted to UTC (BST = UTC+1 in July → 17:20 UTC).
_UK = ZoneInfo("Europe/London")
DOUBLE_VOTE_FROM_UK = datetime(2026, 7, 24, 18, 20, tzinfo=_UK)
DOUBLE_VOTE_FROM = DOUBLE_VOTE_FROM_UK.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def joke_allows_double_vote(joke: Joke) -> bool:
    """True if this joke may receive a +2 hold-upvote (posted on/after cutoff, UK time)."""
    created = getattr(joke, "created_at", None)
    if created is None:
        return False
    # Site stores naive UTC timestamps
    if created.tzinfo is not None:
        created = created.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    return created >= DOUBLE_VOTE_FROM


@main_bp.app_context_processor
def inject_vote_helpers():
    return {
        "joke_allows_double_vote": joke_allows_double_vote,
        "double_vote_from_uk_label": "24/07/2026 18:20 UK",
    }


def _joke_vote_sum(joke_id: int) -> int:
    total = (
        db.session.query(func.coalesce(func.sum(Vote.value), 0))
        .filter(Vote.joke_id == joke_id)
        .scalar()
    )
    return int(total or 0)


def _vote_json_response(joke: Joke):
    """Return authoritative score + current user's vote (recompute score from rows)."""
    score = _joke_vote_sum(joke.id)
    if joke.score != score:
        joke.score = score
        db.session.commit()
    current_vote = Vote.query.filter_by(
        user_id=current_user.id, joke_id=joke.id
    ).first()
    user_vote = current_vote.value if current_vote else 0
    tallies = joke_vote_tallies(joke.id)
    return jsonify({
        "score": score,
        "user_vote": user_vote,
        "double_vote": joke_allows_double_vote(joke),
        "tallies": tallies,
    })


@main_bp.route("/api/jokes/<int:joke_id>/voters", methods=["GET"])
@login_required
def joke_voters(joke_id):
    """
    Moderator/admin only: list who voted and how (lazy-loaded for the 🛂 card).
    Returns [{username, value}, ...] ordered super(+2), up(+1), down(-1), then name.
    """
    if not (
        current_user.is_admin or getattr(current_user, "is_moderator", False)
    ):
        return jsonify({"error": "Forbidden"}), 403

    Joke.query.get_or_404(joke_id)
    rows = (
        db.session.query(User.username, Vote.value)
        .join(Vote, Vote.user_id == User.id)
        .filter(Vote.joke_id == joke_id)
        .all()
    )

    def sort_key(row):
        username, value = row
        # +2 first, then +1, then -1; name as tiebreaker
        order = {2: 0, 1: 1, -1: 2}.get(value, 9)
        return (order, (username or "").lower())

    voters = [
        {"username": u, "value": int(v)}
        for u, v in sorted(rows, key=sort_key)
    ]
    return jsonify({"ok": True, "joke_id": joke_id, "voters": voters})


@main_bp.route("/api/jokes/<int:joke_id>/vote", methods=["POST"])
@login_required
def vote(joke_id):
    """
    Vote on a joke.
    - up + strength 1 (tap) or 2 (hold 2–4s, only on jokes from DOUBLE_VOTE_FROM)
    - down is always -1 (never -2)
    - authors keep the automatic +1 on post, but cannot vote again later
    """
    joke = Joke.query.get_or_404(joke_id)
    data = request.get_json(silent=True) or {}
    direction = data.get("direction")

    if direction not in ("up", "down"):
        return jsonify({"error": "Invalid vote"}), 400

    # Own jokes: auto +1 on submit only — no later up/down from the author
    if joke.user_id == current_user.id:
        return jsonify({"error": "You cannot vote on your own joke"}), 403

    if direction == "down" and current_user.is_banned():
        return jsonify({"error": "Banned users cannot downvote"}), 403

    # Users flagged for submission review (needs_moderator) cannot downvote
    if direction == "down" and current_user.needs_moderator:
        return jsonify({"error": "Accounts under review cannot downvote"}), 403

    # Resolve target value
    if direction == "down":
        value = -1  # downvotes are never stronger than -1
    else:
        try:
            strength = int(data.get("strength", 1))
        except (TypeError, ValueError):
            strength = 1
        if strength not in (1, 2):
            strength = 1
        # Grandfather older jokes: no +2 before DOUBLE_VOTE_FROM
        if strength == 2 and not joke_allows_double_vote(joke):
            strength = 1
        value = strength

    author = joke.author

    def apply_delta(delta: int) -> None:
        joke.score = (joke.score or 0) + delta
        if author is not None:
            author.reputation = (author.reputation or 0) + delta

    existing = Vote.query.filter_by(
        user_id=current_user.id, joke_id=joke.id
    ).first()

    try:
        if existing:
            old = existing.value
            if old == value:
                # same vote again → clear vote
                db.session.delete(existing)
                apply_delta(-old)
            else:
                # change strength or flip direction: net -old + value
                existing.value = value
                apply_delta(value - old)
        else:
            db.session.add(
                Vote(user_id=current_user.id, joke_id=joke.id, value=value)
            )
            apply_delta(value)
        db.session.commit()
    except (IntegrityError, StaleDataError):
        # Concurrent double-click / race: another request already changed the row
        db.session.rollback()
        joke = Joke.query.get_or_404(joke_id)
        return _vote_json_response(joke)

    return _vote_json_response(joke)


@main_bp.route("/forum")
def forum():
    threads = ForumThread.query.order_by(ForumThread.created_at.desc()).all()
    return render_template("forum.html", threads=threads)


@main_bp.route("/joke/comment/<int:comment_id>/react", methods=["POST"])
@login_required
def react_to_joke_comment(comment_id):
    data = request.get_json(silent=True) or {}
    reaction_type = data.get("reaction_type")

    if (
        reaction_type is not None
        and reaction_type != "none"
        and reaction_type not in REACTION_TYPES
    ):
        return jsonify({"error": "invalid_reaction"}), 400

    comment = Comment.query.get_or_404(comment_id)

    existing = JokeCommentReaction.query.filter_by(
        user_id=current_user.id,
        comment_id=comment.id,
    ).first()

    # Toggle behaviour:
    # - "none" or same reaction → delete
    # - different reaction → update
    if reaction_type == "none" or (
        existing and existing.reaction_type == reaction_type
    ):
        if existing:
            db.session.delete(existing)
            db.session.commit()
        user_reaction = None
    else:
        if not existing:
            existing = JokeCommentReaction(
                user_id=current_user.id,
                comment_id=comment.id,
                reaction_type=reaction_type,
            )
            db.session.add(existing)
        else:
            existing.reaction_type = reaction_type

        db.session.commit()
        user_reaction = existing.reaction_type

    # Recompute counts for this comment
    rows = (
        db.session.query(
            JokeCommentReaction.reaction_type,
            func.count(JokeCommentReaction.id),
        )
        .filter_by(comment_id=comment.id)
        .group_by(JokeCommentReaction.reaction_type)
        .all()
    )
    counts = {rtype: count for rtype, count in rows}

    return jsonify(
        {
            "comment_id": comment.id,
            "user_reaction": user_reaction,
            "counts": counts,
        }
    )


@main_bp.route("/joke/comment/<int:comment_id>/reaction-users/<reaction_type>")
@login_required
def joke_comment_reaction_users(comment_id, reaction_type):
    if reaction_type not in REACTION_TYPES:
        return jsonify({"error": "invalid_reaction"}), 400

    comment = Comment.query.get_or_404(comment_id)

    rows = (
        JokeCommentReaction.query.filter_by(
            comment_id=comment.id, reaction_type=reaction_type
        )
        .join(User)
        .with_entities(User.username)
        .order_by(User.username.asc())
        .all()
    )
    usernames = [r[0] for r in rows]

    return jsonify(
        {
            "comment_id": comment.id,
            "reaction_type": reaction_type,
            "usernames": usernames,
        }
    )


@main_bp.route("/forum/new", methods=["GET", "POST"])
@login_required
def new_thread():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        body = request.form.get("body", "").strip()
        body = maybe_sanitize_post_html(body, current_user)
        if not title or not body:
            flash("Title and body are required.", "danger")
            return render_template("forum_new.html")

        thread = ForumThread(
            title=title,
            body=body,
            user_id=current_user.id,
        )
        db.session.add(thread)
        db.session.commit()
        return redirect(url_for("main.thread_detail", thread_id=thread.id))

    return render_template("forum_new.html")


@main_bp.route("/forum/<int:thread_id>")
def thread_detail(thread_id):
    thread = ForumThread.query.get_or_404(thread_id)
    replies = (
        ForumReply.query.filter_by(thread_id=thread.id)
        .order_by(ForumReply.created_at.asc())
        .all()
    )

    from sqlalchemy import func
    from app.models import ForumReaction, REACTION_TYPES

    # replies = thread.replies.order_by(...).all()

    reply_ids = [r.id for r in replies]
    reaction_summary = {}
    user_reactions = {}

    if reply_ids:
        # counts per reply + per reaction_type
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

    return render_template(
        "forum_thread.html",
        thread=thread,
        replies=replies,
        reaction_types=REACTION_TYPES,
        reaction_summary=reaction_summary,
        user_reactions=user_reactions,
    )


@main_bp.route("/forum/<int:thread_id>/reply", methods=["POST"])
@login_required
def add_reply(thread_id):
    thread = ForumThread.query.get_or_404(thread_id)
    body = request.form.get("body", "").strip()
    body = maybe_sanitize_post_html(body, current_user)
    if not body:
        flash("Reply cannot be empty.", "danger")
        return redirect(url_for("main.thread_detail", thread_id=thread.id))

    reply = ForumReply(
        body=body,
        user_id=current_user.id,
        thread_id=thread.id,
    )
    db.session.add(reply)
    db.session.commit()
    return redirect(url_for("main.thread_detail", thread_id=thread.id))


def seo_text_snippet(raw: str, limit: int = 160) -> str:
    """Plain-text snippet for meta description / titles (strip tags, collapse space)."""
    import re

    text = (raw or "").replace("\r", " ").replace("\n", " ").replace("\u00a0", " ")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0]
    return (cut or text[: limit - 1]).rstrip(".,;:") + "…"


@main_bp.route("/sitemap.xml")
def sitemap():
    urls = []
    today = datetime.utcnow().date().isoformat()

    # Core landing pages (clean, indexable)
    for endpoint in (
        "main.index",
        "main.memes",
        "main.clips",
        "forum.index",
        "main.wankerboard",
    ):
        try:
            urls.append({"loc": url_for(endpoint, _external=True), "lastmod": today})
        except Exception:
            pass

    # Approved, non-quarantined joke detail pages only
    for joke_id, created_at in (
        Joke.query.with_entities(Joke.id, Joke.created_at)
        .filter(
            Joke.review_status == "approved",
            Joke.is_quarantined.is_(False),
        )
        .order_by(Joke.id.asc())
        .all()
    ):
        urls.append(
            {
                "loc": url_for("main.joke_detail", joke_id=joke_id, _external=True),
                "lastmod": created_at.date().isoformat() if created_at else today,
            }
        )

    xml = render_template("sitemap.xml", urls=urls)
    return Response(xml, mimetype="application/xml")


@main_bp.route("/messages")
@login_required
def inbox():
    # Subquery: for each sender -> latest message *to you* that isn't deleted
    subq = (
        db.session.query(func.max(Message.id).label("last_id"))
        .filter(
            Message.recipient_id == current_user.id,
            Message.deleted_by_recipient.is_(False),
        )
        .group_by(Message.sender_id)
        .subquery()
    )

    # Get those latest messages as real Message objects
    messages = (
        Message.query.join(subq, Message.id == subq.c.last_id)
        .order_by(Message.created_at.desc())
        .all()
    )

    return render_template("messages_inbox.html", messages=messages)


@main_bp.route("/messages/sent")
@login_required
def sent_messages():
    messages = (
        Message.query.filter_by(sender_id=current_user.id, deleted_by_sender=False)
        .order_by(Message.created_at.desc())
        .all()
    )
    return render_template("messages_sent.html", messages=messages)


@main_bp.route("/messages/<int:message_id>", methods=["GET", "POST"])
@login_required
def view_message(message_id):
    # Base message to identify the conversation
    msg = Message.query.get_or_404(message_id)

    # Only participants can see it
    if current_user.id not in (msg.sender_id, msg.recipient_id):
        abort(403)

    # Who is the other user?
    other_user = msg.sender if msg.sender_id != current_user.id else msg.recipient

    # Mark all messages from them to you as read
    unread_from_other = Message.query.filter(
        Message.sender_id == other_user.id,
        Message.recipient_id == current_user.id,
        Message.read_at.is_(None),
    ).all()
    for m in unread_from_other:
        m.read_at = datetime.utcnow()
    db.session.commit()

    # Handle reply
    if request.method == "POST":
        body = (request.form.get("body") or "").strip()
        subject = (request.form.get("subject") or "").strip()

        if not body:
            flash("Message cannot be empty.", "danger")
            return redirect(url_for("main.view_message", message_id=message_id))

        # Reuse subject if blank
        if not subject:
            subject = msg.subject or None

        reply = Message(
            sender_id=current_user.id,
            recipient_id=other_user.id,
            subject=subject,
            body=body,
        )
        db.session.add(reply)
        db.session.commit()

        # Jump into the thread using the new message id
        return redirect(url_for("main.view_message", message_id=reply.id))

    # Full thread between you and other_user, newest at top
    # Full thread between you and other_user, newest at top
    thread = (
        Message.query.filter(
            or_(
                and_(
                    Message.sender_id == current_user.id,
                    Message.recipient_id == other_user.id,
                    Message.deleted_by_sender.is_(False),
                ),
                and_(
                    Message.sender_id == other_user.id,
                    Message.recipient_id == current_user.id,
                    Message.deleted_by_recipient.is_(False),
                ),
            )
        )
        .order_by(Message.created_at.desc())  # newest first
        .all()
    )

    # Is this user currently blocked by you?
    existing_block = MessageBlock.query.filter_by(
        blocker_id=current_user.id,
        blocked_id=other_user.id,
    ).first()
    is_blocked = existing_block is not None

    return render_template(
        "message_view.html",
        thread=thread,
        other_user=other_user,
        is_blocked=is_blocked,
    )


@main_bp.route("/messages/bulk_delete", methods=["POST"])
@login_required
def bulk_delete_messages():
    ids = request.form.getlist("message_ids")
    if not ids:
        flash("No messages selected.", "warning")
        return redirect(url_for("main.inbox"))

    msgs = Message.query.filter(Message.id.in_(ids)).all()
    for m in msgs:
        if m.recipient_id == current_user.id:
            m.deleted_by_recipient = True
        if m.sender_id == current_user.id:
            m.deleted_by_sender = True

    db.session.commit()
    flash("Messages deleted.", "success")
    return redirect(request.referrer or url_for("main.inbox"))


@main_bp.route("/messages/<int:message_id>/delete", methods=["POST"])
@login_required
def delete_message(message_id):
    msg = Message.query.get_or_404(message_id)

    # Only participants can delete
    if current_user.id not in (msg.sender_id, msg.recipient_id):
        abort(403)

    changed = False
    if msg.sender_id == current_user.id and not msg.deleted_by_sender:
        msg.deleted_by_sender = True
        changed = True
    if msg.recipient_id == current_user.id and not msg.deleted_by_recipient:
        msg.deleted_by_recipient = True
        changed = True

    if changed:
        db.session.commit()

    flash("Message deleted.", "success")
    return redirect(request.referrer or url_for("main.inbox"))


@main_bp.route("/messages/thread/<int:user_id>/delete", methods=["POST"])
@login_required
def delete_thread(user_id):
    """
    Soft-delete the whole conversation *for the current user only*.
    The other person still sees their copy until they delete it.
    """
    # Messages you sent -> mark deleted_by_sender
    Message.query.filter(
        Message.sender_id == current_user.id,
        Message.recipient_id == user_id,
        Message.deleted_by_sender.is_(False),
    ).update({Message.deleted_by_sender: True}, synchronize_session=False)

    # Messages you received -> mark deleted_by_recipient
    Message.query.filter(
        Message.sender_id == user_id,
        Message.recipient_id == current_user.id,
        Message.deleted_by_recipient.is_(False),
    ).update({Message.deleted_by_recipient: True}, synchronize_session=False)

    db.session.commit()
    flash("Conversation deleted.", "success")
    return redirect(url_for("main.inbox"))


@main_bp.route("/user/<username>/message", methods=["GET", "POST"])
@login_required
def send_message(username):
    recipient = User.query.filter_by(username=username).first_or_404()

    if recipient.id == current_user.id:
        flash("You can't message yourself.", "warning")
        return redirect(url_for("main.user_profile", username=recipient.username))

    # Check if they have blocked you
    blocked = MessageBlock.query.filter_by(
        blocker_id=recipient.id,
        blocked_id=current_user.id,
    ).first()
    if blocked:
        flash("This user is not accepting messages from you.", "danger")
        return redirect(url_for("main.user_profile", username=recipient.username))

    if request.method == "POST":
        subject = request.form.get("subject", "").strip() or None
        body = request.form.get("body", "").strip()
        body = maybe_sanitize_post_html(body, current_user)
        if not body:
            flash("Message body is required.", "danger")
        else:
            msg = Message(
                sender_id=current_user.id,
                recipient_id=recipient.id,
                subject=subject,
                body=body,
            )
            db.session.add(msg)
            db.session.commit()
            flash("Message sent.", "success")
            return redirect(url_for("main.inbox"))

    return render_template("message_compose.html", recipient=recipient)


@main_bp.route("/account/password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        current_pw = request.form.get("current_password", "")
        new_pw = request.form.get("new_password", "")
        confirm_pw = request.form.get("confirm_password", "")

        if not current_user.check_password(current_pw):
            flash("Current password is incorrect.", "danger")
        elif not new_pw:
            flash("New password cannot be empty.", "danger")
        elif new_pw != confirm_pw:
            flash("New passwords do not match.", "danger")
        else:
            current_user.set_password(new_pw)
            db.session.commit()
            flash("Password updated.", "success")
            return redirect(url_for("main.edit_profile"))
    return render_template("change_password.html")


@main_bp.route("/account/comments")
@login_required
def user_view_comments():
    """
    Current user's own posts that have at least one comment.
    Sort: newest|oldest (by post date) or most|least (by comment count).
    Optional type filter: all|joke|meme|clip
    """
    sort = (request.args.get("sort") or "newest").strip().lower()
    if sort not in ("newest", "oldest", "most", "least"):
        sort = "newest"
    type_filter = (request.args.get("type") or "all").strip().lower()
    if type_filter not in ("all", "joke", "meme", "clip"):
        type_filter = "all"

    cc = (
        db.session.query(
            Comment.joke_id.label("joke_id"),
            func.count(Comment.id).label("comment_count"),
            func.max(Comment.created_at).label("last_comment_at"),
        )
        .group_by(Comment.joke_id)
        .having(func.count(Comment.id) > 0)
        .subquery()
    )

    q = (
        db.session.query(
            Joke,
            cc.c.comment_count,
            cc.c.last_comment_at,
        )
        .join(cc, Joke.id == cc.c.joke_id)
        .filter(Joke.user_id == current_user.id)
    )

    if type_filter == "meme":
        q = q.filter(Joke.is_meme.is_(True))
    elif type_filter == "clip":
        q = q.filter(Joke.is_clip.is_(True))
    elif type_filter == "joke":
        q = q.filter(Joke.is_meme.is_(False), Joke.is_clip.is_(False))

    if sort == "oldest":
        q = q.order_by(Joke.created_at.asc(), Joke.id.asc())
    elif sort == "most":
        q = q.order_by(cc.c.comment_count.desc(), Joke.created_at.desc())
    elif sort == "least":
        q = q.order_by(cc.c.comment_count.asc(), Joke.created_at.desc())
    else:  # newest
        q = q.order_by(Joke.created_at.desc(), Joke.id.desc())

    rows = q.limit(500).all()
    joke_ids = [joke.id for joke, _, _ in rows]
    try:
        from .comment_reads import unseen_comment_joke_ids

        unseen_ids = unseen_comment_joke_ids(current_user.id, joke_ids)
    except Exception:
        current_app.logger.exception("unseen_comment_joke_ids failed")
        unseen_ids = set()

    items = [
        {
            "joke": joke,
            "comment_count": int(count or 0),
            "last_comment_at": last_at,
            "has_unseen": joke.id in unseen_ids,
        }
        for joke, count, last_at in rows
    ]

    return render_template(
        "user_view_comments.html",
        items=items,
        sort=sort,
        type_filter=type_filter,
        has_any_unseen=bool(unseen_ids),
        noindex=True,
    )


@main_bp.route("/account/comments/mark-all-seen", methods=["POST"])
@login_required
def user_mark_all_comments_seen():
    """Clear unseen-comment glow for all of the current user's posts."""
    try:
        from .comment_reads import mark_all_joke_comments_seen

        n = mark_all_joke_comments_seen(current_user.id)
        if n:
            flash("All comments marked as seen.", "success")
        else:
            flash("Nothing to mark as seen.", "info")
    except Exception:
        current_app.logger.exception("mark_all_joke_comments_seen failed")
        flash("Could not mark comments as seen. Try again.", "danger")
    return redirect(url_for("main.user_view_comments"))


@main_bp.route("/jokes/<int:joke_id>/follow", methods=["POST"])
@login_required
def follow_joke_route(joke_id):
    joke = Joke.query.get_or_404(joke_id)
    if joke.review_status != "approved" or joke.is_quarantined:
        abort(404)
    try:
        from .follows import follow_joke

        follow_joke(current_user.id, joke.id)
        flash("Following this post — you’ll get a cyan alert when it gets new comments.", "success")
    except Exception:
        current_app.logger.exception("follow_joke failed joke=%s", joke_id)
        flash("Could not follow this post.", "danger")
    return redirect(url_for("main.joke_detail", joke_id=joke.id))


@main_bp.route("/jokes/<int:joke_id>/unfollow", methods=["POST"])
@login_required
def unfollow_joke_route(joke_id):
    joke = Joke.query.get_or_404(joke_id)
    try:
        from .follows import unfollow_joke

        unfollow_joke(current_user.id, joke.id)
        flash("Unfollowed.", "info")
    except Exception:
        current_app.logger.exception("unfollow_joke failed joke=%s", joke_id)
        flash("Could not unfollow this post.", "danger")
    return redirect(url_for("main.joke_detail", joke_id=joke.id))


@main_bp.route("/account/followed")
@login_required
def user_followed_jokes():
    from .follows import list_followed_jokes

    items = list_followed_jokes(current_user.id)
    has_any_unseen = any(i.get("has_unseen") for i in items)
    return render_template(
        "followed_jokes.html",
        items=items,
        has_any_unseen=has_any_unseen,
        noindex=True,
    )


@main_bp.route("/account/followed/mark-all-seen", methods=["POST"])
@login_required
def user_mark_all_followed_seen():
    try:
        from .follows import mark_all_followed_seen

        n = mark_all_followed_seen(current_user.id)
        if n:
            flash("All followed posts marked as seen.", "success")
        else:
            flash("You are not following any posts.", "info")
    except Exception:
        current_app.logger.exception("mark_all_followed_seen failed")
        flash("Could not mark followed posts as seen.", "danger")
    return redirect(url_for("main.user_followed_jokes"))


@main_bp.route("/account/followed/unfollow", methods=["POST"])
@login_required
def user_unfollow_selected():
    """Bulk unfollow from Followed Jokes page (selected checkboxes)."""
    raw = request.form.getlist("joke_ids")
    joke_ids = []
    for x in raw:
        try:
            joke_ids.append(int(x))
        except (TypeError, ValueError):
            continue
    try:
        from .follows import unfollow_jokes

        n = unfollow_jokes(current_user.id, joke_ids)
        if n:
            flash(f"Unfollowed {n} post{'s' if n != 1 else ''}.", "success")
        else:
            flash("No followed posts selected.", "info")
    except Exception:
        current_app.logger.exception("user_unfollow_selected failed")
        flash("Could not unfollow selected posts.", "danger")
    return redirect(url_for("main.user_followed_jokes"))


@main_bp.route("/messages/block/<int:user_id>", methods=["POST"])
@login_required
def block_user(user_id):
    if user_id == current_user.id:
        flash("Blocking yourself is just called being online.", "warning")
        return redirect(url_for("main.inbox"))

    existing = MessageBlock.query.filter_by(
        blocker_id=current_user.id,
        blocked_id=user_id,
    ).first()

    if not existing:
        block = MessageBlock(blocker_id=current_user.id, blocked_id=user_id)
        db.session.add(block)
        db.session.commit()

    flash("User blocked from messaging you.", "success")
    return redirect(request.referrer or url_for("main.inbox"))


@main_bp.route("/moderation/ban24/<int:user_id>", methods=["POST"])
@moderator_required
def moderator_ban_24h(user_id):
    user = User.query.get_or_404(user_id)

    if user.id == current_user.id:
        flash("You can't ban yourself.", "warning")
        return redirect(
            request.referrer or url_for("main.user_profile", username=user.username)
        )

    if user.is_admin:
        flash("You can't ban an admin.", "danger")
        return redirect(
            request.referrer or url_for("main.user_profile", username=user.username)
        )

    user.ban_until = datetime.utcnow() + timedelta(hours=24)

    note = AdminNotification(
        action="24h ban",
        message=f"{current_user.username} banned {user.username} for 24 hours.",
        performed_by_id=current_user.id,
        target_user_id=user.id,
    )
    db.session.add(note)
    db.session.commit()
    flash("User banned for 24 hours.", "success")
    return redirect(
        request.referrer or url_for("main.user_profile", username=user.username)
    )

@main_bp.route("/moderation/jokes/<int:joke_id>/toggle-author-review", methods=["POST"])
@moderator_required
def toggle_joke_author_review(joke_id):
    joke = Joke.query.get_or_404(joke_id)
    author = joke.author
    if not author:
        flash("This joke has no valid author account.", "danger")
        return redirect(url_for("main.joke_detail", joke_id=joke.id))
    if author.is_admin or author.is_moderator:
        flash("Admin and moderator accounts cannot be put into submission review from here.", "info")
        return redirect(url_for("main.joke_detail", joke_id=joke.id))
    author.needs_moderator = not bool(author.needs_moderator)
    note = AdminNotification(
        action="Toggle user moderation",
        message=f"{current_user.username} set needs_moderator={author.needs_moderator} for {author.username} from joke #{joke.id}.",
        performed_by_id=current_user.id,
        target_user_id=author.id,
        target_joke_id=joke.id,
    )
    db.session.add(note)
    db.session.commit()
    if author.needs_moderator:
        flash(f"{author.username} is now questionable. All future posts will need moderator approval.", "warning")
    else:
        flash(f"{author.username} has been marked as safe. Future posts will go live immediately.", "success")
    return redirect(url_for("main.joke_detail", joke_id=joke.id))

@main_bp.route("/moderation/jokes/<int:joke_id>/quarantine", methods=["POST"])
@moderator_required
def quarantine_joke(joke_id):
    from .models import QUARANTINE_REASONS

    joke = Joke.query.get_or_404(joke_id)
    # Already quarantined?
    if joke.is_quarantined:
        flash("This joke is already quarantined.", "info")
        return redirect(url_for("main.joke_detail", joke_id=joke.id))
    # Has been restored/locked by an admin? Mods can't quarantine again.
    if joke.quarantine_locked and not current_user.is_admin:
        flash(
            "This joke has already been reviewed by an admin and can't be quarantined again.",
            "info",
        )
        return redirect(url_for("main.joke_detail", joke_id=joke.id))

    # Mandatory reason(s)
    allowed = set(QUARANTINE_REASONS)
    picked = [
        r.strip()
        for r in request.form.getlist("reasons")
        if r and r.strip() in allowed
    ]
    if not picked:
        flash("Choose at least one quarantine reason.", "danger")
        return redirect(url_for("main.joke_detail", joke_id=joke.id))
    reasons_str = ",".join(picked)

    # Create or update quarantine record
    q = getattr(joke, "quarantine_record", None)
    if not q:
        q = QuarantinedJoke(
            joke_id=joke.id,
            original_title=joke.title,
            original_body=joke.body,
            original_category_id=joke.category_id,
            quarantined_by_id=current_user.id,
            reasons=reasons_str,
        )
        db.session.add(q)
    else:
        q.original_title = joke.title
        q.original_body = joke.body
        q.original_category_id = joke.category_id
        q.quarantined_by_id = current_user.id
        q.reasons = reasons_str
        q.restored_at = None
        q.permanently_deleted = False

    joke.is_quarantined = True
    joke.quarantined_at = datetime.utcnow()
    joke.quarantined_by_id = current_user.id
    note = AdminNotification(
        action="Quarantine joke",
        message=(
            f"{current_user.username} quarantined joke #{joke.id} by "
            f"{joke.author.username}. Reasons: {reasons_str}"
        ),
        performed_by_id=current_user.id,
        target_user_id=joke.user_id,
        target_joke_id=joke.id,
    )
    db.session.add(note)
    db.session.commit()
    flash(
        f"Joke quarantined for admin review. Reasons: {reasons_str}",
        "warning",
    )
    return redirect(url_for("main.index"))

@main_bp.route("/moderation/review")
@moderator_required
def moderation_review():
    items = Joke.query.filter_by(review_status="pending").order_by(Joke.created_at.asc()).all()
    return render_template("moderation_review.html", items=items)


@main_bp.route("/moderation/categories", methods=["GET", "POST"])
@moderator_required
def moderation_categories():
    """
    Moderators suggest add/remove of categories and subcategories.
    Admins approve on Suggested Category Mods.
    """
    from .models import CategoryModSuggestion
    import re

    def _slug_preview(value: str) -> str:
        s = (value or "").strip().lower()
        s = re.sub(r"[^a-z0-9]+", "", s)
        return (s or "item")[:60]

    categories = Category.query.order_by(Category.name.asc()).all()
    subcategories_by_category = subcategories_by_category_map(categories)

    my_pending = (
        CategoryModSuggestion.query.filter_by(
            suggested_by_id=current_user.id, status="pending"
        )
        .order_by(CategoryModSuggestion.created_at.desc())
        .limit(30)
        .all()
    )

    if request.method == "POST":
        form = (request.form.get("form") or "").strip()
        note = (request.form.get("note") or "").strip()[:255] or None

        if form == "add":
            parent_raw = (request.form.get("parent_category_id") or "").strip()
            name = (request.form.get("proposed_name") or "").strip()
            if not name:
                flash("Enter a name for the suggested category/subcategory.", "danger")
                return redirect(url_for("main.moderation_categories"))
            if len(name) > 120:
                flash("Name is too long (max 120).", "danger")
                return redirect(url_for("main.moderation_categories"))

            if parent_raw in ("", "new", "0"):
                # New top-level category
                if Category.query.filter(
                    func.lower(Category.name) == name.lower()
                ).first():
                    flash(f"Category “{name}” already exists.", "warning")
                    return redirect(url_for("main.moderation_categories"))
                sug = CategoryModSuggestion(
                    suggested_by_id=current_user.id,
                    suggested_by_username=current_user.username,
                    action="add",
                    target_kind="category",
                    proposed_name=name,
                    note=note,
                    status="pending",
                )
            else:
                try:
                    parent_id = int(parent_raw)
                except (TypeError, ValueError):
                    flash("Invalid category selection.", "danger")
                    return redirect(url_for("main.moderation_categories"))
                parent = Category.query.get(parent_id)
                if not parent:
                    flash("Selected category not found.", "danger")
                    return redirect(url_for("main.moderation_categories"))
                if Subcategory.query.filter(
                    Subcategory.category_id == parent.id,
                    func.lower(Subcategory.name) == name.lower(),
                ).first():
                    flash(
                        f"Subcategory “{name}” already exists under {parent.name}.",
                        "warning",
                    )
                    return redirect(url_for("main.moderation_categories"))
                sug = CategoryModSuggestion(
                    suggested_by_id=current_user.id,
                    suggested_by_username=current_user.username,
                    action="add",
                    target_kind="subcategory",
                    proposed_name=name,
                    parent_category_id=parent.id,
                    parent_category_name=parent.name,
                    note=note,
                    status="pending",
                )
            db.session.add(sug)
            db.session.commit()
            flash(
                "Suggestion submitted for admin approval.",
                "success",
            )
            return redirect(url_for("main.moderation_categories"))

        if form == "remove":
            cat_raw = (request.form.get("remove_category_id") or "").strip()
            sub_raw = (request.form.get("remove_subcategory_id") or "").strip()
            try:
                cat_id = int(cat_raw) if cat_raw else None
            except (TypeError, ValueError):
                cat_id = None
            if not cat_id:
                flash("Select a category to remove (or remove a subcategory under it).", "danger")
                return redirect(url_for("main.moderation_categories"))
            cat = Category.query.get(cat_id)
            if not cat:
                flash("Category not found.", "danger")
                return redirect(url_for("main.moderation_categories"))

            # "main" or empty → remove whole category
            if sub_raw in ("", "main", "0"):
                sug = CategoryModSuggestion(
                    suggested_by_id=current_user.id,
                    suggested_by_username=current_user.username,
                    action="remove",
                    target_kind="category",
                    category_id=cat.id,
                    category_name=cat.name,
                    note=note,
                    status="pending",
                )
            else:
                try:
                    sub_id = int(sub_raw)
                except (TypeError, ValueError):
                    flash("Invalid subcategory selection.", "danger")
                    return redirect(url_for("main.moderation_categories"))
                sub = Subcategory.query.get(sub_id)
                if not sub or int(sub.category_id) != int(cat.id):
                    flash("Subcategory not found under that category.", "danger")
                    return redirect(url_for("main.moderation_categories"))
                sug = CategoryModSuggestion(
                    suggested_by_id=current_user.id,
                    suggested_by_username=current_user.username,
                    action="remove",
                    target_kind="subcategory",
                    category_id=cat.id,
                    category_name=cat.name,
                    subcategory_id=sub.id,
                    subcategory_name=sub.name,
                    note=note,
                    status="pending",
                )
            db.session.add(sug)
            db.session.commit()
            flash("Removal suggestion submitted for admin approval.", "success")
            return redirect(url_for("main.moderation_categories"))

        flash("Unknown form.", "danger")
        return redirect(url_for("main.moderation_categories"))

    return render_template(
        "moderation_categories.html",
        categories=categories,
        subcategories_by_category=subcategories_by_category,
        my_pending=my_pending,
    )


@main_bp.route("/moderation/duplicates", methods=["GET", "POST"])
@moderator_required
def moderation_duplicates():
    """Moderator queue: near-duplicate pairs + de-duplication action log."""
    from .models import JokeDupePair, DupeModerationLog

    # Admin-only: delete selected log lines
    if request.method == "POST" and request.form.get("form") == "dupe_log_delete":
        if not current_user.is_admin:
            abort(403)
        raw_ids = request.form.getlist("log_ids")
        ids = []
        for x in raw_ids:
            try:
                ids.append(int(x))
            except (TypeError, ValueError):
                continue
        if not ids:
            flash("No log entries selected.", "info")
        else:
            n = DupeModerationLog.query.filter(
                DupeModerationLog.id.in_(ids)
            ).delete(synchronize_session=False)
            db.session.commit()
            flash(f"Deleted {n} log entr{'y' if n == 1 else 'ies'}.", "success")
        return redirect(url_for("main.moderation_duplicates") + "#dupe-log")

    # Confidence band for the queue (score is 0..1 from scanner)
    # high  >= 0.90  (default)  |  medium >= 0.75 (includes high)
    conf = (request.args.get("conf") or "high").strip().lower()
    if conf not in ("high", "medium"):
        conf = "high"
    min_score = 0.90 if conf == "high" else 0.75

    pairs_q = (
        JokeDupePair.query.filter_by(status="pending")
        .filter(JokeDupePair.score >= min_score)
        .order_by(JokeDupePair.score.desc(), JokeDupePair.created_at.asc())
    )
    pairs = pairs_q.limit(100).all()
    # Counts for the radio labels (pending only)
    pending_base = JokeDupePair.query.filter_by(status="pending")
    count_high = pending_base.filter(JokeDupePair.score >= 0.90).count()
    count_medium = pending_base.filter(JokeDupePair.score >= 0.75).count()

    # Preload jokes
    ids = set()
    for p in pairs:
        ids.add(p.joke_older_id)
        ids.add(p.joke_newer_id)
    jokes = {j.id: j for j in Joke.query.filter(Joke.id.in_(ids)).all()} if ids else {}

    log_entries = (
        DupeModerationLog.query.order_by(DupeModerationLog.created_at.desc())
        .limit(500)
        .all()
    )
    return render_template(
        "moderation_duplicates.html",
        pairs=pairs,
        jokes=jokes,
        log_entries=log_entries,
        conf=conf,
        min_score=min_score,
        count_high=count_high,
        count_medium=count_medium,
    )


@main_bp.route("/moderation/duplicates/<int:pair_id>/<action>", methods=["POST"])
@moderator_required
def moderation_duplicates_action(pair_id, action):
    from .duplicates import resolve_dupe_pair
    from .models import JokeDupePair

    pair = JokeDupePair.query.get_or_404(pair_id)
    if action not in ("delete_newer", "not_dupe"):
        flash("Unknown action.", "danger")
        return redirect(url_for("main.moderation_duplicates"))
    msg = resolve_dupe_pair(pair, action=action, actor=current_user)
    flash(msg, "success")
    conf = (request.args.get("conf") or request.form.get("conf") or "high").strip()
    if conf not in ("high", "medium"):
        conf = "high"
    return redirect(url_for("main.moderation_duplicates", conf=conf))


@main_bp.route("/appeals/duplicate/<int:archived_id>", methods=["GET", "POST"])
@login_required
def appeal_duplicate(archived_id):
    """User appeal form for a joke archived as a duplicate."""
    from .models import ArchivedJoke, DuplicateAppeal

    archived = ArchivedJoke.query.get_or_404(archived_id)
    if archived.user_id != current_user.id and not current_user.is_admin:
        abort(403)
    if archived.archive_reason != "duplicate":
        flash("This archive entry is not a duplicate removal.", "warning")
        return redirect(url_for("main.inbox"))

    existing = (
        DuplicateAppeal.query.filter_by(archived_joke_id=archived.id)
        .order_by(DuplicateAppeal.created_at.desc())
        .first()
    )

    if getattr(current_user, "duplicate_appeals_banned", False):
        flash(
            "You are not allowed to submit duplicate appeals. Contact an admin if needed.",
            "danger",
        )
        return render_template(
            "appeal_duplicate.html",
            archived=archived,
            kept=Joke.query.get(archived.related_kept_joke_id)
            if archived.related_kept_joke_id
            else None,
            existing=existing,
            banned=True,
        )

    if request.method == "POST":
        if existing and existing.status == "pending":
            flash("You already have a pending appeal for this joke.", "info")
            return redirect(url_for("main.appeal_duplicate", archived_id=archived.id))
        if existing and existing.status == "upheld":
            flash("This joke was already restored.", "info")
            return redirect(url_for("main.index"))
        if existing and existing.status == "rejected":
            flash(
                "Your previous appeal was rejected. Contact an admin if you need a review.",
                "warning",
            )
            return redirect(url_for("main.inbox"))

        note = (request.form.get("body") or "").strip()
        if len(note) > 2000:
            note = note[:2000]
        appeal = DuplicateAppeal(
            archived_joke_id=archived.id,
            original_joke_id=archived.original_joke_id,
            user_id=current_user.id,
            kept_joke_id=archived.related_kept_joke_id,
            body=note or None,
            status="pending",
        )
        db.session.add(appeal)
        db.session.add(
            AdminNotification(
                action="Duplicate appeal",
                message=(
                    f"{current_user.username} appealed duplicate removal of "
                    f"joke #{archived.original_joke_id} (archive #{archived.id})."
                ),
                performed_by_id=current_user.id,
                target_user_id=current_user.id,
                target_joke_id=None,
            )
        )
        db.session.commit()
        flash("Appeal submitted. An admin will review it.", "success")
        return redirect(url_for("main.inbox"))

    kept = (
        Joke.query.get(archived.related_kept_joke_id)
        if archived.related_kept_joke_id
        else None
    )
    return render_template(
        "appeal_duplicate.html",
        archived=archived,
        kept=kept,
        existing=existing,
        banned=False,
    )


@main_bp.route("/api/internal/dupe-pairs", methods=["POST"])
def api_internal_dupe_pairs():
    """
    X4 worker ingest: POST JSON { "pairs": [ {older_id, newer_id, score, method, detail}, ... ] }
    Auth: X-BFH-Uploads-Secret (same as upload finalize) or X-BFH-Dupe-Secret.
    """
    expected = (
        (current_app.config.get("DUPE_WORKER_SECRET") or "").strip()
        or (current_app.config.get("UPLOADS_SHARED_SECRET") or "").strip()
    )
    if not expected:
        return jsonify({"ok": False, "error": "Worker secret not configured"}), 500
    got = (
        (request.headers.get("X-BFH-Dupe-Secret") or "").strip()
        or (request.headers.get("X-BFH-Uploads-Secret") or "").strip()
    )
    if got != expected:
        return jsonify({"ok": False, "error": "Forbidden"}), 403

    data = request.get_json(silent=True) or {}
    pairs = data.get("pairs") or []
    if not isinstance(pairs, list):
        return jsonify({"ok": False, "error": "pairs must be a list"}), 400
    if len(pairs) > 500:
        return jsonify({"ok": False, "error": "max 500 pairs per request"}), 400

    from .duplicates import ingest_dupe_pairs

    result = ingest_dupe_pairs(pairs)
    return jsonify({"ok": True, **result})
@main_bp.route("/moderation/review/<int:joke_id>/approve", methods=["POST"])
@moderator_required
def moderation_approve(joke_id):
    joke = Joke.query.get_or_404(joke_id)
    if joke.review_status != "pending":
        flash("That item is not pending review.", "info")
        return redirect(url_for("main.moderation_review"))
    joke.review_status = "approved"
    joke.reviewed_at = datetime.utcnow()
    joke.reviewed_by_id = current_user.id
    existing_vote = Vote.query.filter_by(user_id=joke.user_id, joke_id=joke.id).first()
    if not existing_vote:
        db.session.add(Vote(user_id=joke.user_id, joke_id=joke.id, value=1))
        joke.score += 1
        joke.author.reputation += 1
    db.session.commit()
    flash("Submission approved.", "success")
    return redirect(url_for("main.moderation_review"))
@main_bp.route("/moderation/review/<int:joke_id>/reject", methods=["POST"])
@moderator_required
def moderation_reject(joke_id):
    joke = Joke.query.get_or_404(joke_id)
    if joke.review_status != "pending":
        flash("That item is not pending review.", "info")
        return redirect(url_for("main.moderation_review"))
    allowed = ["Rubbish", "Illegal", "Repeat Post", "Wrong Category", "Not A Joke", "Low Effort", "Shitty Bandwagon", "Wasp-Like", "Spam"]
    reasons = [r for r in request.form.getlist("reasons") if r in allowed]
    if not reasons:
        flash("Select at least one rejection reason.", "danger")
        return redirect(url_for("main.moderation_review"))
    moderator = User.query.filter_by(username="Moderator").first()
    if not moderator:
        moderator = User(username="Moderator", email="moderator@bannedfromheaven.local", needs_moderator=False)
        moderator.set_password(secrets.token_urlsafe(32))
        db.session.add(moderator)
        db.session.flush()
    if joke.is_meme:
        category_label = "Meme"
    elif joke.is_clip:
        category_label = "Clip"
    else:
        category_label = "Joke"
    body = f"""Apologies, your joke has been rejected for the following reason(s):
{", ".join(reasons)}
{category_label}
{joke.body or ""}"""
    msg = Message(sender_id=moderator.id, recipient_id=joke.user_id, subject="Submission rejected", body=body)
    joke.review_status = "rejected"
    joke.reviewed_at = datetime.utcnow()
    joke.reviewed_by_id = current_user.id
    joke.reject_reasons = ", ".join(reasons)
    db.session.add(msg)
    db.session.commit()
    flash("Submission rejected and user notified.", "success")
    return redirect(url_for("main.moderation_review"))

@main_bp.route("/messages/unblock/<int:user_id>", methods=["POST"])
@login_required
def unblock_user(user_id):
    if user_id == current_user.id:
        flash(
            "You don't need to unblock yourself, just log out like everyone else.",
            "warning",
        )
        return redirect(url_for("main.inbox"))

    existing = MessageBlock.query.filter_by(
        blocker_id=current_user.id,
        blocked_id=user_id,
    ).first()

    if existing:
        db.session.delete(existing)
        db.session.commit()
        flash("User unblocked.", "success")
    else:
        flash("This user wasn't blocked.", "info")

    return redirect(request.referrer or url_for("main.inbox"))
