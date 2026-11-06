# BannedFromHeaven arcade

Three original, self-hosted arcade games with original canvas artwork:

- **Fappy Turd:** fap through the gates; one point per gate.
- **Spunk Invaders:** swimming tadpole projectiles turn egg invaders into fading
  embryos rendered from `/static/Embryo.png`. Ten points per egg, plus 25 per wave.
- **Hell Serpent:** steer around a 24×28 garden and collect souls; ten points each.
  Its smooth, skin-tone body uses the same `Spaceship.png` asset for its rotating head.

`core.js` holds deterministic integer-only rules. `art.js` contains the original
canvas artwork; `play.js` supplies keyboard/touch controls, pausing, input recording
and score saving. The matching server rules live in `app/arcade/rules.py`.
These arcade files and the matching replay rules are covered by the MIT license
in this directory. The license does not relicense unrelated website code.
The user-supplied `app/static/Embryo.png` and `app/static/Spaceship.png` assets are
separate from the original MIT-licensed canvas artwork; the arcade code licence
does not grant rights to those images.
Artwork and UI cache updates use `ASSET_VERSION` in `app/games.py`, independently
of the gameplay rules version and existing leaderboards.

## Scores

Rounds run at 30 simulation ticks per second and finish after at most five minutes
of active simulation. The server issues a fresh seed and a round ID tied to the
signed-in account. At game over it replays the recorded controls, validates elapsed
wall time, and computes the score itself. It rejects incomplete rounds, duplicate
or invalid events, forged scores, reused round IDs and submissions from another
account. An identical retry is idempotent so a dropped response does not lose a score.
Rounds expire 30 minutes after creation. Starting another round of the same game
abandons the previous unfinished round.

Each user has one best positive score per game and rules version. Ties favour the
best set first. Replay validation prevents simply posting arbitrary numbers; it
cannot distinguish a human from a program producing valid controls. If the rules
change, increment `RULES_VERSION` in both implementations so incomparable scores
are kept on separate leaderboards.

## Validation

Run `node tests/check_arcade_js.cjs` from the repository root to replay the recorded
round fixtures in JavaScript. The same fixtures are checked on the server by
`jokeDB=/tmp/arcade-test.db python -m unittest discover -s tests -p test_arcade.py`.
Always use an explicitly disposable `/tmp/` database for tests.
