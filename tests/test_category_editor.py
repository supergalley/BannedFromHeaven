"""Atomic admin category editing, always against an explicit disposable database."""
import os
import unittest
from copy import deepcopy

if not os.environ.get('jokeDB', '').startswith('/tmp/'):
    raise RuntimeError('Set jokeDB to a disposable /tmp/ database')

from flask import g
from app import app, db
from app.models import User, Category, Subcategory, Joke, JokeDraft


class CategoryEditorTest(unittest.TestCase):
    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        self.assertTrue(app.config['SQLALCHEMY_DATABASE_URI'].startswith('sqlite:////tmp/'))
        app.config.update(TESTING=True, SECRET_KEY='test', SESSION_PROTECTION=None)
        db.drop_all()
        db.create_all()
        db.session.add_all([
            User(id=1, username='admin', email='admin@test.example', password_hash='test', is_admin=True),
            User(id=2, username='mod', email='mod@test.example', password_hash='test', is_moderator=True),
            User(id=3, username='member', email='member@test.example', password_hash='test'),
            Category(id=1, name='First category', slug='first'),
            Category(id=2, name='Second category', slug='second'),
        ])
        db.session.flush()
        db.session.add_all([
            Subcategory(id=1, category_id=1, name='First topic', slug='one'),
            Subcategory(id=2, category_id=1, name='Second topic', slug='two'),
            Subcategory(id=3, category_id=2, name='Other topic', slug='other'),
        ])
        db.session.flush()
        for jid, cat, sub in [(1, 1, 1), (2, 1, 2), (3, 2, 3), (4, 2, 1)]:
            db.session.add(Joke(id=jid, user_id=3, body='Keep joke ' + str(jid), category_id=cat, subcategory_id=sub))
        db.session.add_all([
            JokeDraft(id=1, user_id=3, body='Keep draft 1', category_id=1, subcategory_id=1),
            JokeDraft(id=2, user_id=3, body='Keep draft 2', category_id=2, subcategory_id=3),
        ])
        db.session.commit()
        self.client = app.test_client()
        self.login(1)

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def login(self, uid):
        g.pop('_login_user', None)
        with self.client.session_transaction() as session:
            session['_user_id'] = str(uid)
            session['_fresh'] = True

    def snapshot(self):
        response = self.client.get('/admin/categories/editor')
        self.assertEqual(response.status_code, 200)
        return response.json['snapshot']

    def payload(self):
        snapshot = self.snapshot()
        return dict(version=snapshot['version'], categories=deepcopy(snapshot['categories']),
                    delete_category_ids=[], delete_subcategory_ids=[])

    def save(self, payload, status=200):
        response = self.client.post('/admin/categories/editor', json=payload)
        self.assertEqual(response.status_code, status, response.get_data(as_text=True)[:500])
        return response.json

    def test_page_data_and_admin_only_permissions(self):
        page = self.client.get('/admin/categories').get_data(as_text=True)
        self.assertIn('Save all changes', page)
        self.assertIn('category-editor-grid', page)
        self.assertIn('category_editor.js', page)
        data = self.snapshot()
        self.assertEqual(data['categories'][0]['posts'], 2)
        self.assertEqual(data['categories'][0]['drafts'], 1)
        self.assertEqual(data['categories'][0]['subcategories'][0]['posts'], 2)
        for uid in [2, 3]:
            self.login(uid)
            self.assertEqual(self.client.get('/admin/categories').status_code, 403)
            self.assertEqual(self.client.get('/admin/categories/editor').status_code, 403)
            self.assertEqual(self.client.post('/admin/categories/editor', json={}).status_code, 403)

    def test_bulk_subcategory_removal_and_edits_across_parents(self):
        data = self.payload()
        data['categories'][0]['name'] = 'Renamed parent'
        data['categories'][0]['subcategories'] = []
        data['delete_subcategory_ids'] = [1, 2]
        data['categories'][1]['subcategories'][0]['name'] = 'Renamed topic'
        result = self.save(data)
        self.assertNotEqual(result['snapshot']['version'], data['version'])
        self.assertEqual(db.session.get(Category, 1).name, 'Renamed parent')
        self.assertEqual(db.session.get(Subcategory, 3).name, 'Renamed topic')
        self.assertIsNone(db.session.get(Subcategory, 1))
        for jid in [1, 2, 4]:
            self.assertIsNone(db.session.get(Joke, jid).subcategory_id)
        self.assertEqual(db.session.get(Joke, 1).category_id, 1)
        self.assertEqual(db.session.get(Joke, 4).category_id, 2)
        self.assertIsNone(db.session.get(JokeDraft, 1).subcategory_id)
        self.assertEqual(Joke.query.count(), 4)
        self.assertEqual(JokeDraft.query.count(), 2)

    def test_add_category_and_multiple_children_and_generated_slugs(self):
        data = self.payload()
        data['categories'].append(dict(id=None, key='new-cat', name='New Parent', slug='', subcategories=[
            dict(id=None, key='new-sub-a', name='Topic A', slug=''),
            dict(id=None, key='new-sub-b', name='Topic B', slug='custom'),
        ]))
        result = self.save(data)
        cat = db.session.get(Category, result['created_ids']['new-cat'])
        self.assertEqual(cat.slug, 'newparent')
        self.assertEqual({s.slug for s in cat.subcategories}, {'topica', 'custom'})
        self.assertEqual(db.session.get(Subcategory, result['created_ids']['new-sub-a']).category_id, cat.id)

    def test_swapping_unique_slugs_in_one_transaction(self):
        data = self.payload()
        data['categories'][0]['slug'], data['categories'][1]['slug'] = 'second', 'first'
        children = data['categories'][0]['subcategories']
        children[0]['slug'], children[1]['slug'] = 'two', 'one'
        self.save(data)
        self.assertEqual(db.session.get(Category, 1).slug, 'second')
        self.assertEqual(db.session.get(Category, 2).slug, 'first')
        self.assertEqual(db.session.get(Subcategory, 1).slug, 'two')
        self.assertEqual(db.session.get(Subcategory, 2).slug, 'one')

    def test_validation_is_atomic_and_missing_rows_are_not_deletions(self):
        original = self.payload()
        bad = deepcopy(original)
        bad['categories'][0]['name'] = 'Must not persist'
        bad['categories'][1]['slug'] = bad['categories'][0]['slug']
        self.save(bad, 400)
        self.assertEqual(db.session.get(Category, 1).name, 'First category')
        for mutate in [
            lambda d: d.update(categories=[]),
            lambda d: d['categories'][0].update(subcategories=[]),
            lambda d: d['categories'][0]['subcategories'][0].update(id=3),
            lambda d: d.update(delete_category_ids=[999]),
            lambda d: d['categories'][0].update(name=''),
            lambda d: d['categories'][0].update(name='x' * 101),
            lambda d: d['categories'][0].update(id=True),
        ]:
            data = deepcopy(original)
            mutate(data)
            self.save(data, 400)
            self.assertEqual(Category.query.count(), 2)
            self.assertEqual(Subcategory.query.count(), 3)
        self.assertEqual(self.snapshot()['version'], original['version'])

    def test_stale_version_cannot_overwrite_another_save(self):
        original = self.payload()
        changed = deepcopy(original)
        changed['categories'][0]['name'] = 'Saved elsewhere'
        self.save(changed)
        original['categories'][1]['name'] = 'Outdated change'
        result = self.save(original, 409)
        self.assertIn('another session', result['error'])
        self.assertEqual(db.session.get(Category, 1).name, 'Saved elsewhere')
        self.assertEqual(db.session.get(Category, 2).name, 'Second category')

    def test_category_and_subcategory_removals_preserve_posts_and_drafts(self):
        data = self.payload()
        data['categories'] = data['categories'][:1]
        data['categories'][0]['subcategories'] = [s for s in data['categories'][0]['subcategories'] if s['id'] != 1]
        data['delete_category_ids'] = [2]
        data['delete_subcategory_ids'] = [1]
        self.save(data)
        self.assertIsNone(db.session.get(Category, 2))
        self.assertEqual(Subcategory.query.count(), 1)
        for jid in [3, 4]:
            self.assertIsNone(db.session.get(Joke, jid).category_id)
            self.assertIsNone(db.session.get(Joke, jid).subcategory_id)
        self.assertIsNone(db.session.get(JokeDraft, 2).category_id)
        self.assertIsNone(db.session.get(JokeDraft, 2).subcategory_id)
        self.assertEqual(db.session.get(Joke, 1).body, 'Keep joke 1')
        self.assertEqual(Joke.query.count(), 4)
        self.assertEqual(JokeDraft.query.count(), 2)

    def test_explicit_delete_all_and_empty_editor_roundtrip(self):
        data = self.payload()
        data.update(categories=[], delete_category_ids=[1, 2])
        result = self.save(data)
        self.assertEqual(result['snapshot']['categories'], [])
        self.assertEqual(Subcategory.query.count(), 0)
        self.assertEqual(Joke.query.count(), 4)
        self.assertEqual(JokeDraft.query.count(), 2)
        self.save(self.payload())

    def test_new_posts_do_not_cause_false_edit_conflicts(self):
        data = self.payload()
        db.session.add(Joke(user_id=3, body='New post while editing', category_id=1))
        db.session.commit()
        result = self.save(data)
        self.assertEqual(result['snapshot']['version'], data['version'])
        self.assertEqual(result['snapshot']['categories'][0]['posts'], 3)
