# app/routes.py
import base64
import bleach
import os
import secrets
import subprocess
from datetime import datetime, timedelta
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
)
from flask_login import login_required, current_user
from functools import wraps
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from sqlalchemy import desc, or_, and_, func, case, text
from werkzeug.utils import secure_filename
from . import db
from .models import (
    Joke,
    Comment,
    Category,
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
from PIL import Image
from io import BytesIO

ALLOWED_TAGS = [
    "b",
    "strong",
    "i",
    "em",
    "u",
    "br",
    "p",
    "ul",
    "ol",
    "li",
    "a",
    "img",
]
ALLOWED_ATTRS = {
    "a": ["href", "title", "target", "rel"],
    "img": ["src", "alt", "title", "width", "height"],
}
ALLOWED_PROTOCOLS = [
    "http",
    "https",
    "mailto",
]  # note: /static/... has no scheme, that's fine


def sanitize_admin_html(raw: str) -> str:
    cleaned = bleach.clean(
        raw,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRS,
        protocols=ALLOWED_PROTOCOLS,
        strip=True,
    )
    # Also prevent <a target=_blank> tabnabbing if you allow target
    cleaned = cleaned.replace(
        'target="_blank"', 'target="_blank" rel="noopener noreferrer"'
    )
    return cleaned


main_bp = Blueprint("main", __name__)


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
        if len(data) > 2200 * 1024:
            raise ValueError("Animated GIF must be under 2.2mb. Upload a smaller one.")

        with open(final_path, "wb") as f:
            f.write(data)

        return filename

    # -------------------------
    # Static GIF: convert to JPG
    # -------------------------
    if is_gif and not is_animated:
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


def process_clip_video(file_storage, max_seconds=60, max_mb=50):
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


@main_bp.route("/jokes/<int:joke_id>/delete", methods=["POST"])
@login_required
def delete_joke(joke_id):
    joke = Joke.query.get_or_404(joke_id)

    # Only author or admin can delete
    if not (current_user.is_admin or current_user.id == joke.user_id):
        abort(403)

    author = joke.author

    # Undo reputation / score from all votes on this joke,
    # then delete the vote rows.
    votes = Vote.query.filter_by(joke_id=joke.id).all()
    for v in votes:
        author.reputation -= v.value  # reverse rep gain/loss
        joke.score -= v.value  # reverse score on the joke
        db.session.delete(v)

    # Delete related comments
    Comment.query.filter_by(joke_id=joke.id).delete(synchronize_session=False)

    # Delete the joke itself
    db.session.delete(joke)
    db.session.commit()

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
        if current_user.is_admin:
            body = sanitize_admin_html(body)
        else:
            # Non-admins stay plain text, no HTML
            # Optionally escape brackets or just rely on Jinja autoescape
            pass
        body = body.replace("\r\n", "\n")
        body = body.replace("\u00a0", " ")  # NBSP --> Normal Space
        body = body.lstrip()
        joke.body = body

        # Update category if provided
        cat_id = request.form.get("category_id") or None
        joke.category_id = int(cat_id) if cat_id else None

        # Optionally switch joke/meme flag
        kind = request.form.get("type", "joke")
        joke.is_meme = kind == "meme"

        db.session.commit()
        return redirect(url_for("main.joke_detail", joke_id=joke.id))

    categories = Category.query.order_by(Category.name).all()
    return render_template("edit_joke.html", joke=joke, categories=categories)


@main_bp.route("/comments/<int:comment_id>/edit", methods=["GET", "POST"])
@login_required
def edit_comment(comment_id):
    comment = Comment.query.get_or_404(comment_id)

    # Only author or admin can edit
    if not (current_user.is_admin or current_user.id == comment.user_id):
        abort(403)

    if request.method == "POST":
        body = request.form.get("body", "").strip()
        if current_user.is_admin:
            body = sanitize_admin_html(body)
        else:
            # Non-admins stay plain text, no HTML
            # Optionally escape brackets or just rely on Jinja autoescape
            pass
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

    # Base query: all jokes (not memes)
    base_query = Joke.query.filter_by(
        is_meme=False, is_clip=False, is_quarantined=False
    )

    # Optional category filter (by category.name)
    if category_name:
        category = Category.query.filter_by(name=category_name).first()
        if category:
            base_query = base_query.filter_by(category_id=category.id)

    # Time range handling (unchanged logic, applied to filtered base_query)
    valid_ranges = {"today", "week", "month", "all"}
    if requested_range in valid_ranges:
        time_range = requested_range
        query = apply_time_filter(base_query, time_range)
    else:
        # No explicit range: auto-widen until we get at least 10 jokes
        query, time_range = choose_best_time_range(base_query, min_results=10)

    # ----------------------------------------------------------------------
    # FIX: If user selected "all", return ALL jokes (not top 50)
    # ----------------------------------------------------------------------
    if time_range == "all":
        jokes = query.order_by(
            Joke.score.desc(),
            Joke.created_at.desc(),
        ).all()  # or desc(Joke.score) if you prefer
    else:
        # Existing behaviour: only top 50 jokes for other ranges
        jokes = query.order_by(desc(Joke.score)).limit(50).all()
    # ----------------------------------------------------------------------

    # NEW: build a map of joke_id -> vote value (+1 / -1) for the current user
    vote_map = {}
    if current_user.is_authenticated and jokes:
        joke_ids = [j.id for j in jokes]
        votes = Vote.query.filter(
            Vote.user_id == current_user.id,
            Vote.joke_id.in_(joke_ids),
        ).all()
        vote_map = {v.joke_id: v.value for v in votes}

    settings = SiteSettings.get()

    return render_template(
        "index.html",
        jokes=jokes,
        time_range=time_range,
        mode="jokes",
        settings=settings,
        vote_map=vote_map,  # <--- pass to template
        page_views=pv.count,
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

    # Base query: memes only
    base_query = Joke.query.filter_by(is_meme=True, is_quarantined=False)

    # Optional category filter
    if category_name:
        category = Category.query.filter_by(name=category_name).first()
        if category:
            base_query = base_query.filter_by(category_id=category.id)

    # Time range logic (same as before)
    valid_ranges = {"today", "week", "month", "all"}
    if requested_range in valid_ranges:
        time_range = requested_range
        query = apply_time_filter(base_query, time_range)
    else:
        query, time_range = choose_best_time_range(base_query, min_results=10)

    jokes = query.order_by(desc(Joke.score)).limit(50).all()

    # NEW: per-user vote mapping
    vote_map = {}
    if current_user.is_authenticated and jokes:
        joke_ids = [j.id for j in jokes]
        votes = Vote.query.filter(
            Vote.user_id == current_user.id,
            Vote.joke_id.in_(joke_ids),
        ).all()
        vote_map = {v.joke_id: v.value for v in votes}

    settings = SiteSettings.get()

    return render_template(
        "index.html",
        jokes=jokes,
        mode="memes",
        time_range=time_range,
        settings=settings,
        vote_map=vote_map,
    )


@main_bp.route("/clips")
def clips():
    requested_range = request.args.get("range", None)
    category_name = request.args.get("category", None)

    base_query = Joke.query.filter_by(is_clip=True, is_quarantined=False)

    if category_name:
        category = Category.query.filter_by(name=category_name).first()
        if category:
            base_query = base_query.filter_by(category_id=category.id)

    valid_ranges = {"today", "week", "month", "all"}
    if requested_range in valid_ranges:
        time_range = requested_range
        query = apply_time_filter(base_query, time_range)
    else:
        query, time_range = choose_best_time_range(base_query, min_results=10)

    # Same behaviour as memes for now (top 50 by score)
    jokes = query.order_by(desc(Joke.score)).limit(50).all()

    vote_map = {}
    if current_user.is_authenticated and jokes:
        joke_ids = [j.id for j in jokes]
        votes = Vote.query.filter(
            Vote.user_id == current_user.id,
            Vote.joke_id.in_(joke_ids),
        ).all()
        vote_map = {v.joke_id: v.value for v in votes}

    settings = SiteSettings.get()

    return render_template(
        "index.html",
        jokes=jokes,
        mode="clips",
        time_range=time_range,
        settings=settings,
        vote_map=vote_map,
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
    query = Joke.query.filter_by(is_quarantined=False)

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

    categories = Category.query.order_by(Category.name.asc()).all()
    current_args = request.args.to_dict(flat=True)

    # Category URLs for the category badge inside result cards:
    # clicking a badge should set ONLY that category (replace current category selection)
    base_args = request.args.to_dict(flat=True)
    base_args.pop("category_id", None)
    base_args.pop("category_ids", None)

    category_url_map = {}
    for cat in categories:
        args = dict(base_args)
        args["category_ids"] = str(cat.id)
        category_url_map[cat.id] = url_for("main.search", **args)

    return render_template(
        "search.html",
        results=results,
        categories=categories,
        category_url_map=category_url_map,
        time_range=time_range,
        vote_map=vote_map,
        current_args=current_args,
        search_type=search_type,
        selected_category_ids=selected_category_ids,
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
    comments = (
        Comment.query.filter_by(joke_id=joke.id)
        .order_by(desc(Comment.created_at))
        .all()
    )
    user_vote = 0
    if current_user.is_authenticated:
        v = Vote.query.filter_by(user_id=current_user.id, joke_id=joke.id).first()
        if v:
            user_vote = v.value  # 1 or -1
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
    return render_template(
        "joke_detail.html",
        joke=joke,
        comments=comments,
        user_vote=user_vote,
        reaction_types=REACTION_TYPES,
        reaction_summary=reaction_summary,
        user_reactions=user_reactions,
        canonical_url=url_for("main.joke_detail", joke_id=joke.id, _external=True),
    )


@main_bp.route("/user/<username>")
def user_profile(username):
    if not username or username.lower() in ("none", "null", "undefined"):
        return redirect(url_for("main.index"), code=301)
    user = User.query.filter_by(username=username).first_or_404()
    jokes = (
        Joke.query.filter_by(user_id=user.id, is_meme=False)
        .order_by(desc(Joke.score))
        .all()
    )
    return render_template(
        "user_profile.html",
        profile_user=user,
        jokes=jokes,
    )


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

    category_id = data.get("category_id")
    try:
        category_id_int = int(category_id) if category_id not in (None, "") else None
    except Exception:
        category_id_int = None

    body = (data.get("body") or "").strip()

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
        title=title,
        body=body,
        user_id=user.id,
        category_id=category_id_int,
        is_meme=is_meme,
        image_filename=image_filename,
        is_clip=is_clip,
        video_filename=video_filename,
        video_thumb=video_thumb,
        video_duration=video_duration,
        video_size=video_size,
    )
    db.session.add(joke)
    db.session.flush()

    auto_vote = Vote(user_id=user.id, joke_id=joke.id, value=1)
    db.session.add(auto_vote)
    joke.score += 1
    user.reputation += 1

    db.session.commit()
    return jsonify({"ok": True, "joke_id": joke.id})


@main_bp.route("/submit", methods=["GET", "POST"])
@login_required
def submit_joke():
    categories = Category.query.order_by(Category.name).all()
    joke_type = request.form.get("joke_type", "joke")
    is_meme = joke_type == "meme"
    is_clip = joke_type == "clip"
    if request.method == "POST":
        category_id = request.form.get("category_id")
        body = request.form.get("body", "").strip()
        if current_user.is_admin:
            body = sanitize_admin_html(body)
        else:
            # Non-admins stay plain text, no HTML
            # Optionally escape brackets or just rely on Jinja autoescape
            pass
        is_meme = joke_type == "meme"
        is_clip = joke_type == "clip"
        image_filename = None
        video_filename = None
        video_thumb = None
        video_duration = None
        video_size = None

        if is_meme:
            file = request.files.get("image")
            try:
                image_filename = process_meme_image(file)
            except ValueError as e:
                flash(str(e), "danger")
                return render_template(
                    "submit_joke.html", categories=categories, joke_type=joke_type
                )

        elif is_clip:
            file = request.files.get("clip")
            try:
                video_filename, video_thumb, video_duration, video_size = (
                    process_clip_video(file)
                )
            except ValueError as e:
                flash(str(e), "danger")
                return render_template(
                    "submit_joke.html", categories=categories, joke_type=joke_type
                )

        else:
            if not body:
                flash("Joke text is required.", "danger")
                return render_template(
                    "submit_joke.html", categories=categories, joke_type=joke_type
                )
        # Auto-generate a "title" from the body, purely internal
        if is_meme:
            title = "Meme"
        elif is_clip:
            title = body[:80] or "Clip"
        else:
            title = body[:80]
        settings = SiteSettings.get()

        # ------------------------------------------------------------------
        # Rate limit (SQLite-safe): compare using SQLite datetime(), not Python datetimes
        # ------------------------------------------------------------------
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
                return render_template(
                    "submit_joke.html",
                    categories=categories,
                    joke_type=joke_type,
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
                return render_template(
                    "submit_joke.html",
                    categories=categories,
                    joke_type=joke_type,
                )
        # ------------------------------------------------------------------

        joke = Joke(
            title=title,
            body=body,
            user_id=current_user.id,
            category_id=int(category_id) if category_id else None,
            is_meme=is_meme,
            image_filename=image_filename,
            is_clip=is_clip,
            video_filename=video_filename,
            video_thumb=video_thumb,
            video_duration=video_duration,
            video_size=video_size,
        )

        db.session.add(joke)
        db.session.flush()

        # Auto-upvote own post
        auto_vote = Vote(user_id=current_user.id, joke_id=joke.id, value=1)
        db.session.add(auto_vote)
        joke.score += 1
        current_user.reputation += 1

        db.session.commit()
        flash("Joke submitted.", "success")
        return redirect(url_for("main.joke_detail", joke_id=joke.id))

    return render_template(
        "submit_joke.html",
        categories=categories,
        joke_type=joke_type,
    )


@main_bp.route("/jokes/<int:joke_id>/comment", methods=["POST"])
@login_required
def add_comment(joke_id):
    body = request.form.get("body", "").strip()
    if current_user.is_admin:
        body = sanitize_admin_html(body)
    else:
        # Non-admins stay plain text, no HTML
        # Optionally escape brackets or just rely on Jinja autoescape
        pass
    if not body:
        flash("Comment cannot be empty.", "danger")
        return redirect(url_for("main.joke_detail", joke_id=joke_id))

    comment = Comment(body=body, user_id=current_user.id, joke_id=joke_id)
    db.session.add(comment)
    db.session.commit()
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
        .filter(Vote.value == 1)
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
    upvote_sum = func.sum(case((Vote.value == 1, 1), else_=0))

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


@main_bp.route("/api/jokes/<int:joke_id>/vote", methods=["POST"])
@login_required
def vote(joke_id):
    joke = Joke.query.get_or_404(joke_id)
    direction = request.json.get("direction")

    if direction not in ("up", "down"):
        return jsonify({"error": "Invalid vote"}), 400

    value = 1 if direction == "up" else -1
    author = joke.author

    existing = Vote.query.filter_by(user_id=current_user.id, joke_id=joke.id).first()

    if existing:
        # remove previous effect
        joke.score -= existing.value
        author.reputation -= existing.value

        if existing.value == value:
            # same vote clicked again → clear vote
            db.session.delete(existing)
        else:
            # flip vote
            existing.value = value
            joke.score += value
            author.reputation += value
    else:
        vote = Vote(user_id=current_user.id, joke_id=joke.id, value=value)
        db.session.add(vote)
        joke.score += value
        author.reputation += value

    db.session.commit()

    # NEW: determine user’s final vote after applying changes
    current_vote = Vote.query.filter_by(
        user_id=current_user.id, joke_id=joke.id
    ).first()

    user_vote = current_vote.value if current_vote else 0

    return jsonify({"score": joke.score, "user_vote": user_vote})


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
        if current_user.is_admin:
            body = sanitize_admin_html(body)
        else:
            # Non-admins stay plain text, no HTML
            # Optionally escape brackets or just rely on Jinja autoescape
            pass
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
    if current_user.is_admin:
        body = sanitize_admin_html(body)
    else:
        # Non-admins stay plain text, no HTML
        # Optionally escape brackets or just rely on Jinja autoescape
        pass
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


@main_bp.route("/sitemap.xml")
def sitemap():
    urls = []

    # Homepage (or your "jokes list" page)
    urls.append(
        {
            "loc": url_for("main.index", _external=True),
            "lastmod": datetime.utcnow().date().isoformat(),
        }
    )

    # Joke detail pages
    for joke_id, created_at in Joke.query.with_entities(Joke.id, Joke.created_at).all():
        urls.append(
            {
                "loc": url_for("main.joke_detail", joke_id=joke_id, _external=True),
                "lastmod": created_at.date().isoformat() if created_at else None,
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
        if current_user.is_admin:
            body = sanitize_admin_html(body)
        else:
            # Non-admins stay plain text, no HTML
            # Optionally escape brackets or just rely on Jinja autoescape
            pass
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


@main_bp.route("/moderation/jokes/<int:joke_id>/quarantine", methods=["POST"])
@moderator_required
def quarantine_joke(joke_id):
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
    # Create quarantine record if it doesn't exist yet
    if not getattr(joke, "quarantine_record", None):
        q = QuarantinedJoke(
            joke_id=joke.id,
            original_title=joke.title,
            original_body=joke.body,
            original_category_id=joke.category_id,
            quarantined_by_id=current_user.id,
        )
        db.session.add(q)

    joke.is_quarantined = True
    joke.quarantined_at = datetime.utcnow()
    joke.quarantined_by_id = current_user.id
    note = AdminNotification(
        action="Quarantine joke",
        message=f"{current_user.username} quarantined joke #{joke.id} by {joke.author.username}.",
        performed_by_id=current_user.id,
        target_user_id=joke.user_id,
        target_joke_id=joke.id,
    )
    db.session.add(note)
    db.session.commit()
    flash("Joke has been sent to quarantine for admin review.", "warning")
    return redirect(url_for("main.index"))


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
