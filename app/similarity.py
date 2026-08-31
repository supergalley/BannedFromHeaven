"""Find near-duplicate jokes for the submit intermediate page."""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from difflib import SequenceMatcher
from pathlib import Path

from .mbed_index import read_mbed, top_k as mbed_top_k

log = logging.getLogger(__name__)

MBED_CANDIDATES = (
    os.environ.get("BFH_MBED_PATH"),
    "/data/jokes.mbed",
    "/jokes/data/jokes.mbed",
    str(Path(__file__).resolve().parent.parent / "data" / "jokes.mbed"),
)

MEME_MBED_CANDIDATES = (
    os.environ.get("BFH_MEME_MBED_PATH"),
    "/data/memes.mbed",
    "/jokes/data/memes.mbed",
    str(Path(__file__).resolve().parent.parent / "data" / "memes.mbed"),
)


def normalize(text: str) -> str:
    t = (text or "").lower().replace("\u00a0", " ")
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"[^\w\s]", " ", t, flags=re.UNICODE)
    return re.sub(r"\s+", " ", t).strip()


def mbed_path() -> Path | None:
    for c in MBED_CANDIDATES:
        if not c:
            continue
        p = Path(c)
        if p.is_file() and p.stat().st_size > 32:
            return p
    return None


def meme_mbed_path() -> Path | None:
    for c in MEME_MBED_CANDIDATES:
        if not c:
            continue
        p = Path(c)
        if p.is_file() and p.stat().st_size > 32:
            return p
    return None


def remote_embed(text: str, timeout: float = 25.0) -> list[float] | None:
    """Ask host-side proxy (which SSHes to X4 Ollama) for one text embedding."""
    url = (os.environ.get("BFH_EMBED_PROXY_URL") or "http://192.168.4.1:18765/embed").strip()
    if not url:
        return None
    secret = (
        os.environ.get("BFH_EMBED_PROXY_SECRET")
        or os.environ.get("UPLOADS_SHARED_SECRET")
        or ""
    ).strip()
    payload = json.dumps({"text": text[:4000]}).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-BFH-Embed-Secret": secret,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
        if data.get("ok") and data.get("embedding"):
            return data["embedding"]
        log.info("embed proxy: %s", data.get("error"))
    except Exception as e:
        log.info("embed proxy failed: %s", e)
    return None


def remote_embed_image(
    *,
    url: str | None = None,
    b64: str | None = None,
    file_path: str | None = None,
    timeout: float = 60.0,
) -> list[float] | None:
    """Ask host-side proxy (which SSHes to X4 Ollama Vision) for one image embedding."""
    base = (os.environ.get("BFH_EMBED_PROXY_URL") or "http://192.168.4.1:18765/embed").strip()
    if not base:
        return None
    # Derive image endpoint
    if base.endswith("/embed"):
        proxy_url = base[:-6] + "/embed-image"
    else:
        proxy_url = base.rstrip("/") + "/embed-image"

    secret = (
        os.environ.get("BFH_EMBED_PROXY_SECRET")
        or os.environ.get("UPLOADS_SHARED_SECRET")
        or ""
    ).strip()
    body = {}
    if url:
        body["url"] = url
    elif b64:
        body["b64"] = b64
    elif file_path:
        body["file"] = file_path
    else:
        return None

    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        proxy_url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-BFH-Embed-Secret": secret,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
        if data.get("ok") and data.get("embedding"):
            return data["embedding"]
        log.info("embed image proxy: %s", data.get("error"))
    except Exception as e:
        log.info("embed image proxy failed: %s", e)
    return None


def fuzzy_candidates(
    body: str,
    jokes: list,
    *,
    limit: int = 8,
    min_score: float = 0.72,
) -> list[tuple[object, float, str]]:
    """jokes: iterable of objects with .id .body .title"""
    norm = normalize(body)
    if len(norm) < 12:
        return []
    out: list[tuple[object, float, str]] = []
    for j in jokes:
        other = normalize(getattr(j, "body", None) or "")
        if len(other) < 12:
            continue
        if other == norm:
            score, method = 1.0, "exact"
        else:
            score = SequenceMatcher(None, norm, other).ratio()
            method = "fuzzy"
        if score >= min_score:
            out.append((j, score, method))
    out.sort(key=lambda t: t[1], reverse=True)
    return out[:limit]


def mbed_score_hits(
    body: str,
    *,
    limit: int = 8,
    min_score: float = 0.72,
) -> list[tuple[int, float]]:
    """Return [(joke_id, cosine), ...] from jokes.mbed (needs embed proxy)."""
    path = mbed_path()
    if not path:
        return []
    try:
        data = read_mbed(path)
    except Exception as e:
        log.warning("mbed read failed: %s", e)
        return []
    vec = remote_embed(normalize(body)[:2000] or body[:2000])
    if not vec:
        return []
    try:
        return mbed_top_k(data, vec, k=limit, min_score=min_score)
    except Exception as e:
        log.warning("mbed top_k failed: %s", e)
        return []


