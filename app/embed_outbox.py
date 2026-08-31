"""Durable outbox: new/updated jokes → X4 embed inbox (host flush cron)."""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path


def outbox_dir() -> Path:
    """
    Prefer host-mounted data path. Inside the container this is /data/embed_outbox
    when the volume is mounted; fall back to app-relative data.
    """
    for candidate in (
        os.environ.get("BFH_EMBED_OUTBOX"),
        "/data/embed_outbox",
        "/jokes/data/embed_outbox",
    ):
        if candidate:
            p = Path(candidate)
            try:
                p.mkdir(parents=True, exist_ok=True)
                return p
            except OSError:
                continue
    p = Path(__file__).resolve().parent.parent / "data" / "embed_outbox"
    p.mkdir(parents=True, exist_ok=True)
    return p


def enqueue_joke_upsert(*, joke_id: int, body: str, is_meme: bool = False, is_clip: bool = False) -> Path | None:
    """Queue a text joke for embedding on X4. Skips memes/clips/empty."""
    if is_meme or is_clip:
        return None
    text = (body or "").strip()
    if len(text) < 12:
        return None
    dest = outbox_dir()
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    name = f"{ts}_{joke_id}_{uuid.uuid4().hex[:8]}.json"
    path = dest / name
    payload = {
        "action": "upsert",
        "id": int(joke_id),
        "body": text,
        "enqueued_at": ts,
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def enqueue_joke_delete(*, joke_id: int) -> Path:
    dest = outbox_dir()
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    name = f"{ts}_del_{joke_id}_{uuid.uuid4().hex[:8]}.json"
    path = dest / name
    payload = {
        "action": "delete",
        "ids": [int(joke_id)],
        "enqueued_at": ts,
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path
