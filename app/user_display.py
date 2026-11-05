"""Consistent presentation of usernames without changing account identifiers."""
from datetime import datetime, timedelta

from flask import g
from markupsafe import Markup, escape

from .models import User


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


def display_username(username):
    label = escape(username)
    if username in new_member_names():
        label += Markup(
            ' <span class="new-member-badge"'
            ' title="New member — joined within the last 30 days"'
            ' aria-label="New member — joined within the last 30 days">✨</span>'
        )
    return label


def register_user_display(app):
    app.jinja_env.filters["display_username"] = display_username

    @app.context_processor
    def inject_new_members():
        return {"new_member_names": sorted(new_member_names())}
