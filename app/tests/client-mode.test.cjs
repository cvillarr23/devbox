const {test} = require('node:test');
const assert = require('node:assert/strict');
const {resolveMode} = require('../web/client-mode.js');

test('auto selects touch phones/tablets but not narrow desktop windows', () => {
  assert.equal(resolveMode('auto', true, 390), 'mobile');
  assert.equal(resolveMode('auto', true, 900), 'mobile');
  assert.equal(resolveMode('auto', false, 390), 'desktop');
  assert.equal(resolveMode('auto', true, 1400), 'desktop');
});
test('explicit override wins over device detection', () => {
  assert.equal(resolveMode('desktop', true, 390), 'desktop');
  assert.equal(resolveMode('mobile', false, 1920), 'mobile');
  assert.equal(resolveMode('invalid', false, 1920), 'desktop');
});