def find_recent_quick_dupes(
    body: str,
    *,
    actor_user_id: int | None = None,
    hours: int = 48,
    per_user_limit: int = 80,
    site_limit: int = 200,
    same_user_min: float = 0.90,
    site_exact_only: bool = False,
    site_min: float = 0.97,
) -> list[tuple[object, float, str]]:
    """
    Fast local check (no embeddings / X4) against very recent posts.

    Catches double-clicks and near-identical re-posts within minutes/hours
    before the embed index has caught up.
    """
    from datetime import datetime, timedelta

    from .models import Joke

    norm = normalize(body)
    if len(norm) < 12:
        return []

    cutoff = datetime.utcnow() - timedelta(hours=max(1, hours))
    pool: list = []
    seen_ids: set[int] = set()

    def _add(rows):
        for j in rows:
            if j.id in seen_ids:
                continue
            seen_ids.add(j.id)
            pool.append(j)

    # Prefer the poster's own recent text jokes (double-submit target)
    if actor_user_id:
        _add(
            Joke.query.filter(
                Joke.user_id == actor_user_id,
                Joke.is_meme.is_(False),
                Joke.is_clip.is_(False),
                Joke.created_at >= cutoff,
            )
            .order_by(Joke.id.desc())
            .limit(per_user_limit)
            .all()
        )

    # Plus recent public text jokes site-wide
    _add(
        Joke.query.filter(
            Joke.review_status == "approved",
            Joke.is_quarantined.is_(False),
            Joke.is_meme.is_(False),
            Joke.is_clip.is_(False),
            Joke.created_at >= cutoff,
        )
        .order_by(Joke.id.desc())
        .limit(site_limit)
        .all()
    )

    hits: list[tuple[object, float, str]] = []
    for j in pool:
        other = normalize(getattr(j, "body", None) or "")
        if len(other) < 12:
            continue
        if other == norm:
            score, method = 1.0, "exact"
        else:
            # Quick reject on length mismatch before SequenceMatcher
            if abs(len(other) - len(norm)) > max(40, int(0.35 * max(len(other), len(norm)))):
                continue
            score = SequenceMatcher(None, norm, other).ratio()
            method = "fuzzy"
        is_own = actor_user_id is not None and int(j.user_id) == int(actor_user_id)
        thr = same_user_min if is_own else (1.0 if site_exact_only else site_min)
        if score >= thr:
            hits.append((j, float(score), method))
    hits.sort(key=lambda t: t[1], reverse=True)
    return hits


def hard_block_recent_duplicate(
    body: str,
    *,
    actor_user_id: int,
) -> tuple[object, float, str] | None:
    """
    Return the best blocking hit if this post is clearly a recent re-submit.

    - Same user: >= 90% fuzzy or exact within 48h
    - Anyone: exact normalized body within 48h
    """
    hits = find_recent_quick_dupes(
        body,
        actor_user_id=actor_user_id,
        hours=48,
        same_user_min=0.90,
        site_exact_only=True,  # non-own: only exact norm match
        site_min=1.0,
    )
    return hits[0] if hits else None


def find_similar_jokes(
    body: str,
    *,
    jokes_for_fuzzy: list,
    joke_by_id: dict[int, object] | None = None,
    load_jokes_by_ids=None,
    limit: int = 8,
    fuzzy_min: float = 0.72,
    embed_min: float = 0.72,
) -> list[tuple[object, float, str]]:
    """
    Merge fuzzy + embed hits; keep best score per joke id.
    Returns list of (joke, score, method).
    load_jokes_by_ids(ids) -> list[Joke] optional hydrator for embed-only hits.
    """
    by_id = dict(joke_by_id or {})
    best: dict[int, tuple[object, float, str]] = {}
    for j, score, method in fuzzy_candidates(
        body, jokes_for_fuzzy, limit=limit * 2, min_score=fuzzy_min
    ):
        jid = int(j.id)
        by_id[jid] = j
        prev = best.get(jid)
        if not prev or score > prev[1]:
            best[jid] = (j, score, method)

    embed_hits = mbed_score_hits(body, limit=limit * 2, min_score=embed_min)
    missing = [jid for jid, _ in embed_hits if jid not in by_id]
    if missing and load_jokes_by_ids is not None:
        for j in load_jokes_by_ids(missing) or []:
            by_id[int(j.id)] = j
    for jid, score in embed_hits:
        j = by_id.get(int(jid))
        if j is None:
            continue
        prev = best.get(int(jid))
        if not prev or score > prev[1]:
            best[int(jid)] = (j, float(score), "embed")

    ranked = sorted(best.values(), key=lambda t: t[1], reverse=True)
    return ranked[:limit]


def find_similar_memes(
    *,
    image_filename: str | None = None,
    image_url: str | None = None,
    image_b64: str | None = None,
    image_file_path: str | None = None,
    limit: int = 8,
    min_score: float = 0.85,
    load_jokes_by_ids=None,
) -> list[tuple[object, float, str]]:
    """
    Find near-duplicate meme jokes using visual embeddings from memes.mbed.
    Returns [(joke, score, method), ...] sorted best first.
    """
    path = meme_mbed_path()
    if not path:
        return []
    try:
        data = read_mbed(path)
    except Exception as e:
        log.warning("meme mbed read failed: %s", e)
        return []

    # Get visual embedding via remote proxy
    vec = None
    if image_url:
        vec = remote_embed_image(url=image_url)
    elif image_b64:
        vec = remote_embed_image(b64=image_b64)
    elif image_file_path:
        vec = remote_embed_image(file_path=image_file_path)
    elif image_filename:
        import urllib.parse
        media_base = (os.environ.get("MEDIA_BASE") or "https://media.bannedfromheaven.com/uploads").rstrip("/")
        vec = remote_embed_image(url=f"{media_base}/{urllib.parse.quote(image_filename)}")

    if not vec:
        return []

    try:
        hits = mbed_top_k(data, vec, k=limit, min_score=min_score)
    except Exception as e:
        log.warning("meme mbed top_k failed: %s", e)
        return []

    if not hits:
        return []

    out = []
    if load_jokes_by_ids is not None:
        joke_map = {int(j.id): j for j in (load_jokes_by_ids([h[0] for h in hits]) or [])}
        for jid, score in hits:
            j = joke_map.get(int(jid))
            if j:
                out.append((j, float(score), "image_embed"))
    return out

