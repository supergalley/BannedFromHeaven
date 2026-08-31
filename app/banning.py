# app/banning.py — blacklist helpers + verbose banning.log writer
from __future__ import annotations

import os
from datetime import datetime

from flask import current_app
from sqlalchemy import func

from . import db
from .models import BlacklistEntry, LoginAudit, LoginEvent


VALID_BLACKLIST_KINDS = frozenset({"email", "ip", "username"})


def normalize_blacklist_value(kind: str, value: str) -> str:
    v = (value or "").strip()
    if kind in ("email", "username"):
        return v.lower()
    return v  # IPs: keep as seen (case doesn't matter for v4; v6 may vary)


def is_blacklisted(kind: str, value: str) -> bool:
    kind = (kind or "").strip().lower()
    if kind not in VALID_BLACKLIST_KINDS:
        return False
    norm = normalize_blacklist_value(kind, value)
    if not norm:
        return False
    return (
        BlacklistEntry.query.filter_by(kind=kind, value=norm).first() is not None
    )


def add_blacklist_entry(
    *,
    kind: str,
    value: str,
    reason: str | None = None,
    created_by_id: int | None = None,
    source_user_id: int | None = None,
    source_username: str | None = None,
) -> BlacklistEntry | None:
    """Insert if missing. Returns the entry (new or existing), or None if invalid."""
    kind = (kind or "").strip().lower()
    if kind not in VALID_BLACKLIST_KINDS:
        return None
    norm = normalize_blacklist_value(kind, value)
    if not norm:
        return None
    existing = BlacklistEntry.query.filter_by(kind=kind, value=norm).first()
    if existing:
        return existing
    row = BlacklistEntry(
        kind=kind,
        value=norm,
        reason=(reason or "")[:255] or None,
        created_by_id=created_by_id,
        source_user_id=source_user_id,
        source_username=(source_username or "")[:64] or None,
    )
    db.session.add(row)
    return row


def collect_user_ips(user_id: int) -> list[str]:
    """Distinct IPs seen for this user in login tables."""
    ips: set[str] = set()
    for (ip,) in (
        db.session.query(LoginAudit.ip)
        .filter(LoginAudit.user_id == user_id, LoginAudit.ip.isnot(None))
        .distinct()
        .all()
    ):
        if ip and str(ip).strip():
            ips.add(str(ip).strip())
    try:
        for (ip,) in (
            db.session.query(LoginEvent.ip)
            .filter(LoginEvent.user_id == user_id, LoginEvent.ip.isnot(None))
            .distinct()
            .all()
        ):
            if ip and str(ip).strip():
                ips.add(str(ip).strip())
    except Exception:
        pass
    return sorted(ips)


def banning_log_path() -> str:
    return current_app.config.get("BANNING_LOG_PATH") or "/data/banning.log"


def write_banning_log(text: str) -> None:
    """Append a verbose multi-line record to banning.log (best-effort)."""
    path = banning_log_path()
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)
            if not text.endswith("\n"):
                f.write("\n")
            f.flush()
    except Exception:
        current_app.logger.exception("Failed writing banning.log at %s", path)


def format_user_deletion_log(
    *,
    user,
    deleted_by,
    ips: list[str],
    blacklisted: dict[str, list[str]],
    counts: dict[str, int],
    login_rows: list,
    extra_notes: str = "",
) -> str:
    """Build a verbose dump of what is being removed / blacklisted."""
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        "=" * 72,
        f"USER DELETION  {now}",
        f"Deleted by:  id={getattr(deleted_by, 'id', None)} "
        f"username={getattr(deleted_by, 'username', None)}",
        "-" * 72,
        "USER RECORD",
        f"  id:              {user.id}",
        f"  username:        {user.username!r}",
        f"  email:           {user.email!r}",
        f"  created_at:      {user.created_at}",
        f"  reputation:      {user.reputation}",
        f"  is_admin:        {user.is_admin}",
        f"  is_moderator:    {user.is_moderator}",
        f"  needs_moderator: {user.needs_moderator}",
        f"  ban_until:       {user.ban_until}",
        f"  forum_last_seen: {getattr(user, 'forum_last_seen_at', None)}",
        f"  password_hash:   {user.password_hash}",
        "-" * 72,
        "KNOWN IPs (from login audit/events)",
    ]
    if ips:
        for ip in ips:
            lines.append(f"  - {ip}")
    else:
        lines.append("  (none found)")

    lines.append("-" * 72)
    lines.append("LOGIN AUDIT ROWS (snapshot before delete)")
    if login_rows:
        for row in login_rows:
            lines.append(
                f"  id={row.id} success={row.success} ip={row.ip!r} "
                f"host={row.host!r} at={row.created_at} "
                f"ua={(row.user_agent or '')[:120]!r}"
            )
    else:
        lines.append("  (none)")

    lines.append("-" * 72)
    lines.append("CONTENT COUNTS REMOVED / CLEANED")
    for k, v in sorted(counts.items()):
        lines.append(f"  {k}: {v}")

    lines.append("-" * 72)
    lines.append("BLACKLIST ACTIONS")
    for kind in ("email", "ip", "username"):
        vals = blacklisted.get(kind) or []
        if vals:
            for v in vals:
                lines.append(f"  + {kind}: {v}")
        else:
            lines.append(f"  (no {kind} blacklist)")

    if extra_notes:
        lines.append("-" * 72)
        lines.append("NOTES")
        lines.append(extra_notes)

    lines.append("=" * 72)
    lines.append("")
    return "\n".join(lines)
