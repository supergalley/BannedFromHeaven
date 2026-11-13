"""Opt-in device subscriptions, notification preferences and app badges."""
import base64
import hashlib
import secrets
from datetime import datetime
from urllib.parse import urlsplit
from flask import Blueprint, abort, current_app, jsonify, render_template, request, session
from flask_login import current_user, login_required
from sqlalchemy.exc import IntegrityError
from . import db
from .models import Message, PushIdentity, PushSubscription, PushDelivery

push_bp = Blueprint('push', __name__)


def unread_count(user_id):
    return Message.query.filter_by(recipient_id=user_id, read_at=None, deleted_by_recipient=False).count()


def identity():
    row = db.session.get(PushIdentity, 1)
    if row:
        return row
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization
    private = ec.generate_private_key(ec.SECP256R1())
    public = private.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    row = PushIdentity(id=1, private_key=private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode(), public_key=base64.urlsafe_b64encode(public).rstrip(b'=').decode())
    db.session.add(row)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        row = db.session.get(PushIdentity, 1)
    return row


def csrf_token():
    if 'push_csrf' not in session:
        session['push_csrf'] = secrets.token_urlsafe(32)
    return session['push_csrf']


def require_csrf():
    if not secrets.compare_digest(request.headers.get('X-Push-CSRF','').encode(), csrf_token().encode()):
        abort(403)


def validate_subscription(data):
    if not isinstance(data,dict):
        raise ValueError('Invalid device subscription.')
    endpoint = data.get('endpoint','')
    if not isinstance(endpoint,str) or len(endpoint)>2500:
        raise ValueError('Invalid push endpoint.')
    try:
        parsed = urlsplit(endpoint)
        host = parsed.hostname or ''
        valid_host = host in {'fcm.googleapis.com','updates.push.services.mozilla.com'} or host.endswith('.push.apple.com') or host.endswith('.notify.windows.com')
        if not valid_host or parsed.scheme!='https' or parsed.port not in (None,443) or parsed.username or parsed.password or parsed.fragment:
            raise ValueError('Unsupported push provider.')
        keys = data['keys']
        public, auth = keys['p256dh'], keys['auth']
        for value, size in ((public,65),(auth,16)):
            if not isinstance(value,str) or len(value)>100:
                raise ValueError('Invalid subscription keys.')
            decoded = base64.b64decode(value+'='*(-len(value)%4), altchars=b'-_', validate=True)
            if len(decoded)!=size:
                raise ValueError('Invalid subscription keys.')
        from cryptography.hazmat.primitives.asymmetric import ec
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(),base64.urlsafe_b64decode(public+'='*(-len(public)%4)))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError('Invalid device subscription.') from exc
    return endpoint,public,auth


@push_bp.after_request
def private_push_responses(response):
    response.headers['Cache-Control']='private, no-store'
    return response


@push_bp.route('/notifications')
@login_required
def settings():
    return render_template('notifications.html',push_csrf=csrf_token())


@push_bp.route('/api/push/status')
@login_required
def status():
    endpoint_hash=request.args.get('device','')[:64]
    enabled=bool(endpoint_hash and PushSubscription.query.filter_by(user_id=current_user.id,endpoint_hash=endpoint_hash).first())
    if enabled:
        session['push_endpoint_hash']=endpoint_hash
    return jsonify(badge=unread_count(current_user.id),csrf=csrf_token(),public_key=identity().public_key,enabled=enabled)


@push_bp.route('/api/push/subscribe',methods=['POST'])
@login_required
def subscribe():
    request.max_content_length=8192
    require_csrf()
    try:
        endpoint,public,auth=validate_subscription(request.get_json(silent=True))
    except ValueError as exc:
        return jsonify(error=str(exc)),400
    digest=hashlib.sha256(endpoint.encode()).hexdigest()
    row=PushSubscription.query.filter_by(endpoint_hash=digest).first()
    if row and (row.p256dh!=public or row.auth!=auth):
        return jsonify(error='This device subscription could not be verified. Disable notifications in your browser and try again.'),409
    if not row:
        if PushSubscription.query.filter_by(user_id=current_user.id).count()>=10:
            return jsonify(error='Device limit reached. Remove an old device first.'),429
        row=PushSubscription(endpoint_hash=digest,endpoint=endpoint,p256dh=public,auth=auth)
        db.session.add(row)
    elif row.user_id!=current_user.id:
        PushDelivery.query.filter_by(subscription_id=row.id).delete(synchronize_session=False)
        row.created_at=datetime.utcnow()
    row.user_id=current_user.id
    session['push_endpoint_hash']=digest
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify(error='Subscription changed in another tab. Please try again.'),409
    return jsonify(enabled=True,badge=unread_count(current_user.id))


def remove_device(user_id,digest):
    row=PushSubscription.query.filter_by(user_id=user_id,endpoint_hash=digest).first()
    if row:
        PushDelivery.query.filter_by(subscription_id=row.id).delete(synchronize_session=False)
        db.session.delete(row)
        db.session.commit()


@push_bp.route('/api/push/unsubscribe',methods=['POST'])
@login_required
def unsubscribe():
    request.max_content_length=8192
    require_csrf()
    data=request.get_json(silent=True) or {}
    digest=data.get('device','') if isinstance(data,dict) else ''
    if isinstance(digest,str):
        remove_device(current_user.id,digest[:64])
    session.pop('push_endpoint_hash',None)
    return jsonify(enabled=False)


@push_bp.route('/push-sw.js')
def service_worker():
    response=current_app.send_static_file('push-sw.js')
    response.headers['Content-Type']='application/javascript'
    response.headers['Service-Worker-Allowed']='/'
    return response
