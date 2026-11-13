"""Formatting safety, readable rollback storage and submit/draft/edit integration."""
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

if not os.environ.get('jokeDB', '').startswith('/tmp/'):
    raise RuntimeError('Set jokeDB to a disposable /tmp/ database')
from app import app, db
from app.models import User, Joke, JokeDraft
from app.joke_formatting import parse_format, prepare_joke_body, render_joke_body

DELTA = json.dumps({'ops':[{'insert':'A bold', 'attributes':{'bold':True}}, {'insert':' punchline\nNext line\n'}]})
TEXT = 'A bold punchline\nNext line'


class JokeFormattingTest(unittest.TestCase):
    def setUp(self):
        self.ctx=app.app_context(); self.ctx.push()
        app.config.update(TESTING=True, SECRET_KEY='test', SESSION_PROTECTION=None)
        db.drop_all(); db.create_all()
        self.user=User(id=1, username='writer', email='writer@example.test',password_hash='test', needs_moderator=False)
        db.session.add(self.user); db.session.commit()
        self.client=app.test_client()
        with self.client.session_transaction() as session:
            session['_user_id']='1'; session['_fresh']=True
        self.patches=[patch('app.similarity.hard_block_recent_duplicate',return_value=None),patch('app.embed_outbox.enqueue_joke_upsert')]
        for p in self.patches:p.start()

    def tearDown(self):
        for p in self.patches:p.stop()
        db.session.remove(); db.drop_all(); self.ctx.pop()

    def post(self, **extra):
        data=dict(body=TEXT, body_format=DELTA, joke_type='joke', confirm_original='1')
        data.update(extra)
        return self.client.post('/submit',data=data)

    def test_plain_storage_and_safe_rendering(self):
        text, formatting=prepare_joke_body('ignored',DELTA,self.user)
        self.assertEqual(text,TEXT)
        self.assertNotIn('<strong>', text)
        post=SimpleNamespace(body=text,body_format=formatting,author=self.user)
        self.assertEqual(str(render_joke_body(post)),'<strong>A bold</strong> punchline\nNext line')

    def test_scripts_and_non_whitelisted_formats_never_render(self):
        delta=json.dumps({'ops':[{'insert':'<script>alert(1)</script>', 'attributes':{'bold':True,'link':'javascript:alert(2)','color':'red','onclick':'x'}}]})
        body,formatting=prepare_joke_body('',delta,self.user)
        result=str(render_joke_body(SimpleNamespace(body=body,body_format=formatting,author=self.user)))
        self.assertEqual(result,'<strong>&lt;script&gt;alert(1)&lt;/script&gt;</strong>')
        self.assertNotIn('javascript:',formatting)
        self.assertNotIn('onclick',formatting)
        for raw in ('{broken',json.dumps({'ops':[{'insert':{'image':'x'}}]}),json.dumps({'ops':[{'retain':3}]})):
            self.assertIsNone(parse_format(raw))

    def test_no_format_and_stale_format_are_plain_text(self):
        self.assertEqual(prepare_joke_body('ordinary',None,self.user),('ordinary',None))
        post=SimpleNamespace(body='Changed <b>text</b>',body_format=DELTA,author=self.user)
        self.assertEqual(str(render_joke_body(post)),'Changed &lt;b&gt;text&lt;/b&gt;')

    def test_submit_keeps_plain_text_for_search_and_copy(self):
        self.assertEqual(self.post().status_code,302)
        joke=Joke.query.one()
        self.assertEqual(joke.body,TEXT)
        self.assertIsNotNone(joke.body_format)
        for path in ('/','/jokes/'+str(joke.id),'/search?q=bold'):
            response=self.client.get(path)
            self.assertEqual(response.status_code,200,path)
            self.assertIn(b'<strong>A bold</strong> punchline',response.data,path)
        self.assertEqual(db.session.execute(db.text('SELECT body FROM jokes')).scalar(),TEXT)

    def test_draft_reload_publish_and_edit(self):
        self.assertEqual(self.post(form_action='save_draft').status_code,302)
        draft=JokeDraft.query.one()
        self.assertEqual(draft.body,TEXT)
        self.assertIsNotNone(draft.body_format)
        form=self.client.get('/submit?draft='+str(draft.id)).get_data(as_text=True)
        self.assertIn('body_format',form)
        self.assertEqual(self.post(draft_id=str(draft.id)).status_code,302)
        joke=Joke.query.one()
        self.assertEqual(joke.body_format,draft.body_format)
        changed=json.dumps({'ops':[{'insert':'Edited', 'attributes':{'italic':True,'strike':True}}]})
        self.assertEqual(self.client.post(f'/jokes/{joke.id}/edit',data={'body':'Edited','body_format':changed}).status_code,302)
        db.session.refresh(joke)
        self.assertEqual(str(render_joke_body(joke)),'<s><em>Edited</em></s>')
        self.client.post(f'/jokes/{joke.id}/edit',data={'body':'Plain again'})
        db.session.refresh(joke)
        self.assertEqual(joke.body,'Plain again')
        self.assertIsNone(joke.body_format)

    def test_legacy_staff_html_still_works(self):
        self.user.is_admin=True
        body,formatting=prepare_joke_body('<b>Existing staff HTML</b>',None,self.user)
        self.assertIsNone(formatting)
        self.assertIn('<b>Existing staff HTML</b>',str(render_joke_body(SimpleNamespace(body=body,body_format=None,author=self.user))))

    def test_form_has_locally_hosted_progressive_editor(self):
        page=self.client.get('/submit').get_data(as_text=True)
        self.assertIn('vendor/quill/quill.js',page)
        self.assertIn('textarea data-joke-editor',page)
        self.assertIn('name="body_format" disabled',page)
