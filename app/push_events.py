"""Transactional notification outbox. No network work in a posting transaction."""
from datetime import datetime, timedelta
from urllib.parse import quote
from sqlalchemy import event, inspect, select
from sqlalchemy.orm import Session
from sqlalchemy.dialects.sqlite import insert
from .models import Message, UserWarning, User, Joke, PushEvent, PushSubscription, PushDelivery


@event.listens_for(Session, 'after_flush')
def enqueue_push_events(session, flush_context):
    connection = session.connection()
    now = datetime.utcnow()

    def enqueue(key, user_id, kind, source, title, body, url):
        if not user_id:
            return
        result = connection.execute(insert(PushEvent).values(
            event_key=key, user_id=user_id, kind=kind, source_id=source,
            title=title, body=body, url=url, created_at=now,
        ).on_conflict_do_nothing(index_elements=['event_key']))
        if not result.rowcount:
            return
        event_id = result.inserted_primary_key[0]
        subscription_ids = connection.execute(select(PushSubscription.id).where(
            PushSubscription.user_id == user_id
        )).scalars().all()
        if subscription_ids:
            connection.execute(insert(PushDelivery), [dict(
                event_id=event_id, subscription_id=sid, attempts=0,
                due_at=now+timedelta(seconds=3),
            ) for sid in subscription_ids])

    for obj in list(session.new) + list(session.dirty):
        if isinstance(obj, Message) and obj in session.new and obj.sender_id != obj.recipient_id:
            # Formal notices get their own strike/ban push, rather than a duplicate DM alert.
            sender = connection.execute(select(User.username).where(User.id == obj.sender_id)).scalar()
            if sender == 'Moderator' and ((obj.subject or '').startswith('Official Warning (Strike #') or obj.subject == 'Account Suspension Notice'):
                continue
            enqueue(f'message:{obj.id}:{obj.created_at.isoformat()}',obj.recipient_id,'message',obj.id,
                    'New inbox message','You have a new message on BannedFromHeaven.',f'/messages/{obj.id}')
        elif isinstance(obj, UserWarning) and obj in session.new and obj.is_active:
            enqueue(f'strike:{obj.id}',obj.user_id,'strike',obj.id,
                    'A warning strike was issued','Open your profile for your strike status.',
                    '/user/'+quote(connection.execute(select(User.username).where(User.id==obj.user_id)).scalar() or '',safe='')+'#account-status')
        elif isinstance(obj, User) and obj.ban_until and obj.ban_until > now:
            changed = inspect(obj).attrs.ban_until.history.has_changes()
            if changed:
                enqueue(f'ban:{obj.id}:{obj.ban_until.isoformat()}',obj.id,'ban',obj.id,
                        'Your account has been suspended','Open your profile for the ban duration and details.',
                        '/user/'+quote(obj.username,safe='')+'#account-status')
        elif isinstance(obj, Joke) and (obj.score or 0) >= 10 and obj.review_status == 'approved' and not obj.is_quarantined:
            if obj in session.new or inspect(obj).attrs.score.history.has_changes() or inspect(obj).attrs.review_status.history.has_changes():
                enqueue(f'joke-ten:{obj.id}',obj.user_id,'milestone',obj.id,
                        'Your joke reached 10 points!','A milestone worth celebrating. Thanks for sharing it.',f'/jokes/{obj.id}')
