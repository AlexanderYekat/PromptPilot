// The build caption next to the logo: which build, date and installation.
const fs = require('fs');
const path = require('path');
const assert = require('assert');

const html = fs.readFileSync(path.join(__dirname, '..', 'promptpilot', 'static', 'index.html'), 'utf8');
const source = html.match(/function buildTagText\(b\) \{[\s\S]*?\n\}/);
assert(source, 'buildTagText not found');
const buildTagText = new Function(`${source[0]}; return buildTagText;`)();

const trial = buildTagText({ commit: '828a9c7634a2bf4ec5986dff5bb1258a7ea2d1b1', date: '2026-10-04T10:48:02+05:00', instance: 'trial' });
assert(trial.startsWith('моя сборка · от ') && trial.endsWith(' · стенд'), trial);
assert(buildTagText({ date: '', instance: 'production' }).endsWith('рабочая установка'));
assert.strictEqual(buildTagText({ commit: '', date: '', instance: '' }), 'моя сборка');
assert(html.includes('id="buildTag"') && html.includes('showBuildTag();'), 'caption is not wired into the page');
console.log('Build caption: build, date and installation OK');
