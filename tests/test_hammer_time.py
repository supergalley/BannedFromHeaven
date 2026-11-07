"""Hammer-Time platform physics and server-verified scoring."""
import os
import unittest

if not os.environ.get('jokeDB', '').startswith('/tmp/'):
    raise RuntimeError('Set jokeDB to a disposable /tmp/ database')
from app.arcade.rules import create_game, step_game, replay_score, validate_trace, ReplayError


class HammerTimeTest(unittest.TestCase):
    def test_jump_lands_on_ledge_and_camera_scrolls(self):
        s = create_game('hammer-time', 3)
        step_game(s, 0)
        s['x'] = 31000
        step_game(s, 4)
        self.assertLess(s['y'], 55000)
        for _ in range(40): step_game(s, 0)
        self.assertTrue(s['grounded'])
        self.assertIn(s['y'], [p['y'] for p in s['platforms']])
        for _ in range(100): step_game(s, 10)
        self.assertGreater(s['camera'], 0)
        self.assertLessEqual(s['x']-s['camera'], 15000)

    def test_hit_scores_once_and_swing_cooldown(self):
        s = create_game('hammer-time', 1); step_game(s, 0)
        s['opponents'] = [dict(x=s['x']+5000,y=s['y'],kind=0,left=0,right=100000)]
        step_game(s, 8)
        self.assertEqual(s['swing'], 12)
        for _ in range(5): step_game(s, 8)
        self.assertEqual(s['score'], 10)
        self.assertEqual(s['hits'], 1)
        self.assertEqual(s['opponents'], [])
        self.assertLess(s['swing'], 12)
        for _ in range(10): step_game(s, 8)
        self.assertEqual(s['score'], 10)

    def test_contact_damage_invulnerability_and_game_over(self):
        s = create_game('hammer-time', 1); step_game(s, 0)
        s['opponents'] = [dict(x=s['x'],y=s['y'],kind=1,left=s['x'],right=s['x'])]
        step_game(s, 0)
        self.assertEqual(s['lives'], 2)
        for _ in range(59): step_game(s, 0)
        self.assertEqual(s['lives'], 2)
        while not s['ended']: step_game(s, 0)
        self.assertEqual(s['reason'], 'hammer-hit')

    def test_idle_round_time_limit_and_invalid_input(self):
        s = create_game('hammer-time', 1)
        while not s['ended']: step_game(s, 0)
        self.assertEqual(s['frame'], 2700)
        self.assertEqual(s['reason'], 'hammer-time')
        self.assertEqual(replay_score('hammer-time', 1, 2700, []), 0)
        with self.assertRaises(ReplayError): validate_trace('hammer-time', 5, [[0,16]])
        with self.assertRaises(ReplayError): replay_score('hammer-time', 1, 20, [])

    def test_seeded_random_types_and_platforms_are_reproducible(self):
        def run(seed):
            s = create_game('hammer-time', seed)
            for _ in range(200): step_game(s, 10)
            return s
        self.assertEqual(run(7), run(7))
        self.assertNotEqual(run(7)['opponents'], run(42)['opponents'])
