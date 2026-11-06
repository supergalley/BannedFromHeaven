"""Password recovery tests use a disposable database and never send real mail."""
import os
import re
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

if not os.environ.get('jokeDB', '').startswith('/tmp/'):
    raise RuntimeError('Set jokeDB to a disposable /tmp/ database')

from app import app, db
from app.models import User, PasswordResetRequest


class PasswordResetTest(unittest.TestCase):
    def setUp(self):
        self.ctx = app.app_context(); self.ctx.push()
        app.config.update(TESTING=True, SECRET_KEY='test', DOMAIN='bannedfromheaven.com')
        db.drop_all(); db.create_all()
        self.user = User(username='recoveryuser', email='recover@example.com')
        self.user.set_password('old-password'); db.session.add(self.user); db.session.commit()
        self.client = app.test_client()
        self.mail = patch('app.auth.send_email').start()
        self.client.get('/auth/forgot-password')
        with self.client.session_transaction() as session:
            self.csrf = session['recovery_csrf']

    def tearDown(self):
        patch.stopall(); db.session.remove(); db.drop_all(); self.ctx.pop()

    def request_reset(self, identity='recoveryuser'):
        return self.client.post('/auth/forgot-password', data={'identity':identity, 'csrf_token':self.csrf})

    def path(self):
        body = self.mail.call_args.kwargs['text_body']
        return re.search(r'https://bannedfromheaven.com([^\s]+)', body).group(1)

    def submit(self, path, **extra):
        data = dict(csrf_token=self.csrf, password='new-password', confirm_password='new-password')
        data.update(extra)
        return self.client.post(path, data=data)

    def test_username_and_email_and_generic_response(self):
        known = self.request_reset().data
        self.assertEqual(self.mail.call_args.kwargs['to'], 'recover@example.com')
        self.assertIn('junk/spam', self.mail.call_args.kwargs['text_body'])
        unknown = self.request_reset('nobody').data
        self.assertEqual(known, unknown)
        self.assertEqual(self.mail.call_count, 1)
        self.request_reset('RECOVER@EXAMPLE.COM')
        self.assertEqual(self.mail.call_count, 2)

    def test_single_use_and_password_changed_and_sibling_invalidated(self):
        self.request_reset(); first = self.path()
        self.request_reset(); second = self.path()
        row = PasswordResetRequest.query.first()
        self.assertNotIn(row.token_hash, first)
        response = self.client.get(first)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['Referrer-Policy'], 'no-referrer')
        self.assertEqual(self.client.get(first).status_code, 200)  # email scanners don't consume it
        self.assertEqual(self.submit(first).status_code, 302)
        db.session.expire_all()
        self.assertTrue(db.session.get(User, self.user.id).check_password('new-password'))
        self.assertEqual(self.submit(first).status_code, 400)
        self.assertEqual(self.client.get(second).status_code, 400)

    def test_expiry_tampering_password_validation_and_csrf(self):
        self.assertEqual(self.client.post('/auth/forgot-password', data={'identity':'recoveryuser'}).status_code, 400)
        self.request_reset(); path = self.path()
        self.assertEqual(self.submit(path, csrf_token='wrong').status_code, 400)
        self.assertEqual(self.submit(path, csrf_token='☃').status_code, 400)
        self.assertEqual(self.submit(path, confirm_password='wrong').status_code, 200)
        self.assertEqual(self.submit(path, password='a', confirm_password='a').status_code, 200)
        self.assertIsNone(PasswordResetRequest.query.first().used_at)
        self.assertEqual(self.client.get(path+'tampered').status_code, 400)
        PasswordResetRequest.query.first().expires_at = datetime.utcnow()-timedelta(seconds=1)
        db.session.commit()
        self.assertEqual(self.submit(path).status_code, 400)

    def test_password_change_invalidates_link(self):
        self.request_reset(); path = self.path()
        self.user.set_password('changed-elsewhere'); db.session.commit()
        self.assertEqual(self.client.get(path).status_code, 400)

    def test_limits_and_mail_failure(self):
        for _ in range(4): self.assertEqual(self.request_reset().status_code, 200)
        self.assertEqual(self.mail.call_count, 3)
        self.request_reset('nobody')
        self.assertEqual(self.request_reset('nobody').status_code, 429)

    def test_mail_failure_is_generic_and_host_cannot_poison_link(self):
        self.mail.side_effect = RuntimeError('SMTP offline')
        self.assertEqual(self.request_reset().status_code, 200)
        self.assertTrue(self.path().startswith('/auth/reset-password/'))
        self.assertNotIn('localhost', self.mail.call_args.kwargs['text_body'])

    def test_login_has_link(self):
        self.assertIn(b'Forgot My Password', self.client.get('/auth/login').data)
