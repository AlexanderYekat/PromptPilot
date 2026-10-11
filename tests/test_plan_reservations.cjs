const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../promptpilot/static/index.html'), 'utf8');
const calls = [];
const messages = [];
let confirmed = true;
const context = vm.createContext({
  selectedWorkflowId:'wf-test', selectedWorkflowData:{state_version:13}, API:'/api',
  sessionStorage:{getItem:()=>null, setItem:()=>{}, removeItem:()=>{}},
  document:{getElementById:()=>null},
  esc:x=>String(x).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;'),
  escAttr:String, formatTime:()=>'', workflowStageEditorCard:s=>`<stage-editor ${s.code}>`,
  confirm:()=>confirmed, toast:message=>messages.push(message),
  fetchJSON:async (url, options)=>{calls.push({url,body:JSON.parse(options.body)});},
  loadWorkflows:async()=>{},
});
function load(from, until) {
  const start = html.indexOf(from), end = html.indexOf(until, start);
  assert.ok(start>=0 && end>start, from);
  vm.runInContext(html.slice(start,end), context);
}
load('function wfPlanFeedbackKey(', 'function workflowPlannerFailure(');
load('async function wfApplyPlannerOutput(', 'async function wfRevisePlan(');

const stage = {code:'S1', title:'Замер', objective:'Цель', stage_type:'integration'};
const config = {planning:{enabled:true}};
const narrative = 'Изменения: S1 сокращён.\nДействия оператора: <выдать доступ>';
const result = narrative + '\nWORKFLOW_PLAN_JSON_BEGIN\n{"stages":[{"code":"SECRET-JSON"}]}\nWORKFLOW_PLAN_JSON_END\nИТОГ: НУЖЕН ЧЕЛОВЕК';

// Approval of a plan with reservations shows the planner's verdict and words next to the button.
const approval = {status:'awaiting_plan_approval', current_round:0, config};
let rendered = context.workflowPlanHtml(approval,
  {status:'awaiting_approval', planner_task_id:26, output:{verdict:'НУЖЕН ЧЕЛОВЕК', manual_approval:'planner_verdict', result}}, [stage]);
assert.match(rendered, /Итог планировщика: НУЖЕН ЧЕЛОВЕК/);
assert.match(rendered, /Изменения: S1 сокращён/);
assert.match(rendered, /&lt;выдать доступ&gt;/);
assert.doesNotMatch(rendered, /SECRET-JSON|<выдать доступ>/);
assert.ok(rendered.indexOf('Итог планировщика') < rendered.indexOf('wfApprovePlan()'));
assert.ok(rendered.indexOf('<stage-editor S1>') < rendered.indexOf('Итог планировщика'));
rendered = context.workflowPlanHtml(approval,
  {status:'awaiting_approval', planner_task_id:27, output:{verdict:'НЕ СМОГ', manual_approval:'planner_output_applied', result}}, [stage]);
assert.match(rendered, /вручную из ответа задачи #27/);
// An ordinary successful plan has no reservations block.
rendered = context.workflowPlanHtml(approval, {status:'awaiting_approval', output:{verdict:'ГОТОВО', result}}, [stage]);
assert.doesNotMatch(rendered, /Итог планировщика/);

// A failed revision says the cards hold the previous version and offers the recorded plan.
const stopped = {status:'awaiting_human', current_round:0, config};
const failed = {status:'failed', planner_task_id:26, output:{verdict:'НУЖЕН ЧЕЛОВЕК', failure:{code:'unsuccessful_result'}},
  planner_output_plan:{applicable:true, task_id:26, verdict:'НУЖЕН ЧЕЛОВЕК', stage_codes:['S1','S2'], reason:''}};
rendered = context.workflowPlanHtml(stopped, failed, [stage]);
assert.match(rendered, /План не применён; в карточках — предыдущая версия этапов\./);
assert.match(rendered, /wfApplyPlannerOutput\(\)/);
assert.match(rendered, /задачи #26 есть план: S1, S2/);
assert.ok(rendered.indexOf('План не применён') < rendered.indexOf('<stage-editor S1>'));
rendered = context.workflowPlanHtml(stopped, {...failed, planner_output_plan:{applicable:false, reason:'no block'}}, [stage]);
assert.match(rendered, /План не применён; в карточках/);
assert.doesNotMatch(rendered, /wfApplyPlannerOutput/);
rendered = context.workflowPlanHtml(stopped, {...failed, planner_output_plan:{applicable:false}}, []);
assert.doesNotMatch(rendered, /План не применён/);
rendered = context.workflowPlanHtml({...stopped, current_round:2}, failed, [stage]);
assert.doesNotMatch(rendered, /План не применён|wfApplyPlannerOutput/);

// Revision history marks what happened to each revision and why.
const dispatched = (seq, task) => ({seq, event_type:'planner.dispatched', payload:{task_id:task, plan_revision:{
  feedback:'Сократите', stages:[stage], previous_stages:[stage]}}});
const events = [
  dispatched(129, 26),
  {seq:130, event_type:'planner.unsuccessful_result', payload:{task_id:26, reason:'Планировщик завершил задачу без успешного итога <ИТОГ>.'}},
];
rendered = context.workflowPlanHtml(stopped, failed, [stage], events);
assert.match(rendered, /task #26 · .* · не применена<\/summary>/);
assert.match(rendered, /Доработка не применена: Планировщик завершил задачу без успешного итога &lt;ИТОГ&gt;\./);
events.push({seq:131, event_type:'planner.output_applied', payload:{task_id:26, verdict:'НУЖЕН ЧЕЛОВЕК'}});
rendered = context.workflowPlanHtml(approval, {status:'awaiting_approval', planner_task_id:26, output:{}}, [stage], events);
assert.match(rendered, /task #26 · .* · применена вручную из ответа планировщика<\/summary>/);
assert.doesNotMatch(rendered, /Доработка не применена/);
rendered = context.workflowPlanHtml(approval, {status:'awaiting_approval', planner_task_id:28, output:{}}, [stage], [
  dispatched(140, 28), {seq:141, event_type:'planner.completed', payload:{task_id:28, manual_approval:'planner_verdict'}}]);
assert.match(rendered, /task #28 · .* · применена с оговорками планировщика<\/summary>/);

(async()=>{
  await context.wfApplyPlannerOutput();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, '/api/workflows/wf-test/plan/apply-planner-output');
  assert.deepEqual(calls[0].body, {expected_version:13});
  confirmed = false;
  await context.wfApplyPlannerOutput();
  assert.equal(calls.length, 1);
  confirmed = true;
  context.fetchJSON = async()=>{throw Error('409 there is no failed planner result to apply');};
  await context.wfApplyPlannerOutput();
  assert.match(messages.at(-1), /Применение плана: 409/);
  console.log('Plan reservations: verdict near approval, not-applied notice, apply action and revision outcomes OK');
})().catch(error=>{console.error(error);process.exitCode=1;});
