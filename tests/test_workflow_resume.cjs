const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../promptpilot/static/index.html'), 'utf8');
const pane = {innerHTML: '', querySelectorAll: () => [], closest: () => null};
const requests = [];
const context = vm.createContext({
  document: {getElementById: id => id === 'wfHumanNote' ? {value: 'Указания операторa'} : pane},
  esc: x => String(x).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;'),
  escAttr: String, formatTime: () => '', uiSnapScrolls: () => ({}), uiRestoreScrolls: () => {},
  workflowProviderOptions: () => '<option>herdr-session</option>',
  workflowSettingsHtml: () => '', workflowPlanHtml: () => '',
  wfPost: (action, body) => requests.push({action, body}),
  setTimeout: () => {}, wfProviderChanged: () => {}, wfRefreshScreen: () => {},
});
function load(from, until) {
  const start = html.indexOf(from);
  const end = html.indexOf(until, start);
  assert.ok(start >= 0 && end > start, from);
  vm.runInContext(html.slice(start, end), context);
}
load('function workflowStatusInfo(', 'function workflowStageEditorCard(');
load('function workflowPlannerFailure(', 'async function wfProviderChanged(');
load('function wfAdvance(', 'function wfGate(');

const wf = {status: 'queued', current_round: 1, state_version: 25, config: {
  automation: {enabled: true}, roles: {executor: {provider: 'claude', model: 'opus'}},
}};
function render() { context.renderWorkflowDetail(wf, [], [], [], [], null, []); }
render();
assert.match(pane.innerHTML, /Продолжить автоматически/);
assert.match(pane.innerHTML, /onclick="wfAdvance\(\)"/);
assert.match(pane.innerHTML, /claude · opus/);
assert.doesNotMatch(pane.innerHTML, /wfPrompt|wfProvider|wfTarget|herdr-session/);
assert.doesNotMatch(pane.innerHTML, /выбрать исполнителя и отправить промпт/);
context.wfAdvance();
assert.equal(requests[0].action, 'advance');
assert.deepEqual(Object.keys(requests[0].body), []);

wf.config.roles.executor.model = '<script>bad</script>';
render();
assert.match(pane.innerHTML, /&lt;script&gt;/);
assert.doesNotMatch(pane.innerHTML, /<script>/);
wf.config.roles.executor = {};
render();
assert.match(pane.innerHTML, /провайдер по умолчанию/);

wf.config.automation.enabled = false;
render();
assert.match(pane.innerHTML, /wfPrompt|Запустить executor/);
assert.doesNotMatch(pane.innerHTML, /Продолжить автоматически/);
wf.config.automation = {enabled: true, auto_dispatch_executor: false};
render();
assert.match(pane.innerHTML, /Запустить executor/);
assert.doesNotMatch(pane.innerHTML, /Продолжить автоматически/);

wf.config.automation.auto_dispatch_executor = true;
wf.config.stage = {execution_mode: 'external'};
render();
assert.match(pane.innerHTML, /Выдать задание внешнему исполнителю/);
delete wf.config.stage;
wf.status = 'awaiting_human';
render();
assert.match(pane.innerHTML, /Продолжить текущий раунд/);
assert.doesNotMatch(pane.innerHTML, /Запустить executor/);
context.selectedWorkflowData = wf;
context.wfResume();
assert.equal(requests[1].action, 'human-input');
assert.equal(requests[1].body.resume, true);
assert.equal(requests[1].body.expected_version, 25);
assert.equal(requests[1].body.note, 'Указания операторa');
wf.current_stage_id = 's1';
wf.pause_version = 3;
const stages = [{id:'s1', code:'S1', title:'First', status:'executing'}, {id:'s2', code:'S2', status:'pending'}];
context.renderWorkflowDetail(wf, [], [], [], [], null, stages);
assert.match(pane.innerHTML, /Пауза после текущего этапа/);
context.wfStagePause(true);
assert.equal(requests[2].action, 'stage-pause');
assert.equal(requests[2].body.expected_pause_version, 3);
assert.equal(requests[2].body.enabled, true);
wf.pause_after_stage_id = 's1';
context.renderWorkflowDetail(wf, [], [], [], [], null, stages);
assert.match(pane.innerHTML, /Пауза после S1 включена/);
assert.match(pane.innerHTML, /Отменить паузу после этапа/);
wf.status = 'paused_between_stages';
stages[0].status = 'completed';
context.renderWorkflowDetail(wf, [], [], [], [], null, stages);
assert.match(pane.innerHTML, /Продолжить со следующего этапа/);
assert.doesNotMatch(pane.innerHTML, /Продолжить текущий раунд|Review PASS|автоматический этап остановлен/);
context.wfNextStage();
assert.equal(requests[3].action, 'next-stage');
assert.equal(requests[3].body.expected_version, 25);
console.log('Workflow resume: automatic queued action, saved executor, manual/external controls and notes OK');
