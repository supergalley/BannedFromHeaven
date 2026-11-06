"""Arcade score integrity and account isolation; disposable database only."""
import json
import os
import unittest
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path

if not os.environ.get('jokeDB', '').startswith('/tmp/'):
    raise RuntimeError('Set jokeDB to a disposable /tmp/ database')

from flask import g
from app import app, db
from app.arcade.rules import FPS, MAX_FRAMES, RULES_VERSION, ReplayError, create_game, step_game, replay_score
from app.models import User, ArcadeRun, ArcadeBestScore

REPLAYS = json.loads((Path(__file__).parent / 'fixtures' / 'arcade_replays.json').read_text())


class ArcadeRulesTest(unittest.TestCase):
    def test_recorded_positive_rounds_for_all_games(self):
        for replay in REPLAYS:
            with self.subTest(game=replay['game'], score=replay['score']):
                self.assertEqual(replay_score(replay['game'], replay['seed'], replay['frames'], replay['events']), replay['score'])

    def test_incomplete_and_invalid_replays_are_rejected(self):
        for frames, events in [(1, []), (20, [[0, 2]]), (20, [[0, 1], [0, 0]]),
                               (20, [[20, 1]]), (True, []), (MAX_FRAMES + 1, [])]:
            with self.subTest(frames=frames, events=events), self.assertRaises(ReplayError):
                replay_score('flappy-devil', 1, frames, events)
        replay = REPLAYS[0]
        with self.assertRaises(ReplayError):
            replay_score(replay['game'], replay['seed'], replay['frames'] + 1, replay['events'])

    def test_snake_cannot_reverse_and_ended_game_cannot_gain_points(self):
        state = create_game('hell-serpent', 1)
        for _ in range(5): step_game(state, 8)
        self.assertEqual(state['direction'], 1)
        self.assertEqual(state['body'][0], [13, 14])
        while not state['ended']: step_game(state, 0)
        frozen = deepcopy(state)
        step_game(state, 1)
        self.assertEqual(state, frozen)

    def test_egg_hits_create_temporary_embryo_effects(self):
        replay = next(r for r in REPLAYS if r['game'] == 'spunk-invaders')
        state = create_game(replay['game'], replay['seed'])
        events = dict(replay['events'])
        mask = 0
        for frame in range(replay['frames']):
            mask = events.get(frame, mask)
            step_game(state, mask)
            if state['effects']: break
        self.assertGreater(state['score'], 0)
        self.assertTrue(any(not egg['alive'] for egg in state['eggs']))
        at = state['effects'][0]['at']
        for _ in range(23): step_game(state, 0)
        self.assertFalse(any(effect['at'] == at for effect in state['effects']))

    def test_wave_clear_advances_and_round_time_limit_is_enforced(self):
        state = create_game('spunk-invaders', 1)
        for egg in state['eggs'][:-1]: egg['alive'] = False
        target = state['eggs'][-1]
        state['shots'] = [dict(x=target['x']+110, y=target['y']+1100)]
        step_game(state, 0)
        self.assertEqual(state['wave'], 2)
        self.assertEqual(state['score'], 35)
        self.assertEqual(sum(egg['alive'] for egg in state['eggs']), 15)
        self.assertEqual(state['lives'], 3)
        self.assertEqual(replay_score('flappy-devil', 1, MAX_FRAMES, []), 0)


