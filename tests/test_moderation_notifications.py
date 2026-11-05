"""Run only with jokeDB set to a disposable /tmp/ database."""
import os
import unittest

if not os.environ.get('jokeDB', '').startswith('/tmp/'):
    raise RuntimeError('Set jokeDB to a disposable /tmp/ database')

from app import app, db
from app.models import User, Joke, JokeDupePair, ModRoomMessage, ModerationAcknowledgement
from app.routes import inject_pending_review_count
from flask_login import login_user


class ModerationNotificationsTest(unittest.TestCase):
    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        self.assertTrue(app.config['SQLALCHEMY_DATABASE_URI'].startswith('sqlite:////tmp/'))
        app.config.update(TESTING=True, SESSION_PROTECTION=None, SECRET_KEY='test')
        db.drop_all()
        db.create_all()
        db.session.add_all([
            User(id=1, username='admin', email='admin@test.example', password_hash='test', is_admin=True),
            User(id=2, username='mod', email='mod@test.example', password_hash='test', is_moderator=True),
        ])
        for i, score in enumerate([.95, .8, .6], 1):
            db.session.add(JokeDupePair(id=i, joke_older_id=10, joke_newer_id=10+i, score=score))
        db.session.commit()
        self.client = app.test_client()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def login(self, uid):
        from flask import g
        g.pop("_login_user", None)
        with self.client.session_transaction() as session:
            session['_user_id'] = str(uid)
            session['_fresh'] = True

    def counts(self, uid):
        with app.test_request_context('/'):
            login_user(db.session.get(User, uid))
            return inject_pending_review_count()

    def test_global_acknowledgement_and_new_findings(self):
        self.assertEqual(self.counts(1)['pending_dupe_count'], 2)
        self.assertEqual(self.counts(1)['pending_dupe_high_count'], 1)
        self.assertEqual(self.counts(1)['pending_dupe_medium_count'], 1)
        self.login(1)
        self.assertEqual(self.client.post('/moderation/room/acknowledge').status_code, 302)
        self.assertEqual(self.counts(2)['pending_dupe_count'], 0)
        self.assertEqual(JokeDupePair.query.filter_by(status='pending').count(), 3)
        db.session.add(JokeDupePair(joke_older_id=20, joke_newer_id=21, score=.8))
        db.session.commit()
        self.assertEqual(self.counts(2)['pending_dupe_count'], 1)

    def test_moderator_cannot_acknowledge(self):
        self.login(2)
        self.assertEqual(self.client.post('/moderation/room/acknowledge').status_code, 403)
        self.assertEqual(ModerationAcknowledgement.query.count(), 0)

    def test_templates_compile(self):
        for name in ['base.html', 'moderation_room.html', 'moderation_duplicates.html',
                     'moderation_review.html', 'moderation_categories.html']:
            app.jinja_env.get_template(name)

    def test_unpin_permissions_and_controls(self):
        db.session.add_all([
            ModRoomMessage(id=1, user_id=1, body='Admin note', is_pinned=True),
            ModRoomMessage(id=2, user_id=2, body='Mod note', is_pinned=True),
        ])
        db.session.commit()
        self.login(2)
        page = self.client.get('/moderation/room')
        self.assertEqual(page.status_code, 200)
        self.assertIn('/messages/2/unpin', page.get_data(as_text=True))
        self.assertNotIn('/messages/1/unpin', page.get_data(as_text=True))
        self.assertEqual(self.client.post('/moderation/room/messages/1/unpin').status_code, 403)
        self.assertTrue(db.session.get(ModRoomMessage, 1).is_pinned)
        self.assertEqual(self.client.post('/moderation/room/messages/2/unpin').status_code, 302)
        self.assertFalse(db.session.get(ModRoomMessage, 2).is_pinned)
        self.login(1)
        db.session.get(ModRoomMessage, 2).is_pinned = True
        db.session.commit()
        page = self.client.get('/moderation/room').get_data(as_text=True)
        self.assertIn('/messages/1/unpin', page)
        self.assertIn('/messages/2/unpin', page)
        self.assertEqual(self.client.post('/moderation/room/messages/2/unpin').status_code, 302)
        self.assertFalse(db.session.get(ModRoomMessage, 2).is_pinned)
        self.assertEqual(ModRoomMessage.query.count(), 2)

    def test_new_pending_review_restores_glow_after_acknowledgement(self):
        db.session.add(Joke(body='Pending joke', user_id=2, review_status='pending'))
        db.session.commit()
        self.login(1)
        self.assertEqual(self.counts(1)['pending_review_count'], 1)
        self.client.post('/moderation/room/acknowledge')
        self.assertEqual(self.counts(2)['pending_review_count'], 0)
        db.session.add(Joke(body='New pending joke', user_id=2, review_status='pending'))
        db.session.commit()
        self.assertEqual(self.counts(2)['pending_review_count'], 1)
        self.login(2)
        page = self.client.get('/moderation/room').get_data(as_text=True)
        self.assertIn('moderator-review-glow', page)
        self.assertIn('id="review-queue"', page)
        self.assertIn('Review Queue <span class="badge bg-secondary">2</span>', page)

    def test_noticeboard_replies(self):
        from app.models import ModRoomReply
        db.session.add(ModRoomMessage(id=1, user_id=1, body='Parent note', is_pinned=True))
        db.session.commit()
        self.login(2)
        result = self.client.post('/moderation/room/messages/1/reply', data={'body': '  A reply <script>alert(1)</script>  '})
        self.assertEqual(result.status_code, 302)
        self.assertTrue(result.location.endswith('#mod-note-1'))
        reply = ModRoomReply.query.one()
        self.assertEqual(reply.user_id, 2)
        self.assertEqual(reply.note_id, 1)
        page = self.client.get('/moderation/room').get_data(as_text=True)
        self.assertIn('noticeboard-reply', page)
        self.assertIn(reply.created_at.strftime('%d-%m %H:%M'), page)
        self.assertIn('A reply &lt;script&gt;', page)
        self.assertLess(page.index('Parent note'), page.index('A reply &lt;script&gt;'))
        self.client.post('/moderation/room/messages/1/reply', data={'body': '  '})
        self.assertEqual(ModRoomReply.query.count(), 1)
        self.assertEqual(self.client.post('/moderation/room/messages/999/reply', data={'body': 'Missing'}).status_code, 404)
        self.login(1)
        self.client.post('/moderation/room/messages/1/delete')
        self.assertEqual(ModRoomReply.query.count(), 0)

    def test_regular_users_cannot_reply(self):
        from app.models import ModRoomReply
        db.session.add(User(id=3, username='member', email='member@test.example', password_hash='test'))
        db.session.add(ModRoomMessage(id=1, user_id=1, body='Private note'))
        db.session.commit()
        self.login(3)
        self.assertEqual(self.client.post('/moderation/room/messages/1/reply', data={'body': 'No'}).status_code, 403)
        self.assertEqual(ModRoomReply.query.count(), 0)

    def test_note_and_reply_reactions(self):
        from app.models import ModRoomReply, ModRoomReaction
        note = ModRoomMessage(id=1, user_id=1, body='React here')
        db.session.add(note)
        db.session.add(ModRoomReply(id=1, note=note, user_id=2, body='Reply here'))
        db.session.commit()
        self.login(2)
        path = '/moderation/room/notes/1/react'
        self.assertEqual(self.client.post(path, data={'reaction_type': 'like'}).status_code, 302)
        self.assertEqual(ModRoomReaction.query.one().reaction_type, 'like')
        self.client.post(path, data={'reaction_type': 'love'})
        self.assertEqual(ModRoomReaction.query.one().reaction_type, 'love')
        self.login(1)
        self.client.post(path, data={'reaction_type': 'love'})
        page = self.client.get('/moderation/room').get_data(as_text=True)
        self.assertIn('Love (2)', page)
        self.assertIn('aria-pressed="true"', page)
        self.client.post(path, data={'reaction_type': 'love'})
        self.assertEqual(ModRoomReaction.query.count(), 1)
        self.client.post('/moderation/room/replies/1/react', data={'reaction_type': 'haha'})
        self.assertEqual(ModRoomReaction.query.filter_by(reply_id=1).count(), 1)
        self.assertEqual(self.client.post(path, data={'reaction_type': 'invalid'}).status_code, 400)
        self.assertEqual(self.client.post('/moderation/room/notes/999/react', data={'reaction_type': 'like'}).status_code, 404)
        self.client.post('/moderation/room/messages/1/delete')
        self.assertEqual(ModRoomReaction.query.count(), 0)

    def test_reactions_require_moderator(self):
        from app.models import ModRoomReaction
        db.session.add(User(id=3, username='member', email='member@test.example', password_hash='test'))
        db.session.add(ModRoomMessage(id=1, user_id=1, body='Private'))
        db.session.commit()
        self.login(3)
        for kind in ['notes', 'replies']:
            self.assertEqual(self.client.post(f'/moderation/room/{kind}/1/react', data={'reaction_type': 'like'}).status_code, 403)
        self.assertEqual(ModRoomReaction.query.count(), 0)

    def test_profile_last_activity_tracks_signed_in_visits(self):
        from datetime import datetime
        from unittest.mock import patch
        fixed = datetime(2026, 11, 5, 14, 23, 10)
        self.login(2)
        with patch('app.datetime') as clock:
            clock.utcnow.return_value = fixed
            self.assertEqual(self.client.get('/user/admin').status_code, 200)
            self.assertEqual(db.session.get(User, 2).last_activity_at, fixed)
            clock.utcnow.return_value = fixed.replace(second=40)
            self.client.get('/user/admin')
            self.assertEqual(db.session.get(User, 2).last_activity_at, fixed)
            clock.utcnow.return_value = fixed.replace(minute=24)
            page = self.client.get('/user/mod').get_data(as_text=True)
            self.assertIn('14:24 05-11-26', page)
        self.assertIsNone(db.session.get(User, 1).last_activity_at)

    def test_anonymous_profile_view_does_not_update_activity(self):
        from datetime import datetime
        db.session.get(User, 1).last_activity_at = datetime(2026, 11, 5, 9, 7)
        db.session.commit()
        page = self.client.get('/user/admin').get_data(as_text=True)
        self.assertIn('Last Activity:', page)
        self.assertIn('09:07 05-11-26', page)
        self.assertEqual(db.session.get(User, 1).last_activity_at, datetime(2026, 11, 5, 9, 7))
        page = self.client.get('/user/mod').get_data(as_text=True)
        self.assertIn('Last Activity:', page)
        self.assertIsNone(db.session.get(User, 2).last_activity_at)

    def test_reactions_return_inline_updates(self):
        from app.models import ModRoomReply
        note = ModRoomMessage(id=1, user_id=1, body='Note')
        db.session.add(note)
        db.session.add(ModRoomReply(id=1, note=note, user_id=2, body='Reply'))
        db.session.commit()
        self.login(2)
        headers = {'Accept': 'application/json'}
        for kind in ['notes', 'replies']:
            path = f'/moderation/room/{kind}/1/react'
            response = self.client.post(path, data={'reaction_type': 'like'}, headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json, {'counts': {'like': 1}, 'user_reaction': 'like'})
            response = self.client.post(path, data={'reaction_type': 'love'}, headers=headers)
            self.assertEqual(response.json, {'counts': {'love': 1}, 'user_reaction': 'love'})
            response = self.client.post(path, data={'reaction_type': 'love'}, headers=headers)
            self.assertEqual(response.json, {'counts': {}, 'user_reaction': None})

    def test_serial_downvoters_ranking_window_and_ratio(self):
        from datetime import datetime, timedelta
        from app.models import Vote
        from app.routes import get_serial_downvoters
        now = datetime(2026, 11, 5, 12)
        for uid in range(3, 10):
            db.session.add(User(id=uid, username=f'voter{uid}', email=f'{uid}@test.example', password_hash='test'))
        for jid in range(1, 25):
            db.session.add(Joke(id=jid, body=f'Joke {jid}', user_id=1))
        db.session.flush()
        for uid in range(2, 9):
            for jid in range(1, 11-uid):
                db.session.add(Vote(user_id=uid, joke_id=jid, value=-1, created_at=now-timedelta(days=1)))
        db.session.add_all([
            Vote(user_id=2, joke_id=20, value=2, created_at=now),
            Vote(user_id=2, joke_id=21, value=1, created_at=now),
            Vote(user_id=2, joke_id=22, value=-1, created_at=now-timedelta(days=8)),
            Vote(user_id=2, joke_id=23, value=-1, created_at=now+timedelta(days=1)),
            Vote(user_id=1, joke_id=1, value=1, created_at=now),
        ])
        db.session.commit()
        entries = get_serial_downvoters(now)
        self.assertEqual([e['user'].id for e in entries], [2, 3, 4, 5, 6])
        self.assertEqual(entries[0]['downvotes'], 8)
        self.assertEqual(entries[0]['upvotes'], 2)
        self.assertEqual(entries[0]['downvote_percent'], 80)
        self.assertEqual(len(entries[0]['jokes']), 8)
        self.assertEqual(entries[1]['upvotes'], 0)
        self.assertEqual(entries[0]['periods'], [
            {'label': 'Week (7 days)', 'upvotes': 2, 'downvotes': 8},
            {'label': 'Month (30 days)', 'upvotes': 2, 'downvotes': 9},
            {'label': 'All time', 'upvotes': 2, 'downvotes': 9},
        ])
        db.session.add(Vote(user_id=2, joke_id=24, value=-1, created_at=now-timedelta(days=40)))
        db.session.commit()
        periods = get_serial_downvoters(now)[0]['periods']
        self.assertEqual(periods[1]['downvotes'], 9)
        self.assertEqual(periods[2]['downvotes'], 10)


    def test_sockpuppet_detector_excludes_test_accounts(self):
        from app.models import LoginAudit
        from app.routes import get_sockpuppet_matches
        for uid, name in enumerate(['Supergalley', 'qwertyuiop', 'wasp', 'vapeman', 'realperson'], 3):
            db.session.add(User(id=uid, username=name, email=f'{uid}@test.example', password_hash='test'))
        db.session.flush()
        for uid, ip in [(2, 'shared'), (3, 'shared'), (4, 'shared'), (5, 'shared'), (6, 'shared'), (7, 'shared')]:
            db.session.add(LoginAudit(user_id=uid, ip=ip, success=True))
        db.session.commit()
        clusters = get_sockpuppet_matches()
        self.assertEqual(len(clusters), 1)
        self.assertEqual({u.username for u in clusters[0]['users']}, {'mod', 'realperson'})
        self.assertEqual(clusters[0]['user_count'], 2)

    def test_empty_bans_move_below_noticeboard_and_warnings_wrap(self):
        from app.models import UserWarning
        from datetime import datetime, timedelta
        reason = 'Upvote to Downvote ratio is not in the expected range'
        notes = 'Full context\nSecond line of context'
        db.session.add(UserWarning(user_id=2, issuer_id=1, reason=reason, notes=notes))
        db.session.commit()
        self.login(1)
        page = self.client.get('/moderation/room').get_data(as_text=True)
        self.assertGreater(page.index('Currently Banned Members'), page.index('Mod Team Noticeboard'))
        self.assertIn(reason, page)
        self.assertIn(notes, page)
        self.assertNotIn('max-height: 280px', page)
        self.assertIn('Serial Downvoters', page)
        db.session.get(User, 2).ban_until = datetime.utcnow() + timedelta(days=1)
        db.session.commit()
        page = self.client.get('/moderation/room').get_data(as_text=True)
        self.assertLess(page.index('Currently Banned Members'), page.index('Mod Team Noticeboard'))

    def queue_get(self):
        response = self.client.get('/moderation/room/queues', headers={'Accept': 'application/json'})
        self.assertEqual(response.status_code, 200)
        return response.json

    def queue_post(self, path, data=None, status=200):
        response = self.client.post(path, data=data or {}, headers={'Accept': 'application/json'})
        self.assertEqual(response.status_code, status)
        return response.json

    def test_embedded_review_loads_next_without_redirect(self):
        from app.models import Vote
        # Only unseen findings receive a glow; acknowledged findings remain reviewable.
        first = Joke(id=1, body='First complete submission\nMore text', user_id=2, review_status='pending')
        second = Joke(id=2, body='Next submission', user_id=2, review_status='pending',
                      is_meme=True, image_filename='test-meme.webp', is_clip=True, video_filename='test-clip.mp4')
        db.session.add_all([first, second])
        db.session.commit()
        self.login(1)
        initial = self.queue_get()
        self.assertIn('First complete submission\nMore text', initial['queues']['review'])
        self.assertNotIn('Next submission', initial['queues']['review'])
        result = self.queue_post('/moderation/review/1/approve')
        self.assertEqual(db.session.get(Joke, 1).review_status, 'approved')
        self.assertEqual(Vote.query.filter_by(joke_id=1).count(), 1)
        self.assertIn('Next submission', result['queues']['review'])
        self.assertIn('test-meme.webp', result['queues']['review'])
        self.assertIn('test-clip.mp4', result['queues']['review'])
        self.assertEqual(result['notifications']['pending_review_count'], 1)
        self.queue_post('/moderation/review/1/approve', status=409)
        self.assertEqual(Vote.query.filter_by(joke_id=1).count(), 1)
        self.assertEqual(db.session.get(User, 2).reputation, 1)

    def test_embedded_reject_notifies_and_empty_queue_removes_glow(self):
        from app.models import Message
        db.session.add(Joke(id=1, body='Rejected submission', user_id=2, review_status='pending'))
        db.session.commit()
        self.login(1)
        error = self.queue_post('/moderation/review/1/reject', status=400)
        self.assertIn('reason', error['error'])
        self.assertEqual(db.session.get(Joke, 1).review_status, 'pending')
        result = self.queue_post('/moderation/review/1/reject', {'reasons': 'Low Effort'})
        self.assertEqual(db.session.get(Joke, 1).review_status, 'rejected')
        self.assertIn('Low Effort', Message.query.one().body)
        self.assertIn('No submissions waiting', result['queues']['review'])
        self.assertNotIn('moderator-review-glow', result['queues']['review'])
        self.assertEqual(result['notifications']['pending_review_count'], 0)
        self.queue_post('/moderation/review/1/reject', {'reasons': 'Spam'}, status=409)
        self.assertEqual(Message.query.count(), 1)

    def test_embedded_duplicates_archive_newer_then_load_next(self):
        from app.models import ArchivedJoke, DupeModerationLog
        db.session.add_all([
            Joke(id=10, body='Original post body', user_id=1),
            Joke(id=11, body='Newer post body', user_id=2),
            Joke(id=12, body='Medium post body', user_id=2),
        ])
        db.session.commit()
        self.login(1)
        initial = self.queue_get()['queues']['duplicates']
        self.assertLess(initial.index('Newer post body'), initial.index('Original post body'))
        self.assertIn('Not Duplicate', initial)
        self.assertIn('Duplicate 🚮', initial)
        result = self.queue_post('/moderation/duplicates/1/delete_newer')
        self.assertIsNone(db.session.get(Joke, 11))
        self.assertIsNotNone(db.session.get(Joke, 10))
        self.assertEqual(ArchivedJoke.query.count(), 1)
        self.assertEqual(DupeModerationLog.query.count(), 1)
        self.assertIn('Medium post body', result['queues']['duplicates'])
        self.assertEqual(result['notifications']['pending_dupe_count'], 1)
        self.queue_post('/moderation/duplicates/1/delete_newer', status=409)
        self.assertEqual(ArchivedJoke.query.count(), 1)
        result = self.queue_post('/moderation/duplicates/2/not_dupe')
        self.assertEqual(result['notifications']['pending_dupe_count'], 0)
        self.assertIn('No pending duplicates', result['queues']['duplicates'])
        self.assertNotIn('moderator-review-glow', result['queues']['duplicates'])
        self.assertIsNotNone(db.session.get(Joke, 12))

    def test_acknowledged_queue_items_do_not_hide_new_alerts(self):
        db.session.add_all([
            Joke(id=10, body='Original', user_id=1),
            Joke(id=11, body='Acknowledged high probability', user_id=2),
            Joke(id=12, body='Unseen medium probability', user_id=2),
            ModerationAcknowledgement(kind='duplicate', finding_id=1),
        ])
        db.session.commit()
        self.login(1)
        result = self.queue_get()
        self.assertIn('Unseen medium probability', result['queues']['duplicates'])
        self.assertNotIn('Acknowledged high probability', result['queues']['duplicates'])
        self.assertEqual(result['notifications']['pending_dupe_high_count'], 0)
        self.assertEqual(result['notifications']['pending_dupe_medium_count'], 1)
        result = self.queue_post('/moderation/room/acknowledge')
        self.assertEqual(result['notifications']['pending_dupe_count'], 0)
        self.assertNotIn('moderator-review-glow', result['queues']['duplicates'])

    def test_queue_permissions_redirects_and_log(self):
        self.login(1)
        self.assertTrue(self.client.get('/moderation/review').location.endswith('#review-queue'))
        self.assertTrue(self.client.get('/moderation/duplicates').location.endswith('#duplicates-queue'))
        self.assertEqual(self.client.get('/moderation/duplicates?view=log').status_code, 200)
        page = self.client.get('/moderation/room').get_data(as_text=True)
        self.assertNotIn('>\n      Review Queue', page)
        self.assertIn('id="review-queue"', page)
        self.assertIn('id="duplicates-queue"', page)
        self.assertIn('moderation_queues.js', page)
        db.session.add(User(id=3, username='member', email='member@test.example', password_hash='test'))
        db.session.commit()
        self.login(3)
        self.assertEqual(self.client.get('/moderation/room/queues', headers={'Accept': 'application/json'}).status_code, 403)
        for path in ['/moderation/review/1/approve', '/moderation/review/1/reject', '/moderation/duplicates/1/not_dupe']:
            self.assertEqual(self.client.post(path, headers={'Accept': 'application/json'}).status_code, 403)

    def test_removing_review_logs_activity_without_noticeboard_note(self):
        from app.models import AdminNotification
        db.session.add(User(id=3, username='member', email='member@test.example', password_hash='test', needs_moderator=True))
        db.session.add(Joke(id=1, body='Member joke', user_id=3))
        db.session.add(ModRoomMessage(user_id=1, body='Manual team note'))
        db.session.commit()
        for actor in [1, 2]:
            self.login(actor)
            for path in ['/moderation/users/3/toggle-review', '/moderation/jokes/1/toggle-author-review']:
                with self.subTest(actor=actor, path=path):
                    target = db.session.get(User, 3)
                    target.needs_moderator = True
                    target.needs_mod_reason = 'Old reason'
                    db.session.commit()
                    count = AdminNotification.query.count()
                    response = self.client.post(path)
                    self.assertEqual(response.status_code, 302)
                    self.assertFalse(db.session.get(User, 3).needs_moderator)
                    self.assertIsNone(db.session.get(User, 3).needs_mod_reason)
                    self.assertEqual(ModRoomMessage.query.count(), 1)
                    self.assertEqual(ModRoomMessage.query.one().body, 'Manual team note')
                    self.assertEqual(AdminNotification.query.count(), count + 1)
                    self.assertIn('marked as safe', AdminNotification.query.order_by(AdminNotification.id.desc()).first().message)

    def test_placing_user_on_review_keeps_existing_noticeboard_update(self):
        db.session.add(User(id=3, username='member', email='member@test.example', password_hash='test', needs_moderator=False))
        db.session.commit()
        self.login(1)
        self.client.post('/moderation/users/3/toggle-review', data={'reason': 'Spam review'})
        self.assertTrue(db.session.get(User, 3).needs_moderator)
        self.assertIn('Spam review', ModRoomMessage.query.one().body)
