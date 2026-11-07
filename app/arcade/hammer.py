# SPDX-License-Identifier: MIT
"""Integer-only Hammer-Time simulation. Mirrored by static/arcade/hammer.js."""

def create_hammer(state):
    state.update(x=10000, y=55000, vy=0, facing=1, grounded=True, lives=3,
                 camera=0, chunk=0, platforms=[], opponents=[], effects=[],
                 swing=0, next_swing=0, invulnerable_until=0, last_input=0, hits=0)


def step_hammer(s, controls, random_number):
    frame = s['frame']
    move = int(bool(controls & 2)) - int(bool(controls & 1))
    if move:
        s['facing'] = move
    s['x'] = max(s['camera'] + 1800, s['x'] + move * 340)
    if controls & 4 and not s['last_input'] & 4 and s['grounded']:
        s['vy'] = -1450
        s['grounded'] = False
    previous_y = s['y']
    s['vy'] += 80
    s['y'] += s['vy']
    s['grounded'] = False
    floors = [55000] + [p['y'] for p in s['platforms']
                        if p['x'] - 1200 <= s['x'] <= p['x'] + p['w'] + 1200]
    if s['vy'] >= 0:
        landings = [y for y in floors if previous_y <= y <= s['y']]
        if landings:
            s['y'] = min(landings)
            s['vy'] = 0
            s['grounded'] = True
    s['camera'] = max(s['camera'], s['x'] - 15000)
    while s['chunk'] * 72000 < s['camera'] + 100000:
        start = s['chunk'] * 72000
        height = 45000 - (random_number(s) % 2) * 1000
        s['platforms'].append(dict(x=start + 28000, y=height, w=16000))
        for offset, y in ((36000, height), (59000, 55000)):
            s['opponents'].append(dict(x=start + offset, y=y, kind=random_number(s) % 3,
                                       left=start + offset - 6500, right=start + offset + 6500))
        s['chunk'] += 1
    s['swing'] = max(0, s['swing'] - 1)
    if controls & 8 and frame >= s['next_swing']:
        s['swing'] = 12
        s['next_swing'] = frame + 23
    active_swing = 3 <= s['swing'] <= 10
    remaining = []
    for opponent in s['opponents']:
        direction = -1 if opponent['x'] > s['x'] else 1
        opponent['x'] = max(opponent['left'], min(opponent['right'], opponent['x'] + direction * 65))
        distance = (opponent['x'] - s['x']) * s['facing']
        if active_swing and -1200 <= distance <= 8500 and abs(opponent['y'] - s['y']) <= 6500:
            s['hits'] += 1
            s['score'] += 10
            s['effects'].append(dict(x=opponent['x'], y=opponent['y'], kind=opponent['kind'],
                                     at=frame, direction=s['facing']))
            continue
        if abs(opponent['x'] - s['x']) < 2700 and abs(opponent['y'] - s['y']) < 6000 and frame >= s['invulnerable_until']:
            s['lives'] -= 1
            s['invulnerable_until'] = frame + 60
        if opponent['x'] > s['camera'] - 6000:
            remaining.append(opponent)
    s['opponents'] = remaining
    s['platforms'] = [p for p in s['platforms'] if p['x'] + p['w'] > s['camera'] - 6000]
    s['effects'] = [e for e in s['effects'] if frame - e['at'] < 24]
    s['last_input'] = controls
    if s['lives'] <= 0:
        s.update(ended=True, reason='hammer-hit')
    elif frame + 1 >= 90 * 30:
        s.update(ended=True, reason='hammer-time')
