const assert = require('node:assert/strict');
const {test} = require('node:test');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const vm = require('node:vm');
const app = readFileSync(join(__dirname, '../views/digest/app.js'), 'utf8');
const apiSource = app.slice(app.indexOf('async function api('), app.indexOf('async function guard('));

test('native bridge unwrapped task list reaches the view', async () => {
  const data = {tasks: [], settings: {concurrency: 1}};
  const bridge = {apiPost: async (endpoint, payload) => {
    assert.equal(endpoint, 'rpc');
    assert.equal(payload.action, 'list');
    return data;
  }};
  const api = vm.runInNewContext(apiSource + '; api', {bridge});
  assert.equal(await api('list'), data);
});

test('native bridge preserves null and array results', async () => {
  for (const data of [null, []]) {
    const api = vm.runInNewContext(apiSource + '; api', {bridge: {apiPost: async () => data}});
    assert.equal(await api('run', {id: 'missing'}), data);
  }
});

test('native bridge errors retain the host message', async () => {
  const api = vm.runInNewContext(apiSource + '; api', {bridge: {apiPost: async () => {
    throw new Error('配置版本冲突');
  }}});
  await assert.rejects(api('save'), /配置版本冲突/);
});
