"""Run against a disposable database: jokeDB=/tmp/message-test.db python -m unittest discover -s tests."""
import os
import tempfile
import unittest
from io import BytesIO
from unittest.mock import patch

from flask import g
from PIL import Image
if not os.environ.get('jokeDB', '').startswith('/tmp/'):
    raise RuntimeError('Tests require an explicit disposable /tmp/ jokeDB before importing app')
from app import app, db
from app.models import Message, MessageImage, MessageBlock, User


class MessageImagesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.environ.get('jokeDB', '').startswith('/tmp/'):
            raise RuntimeError('Tests require an explicit disposable /tmp/ jokeDB')
        cls.folder = tempfile.TemporaryDirectory()
        if not app.config['SQLALCHEMY_DATABASE_URI'].startswith('sqlite:////tmp/'):
            raise RuntimeError('Refusing to test against a non-temporary database')
        app.config.update(TESTING=True, MESSAGE_IMAGE_FOLDER=cls.folder.name, SESSION_PROTECTION=None)

    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        db.drop_all()
        db.create_all()
        for name in ('sender', 'recipient', 'outsider'):
            db.session.add(User(username=name, email=name + '@example.test', password_hash='test'))
        db.session.commit()
        self.client = app.test_client()
        self.login(1)

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()

    def login(self, uid):
        g.pop("_login_user", None)
        with self.client.session_transaction() as session:
            session['_user_id'] = str(uid)
            session['_fresh'] = True

    def image(self):
        stream = BytesIO()
        Image.new('RGB', (20, 20), 'red').save(stream, 'PNG')
        stream.seek(0)
        return stream, '../../unsafe.png'

    def post(self, path, **data):
        return self.client.post(path, data=data, base_url='https://localhost')

    def test_compose_reply_access_and_delete(self):
        result = self.post('/user/recipient/message', body='hello', image=self.image())
        self.assertEqual(result.status_code, 302)
        msg = Message.query.one()
        self.assertEqual(msg.body, 'hello')
        filename = msg.attachment.filename
        self.assertNotIn('unsafe', filename)
        url = '/static/msg/' + filename
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, 'image/webp')
        self.assertEqual(response.headers['Cache-Control'], 'private, no-store')
        self.assertEqual(self.client.get('/static/message_images/' + filename).status_code, 404)
        self.login(3)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.login(2)
        self.assertEqual(self.client.get(url).status_code, 200)
        response = self.client.get('/messages/' + str(msg.id))
        self.assertEqual(response.status_code, 200)
        self.assertIn(url.encode(), response.data)
        result = self.post('/messages/' + str(msg.id), body='', image=self.image())
        self.assertEqual(result.status_code, 302)
        self.assertEqual(Message.query.count(), 2)
        self.assertEqual(MessageImage.query.count(), 2)
        self.post('/messages/' + str(msg.id) + '/delete')
        self.assertEqual(self.client.get(url).status_code, 404)
        self.login(1)
        self.assertEqual(self.client.get(url).status_code, 200)
        self.client.get('/auth/logout')
        g.pop('_login_user', None)
        self.assertNotEqual(self.client.get(url).status_code, 200)

    def test_text_empty_invalid_and_limits(self):
        self.post('/user/recipient/message', body='text only')
        self.assertEqual(Message.query.count(), 1)
        self.post('/user/recipient/message', body='')
        self.post('/user/recipient/message', body='bad', image=(BytesIO(b'bad'), 'fake.png'))
        self.post('/user/recipient/message', body='bad', image=(BytesIO(b'x' * (5*1024*1024+1)), 'big.png'))
        self.assertEqual(Message.query.count(), 1)
        self.assertEqual(MessageImage.query.count(), 0)
        response = self.post('/user/recipient/message', image=(BytesIO(b'x' * (7*1024*1024)), 'big.png'))
        self.assertEqual(response.status_code, 413)
        response = self.client.get('/user/recipient/message')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'multipart/form-data', response.data)

    def test_blocks_apply_to_compose_and_replies(self):
        self.post('/user/recipient/message', body='first')
        msg = Message.query.one()
        db.session.add(MessageBlock(blocker_id=2, blocked_id=1))
        db.session.commit()
        self.post('/user/recipient/message', image=self.image())
        self.post('/messages/' + str(msg.id), image=self.image())
        self.assertEqual(Message.query.count(), 1)
        self.assertEqual(MessageImage.query.count(), 0)

    def test_failed_commit_removes_file(self):
        before = set(os.listdir(self.folder.name))
        with patch.object(db.session, 'commit', side_effect=RuntimeError('simulated failure')):
            # Test the helper directly to avoid the thread's independent read receipt commit.
            from app.message_images import save_message
            with app.test_request_context('/', method='POST', data={'image': self.image()}):
                with self.assertRaises(RuntimeError):
                    save_message(Message(sender_id=1, recipient_id=2, body='failure'))
        self.assertEqual(set(os.listdir(self.folder.name)), before)
        self.assertEqual(Message.query.count(), 0)


if __name__ == '__main__':
    unittest.main()
