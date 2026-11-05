// Run with: node tests/check_arcade_js.cjs
// The same completed-round fixtures are replayed by the Python test suite.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const core = require('../app/static/arcade/core.js');
const replays = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures/arcade_replays.json'), 'utf8'));
for (const replay of replays) {
  const state = core.create(replay.game, replay.seed);
  let controls = 0, index = 0;
  for (let frame = 0; frame < replay.frames; frame++) {
    if (index < replay.events.length && replay.events[index][0] === frame) controls = replay.events[index++][1];
    core.step(state, controls);
  }
  assert.equal(state.ended, true, replay.game);
  assert.equal(state.frame, replay.frames, replay.game);
  assert.equal(state.score, replay.score, replay.game);
  const endedState = JSON.stringify(state);
  core.step(state, 0);
  assert.equal(JSON.stringify(state), endedState, 'Completed rounds must stay frozen');
}
console.log('All six recorded arcade rounds match the JavaScript and Python score fixtures.');
