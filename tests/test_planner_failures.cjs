const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../promptpilot/static/index.html'), 'utf8');
const pane = {innerHTML: '', querySelectorAll: () => [], closest: () => null};
const context = vm.createContext({
  document: {getElementById: () => pane},
  esc: x => String(x).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;'),
  escAttr: String, formatTime: () => '', uiSnapScrolls: () => ({}), uiRestoreScrolls: () => {},
  workflowStatusInfo: () => ['Остановлен', 'Проверьте причину'],
  workflowOwnerInfo: () => ({who: 'Вы', detail: '', color: '', next: ''}),
  workflowProviderOptions: () => '', workflowSettingsHtml: () => '', workflowPlanHtml: () => '',
});
function load(from, until) {
  const start = html.indexOf(from);
  const end = html.indexOf(until, start);
  assert.ok(start >= 0 && end > start);
  vm.runInContext(html.slice(start, end), context);
}
load('function workflowStopReason(', 'function workflowStageEditorCard(');
load('function workflowPlannerFailure(', 'async function wfProviderChanged(');
const wf = {status: 'awaiting_human', current_round: 0, config: {planning: {enabled: true}}};
const legacyEvents = [{seq: 119, event_type: 'planner.invalid_output', payload: {
  task_id: 24, reason: 'planner did not return a valid stage-plan-v1 contract',
}}];
const plan = {status: 'failed', planner_task_id: 24, output: {
  task_status: 'failed', error: 'OperationalError: disk I/O error <unsafe>', result: null,
}};
context.renderWorkflowDetail(wf, [], [], legacyEvents, [], plan, [], {id:24, status:'failed'});
assert.match(pane.innerHTML, /disk I\/O error &lt;unsafe&gt;/);
assert.match(pane.innerHTML, /журнал worker/);
assert.match(pane.innerHTML, /Повторно сформировать план/);
assert.doesNotMatch(pane.innerHTML, /valid stage-plan-v1|wfPlannerPrompt|<unsafe>/);
plan.status = 'cancelled';
plan.output = {task_status: 'cancelled'};
context.renderWorkflowDetail(wf, [], [], [], [], plan, [], {id:24, status:'cancelled'});
assert.match(pane.innerHTML, /Задача планировщика отменена/);
assert.doesNotMatch(pane.innerHTML, /wfPlannerPrompt/);
plan.status = 'failed';
plan.output = {task_status: 'completed', failure: {
  code: 'invalid_output', reason: 'Некорректный stage-plan-v1', next_action: 'Уточните указания',
}};
context.renderWorkflowDetail(wf, [], [], [], [], plan, [], {id:24, status:'completed'});
assert.match(pane.innerHTML, /Некорректный stage-plan-v1/);
assert.match(pane.innerHTML, /wfPlannerPrompt/);
for (const code of ['task_failed', 'cancelled', 'unsuccessful_result', 'invalid_output']) {
  const event = {seq:1, event_type:`planner.${code}`, payload:{task_id:24, reason:code}};
  assert.match(context.workflowStopReason([event]), new RegExp(code));
  assert.equal(context.workflowStopReason([event, {seq:2, event_type:'planner.dispatched'}]), '');
}
console.log('Planner failures: original error, cancellation, invalid contract, retry guidance and escaping OK');
