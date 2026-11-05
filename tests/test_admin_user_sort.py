"""Sorting tests use only an explicitly disposable database."""
import os
import unittest
from datetime import datetime
from urllib.parse import parse_qs, urlsplit
from html.parser import HTMLParser

if not os.environ.get('jokeDB', '').startswith('/tmp/'):
    raise RuntimeError('Set jokeDB to a disposable /tmp/ database')

from flask import template_rendered
from app import app, db
from app.models import User


class Links(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.sorts = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        href = dict(attrs).get('href', '')
        args = parse_qs(urlsplit(href).query)
        if tag == 'a' and 'sort' in args:
            self.sorts[args['sort'][0]] = args['direction'][0]


class AdminUserSortTest(unittest.TestCase):
    def setUp(self):
        if not app.config['SQLALCHEMY_DATABASE_URI'].startswith('sqlite:////tmp/'):
            raise RuntimeError('Refusing to use a non-temporary database')
        self.ctx = app.app_context()
        self.ctx.push()
        app.config.update(TESTING=True, SESSION_PROTECTION=None)
        db.drop_all()
        db.create_all()
        for name, email, admin, mod, review, ban in [
            ('Zulu', 'a@test.example', True, False, True, None),
            ('alpha', 'Z@test.example', False, True, False, datetime(2030, 1, 1)),
            ('Bravo', 'b@test.example', False, False, True, datetime(2029, 1, 1)),
        ]:
            db.session.add(User(username=name, email=email, password_hash='test',
                                is_admin=admin, is_moderator=mod,
                                needs_moderator=review, ban_until=ban))
        db.session.commit()
        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session['_user_id'] = '1'
            session['_fresh'] = True
        self.rendered = []
        self.receiver = lambda sender, template, context, **kw: self.rendered.append(context)
        template_rendered.connect(self.receiver, app)

    def tearDown(self):
        template_rendered.disconnect(self.receiver, app)
        db.session.remove()
        self.ctx.pop()

    def fetch(self, query=''):
        response = self.client.get('/admin/users' + query)
        self.assertEqual(response.status_code, 200)
        return [u.username for u in self.rendered[-1]['users']], response.get_data(as_text=True)

    def test_default_and_username_are_case_insensitive(self):
        self.assertEqual(self.fetch()[0], ['alpha', 'Bravo', 'Zulu'])
        self.assertEqual(self.fetch('?sort=username&direction=desc')[0], ['Zulu', 'Bravo', 'alpha'])

    def test_all_columns_both_directions_and_ban_nulls_last(self):
        expected = {
            'id': ['Zulu', 'alpha', 'Bravo'],
            'email': ['Zulu', 'Bravo', 'alpha'],
            'admin': ['alpha', 'Bravo', 'Zulu'],
            'mod': ['Bravo', 'Zulu', 'alpha'],
            'needs_review': ['alpha', 'Bravo', 'Zulu'],
            'ban_until': ['Bravo', 'alpha', 'Zulu'],
        }
        for key, names in expected.items():
            with self.subTest(sort=key):
                self.assertEqual(self.fetch('?sort='+key)[0], names)
                descending = self.fetch('?sort='+key+'&direction=desc')[0]
                if key == 'ban_until':
                    self.assertEqual(descending, ['alpha', 'Bravo', 'Zulu'])
                else:
                    # Each column really reverses its primary key, with stable alphabetical ties.
                    values = {'id': lambda u:u.id, 'email': lambda u:u.email.lower(),
                              'admin': lambda u:u.is_admin, 'mod': lambda u:u.is_moderator,
                              'needs_review': lambda u:u.needs_moderator}
                    value = values[key]
                    users = User.query.all()
                    wanted = sorted(users, key=lambda u:(u.username.lower(), u.id))
                    wanted.sort(key=value, reverse=True)
                    self.assertEqual(descending, [u.username for u in wanted])

    def test_links_toggle_and_invalid_parameters_fall_back(self):
        names, html = self.fetch('?sort=not_a_column&direction=invalid')
        self.assertEqual(names, ['alpha', 'Bravo', 'Zulu'])
        self.assertIn('aria-sort="ascending"', html)
        links = Links(html).sorts
        self.assertEqual(set(links), {'id','username','email','admin','mod','needs_review','ban_until'})
        self.assertEqual(links['username'], 'desc')
        self.assertEqual(links['email'], 'asc')
        _, html = self.fetch('?sort=id&direction=desc')
        self.assertEqual(Links(html).sorts['id'], 'asc')
        self.assertIn('aria-sort="descending"', html)
