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

test('reply window settings load and save through the management form', async () => {
  const html = readFileSync(join(__dirname, '../views/digest/index.html'), 'utf8');
  const nodes = {};
  for (const match of html.matchAll(/<input\b[^>]*id="([^"]+)"[^>]*>/g)) {
    nodes[match[1]] = {value: match[0].match(/value="([^"]*)"/)?.[1] || ''};
  }
  nodes['settings-save'] = {};
  for (const id of ['settings-open','settings-close','settings-dismiss','settings-form']) nodes[id] = {};
  nodes['settings-dialog'] = {close(){this.closed=true;},showModal(){this.closed=false;}};
  const settings = {concurrency:1, retention_days:30, text_limit:1500, send_interval:1,
    generation_timeout:1200, call_budget:64, reply_window_minutes:8, reply_message_limit:6};
  const $ = id => {
    assert.ok(nodes[id], `missing form field ${id}`);
    return nodes[id];
  };
  const requests = [];
  const api = async (action, payload) => {
    if (action === 'list') return {tasks: [], settings};
    requests.push({action, payload});
  };
  const reloadSource = app.slice(app.indexOf('async function reload('), app.indexOf('async function save('));
  const settingsSource = app.slice(app.indexOf('const settingFields='), app.indexOf("$('archive').onclick="));
  const reload = vm.runInNewContext(`let tasks=[],savedSettings={},initialized=true; ${reloadSource}\n${settingsSource}\nreload`, {
    $, api, renderTasks(){}, message(){}, guard: async fn => fn(),
  });
  await reload();
  assert.equal(nodes['reply-window'].value, 8);
  assert.equal(nodes['reply-limit'].value, 6);
  nodes['reply-window'].value = '5';
  nodes['reply-limit'].value = '3';
  await nodes['settings-form'].onsubmit({preventDefault(){}});
  assert.equal(requests[0].action, 'settings');
  assert.equal(requests[0].payload.values.reply_window_minutes, 5);
  assert.equal(requests[0].payload.values.reply_message_limit, 3);
  assert.equal(nodes['settings-dialog'].closed, true);
  nodes['reply-window'].value = '99';
  nodes['settings-open'].onclick();
  assert.equal(nodes['reply-window'].value, 5, 'opening discards unsaved field edits');
});

test('native conflict choices submit the selected job without a generic confirmation', async () => {
  const source=app.slice(app.indexOf('function nativeConflictCard('),app.indexOf('async function related('));
  const requests=[],children=[];
  const render=vm.runInNewContext(source+'; nativeConflictCard',{
    api:async(action,payload)=>{requests.push({action,payload});return {message:'done'};},
    message(){},related:async()=>{},
    textNode:(tag,text)=>({tag,text}),actionButton:(label,fn)=>({label,fn}),
  });
  render({append(...items){children.push(...items);}},{id:'check-1',state:'pending',proposal:{candidates:[
    {id:'native-1',name:'已有会议',at:'2026-12-01T08:00:00Z',enabled:false,run_once:true},
  ]}});
  const buttons=children.filter(item=>item.fn);
  assert.equal(buttons.length,3);
  for(const button of buttons)await button.fn();
  assert.equal(requests[0].payload.values.native_choice,'reuse');
  assert.equal(requests[0].payload.values.native_job_id,'native-1');
  assert.equal(requests[1].payload.values.native_choice,'create');
  assert.equal(requests[2].payload.accept,false);
});
