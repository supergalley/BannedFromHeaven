"""Consistent presentation of usernames without changing account identifiers."""
from datetime import datetime, timedelta

from flask import g, has_request_context, url_for
from flask_login import current_user
from sqlalchemy import func
from markupsafe import Markup, escape

from .models import User, UserWarning
from . import db


def new_member_names():
    if "new_member_names" not in g:
        now = datetime.utcnow()
        g.new_member_names = frozenset(
            name for (name,) in User.query.with_entities(User.username).filter(
                User.created_at > now - timedelta(days=30),
                User.created_at <= now,
            ).all()
        )
    return g.new_member_names


def active_strike_counts():
    """Expose warning status only to a moderator/admin, with one query per request."""
    if not (has_request_context() and current_user.is_authenticated
            and (current_user.is_admin or current_user.is_moderator)):
        return {}
    if "active_strike_counts" not in g:
        rows = (
            db.session.query(User.username, User.id, func.count(UserWarning.id))
            .join(UserWarning, UserWarning.user_id == User.id)
            .filter(UserWarning.is_active.is_(True))
            .group_by(User.id, User.username).all()
        )
        g.active_strike_counts = {name: count for name, uid, count in rows}
        g.active_strike_user_ids = {name: uid for name, uid, count in rows}
    return g.active_strike_counts


def active_bans():
    """Cache current ban expiry times for staff-visible username labels."""
    if not (has_request_context() and current_user.is_authenticated
            and (current_user.is_admin or current_user.is_moderator)):
        return {}
    if "username_active_bans" not in g:
        g.username_active_bans = dict(User.query.with_entities(
            User.username, User.ban_until
        ).filter(User.ban_until > datetime.utcnow()).all())
    return g.username_active_bans


def ban_remaining_label(until):
    from math import ceil
    minutes = max(1, ceil((until - datetime.utcnow()).total_seconds() / 60))
    days, minutes = divmod(minutes, 1440)
    hours, minutes = divmod(minutes, 60)
    parts = []
    for value, unit in ((days, "day"), (hours, "hour"), (minutes, "minute")):
        if value:
            parts.append(f"{value} {unit}{'s' if value != 1 else ''}")
    return "Ban remaining: " + ", ".join(parts)


def display_username(username):
    label = escape(username)
    if username in new_member_names():
        label += Markup(
            ' <span class="new-member-badge"'
            ' title="New member — joined within the last 30 days"'
            ' aria-label="New member — joined within the last 30 days">✨</span>'
        )
    count = active_strike_counts().get(username, 0)
    if count:
        description = f"{count} active warning strike{'s' if count != 1 else ''}"
        label += Markup(
            ' <span class="badge rounded-pill ms-1 user-strike-badge"'
            ' aria-label="{}" role="button" tabindex="0"'
            ' aria-haspopup="dialog" aria-expanded="false" aria-controls="user-strike-card"'
            ' data-strike-level="{}" data-strike-url="{}">Strike</span>'
        ).format(description, min(count, 3),
                 url_for("main.user_strike_summary", user_id=g.active_strike_user_ids[username]))
    until = active_bans().get(username)
    if until and until > datetime.utcnow():
        description = ban_remaining_label(until)
        label += Markup(
            ' <span class="badge rounded-pill ms-1 bg-danger user-ban-badge"'
            ' tabindex="0" title="{}" aria-label="{}" data-ban-until="{}">Ban</span>'
        ).format(description, description, until.isoformat() + "Z")
    return label


def register_user_display(app):
    app.jinja_env.filters["display_username"] = display_username

    @app.context_processor
    def inject_new_members():
        counts = active_strike_counts()
        return {"new_member_names": sorted(new_member_names()),
                "username_strike_counts": counts,
                "username_active_bans": {name: until.isoformat() + "Z"
                                         for name, until in active_bans().items()},
                "username_strike_urls": {
                    name: url_for("main.user_strike_summary", user_id=g.active_strike_user_ids[name])
                    for name in counts
                }}
