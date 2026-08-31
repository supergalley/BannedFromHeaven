"""
Resumable upload processor webhook.

Run this in the processor container (not public).
tusd calls /hooks/tusd on post-finish.
"""

import json
import os
import urllib.request
from typing import Any, Dict, Tuple

from flask import request, jsonify
from werkzeug.datastructures import FileStorage

from . import create_app
from .routes import process_meme_image, process_clip_video

app = create_app()


def _extract_upload(payload: Dict[str, Any]) -> Tuple[str, Dict[str, str]]:
    """
    tusd hook commonly sends:
      {"Type":"post-finish","Event":{"Upload":{"ID":"...","MetaData":{...}}}}

    Sometimes you may also see {"Upload":{...}} directly.
    """
    ev = payload.get("Event") or payload.get("event") or payload
    up = (
        ev.get("Upload")
        or ev.get("upload")
        or payload.get("Upload")
        or payload.get("upload")
        or {}
    )
    upload_id = up.get("ID") or up.get("Id") or up.get("id") or ""
    md = up.get("MetaData") or up.get("Metadata") or up.get("metadata") or {}
    if not isinstance(md, dict):
        md = {}

    # IMPORTANT: do NOT base64 decode here. tusd already gives plain strings.
    md_clean = {str(k): str(v) for k, v in md.items()}
    return str(upload_id), md_clean


def _call_finalize(payload: Dict[str, Any]) -> Tuple[int, str]:
    base = (
        os.environ.get("FLASK_INTERNAL_URL") or "http://bannedfromheaven:8000"
    ).rstrip("/")
    url = f"{base}/api/uploads/finalize"

    secret = (os.environ.get("UPLOADS_SHARED_SECRET") or "").strip()
    if not secret:
        return 500, "UPLOADS_SHARED_SECRET not set"

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "X-BFH-Uploads-Secret": secret},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            text = resp.read().decode("utf-8", errors="replace")
            return resp.getcode(), text
    except Exception as e:
        return 502, f"Finalize call failed: {e}"


@app.route("/hooks/tusd", methods=["POST"])
def tusd_hook():
    payload = request.get_json(silent=True) or {}
    upload_id, md = _extract_upload(payload)
    if not upload_id:
        return jsonify(ok=False, error="Missing upload id"), 400
    joke_type = (md.get("joke_type") or "").strip().lower()
    if joke_type not in ("meme", "clip"):
        return jsonify(ok=False, error="Invalid joke_type", got=joke_type, md=md), 400
    token = (md.get("token") or "").strip()
    if not token:
        return jsonify(ok=False, error="Missing token"), 400
    category_id = (md.get("category_id") or "").strip()
    subcategory_id = (md.get("subcategory_id") or "").strip()
    body_text = (md.get("body") or "").strip()
    salute_to = (md.get("salute_to") or "").strip()
    as_draft = (md.get("as_draft") or "").strip().lower() in ("1", "true", "yes", "on")
    draft_id = (md.get("draft_id") or "").strip()
    tusd_dir = (os.environ.get("TUSD_DIR") or "/tusd").rstrip("/")
    upload_path = os.path.join(tusd_dir, upload_id)
    if not os.path.exists(upload_path):
        return jsonify(ok=False, error=f"Upload file not found: {upload_id}"), 404
    original_filename = (md.get("filename") or "upload.bin").strip() or "upload.bin"
    content_type = (md.get("content_type") or "").strip() or None
    with open(upload_path, "rb") as f:
        fs = FileStorage(
            stream=f, filename=original_filename, content_type=content_type
        )
        if joke_type == "meme":
            image_filename = process_meme_image(fs)
            finalize_payload = {
                "token": token,
                "joke_type": "meme",
                "category_id": category_id,
                "subcategory_id": subcategory_id,
                "body": body_text,
                "image_filename": image_filename,
                "salute_to": salute_to,
            }
        else:
            video_filename, video_thumb, video_duration, video_size = (
                process_clip_video(fs)
            )
            finalize_payload = {
                "token": token,
                "joke_type": "clip",
                "category_id": category_id,
                "subcategory_id": subcategory_id,
                "body": body_text,
                "salute_to": salute_to,
                "video_filename": video_filename,
                "video_thumb": video_thumb,
                "video_duration": video_duration,
                "video_size": video_size,
            }
        if as_draft:
            finalize_payload["as_draft"] = "1"
        if draft_id:
            finalize_payload["draft_id"] = draft_id

    code, resp_text = _call_finalize(finalize_payload)
    if code < 200 or code >= 300:
        # IMPORTANT: don't delete the tusd upload if finalize failed
        # so you can retry / inspect.
        return jsonify(ok=False, error=resp_text, status=code), 502

    # finalize succeeded: safe to delete
    for p in (upload_path, upload_path + ".info"):
        try:
            os.remove(p)
        except Exception:
            pass

    return jsonify(ok=True, finalize_status=code), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
