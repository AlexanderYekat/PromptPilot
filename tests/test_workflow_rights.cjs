const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../promptpilot/static/index.html'), 'utf8');
// Parse every inline script, including code outside the small tested helpers.
for (const match of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)) new vm.Script(match[1]);
const start = html.indexOf('function wfRightsOptions(');
const end = html.indexOf('function wfRefreshRights(', start);
const context = vm.createContext({
  providersData: {codex:{rights:['read','write','full']}, claude:{rights:['none','read','write','full']}},
  document: {getElementById: () => ({value:'codex'})},
});
vm.runInContext(html.slice(start, end), context);
const options = context.wfRightsOptions('codex', 'read', false);
assert.match(options, /value="read" selected/);
assert.match(options, /value="none" disabled/);
assert.match(options, /value="full" disabled/);
// Changing provider must not silently widen a previously selected narrow mode.
assert.match(context.wfRightsOptions('codex','none',true), /value="none" selected disabled/);
assert.match(context.wfRightsOptions('claude','write',true), /value="write" selected/);
assert.doesNotMatch(context.wfRightsOptions('claude','full',true), /value="full" selected disabled/);
console.log('Workflow rights: provider support, independent reviewer, preserved selection and JS syntax OK');
