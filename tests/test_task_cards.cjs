const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../promptpilot/static/index.html'), 'utf8');
const requests = [], notices = [];
let cards = [], openDetails = [], finishRequest, requestError;
const list = {innerHTML: '', querySelectorAll: () => openDetails};
const context = vm.createContext({
  document: {getElementById: () => list}, expandedId: 33, currentFilter: '', API: '/api',
  screenCache: {},
  esc: x => String(x).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;'),
  escAttr: x => String(x).replaceAll('"', '&quot;').replaceAll('<', '&lt;'),
  timeAgo: () => '', parseResult: () => null, githubApiWait: () => null,
  providerOptions: () => '', effortOptions: () => '',
  taskStatusLabel: t => t.status, scheduleInfo: () => '', uiSnapScrolls: () => ({}),
  uiRestoreScrolls: () => {}, toast: (...args) => notices.push(args), refresh: async () => {},
  fetchJSON: async (url, options) => {
    if (!options) return cards;
    requests.push({url, options});
    await new Promise(resolve => { finishRequest = resolve; });
    if (requestError) throw new Error(requestError);
    return {ok: true, worker_paused: true};
  },
});
function load(start, end) {
  const from = html.indexOf(start), to = html.indexOf(end, from);
  assert.ok(from >= 0 && to > from);
  vm.runInContext(html.slice(from, to), context);
}
load('function taskDisplayTitle(', 'function providerOptions(');
load('function verdictIcon(', 'async function saveNote(');
load('async function retryTask(', 'async function cancelTask(');

(async () => {
  const task = {id: 33, status: 'rate_limited', prompt: 'Полный текст <script>bad</script>',
    next_run_at: '2026-10-15T07:47:00Z', created_at: '2026-10-15T02:47:00Z',
    brief: {title: 'S1 · Проверить окружение', goal: 'Подтвердить готовность к измерениям.',
      context: 'pilot · Исполнитель · Раунд 2'}};
  cards = [task];
  await context.loadTasks();
  assert.match(list.innerHTML, /Повторить сейчас/);
  assert.match(list.innerHTML, /S1 · Проверить окружение/);
  assert.ok(list.innerHTML.indexOf(task.brief.goal) < list.innerHTML.indexOf('Полный промпт'));
  assert.match(list.innerHTML, /<details class="task-prompt-details" id="task-prompt-33">/);
  assert.match(list.innerHTML, /&lt;script&gt;bad&lt;\/script&gt;/);
  assert.doesNotMatch(list.innerHTML, /<script>bad/);
  assert.match(list.innerHTML, /data-copy="Полный текст/);
  // Refresh keeps a prompt that the user opened; closing it also persists.
  openDetails = [{id: 'task-prompt-33'}];
  await context.loadTasks();
  assert.match(list.innerHTML, /id="task-prompt-33" open/);
  openDetails = [];
  await context.loadTasks();
  assert.doesNotMatch(list.innerHTML, /id="task-prompt-33" open/);
  for (const status of ['pending', 'running', 'completed', 'failed', 'cancelled']) {
    assert.equal(context.taskRetryButton({...task, status}), '');
  }
  let stopped = 0;
  const button = {disabled: false, dataset: {retryAt: task.next_run_at}};
  const event = {currentTarget: button, stopPropagation: () => stopped++};
  const first = context.retryTask(event, task.id);
  assert.equal(button.disabled, true);
  await context.retryTask(event, task.id);
  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, '/api/tasks/33/retry');
  assert.equal(JSON.parse(requests[0].options.body).expected_next_run_at, task.next_run_at);
  await context.loadTasks();
  assert.match(list.innerHTML, /data-retry-at="[^"]+" disabled/);
  finishRequest();
  await first;
  assert.equal(button.disabled, false);
  assert.equal(stopped, 2);
  assert.match(notices[0][0], /Общая пауза остаётся включённой/);
  requestError = 'Время повтора изменилось';
  const rejected = context.retryTask(event, task.id);
  finishRequest();
  await rejected;
  assert.match(notices.at(-1)[0], /Не удалось повторить.*Время повтора изменилось/);
  assert.equal(button.disabled, false);
  assert.equal(requests.length, 2);
  const audit = {id: 34, status: 'completed', verdict: 'УЖЕ СДЕЛАНО',
    prompt: 'Проверить результат', result: 'ИТОГ: УЖЕ СДЕЛАНО — исходный отчёт',
    brief: {title: 'Проверка: S1', outcome: {label: 'АУДИТ ЗАВЕРШЁН', icon: '✅',
      summary: 'АУДИТ ЗАВЕРШЁН: замечаний не найдено'}}};
  cards = [audit];
  await context.loadTasks();
  const header = list.innerHTML.split('<div class="task-detail">')[0];
  assert.match(header, /АУДИТ ЗАВЕРШЁН/);
  assert.doesNotMatch(header, /УЖЕ СДЕЛАНО/);
  assert.match(list.innerHTML, /<strong>АУДИТ ЗАВЕРШЁН: замечаний не найдено<\/strong>/);
  assert.match(list.innerHTML, /ИТОГ: УЖЕ СДЕЛАНО — исходный отчёт/);
  assert.match(context.taskVerdictHtml({verdict: 'УЖЕ СДЕЛАНО'}), /УЖЕ СДЕЛАНО/);
  const hostile = {brief: {outcome: {label: '<script>bad</script>', icon: '✅', summary: '"<img>'}}};
  assert.doesNotMatch(context.taskVerdictHtml(hostile), /<script>|"<img>/);
  console.log('Task cards: meaningful brief, folded prompt, refresh, escaping and guarded retry OK');
})().catch(error => { console.error(error); process.exitCode = 1; });
