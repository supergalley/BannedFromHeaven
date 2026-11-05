"""Shared quarantine review permissions and decisions for moderators and admins."""
from datetime import datetime

from flask import abort
from flask_login import current_user
from sqlalchemy import or_

from . import db
from .models import AdminNotification, Joke, QuarantinedJoke


def can_review_quarantine(record, reviewer):
    if not reviewer.is_authenticated:
        return False
    if reviewer.is_admin:
        return True
    return bool(
        reviewer.is_moderator
        and record.quarantined_by_id != reviewer.id
        and record.joke.quarantined_by_id != reviewer.id
    )


def quarantine_review_query(reviewer):
    query = QuarantinedJoke.query.join(Joke).filter(
        QuarantinedJoke.permanently_deleted.is_(False),
        QuarantinedJoke.restored_at.is_(None),
        Joke.is_quarantined.is_(True),
    )
    if not reviewer.is_admin:
        if not reviewer.is_moderator:
            abort(403)
        query = query.filter(
            QuarantinedJoke.quarantined_by_id != reviewer.id,
            or_(Joke.quarantined_by_id.is_(None), Joke.quarantined_by_id != reviewer.id),
        )
    return query


def require_quarantine_access(joke):
    """Apply the same visibility rule when a quarantined joke is opened directly."""
    if not joke.is_quarantined:
        return
    if not current_user.is_authenticated or not (current_user.is_admin or current_user.is_moderator):
        abort(404)
    if current_user.is_admin:
        return
    record = joke.quarantine_record
    if not record or record.permanently_deleted:
        abort(404)
    if not can_review_quarantine(record, current_user):
        abort(403)


def review_quarantined_joke(joke_id, action, reviewer):
    if action not in ("restore", "delete"):
        abort(400)
    record = QuarantinedJoke.query.filter_by(joke_id=joke_id).first_or_404()
    if not can_review_quarantine(record, reviewer):
        abort(403)
    joke = record.joke
    claim = QuarantinedJoke.query.filter(
        QuarantinedJoke.id == record.id,
        QuarantinedJoke.permanently_deleted.is_(False),
        QuarantinedJoke.restored_at.is_(None),
        QuarantinedJoke.joke.has(Joke.is_quarantined.is_(True)),
    )
    if not reviewer.is_admin:
        claim = claim.filter(
            QuarantinedJoke.quarantined_by_id != reviewer.id,
            QuarantinedJoke.joke.has(or_(Joke.quarantined_by_id.is_(None), Joke.quarantined_by_id != reviewer.id)),
        )
    changes = {QuarantinedJoke.restored_at: datetime.utcnow()} if action == "restore" else {
        QuarantinedJoke.permanently_deleted: True,
    }
    if not claim.update(changes, synchronize_session="fetch"):
        db.session.rollback()
        abort(409, description="This quarantine has already been reviewed. Refresh the list.")
    joke.is_quarantined = action == "delete"
    joke.quarantine_locked = True
    db.session.add(AdminNotification(
        action="Restore quarantined joke" if action == "restore" else "Delete quarantined joke",
        message=f"{reviewer.username} {'restored' if action == 'restore' else 'marked as deleted'} quarantined joke #{joke.id}.",
        performed_by_id=reviewer.id,
        target_user_id=joke.user_id,
        target_joke_id=joke.id,
    ))
    db.session.commit()
    if action == "restore":
        return "Joke restored and locked from further moderator quarantine."
    return "Joke marked as deleted. It will no longer appear in quarantine or public lists."
