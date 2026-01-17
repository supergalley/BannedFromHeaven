from datetime import datetime, timedelta
from sqlalchemy import func
from flask import current_app, request
from . import db
from .models import LoginAudit


def get_client_ip() -> str:
    """
    Cloudflare proxied traffic:
      - CF-Connecting-IP is the real client IP
      - X-Forwarded-For may contain a chain; we want the first
    """
    cf_ip = (request.headers.get("CF-Connecting-IP") or "").strip()
    if cf_ip:
        return cf_ip

    true_ip = (request.headers.get("True-Client-IP") or "").strip()
    if true_ip:
        return true_ip

    xff = (request.headers.get("X-Forwarded-For") or "").strip()
    if xff:
        return xff.split(",")[0].strip()

    return (request.remote_addr or "").strip()


def too_many_failed_logins(ip: str) -> bool:
    window = current_app.config.get("LOGIN_FAIL_WINDOW", timedelta(minutes=10))
    limit = current_app.config.get("LOGIN_FAIL_LIMIT", 8)

    cutoff = datetime.utcnow() - window

    fail_count = (
        db.session.query(func.count())
        .select_from(LoginAudit)
        .filter(
            LoginAudit.ip == ip,
            LoginAudit.success.is_(False),
            LoginAudit.created_at >= cutoff,
        )
        .scalar()
        or 0
    )

    return fail_count >= limit
