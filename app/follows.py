"""Follow jokes for new-comment alerts (cyan glow)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import and_

from . import db
from .models import Comment, Joke, JokeFollow


def is_following(user_id: int, joke_id: int) -> bool:
    return (
        JokeFollow.query.filter_by(user_id=user_id, joke_id=joke_id).first() is not None
    )


def follow_joke(user_id: int, joke_id: int) -> JokeFollow:
    """Start following; last_seen_at = now so only future comments glow."""
    now = datetime.utcnow()
    row = JokeFollow.query.filter_by(user_id=user_id, joke_id=joke_id).first()
    if row:
        return row
    row = JokeFollow(
        user_id=user_id,
        joke_id=joke_id,
        created_at=now,
        last_seen_at=now,
    )
    db.session.add(row)
    db.session.commit()
    return row


def unfollow_joke(user_id: int, joke_id: int) -> bool:
    row = JokeFollow.query.filter_by(user_id=user_id, joke_id=joke_id).first()
    if not row:
        return False
    db.session.delete(row)
    db.session.commit()
    return True


def unfollow_jokes(user_id: int, joke_ids: list[int]) -> int:
    """Unfollow many jokes at once. Returns number removed."""
    ids = [int(x) for x in joke_ids if x is not None]
    if not ids:
        return 0
    rows = JokeFollow.query.filter(
        JokeFollow.user_id == user_id,
        JokeFollow.joke_id.in_(ids),
    ).all()
    n = len(rows)
    for row in rows:
        db.session.delete(row)
    if n:
        db.session.commit()
    return n


def mark_followed_joke_seen(user_id: int, joke_id: int) -> None:
    """Follower opened the post → clear cyan glow for this follow."""
    row = JokeFollow.query.filter_by(user_id=user_id, joke_id=joke_id).first()
    if not row:
        return
    row.last_seen_at = datetime.utcnow()
    db.session.commit()


def user_has_unseen_followed_comments(user_id: int) -> bool:
    """True if any followed joke has a comment from someone else after last_seen."""
    q = (
        db.session.query(JokeFollow.id)
        .join(Joke, Joke.id == JokeFollow.joke_id)
        .join(Comment, Comment.joke_id == Joke.id)
        .filter(
            JokeFollow.user_id == user_id,
            Comment.user_id != user_id,
            Comment.created_at > JokeFollow.last_seen_at,
            Joke.review_status == "approved",
            Joke.is_quarantined.is_(False),
        )
        .limit(1)
    )
    return q.first() is not None


def unseen_followed_joke_ids(
    user_id: int, joke_ids: list[int] | None = None
) -> set[int]:
    q = (
        db.session.query(JokeFollow.joke_id)
        .join(Joke, Joke.id == JokeFollow.joke_id)
        .join(Comment, Comment.joke_id == Joke.id)
        .filter(
            JokeFollow.user_id == user_id,
            Comment.user_id != user_id,
            Comment.created_at > JokeFollow.last_seen_at,
            Joke.review_status == "approved",
            Joke.is_quarantined.is_(False),
        )
        .distinct()
    )
    if joke_ids is not None:
        if not joke_ids:
            return set()
        q = q.filter(JokeFollow.joke_id.in_(list(joke_ids)))
    return {int(r[0]) for r in q.all()}


def mark_all_followed_seen(user_id: int) -> int:
    now = datetime.utcnow()
    rows = JokeFollow.query.filter_by(user_id=user_id).all()
    for row in rows:
        row.last_seen_at = now
    db.session.commit()
    return len(rows)


def list_followed_jokes(user_id: int, *, limit: int = 500):
    """
    Return list of dicts: joke, followed_at, last_seen_at, comment_count,
    last_comment_at, has_unseen.
    """
    from sqlalchemy import func

    cc = (
        db.session.query(
            Comment.joke_id.label("joke_id"),
            func.count(Comment.id).label("comment_count"),
            func.max(Comment.created_at).label("last_comment_at"),
        )
        .group_by(Comment.joke_id)
        .subquery()
    )

    q = (
        db.session.query(
            Joke,
            JokeFollow,
            cc.c.comment_count,
            cc.c.last_comment_at,
        )
        .join(JokeFollow, JokeFollow.joke_id == Joke.id)
        .outerjoin(cc, cc.c.joke_id == Joke.id)
        .filter(
            JokeFollow.user_id == user_id,
            Joke.review_status == "approved",
            Joke.is_quarantined.is_(False),
        )
        .order_by(JokeFollow.created_at.desc())
        .limit(limit)
    )
    rows = q.all()
    joke_ids = [j.id for j, _, _, _ in rows]
    unseen = unseen_followed_joke_ids(user_id, joke_ids)
    items = []
    for joke, follow, count, last_at in rows:
        items.append(
            {
                "joke": joke,
                "followed_at": follow.created_at,
                "last_seen_at": follow.last_seen_at,
                "comment_count": int(count or 0),
                "last_comment_at": last_at,
                "has_unseen": joke.id in unseen,
            }
        )
    return items
