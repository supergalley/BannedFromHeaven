"""Push events are transaction-safe, scoped to opted-in devices, and never sent live in tests."""
import base64
import json
import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch
from types import SimpleNamespace

if not os.environ.get('jokeDB','').startswith('/tmp/'):
    raise RuntimeError('Use a disposable /tmp/ database')
from app import app, db
from app.models import User, Message, UserWarning, Joke, PushEvent, PushDelivery, PushSubscription, PushIdentity
from app.push import validate_subscription, identity
from app.push_worker import dispatch_one


def subscription_data(name='device'):
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization
    key=ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(serialization.Encoding.X962,serialization.PublicFormat.UncompressedPoint)
    encode=lambda b:base64.urlsafe_b64encode(b).rstrip(b'=').decode()
    return dict(endpoint='https://web.push.apple.com/'+name,keys=dict(p256dh=encode(key),auth=encode(b'0123456789abcdef')))


class PushTest(unittest.TestCase):
    def setUp(self):
        self.ctx=app.app_context();self.ctx.push()
        app.config.update(TESTING=True,SECRET_KEY='test',SESSION_PROTECTION=None)
        db.drop_all();db.create_all()
        self.user=User(id=1,username='recipient',email='recipient@example.test',password_hash='test',needs_moderator=False)
        self.sender=User(id=2,username='sender',email='sender@example.test',password_hash='test',needs_moderator=False,is_admin=True)
        db.session.add_all([self.user,self.sender]);db.session.commit()
        self.client=app.test_client()
        with self.client.session_transaction() as s:s['_user_id']='1';s['_fresh']=True
        self.data=subscription_data()
        self.status=self.client.get('/api/push/status').json

    def tearDown(self):
        db.session.remove();db.drop_all();self.ctx.pop()

    def enable(self):
        result=self.client.post('/api/push/subscribe',json=self.data,headers={'X-Push-CSRF':self.status['csrf']})
        self.assertEqual(result.status_code,200,result.data)
        return PushSubscription.query.one()

    def message(self):
        msg=Message(sender_id=2,recipient_id=1,subject='private subject',body='private text')
        db.session.add(msg);db.session.commit();return msg

    def due(self):
        PushDelivery.query.update({PushDelivery.due_at:datetime.utcnow()-timedelta(seconds=1)});db.session.commit()

    def test_message_events_only_for_existing_subscriptions_and_no_private_text(self):
        self.message()
        self.assertEqual(PushDelivery.query.count(),0)
        self.enable();msg=self.message()
        self.assertEqual(PushDelivery.query.count(),1)
        send=Mock();self.due();self.assertTrue(dispatch_one(send))
        payload=json.loads(send.call_args.kwargs['data'])
        self.assertEqual(payload['badge'],2)
        self.assertEqual(payload['url'],f'/messages/{msg.id}')
        self.assertNotIn('private',payload['body'])
        self.assertFalse(dispatch_one(send));self.assertEqual(send.call_count,1)

    def test_rolled_back_message_creates_no_alert(self):
        self.enable();db.session.add(Message(sender_id=2,recipient_id=1,body='rollback'))
        db.session.flush();self.assertEqual(PushDelivery.query.count(),1)
        db.session.rollback();self.assertEqual(PushDelivery.query.count(),0)
        self.assertEqual(PushEvent.query.count(),0)

    def test_milestone_once_across_threshold_bounces_and_double_upvote(self):
        self.enable();j=Joke(body='Milestone',user_id=1,score=9,review_status='approved');db.session.add(j);db.session.commit()
        j.score=11;db.session.commit();j.score=8;db.session.commit();j.score=10;db.session.commit()
        self.assertEqual(PushEvent.query.filter_by(kind='milestone').count(),1)
        self.assertEqual(PushDelivery.query.count(),1)

    def test_pending_or_quarantined_posts_do_not_announce(self):
        self.enable();j=Joke(body='Pending',user_id=1,score=10,review_status='pending');db.session.add(j);db.session.commit()
        self.assertEqual(PushEvent.query.count(),0)
        j.review_status='approved';db.session.commit();self.assertEqual(PushEvent.query.count(),1)
        j.is_quarantined=True;j.score=12;db.session.commit();self.assertEqual(PushEvent.query.count(),1)

    def test_strike_ban_and_system_dm_combine(self):
        self.enable();bot=User(username='Moderator',email='bot@example.test',password_hash='test');db.session.add(bot);db.session.flush()
        self.user.ban_until=datetime.utcnow()+timedelta(hours=24)
        db.session.add(UserWarning(user_id=1,issuer_id=2,reason='Private reason'))
        db.session.add(Message(sender_id=bot.id,recipient_id=1,subject='Official Warning (Strike #3/3)',body='Private details'))
        db.session.commit();self.assertEqual(PushEvent.query.filter_by(kind='message').count(),0)
        self.due();send=Mock()
        while dispatch_one(send):pass
        self.assertEqual(send.call_count,1)
        payload=json.loads(send.call_args.kwargs['data']);self.assertIn('strike and suspension',payload['body']);self.assertNotIn('Private',payload['body'])

    def test_old_read_messages_and_cleared_bans_do_not_alert(self):
        self.enable();msg=self.message();msg.read_at=datetime.utcnow();self.user.ban_until=datetime.utcnow()+timedelta(hours=1);db.session.commit()
        self.user.ban_until=None;db.session.commit();self.due();send=Mock()
        while dispatch_one(send):pass
        self.assertEqual(send.call_count,0)

    def test_subscription_auth_csrf_and_endpoint_validation(self):
        from flask import g
        g.pop('_login_user',None)
        self.assertEqual(app.test_client().post('/api/push/subscribe',json=self.data).status_code,302)
        g.pop('_login_user',None)
        self.assertEqual(self.client.post('/api/push/subscribe',json=self.data).status_code,403)
        for endpoint in ['http://web.push.apple.com/x','https://127.0.0.1/x','https://web.push.apple.com.evil.test/x','https://web.push.apple.com:8443/x','https://user:pass@web.push.apple.com/x']:
            bad=dict(self.data,endpoint=endpoint)
            with self.subTest(endpoint=endpoint),self.assertRaises(ValueError):validate_subscription(bad)
        invalid=dict(self.data,keys=dict(auth='bad',p256dh='bad'))
        with self.assertRaises(ValueError):validate_subscription(invalid)

    def test_idempotent_subscribe_and_logout_removes_device(self):
        self.enable();self.enable();self.assertEqual(PushSubscription.query.count(),1)
        self.client.get('/auth/logout');self.assertEqual(PushSubscription.query.count(),0)

    def test_account_switch_requires_subscription_keys_and_drops_old_pending(self):
        self.enable();self.message()
        with self.client.session_transaction() as s:s['_user_id']='2'
        from flask import g
        g.pop('_login_user',None)
        status=self.client.get('/api/push/status').json
        headers={'X-Push-CSRF':status['csrf']}
        bad=dict(self.data,keys=subscription_data('different')['keys'])
        self.assertEqual(self.client.post('/api/push/subscribe',json=bad,headers=headers).status_code,409)
        self.assertEqual(self.client.post('/api/push/subscribe',json=self.data,headers=headers).status_code,200)
        self.assertEqual(PushSubscription.query.one().user_id,2)
        self.assertEqual(PushDelivery.query.count(),0)

    def test_expired_subscription_removed_and_transient_failure_retried(self):
        self.enable();self.message();self.due()
        failed=RuntimeError('secret endpoint must not be logged');failed.response=SimpleNamespace(status_code=503)
        dispatch_one(Mock(side_effect=failed));delivery=PushDelivery.query.one()
        self.assertEqual(delivery.attempts,1);self.assertIsNone(delivery.completed_at)
        self.due();failed.response.status_code=410;dispatch_one(Mock(side_effect=failed))
        self.assertEqual(PushSubscription.query.count(),0)
        self.assertEqual(PushDelivery.query.count(),0)

    def test_identity_persistent_and_encryption_signing_without_network(self):
        from pywebpush import webpush
        first=identity().public_key;self.assertEqual(identity().public_key,first)
        self.assertEqual(PushIdentity.query.count(),1)
        self.enable();self.message();self.due()
        with patch('requests.post') as post:
            post.return_value.status_code=201
            dispatch_one(webpush)
            self.assertEqual(post.call_count,1)
            kwargs=post.call_args.kwargs
            self.assertTrue(any(key.lower()=='authorization' for key in kwargs['headers']))
            self.assertIsInstance(kwargs['data'],bytes)
            self.assertNotIn(b'new message',kwargs['data'])

    def test_worker_and_settings_response(self):
        sw=app.test_client().get('/push-sw.js')
        self.assertEqual(sw.status_code,200);self.assertEqual(sw.headers['Service-Worker-Allowed'],'/')
        self.assertIn(b'showNotification',sw.data)
        page=self.client.get('/notifications');self.assertEqual(page.status_code,200)
        self.assertIn(b'Enable notifications',page.data)
        self.assertEqual(page.headers['Cache-Control'],'private, no-store')

    def test_alerts_deep_link_and_own_profile_status_is_private(self):
        self.enable()
        self.user.ban_until=datetime.utcnow()+timedelta(hours=2)
        self.user.ban_reason='Private ban reason'
        db.session.add(UserWarning(user_id=1,issuer_id=2,reason='Private strike reason'))
        joke=Joke(body='Ten points',user_id=1,score=10,review_status='approved')
        db.session.add(joke);db.session.commit()
        for kind in ('ban','strike'):
            self.assertEqual(PushEvent.query.filter_by(kind=kind).one().url,'/user/recipient#account-status')
        self.assertEqual(PushEvent.query.filter_by(kind='milestone').one().url,f'/jokes/{joke.id}')
        own=self.client.get('/user/recipient').get_data(as_text=True)
        self.assertIn('id="account-status"',own)
        self.assertIn('Ban remaining:',own)
        self.assertIn('Private strike reason',own)
        from flask import g
        g.pop('_login_user',None)
        public=app.test_client().get('/user/recipient').get_data(as_text=True)
        self.assertNotIn('id="account-status"',public)
        self.assertNotIn('Private strike reason',public)
        self.assertNotIn('Private ban reason',public)
