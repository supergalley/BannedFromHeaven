# SPDX-License-Identifier: MIT
"""Deterministic, integer-only arcade rules, mirrored by static/arcade/core.js."""
FPS = 30
MAX_FRAMES = FPS * 60 * 5
RULES_VERSION = 1
GAME_INPUTS = {
    "flappy-devil": {0, 1},
    "spunk-invaders": set(range(8)),
    "hell-serpent": {0, 1, 2, 4, 8},
}
DIRECTIONS = ((0, -1), (1, 0), (0, 1), (-1, 0))


class ReplayError(ValueError):
    pass


def random_number(state):
    state["rng"] = (1664525 * state["rng"] + 1013904223) & 0xFFFFFFFF
    return state["rng"]


def eggs():
    return [dict(x=6400 + col * 8500, y=7400 + row * 6200, alive=True)
            for row in range(3) for col in range(5)]


def place_food(state):
    free = 24 * 28 - len(state["body"])
    if not free:
        state.update(ended=True, reason="cleared", food=None)
        return
    index = random_number(state) % free
    occupied = {tuple(cell) for cell in state["body"]}
    for y in range(28):
        for x in range(24):
            if (x, y) not in occupied:
                if index == 0:
                    state["food"] = [x, y]
                    return
                index -= 1


def create_game(game, seed):
    state = dict(game=game, frame=0, score=0, ended=False, reason="", rng=seed & 0xFFFFFFFF)
    if game == "flappy-devil":
        state.update(y=30000, vy=0, started=False, flight=0, pipes=[], last_input=0)
    elif game == "spunk-invaders":
        state.update(x=24000, lives=3, wave=1, direction=1, eggs=eggs(), shots=[], bombs=[],
                     effects=[], next_shot=0, next_bomb=30, invulnerable_until=0)
    elif game == "hell-serpent":
        state.update(body=[[12, 14], [11, 14], [10, 14], [9, 14]], direction=1,
                     wanted=1, countdown=5, apples=0, food=None)
        place_food(state)
    else:
        raise ReplayError("Unknown game.")
    return state


def step_flappy(state, controls):
    if controls & 1 and not state["last_input"] & 1:
        state["started"] = True
        state["vy"] = -750
    state["last_input"] = controls
    if not state["started"]:
        return
    state["vy"] += 75
    state["y"] += state["vy"]
    if state["flight"] % 88 == 0:
        state["pipes"].append(dict(x=52000, center=16000 + random_number(state) % 27001, passed=False))
    state["flight"] += 1
    for pipe in state["pipes"]:
        pipe["x"] -= 260
        touching = pipe["x"] < 12600 and pipe["x"] + 6800 > 9800
        if touching and (state["y"] - 1400 < pipe["center"] - 9000
                         or state["y"] + 1400 > pipe["center"] + 9000):
            state.update(ended=True, reason="crash")
    if state["y"] <= 1400 or state["y"] >= 58600:
        state.update(ended=True, reason="crash")
    if not state["ended"]:
        for pipe in state["pipes"]:
            if not pipe["passed"] and pipe["x"] + 6800 < 11200:
                pipe["passed"] = True
                state["score"] += 1
    state["pipes"] = [pipe for pipe in state["pipes"] if pipe["x"] > -7000]


