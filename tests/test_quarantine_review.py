"""Quarantine review access and decisions, using only a disposable database."""
import os
import unittest
from datetime import datetime

if not os.environ.get('jokeDB', '').startswith('/tmp/'):
    raise RuntimeError('Set jokeDB to a disposable /tmp/ database')

from flask import g
from app import app, db
from app.models import User, Joke, QuarantinedJoke, AdminNotification, JokeDupePair


class QuarantineReviewTest(unittest.TestCase):
    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        self.assertTrue(app.config['SQLALCHEMY_DATABASE_URI'].startswith('sqlite:////tmp/'))
        app.config.update(TESTING=True, SECRET_KEY='test', SESSION_PROTECTION=None)
        db.drop_all()
        db.create_all()
        db.session.add_all([
            User(id=1, username='admin', email='admin@test.example', password_hash='test', is_admin=True),
            User(id=2, username='mod_a', email='mod_a@test.example', password_hash='test', is_moderator=True),
            User(id=3, username='mod_b', email='mod_b@test.example', password_hash='test', is_moderator=True),
            User(id=4, username='member', email='member@test.example', password_hash='test'),
        ])
        db.session.flush()
        for jid, owner in [(1, 2), (2, 3), (3, 1), (4, 3), (5, 3), (6, 3)]:
            joke = Joke(id=jid, body=f'Full quarantined body {jid}', user_id=4,
                        is_quarantined=jid != 5, quarantined_by_id=2 if jid == 6 else owner,
                        quarantined_at=datetime.utcnow(), is_meme=jid == 2,
                        image_filename='review.webp' if jid == 2 else None)
            db.session.add(joke)
            db.session.add(QuarantinedJoke(
                joke=joke, original_body=joke.body, quarantined_by_id=owner,
                reasons='Not a joke', permanently_deleted=jid == 4,
                restored_at=datetime.utcnow() if jid == 5 else None,
            ))
        db.session.commit()
        self.client = app.test_client()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def login(self, uid):
        g.pop('_login_user', None)
        with self.client.session_transaction() as session:
            session['_user_id'] = str(uid)
            session['_fresh'] = True

    def test_mod_list_excludes_own_quarantines_and_resolved_items(self):
        self.login(2)
        response = self.client.get('/moderation/quarantine')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        for jid in [2, 3]:
            self.assertIn(f'Full quarantined body {jid}', html)
            self.assertIn(f'/moderation/quarantine/{jid}/restore', html)
        for jid in [1, 4, 5, 6]:
            self.assertNotIn(f'Full quarantined body {jid}', html)
        self.assertIn('review.webp', html)
        self.assertNotIn('/admin/jokes/', html)
        room = self.client.get('/moderation/room').get_data(as_text=True)
        self.assertIn('href="/moderation/quarantine">Quarantine</a>', room)

    def test_admin_list_includes_own_quarantines(self):
        self.login(1)
        html = self.client.get('/moderation/quarantine').get_data(as_text=True)
        for jid in [1, 2, 3, 6]:
            self.assertIn(f'Full quarantined body {jid}', html)
        for jid in [4, 5]:
            self.assertNotIn(f'Full quarantined body {jid}', html)
        self.assertTrue(self.client.get('/admin/quarantine').location.endswith('/moderation/quarantine'))
        self.assertIn('Edit', html)

    def test_own_quarantine_cannot_be_viewed_or_reviewed_by_mod(self):
        self.login(2)
        for jid in [1, 6]:
            self.assertEqual(self.client.get(f'/jokes/{jid}').status_code, 403)
            for action in ['restore', 'delete']:
                self.assertEqual(self.client.post(f'/moderation/quarantine/{jid}/{action}').status_code, 403)
                self.assertTrue(db.session.get(Joke, jid).is_quarantined)
                self.assertFalse(QuarantinedJoke.query.filter_by(joke_id=jid).one().permanently_deleted)
        self.assertEqual(AdminNotification.query.count(), 0)
        self.assertEqual(self.client.get('/jokes/2').status_code, 200)
        self.assertEqual(self.client.get('/admin/quarantine').status_code, 403)
        self.assertEqual(self.client.post('/admin/quarantine/1/restore').status_code, 403)

    def test_mod_restore_records_activity_and_locks_post(self):
        self.login(2)
        response = self.client.post('/moderation/quarantine/2/restore')
        self.assertEqual(response.status_code, 302)
        joke = db.session.get(Joke, 2)
        self.assertFalse(joke.is_quarantined)
        self.assertTrue(joke.quarantine_locked)
        record = QuarantinedJoke.query.filter_by(joke_id=2).one()
        self.assertIsNotNone(record.restored_at)
        self.assertEqual(record.quarantined_by_id, 3)
        note = AdminNotification.query.one()
        self.assertEqual(note.performed_by_id, 2)
        self.assertEqual(note.target_joke_id, 2)
        self.assertEqual(note.action, 'Restore quarantined joke')
        self.assertEqual(self.client.post('/moderation/quarantine/2/delete').status_code, 409)
        self.assertEqual(AdminNotification.query.count(), 1)
        self.client.post('/moderation/jokes/2/quarantine', data={'reasons': 'Not a joke'})
        self.assertFalse(db.session.get(Joke, 2).is_quarantined)

    def test_mod_delete_is_soft_and_cannot_be_repeated(self):
        self.login(2)
        self.assertEqual(self.client.post('/moderation/quarantine/3/delete').status_code, 302)
        joke = db.session.get(Joke, 3)
        self.assertIsNotNone(joke)
        self.assertTrue(joke.is_quarantined)
        self.assertTrue(joke.quarantine_locked)
        self.assertTrue(QuarantinedJoke.query.filter_by(joke_id=3).one().permanently_deleted)
        self.assertEqual(AdminNotification.query.one().action, 'Delete quarantined joke')
        self.assertEqual(self.client.post('/moderation/quarantine/3/restore').status_code, 409)
        self.assertEqual(self.client.get('/jokes/3').status_code, 404)

    def test_admin_can_review_own_and_legacy_admin_actions_work(self):
        self.login(1)
        self.assertEqual(self.client.post('/moderation/quarantine/3/restore').status_code, 302)
        self.assertFalse(db.session.get(Joke, 3).is_quarantined)
        self.assertEqual(self.client.post('/admin/quarantine/1/delete').status_code, 302)
        self.assertTrue(QuarantinedJoke.query.filter_by(joke_id=1).one().permanently_deleted)

    def test_members_cannot_access_quarantine_pages_or_actions(self):
        self.assertEqual(self.client.get('/moderation/quarantine').status_code, 302)
        self.assertEqual(self.client.get('/jokes/2').status_code, 404)
        self.login(4)
        self.assertEqual(self.client.get('/moderation/quarantine').status_code, 403)
        for action in ['restore', 'delete']:
            self.assertEqual(self.client.post(f'/moderation/quarantine/2/{action}').status_code, 403)
        self.assertEqual(self.client.get('/jokes/2').status_code, 404)
        self.assertEqual(AdminNotification.query.count(), 0)

    def test_quarantine_cannot_be_bypassed_through_inline_queues(self):
        db.session.get(Joke, 1).review_status = 'pending'
        db.session.get(Joke, 2).review_status = 'pending'
        db.session.add(JokeDupePair(id=1, joke_older_id=1, joke_newer_id=2, score=.95))
        db.session.commit()
        self.login(2)
        headers = {'Accept': 'application/json'}
        data = self.client.get('/moderation/room/queues', headers=headers).json
        self.assertEqual(data['notifications']['pending_review_count'], 0)
        self.assertEqual(data['notifications']['pending_dupe_count'], 0)
        self.assertNotIn('Full quarantined body', data['queues']['review'])
        self.assertNotIn('Full quarantined body', data['queues']['duplicates'])
        self.assertEqual(self.client.post('/moderation/review/1/approve', headers=headers).status_code, 403)
        self.assertEqual(self.client.post('/moderation/review/2/approve', headers=headers).status_code, 409)
        self.assertEqual(self.client.post('/moderation/duplicates/1/delete_newer', headers=headers).status_code, 403)
        self.assertEqual(db.session.get(Joke, 1).review_status, 'pending')
        self.assertIsNotNone(db.session.get(Joke, 2))
