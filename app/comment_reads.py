"""Track which comments on a user's own posts they have already seen."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import and_, or_
from sqlalchemy.orm import aliased

from . import db
from .models import Comment, Joke, JokeCommentRead


def mark_joke_comments_seen(user_id: int, joke_id: int, *, commit: bool = True) -> None:
    """Owner opened the post → clear unseen glow for this joke."""
    now = datetime.utcnow()
    row = JokeCommentRead.query.filter_by(user_id=user_id, joke_id=joke_id).first()
    if row:
        row.last_seen_at = now
    else:
        db.session.add(
            JokeCommentRead(user_id=user_id, joke_id=joke_id, last_seen_at=now)
        )
    if commit:
        db.session.commit()


def mark_all_joke_comments_seen(user_id: int) -> int:
    """
    Mark every post owned by the user that has comments as seen.
    Returns how many jokes were updated/created.
    """
    joke_ids = [
        int(r[0])
        for r in (
            db.session.query(Joke.id)
            .join(Comment, Comment.joke_id == Joke.id)
            .filter(Joke.user_id == user_id)
            .distinct()
            .all()
        )
    ]
    if not joke_ids:
        return 0
    now = datetime.utcnow()
    existing = {
        r.joke_id: r
        for r in JokeCommentRead.query.filter(
            JokeCommentRead.user_id == user_id,
            JokeCommentRead.joke_id.in_(joke_ids),
        ).all()
    }
    for jid in joke_ids:
        row = existing.get(jid)
        if row:
            row.last_seen_at = now
        else:
            db.session.add(
                JokeCommentRead(user_id=user_id, joke_id=jid, last_seen_at=now)
            )
    db.session.commit()
    return len(joke_ids)


def user_has_unseen_comments(user_id: int) -> bool:
    """
    True if the user owns any post with a comment from someone else that is
    newer than last_seen_at, or has never been marked seen (no read row).
    """
    read = aliased(JokeCommentRead)
    q = (
        db.session.query(Joke.id)
        .join(Comment, Comment.joke_id == Joke.id)
        .outerjoin(
            read,
            and_(read.joke_id == Joke.id, read.user_id == user_id),
        )
        .filter(
            Joke.user_id == user_id,
            Comment.user_id != user_id,
            or_(read.id.is_(None), Comment.created_at > read.last_seen_at),
        )
        .limit(1)
    )
    return q.first() is not None


def unseen_comment_joke_ids(user_id: int, joke_ids: list[int] | None = None) -> set[int]:
    """Return set of the user's joke ids that currently have unseen comments."""
    if joke_ids is not None and not joke_ids:
        return set()
    read = aliased(JokeCommentRead)
    q = (
        db.session.query(Joke.id)
        .join(Comment, Comment.joke_id == Joke.id)
        .outerjoin(
            read,
            and_(read.joke_id == Joke.id, read.user_id == user_id),
        )
        .filter(
            Joke.user_id == user_id,
            Comment.user_id != user_id,
            or_(read.id.is_(None), Comment.created_at > read.last_seen_at),
        )
        .distinct()
    )
    if joke_ids is not None:
        q = q.filter(Joke.id.in_(list(joke_ids)))
    return {int(r[0]) for r in q.all()}
