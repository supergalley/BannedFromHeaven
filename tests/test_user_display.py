"""Profile and username presentation checks against an explicitly temporary database."""
import os
import re
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

if not os.environ.get('jokeDB', '').startswith('/tmp/'):
    raise RuntimeError('Set jokeDB to a disposable /tmp/ database before running tests')

from flask import g
from app import app, db
from app.models import User, Joke, Comment
from app.user_display import display_username, new_member_names


class UserDisplayTest(unittest.TestCase):
    def setUp(self):
        if not app.config['SQLALCHEMY_DATABASE_URI'].startswith('sqlite:////tmp/'):
            raise RuntimeError('Refusing to use a non-temporary database')
        self.ctx = app.app_context()
        self.ctx.push()
        app.config['TESTING'] = True
        db.drop_all()
        db.create_all()
        self.now = datetime(2026, 10, 30, 12)
        for name, age in [('newbie', timedelta(days=29, hours=23)),
                          ('boundary', timedelta(days=30)),
                          ('veteran', timedelta(days=100)),
                          ('future', timedelta(days=-1))]:
            db.session.add(User(username=name, email=name+'@example.test',
                                password_hash='test', created_at=self.now-age,
                                reputation=-12))
        db.session.commit()
        self.clock = patch('app.user_display.datetime')
        self.mock_clock = self.clock.start()
        self.mock_clock.utcnow.return_value = self.now

    def tearDown(self):
        self.clock.stop()
        db.session.remove()
        self.ctx.pop()

    def test_strict_thirty_day_boundary_and_missing_date(self):
        db.session.execute(db.text("UPDATE users SET created_at=NULL WHERE username='veteran'"))
        db.session.commit()
        self.assertEqual(new_member_names(), {'newbie'})
        self.assertIn('✨', display_username('newbie'))
        for name in ('boundary', 'veteran', 'future', 'deleted'):
            self.assertNotIn('✨', display_username(name))
        self.assertIn('joined within the last 30 days', display_username('newbie'))

    def test_names_are_escaped_and_cache_is_per_request(self):
        self.assertEqual(str(display_username('<script>')), '&lt;script&gt;')
        first = new_member_names()
        self.assertIs(new_member_names(), first)
        g.pop('new_member_names')
        self.mock_clock.utcnow.return_value = self.now + timedelta(hours=1)
        self.assertEqual(new_member_names(), set())

    def test_profile_and_account_identifiers(self):
        response = app.test_client().get('/user/newbie')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('<strong>Joined:</strong>', html)
        self.assertIn('30 September 2026', html)
        self.assertIn('<strong>Reputation:</strong> -12', html)
        self.assertIn('<strong>Public posts:</strong> 0', html)
        self.assertIn('<strong>Public comments:</strong> 0', html)
        self.assertIn('newbie <span class="new-member-badge"', html)
        with app.test_request_context():
            from flask import url_for
            self.assertEqual(url_for('main.send_message', username='newbie'), '/user/newbie/message')
        self.assertEqual(User.query.filter_by(username='newbie').one().username, 'newbie')
        self.assertEqual(re.search(r'<title[^>]*>(.*?)</title>', html, re.S).group(1),
                         'newbie - Profile - BannedFromHeaven.com')

    def test_public_counts_exclude_hidden_content_and_other_authors(self):
        user = User.query.filter_by(username='newbie').one()
        other = User.query.filter_by(username='veteran').one()
        posts = [
            Joke(user_id=user.id, body='Joke'),
            Joke(user_id=user.id, body='', is_meme=True),
            Joke(user_id=user.id, body='', is_clip=True),
            Joke(user_id=user.id, body='Pending', review_status='pending'),
            Joke(user_id=user.id, body='Rejected', review_status='rejected'),
            Joke(user_id=user.id, body='Quarantined', is_quarantined=True),
            Joke(user_id=other.id, body='Someone else'),
        ]
        db.session.add_all(posts)
        db.session.flush()
        for post in posts:
            db.session.add(Comment(user_id=user.id, joke_id=post.id, body='Comment'))
        db.session.add(Comment(user_id=other.id, joke_id=posts[0].id, body='Other author'))
        db.session.commit()
        client = app.test_client()
        for sort in ('newest', 'highest'):
            html = client.get('/user/newbie?sort=' + sort).get_data(as_text=True)
            self.assertIn('<strong>Public posts:</strong> 3', html)
            # Includes this user's comments on other people's public posts.
            self.assertIn('<strong>Public comments:</strong> 4', html)

    def test_all_html_templates_compile(self):
        for name in app.jinja_env.list_templates():
            if name.endswith('.html') and not name.startswith('._'):
                with self.subTest(template=name):
                    app.jinja_env.get_template(name)


if __name__ == '__main__':
    unittest.main()
