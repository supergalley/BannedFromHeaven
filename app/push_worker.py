"""Durable delivery queue, shared safely by gunicorn workers via short DB leases."""
import json
import logging
import threading
import time
from datetime import datetime, timedelta
from sqlalchemy import or_
from . import db
from .models import PushDelivery, PushEvent, PushSubscription, Message, UserWarning, User, Joke
from .push import identity, unread_count

log=logging.getLogger(__name__)
_start_lock=threading.Lock()


def dispatch_one(sender=None):
    from pywebpush import webpush
    from py_vapid import Vapid02
    sender=sender or webpush
    now=datetime.utcnow()
    eligible=(PushDelivery.completed_at.is_(None),PushDelivery.due_at<=now,
              or_(PushDelivery.lease_until.is_(None),PushDelivery.lease_until<now))
    candidate=PushDelivery.query.filter(*eligible).order_by(PushDelivery.id).first()
    if not candidate:
        return False
    delivery_id=candidate.id
    claimed=PushDelivery.query.filter(PushDelivery.id==delivery_id,*eligible).update(
        {PushDelivery.lease_until:now+timedelta(seconds=90)},synchronize_session=False)
    db.session.commit()
    if not claimed:
        return True
    delivery=db.session.get(PushDelivery,delivery_id)
    db.session.refresh(delivery)
    event=db.session.get(PushEvent,delivery.event_id)
    sub=db.session.get(PushSubscription,delivery.subscription_id)
    valid=bool(event and sub and event.user_id==sub.user_id and (now-event.created_at).total_seconds()<86400)
    if valid and event.kind=='message':
        msg=db.session.get(Message,event.source_id)
        valid=bool(msg and msg.read_at is None and not msg.deleted_by_recipient)
    if valid and event.kind=='strike':
        warning=db.session.get(UserWarning,event.source_id)
        valid=bool(warning and warning.is_active)
        # A strike with an automatic ban should produce one combined moderation alert.
        combined=PushEvent.query.filter(PushEvent.user_id==event.user_id,PushEvent.kind=='ban',
            PushEvent.created_at>=event.created_at-timedelta(seconds=5),
            PushEvent.created_at<=event.created_at+timedelta(seconds=5)).first()
        if combined:
            valid=False
    if valid and event.kind=='ban':
        user=db.session.get(User,event.user_id)
        valid=bool(user and user.is_banned())
    if valid and event.kind=='milestone':
        joke=db.session.get(Joke,event.source_id)
        valid=bool(joke and joke.review_status=='approved' and not joke.is_quarantined)
    if not valid:
        delivery.completed_at=now;db.session.commit();return True
    body=event.body
    if event.kind=='ban' and PushEvent.query.filter(PushEvent.user_id==event.user_id,PushEvent.kind=='strike',
            PushEvent.created_at>=event.created_at-timedelta(seconds=5),
            PushEvent.created_at<=event.created_at+timedelta(seconds=5)).first():
        body='A warning strike and suspension were issued. Open your profile for details.'
    payload=dict(title=event.title,body=body,url=event.url,tag=event.event_key,badge=unread_count(event.user_id))
    try:
        sender(subscription_info=dict(endpoint=sub.endpoint,keys=dict(p256dh=sub.p256dh,auth=sub.auth)),
               data=json.dumps(payload),vapid_private_key=Vapid02.from_pem(identity().private_key.encode()),
               vapid_claims={'sub':'mailto:satan@bannedfromheaven.com'},ttl=86400,timeout=15,
               headers={'Urgency':'normal'})
        delivery.completed_at=datetime.utcnow()
        delivery.lease_until=None
    except Exception as exc:
        response=getattr(exc,'response',None)
        code=getattr(exc,'status_code',None) or (getattr(response,'status_code',None) if response is not None else None)
        if code in (404,410):
            PushDelivery.query.filter_by(subscription_id=sub.id).update({PushDelivery.completed_at:now},synchronize_session=False)
            db.session.delete(sub)
        else:
            delivery.attempts+=1
            delivery.lease_until=None
            if delivery.attempts>=5 or code in (400,401,403):
                delivery.completed_at=now
            else:
                delivery.due_at=now+timedelta(seconds=min(3600,30*2**delivery.attempts))
            # Exceptions can contain endpoint URLs and credentials; never log their text.
            log.warning('Web Push delivery %s failed (HTTP %s); attempt %s',delivery.id,code,delivery.attempts)
    db.session.commit()
    return True


def start_dispatcher(app):
    if app.testing or app.config.get('PUSH_WORKER_DISABLED'):
        return
    with _start_lock:
        if app.extensions.get('push_dispatcher'):
            return
        def run():
            while True:
                with app.app_context():
                    try:
                        processed=dispatch_one()
                    except Exception:
                        db.session.rollback()
                        log.error('Push queue processing failed; will retry')
                        processed=False
                time.sleep(.2 if processed else 5)
        thread=threading.Thread(target=run,name='bfh-web-push',daemon=True)
        app.extensions['push_dispatcher']=thread
        thread.start()
