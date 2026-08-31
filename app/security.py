from datetime import datetime, timedelta
from sqlalchemy import func
from flask import current_app, request
from . import db
from .models import LoginAudit
from .banning import is_blacklisted
import ipaddress

BLOCKED_IP_NETWORKS = [
    "2600:6c40:300:1387::/64",
    "2607:fb90:3928:d57::/64",
    "2607:fb90:3918:cbf5::/64",
]


def get_client_ip() -> str:
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


def ip_is_blocked(ip: str) -> bool:
    ip = (ip or "").strip()
    if not ip:
        return False
    # Admin-managed blacklist (exact IP strings)
    if is_blacklisted("ip", ip):
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for net_text in BLOCKED_IP_NETWORKS:
        if addr in ipaddress.ip_network(net_text, strict=False):
            return True
    return False


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