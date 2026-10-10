const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../promptpilot/static/index.html'), 'utf8');
const storage = new Map();
const elements = {wfPlanFeedback:{value:'Use handoff.json'}, wfRevisePlanButton:{disabled:false}};
const calls = [];
const messages = [];
const stage = {code:'S1', title:'Manual edit', objective:'Goal', stage_type:'integration'};
const context = vm.createContext({
  selectedWorkflowId:'wf-test', selectedWorkflowData:{state_version:11}, API:'/api',
  sessionStorage:{getItem:k=>storage.get(k), setItem:(k,v)=>storage.set(k,v), removeItem:k=>storage.delete(k)},
  document:{getElementById:id=>elements[id]},
  esc:x=>String(x).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;'),
  escAttr:String, formatTime:()=>'', workflowStageEditorCard:()=>'<stage-editor>',
  wfCollectPlanStages:()=>[stage], toast:message=>messages.push(message),
  fetchJSON:async (url, options)=>{calls.push({url,body:JSON.parse(options.body)});},
  loadWorkflows:async()=>{},
});
function load(from, until) {
  const start = html.indexOf(from), end = html.indexOf(until, start);
  assert.ok(start>=0 && end>start);
  vm.runInContext(html.slice(start,end), context);
}
load('function wfPlanFeedbackKey(', 'function workflowPlannerFailure(');
load('async function wfRevisePlan(', 'async function wfPlannerDispatch(');
const workflow = {status:'awaiting_plan_approval',current_round:0,config:{planning:{enabled:true}}};
context.wfRememberPlanFeedback('<unsafe>');
let rendered = context.workflowPlanHtml(workflow, null, [stage]);
assert.match(rendered,/Замечания к плану/);
assert.match(rendered,/Доработать по замечаниям/);
assert.match(rendered,/&lt;unsafe&gt;/);
assert.doesNotMatch(rendered,/<unsafe>|Сформировать заново/);
const event = {seq:7,event_type:'planner.dispatched',payload:{task_id:26,plan_revision:{
  feedback:'<script>bad()</script>',stages:[{...stage,title:'Old'}],previous_stages:[stage],
}}};
rendered = context.workflowPlanHtml(workflow,{status:'awaiting_approval',planner_task_id:26},[stage],[event]);
assert.match(rendered,/S1: название/);
assert.match(rendered,/&lt;script&gt;/);
assert.match(rendered,/Сохранённая версия до отправки/);
assert.doesNotMatch(rendered,/<script>bad/);
rendered = context.workflowPlanHtml({...workflow,status:'planning'},null,[stage],[event]);
assert.doesNotMatch(rendered,/id="wfPlanFeedback"|wfApprovePlan\(\)/);
rendered = context.workflowPlanHtml({...workflow,status:'awaiting_human'},null,[stage],[event]);
assert.match(rendered,/id="wfPlanFeedback"/);
assert.doesNotMatch(rendered,/wfApprovePlan\(\)|wfSavePlan\(\)/);

(async()=>{
  await context.wfRevisePlan();
  assert.equal(calls.length,1);
  assert.equal(calls[0].url,'/api/workflows/wf-test/plan/dispatch');
  assert.equal(calls[0].body.feedback,'Use handoff.json');
  assert.equal(calls[0].body.expected_version,11);
  assert.equal(calls[0].body.stages[0].title,'Manual edit');
  assert.equal(context.wfPlanFeedbackDraft(),'');
  assert.equal(elements.wfRevisePlanButton.disabled,false);
  elements.wfPlanFeedback.value='  ';
  await context.wfRevisePlan();
  assert.equal(calls.length,1);
  assert.match(messages.at(-1),/Добавьте замечания/);
  elements.wfPlanFeedback.value='Keep after failure';
  context.wfRememberPlanFeedback(elements.wfPlanFeedback.value);
  context.fetchJSON=async()=>{throw Error('409 conflict');};
  await context.wfRevisePlan();
  assert.equal(context.wfPlanFeedbackDraft(),'Keep after failure');
  assert.equal(elements.wfRevisePlanButton.disabled,false);
  assert.match(messages.at(-1),/409 conflict/);
  console.log('Plan feedback: form, version history, escaping, atomic submission, retry and approval boundary OK');
})().catch(error=>{console.error(error);process.exitCode=1;});