class ArcadeAPITest(unittest.TestCase):
    def setUp(self):
        self.ctx = app.app_context(); self.ctx.push()
        self.assertTrue(app.config['SQLALCHEMY_DATABASE_URI'].startswith('sqlite:////tmp/'))
        app.config.update(TESTING=True, SECRET_KEY='test', SESSION_PROTECTION=None)
        db.drop_all(); db.create_all()
        db.session.add_all([
            User(id=1, username='player_one', email='one@test.example', password_hash='test'),
            User(id=2, username='player_two', email='two@test.example', password_hash='test'),
        ])
        db.session.commit()
        self.client = app.test_client()
        self.login(1)
        response = self.client.get('/games')
        self.assertEqual(response.status_code, 200)
        with self.client.session_transaction() as session: self.csrf = session['arcade_csrf']
        self.replay = REPLAYS[1]

    def tearDown(self):
        db.session.remove(); self.ctx.pop()

    def login(self, uid):
        g.pop('_login_user', None)
        with self.client.session_transaction() as session:
            session['_user_id'] = str(uid); session['_fresh'] = True

    def post(self, path, data=None):
        return self.client.post(path, json=data or {}, headers={'X-Arcade-CSRF': self.csrf})

    def prepare(self, replay=None):
        replay = replay or self.replay
        response = self.post('/games/' + replay['game'] + '/start')
        self.assertEqual(response.status_code, 200)
        run = db.session.get(ArcadeRun, response.json['run_id'])
        run.seed = replay['seed']
        run.started_at = datetime.utcnow() - timedelta(seconds=replay['frames'] / FPS + 3)
        db.session.commit()
        return dict(run_id=run.id, frames=replay['frames'], events=deepcopy(replay['events']), score=replay['score'])

    def test_games_page_login_and_csrf(self):
        page = self.client.get('/games').get_data(as_text=True)
        for name in ['Fappy Turd', 'Spunk Invaders', 'Hell Serpent']:
            self.assertIn(name, page)
        self.assertIn('arcade/core.js', page)
        self.assertEqual(self.client.post('/games/flappy-devil/start', json={}).status_code, 403)
        g.pop('_login_user', None)
        self.assertEqual(app.test_client().get('/games').status_code, 302)
        g.pop('_login_user', None)
        self.assertEqual(app.test_client().get('/games/flappy-devil/leaderboard').status_code, 302)
        self.login(1)
        self.assertEqual(self.post('/games/not-a-game/start').status_code, 404)

    def test_each_game_accepts_verified_scores_and_separate_boards(self):
        for replay in REPLAYS[::2]:
            data = self.prepare(replay)
            result = self.post('/games/' + replay['game'] + '/score', data)
            self.assertEqual(result.status_code, 200, result.get_data(as_text=True))
            self.assertEqual(result.json['score'], replay['score'])
            self.assertTrue(result.json['new_best'])
            self.assertIn('player_one', result.json['leaderboard_html'])
            self.assertEqual(result.json['rank'], 1)
        self.assertEqual(ArcadeBestScore.query.count(), 3)

    def test_forged_score_is_rejected_without_consuming_valid_round(self):
        data = self.prepare()
        wrong = dict(data, score=999999)
        response = self.post('/games/flappy-devil/score', wrong)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(ArcadeBestScore.query.count(), 0)
        self.assertIsNone(db.session.get(ArcadeRun, data['run_id']).consumed_at)
        self.assertEqual(self.post('/games/flappy-devil/score', data).status_code, 200)

    def test_other_accounts_and_game_ids_cannot_use_the_round(self):
        data = self.prepare()
        self.login(2)
        self.assertEqual(self.post('/games/flappy-devil/score', data).status_code, 404)
        self.login(1)
        self.assertEqual(self.post('/games/hell-serpent/score', data).status_code, 404)
        self.assertEqual(ArcadeBestScore.query.count(), 0)

    def test_duplicate_submission_is_idempotent_but_modified_reuse_is_rejected(self):
        data = self.prepare()
        self.assertEqual(self.post('/games/flappy-devil/score', data).status_code, 200)
        self.assertEqual(self.post('/games/flappy-devil/score', data).status_code, 200)
        self.assertEqual(ArcadeBestScore.query.count(), 1)
        self.assertEqual(self.post('/games/flappy-devil/score', dict(data, score=data['score']+1)).status_code, 409)
        self.assertEqual(ArcadeBestScore.query.one().score, data['score'])

    def test_lower_or_equal_round_does_not_replace_personal_best(self):
        data = self.prepare(REPLAYS[1]);self.post('/games/flappy-devil/score', data)
        achieved = ArcadeBestScore.query.one().achieved_at
        low = self.prepare(REPLAYS[0])
        result = self.post('/games/flappy-devil/score', low)
        self.assertFalse(result.json['new_best'])
        self.assertEqual(result.json['personal_best'], REPLAYS[1]['score'])
        self.assertEqual(ArcadeBestScore.query.one().achieved_at, achieved)
        same = self.prepare(REPLAYS[1]);result = self.post('/games/flappy-devil/score', same)
        self.assertFalse(result.json['new_best'])
        self.assertEqual(ArcadeBestScore.query.count(), 1)

    def test_leaderboard_shows_one_best_per_user_with_deterministic_ties(self):
        self.post('/games/flappy-devil/score', self.prepare(REPLAYS[1]))
        self.login(2)
        result = self.post('/games/flappy-devil/score', self.prepare(REPLAYS[1]))
        self.assertEqual(result.json['rank'], 2)
        html = result.json['leaderboard_html']
        self.assertLess(html.index('player_one'), html.index('player_two'))
        self.assertEqual(ArcadeBestScore.query.count(), 2)
        self.assertEqual(self.client.get('/games/hell-serpent/leaderboard').json['personal_best'], 0)

    def test_expired_sped_up_and_replaced_rounds_are_rejected(self):
        data = self.prepare()
        run = db.session.get(ArcadeRun, data['run_id']);run.started_at=datetime.utcnow();db.session.commit()
        self.assertEqual(self.post('/games/flappy-devil/score', data).status_code, 400)
        run.started_at=datetime.utcnow()-timedelta(minutes=31);db.session.commit()
        self.assertEqual(self.post('/games/flappy-devil/score', data).status_code, 409)
        newer = self.prepare()
        self.post('/games/flappy-devil/start')
        self.assertEqual(self.post('/games/flappy-devil/score', newer).status_code, 409)
        self.assertEqual(ArcadeBestScore.query.count(), 0)

    def test_rate_limit_and_old_rule_versions(self):
        for _ in range(30): self.assertEqual(self.post('/games/flappy-devil/start').status_code, 200)
        self.assertEqual(self.post('/games/flappy-devil/start').status_code, 429)
        self.assertEqual(ArcadeRun.query.filter_by(consumed_at=None).count(), 1)
        self.login(2)
        data = self.prepare()
        run = db.session.get(ArcadeRun, data['run_id']);run.rules_version=RULES_VERSION+1;db.session.commit()
        self.assertEqual(self.post('/games/flappy-devil/score', data).status_code, 409)

    def test_malformed_score_events_and_unfinished_round_do_not_save(self):
        data = self.prepare()
        for change in [dict(frames=True), dict(events=[[0, True]]), dict(events=[[0, 1], [0, 0]]),
                       dict(frames=MAX_FRAMES+1), dict(score=True), dict(frames=1, events=[], score=0)]:
            response = self.post('/games/flappy-devil/score', dict(data, **change))
            self.assertEqual(response.status_code, 400)
        self.assertEqual(ArcadeBestScore.query.count(), 0)