def step_invaders(state, controls):
    state["x"] = max(2200, min(45800, state["x"] + (bool(controls & 2) - bool(controls & 1)) * 650))
    if controls & 4 and state["frame"] >= state["next_shot"]:
        state["shots"].append(dict(x=state["x"], y=55100))
        state["next_shot"] = state["frame"] + 8
    alive = [egg for egg in state["eggs"] if egg["alive"]]
    speed = 90 + min(state["wave"], 8) * 20
    dx = speed * state["direction"]
    turn = min(egg["x"] for egg in alive) + dx < 2800 or max(egg["x"] for egg in alive) + dx > 45200
    if turn:
        state["direction"] *= -1
    for egg in alive:
        if turn:
            egg["y"] += 2200
        else:
            egg["x"] += dx
    state["effects"] = [effect for effect in state["effects"] if state["frame"] - effect["at"] < 22]
    shots = []
    for shot in state["shots"]:
        shot["y"] -= 1100
        hit = next((egg for egg in state["eggs"] if egg["alive"]
                    and abs(shot["x"] - egg["x"]) <= 2100 and abs(shot["y"] - egg["y"]) <= 2700), None)
        if hit:
            hit["alive"] = False
            state["score"] += 10
            state["effects"].append(dict(x=hit["x"], y=hit["y"], at=state["frame"]))
        elif shot["y"] > -2000:
            shots.append(shot)
    state["shots"] = shots
    alive = [egg for egg in state["eggs"] if egg["alive"]]
    if alive and state["frame"] >= state["next_bomb"]:
        source = alive[random_number(state) % len(alive)]
        state["bombs"].append(dict(x=source["x"], y=source["y"] + 2600))
        state["next_bomb"] = state["frame"] + max(14, 42 - state["wave"] * 3)
    bombs = []
    for bomb in state["bombs"]:
        bomb["y"] += 430 + state["wave"] * 20
        hit = abs(bomb["x"] - state["x"]) <= 2400 and abs(bomb["y"] - 57400) <= 1800
        if hit and state["frame"] >= state["invulnerable_until"]:
            state["lives"] -= 1
            state["invulnerable_until"] = state["frame"] + 45
        elif bomb["y"] < 64000:
            bombs.append(bomb)
    state["bombs"] = bombs
    if state["lives"] <= 0:
        state.update(ended=True, reason="hit")
    elif alive and max(egg["y"] for egg in alive) >= 54600:
        state.update(ended=True, reason="invasion")
    elif not alive:
        state["score"] += 25
        state["wave"] += 1
        state["eggs"] = eggs()
        state["direction"] = 1
        state["bombs"] = []
        state["next_bomb"] = state["frame"] + 35


def step_snake(state, controls):
    wanted = {1: 0, 2: 1, 4: 2, 8: 3}.get(controls)
    if wanted is not None and (wanted + 2) % 4 != state["direction"]:
        state["wanted"] = wanted
    state["countdown"] -= 1
    if state["countdown"] > 0:
        return
    state["direction"] = state["wanted"]
    dx, dy = DIRECTIONS[state["direction"]]
    head = [state["body"][0][0] + dx, state["body"][0][1] + dy]
    growing = head == state["food"]
    if not 0 <= head[0] < 24 or not 0 <= head[1] < 28:
        state.update(ended=True, reason="wall")
        return
    if head in (state["body"] if growing else state["body"][:-1]):
        state.update(ended=True, reason="tail")
        return
    state["body"].insert(0, head)
    if growing:
        state["apples"] += 1
        state["score"] += 10
        place_food(state)
    else:
        state["body"].pop()
    state["countdown"] = max(2, 5 - state["apples"] // 5)


def step_game(state, controls):
    if state["ended"]:
        return
    if state["game"] == "flappy-devil":
        step_flappy(state, controls)
    elif state["game"] == "spunk-invaders":
        step_invaders(state, controls)
    else:
        step_snake(state, controls)
    state["frame"] += 1
    if state["frame"] >= MAX_FRAMES and not state["ended"]:
        state.update(ended=True, reason="time")


def validate_trace(game, frames, events):
    if type(frames) is not int or not 1 <= frames <= MAX_FRAMES:
        raise ReplayError("Invalid round length.")
    if not isinstance(events, list) or len(events) > frames:
        raise ReplayError("Invalid input replay.")
    previous = -1
    for event in events:
        if (not isinstance(event, list) or len(event) != 2
                or type(event[0]) is not int or type(event[1]) is not int
                or not previous < event[0] < frames or event[0] < 0
                or event[1] not in GAME_INPUTS[game]):
            raise ReplayError("Invalid input replay.")
        previous = event[0]


def replay_score(game, seed, frames, events):
    validate_trace(game, frames, events)
    state = create_game(game, seed)
    index, controls = 0, 0
    for frame in range(frames):
        if index < len(events) and events[index][0] == frame:
            controls = events[index][1]
            index += 1
        step_game(state, controls)
        if state["ended"] and frame != frames - 1:
            raise ReplayError("The replay continued after the round ended.")
    if not state["ended"]:
        raise ReplayError("Finish the round before saving a score.")
    return state["score"]
