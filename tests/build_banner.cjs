// The build banner text: which build, date, commit and installation.
const fs = require('fs');
const path = require('path');
const assert = require('assert');

const html = fs.readFileSync(path.join(__dirname, '..', 'promptpilot', 'static', 'index.html'), 'utf8');
const source = html.match(/function buildBannerText\(b\) \{[\s\S]*?\n\}/);
assert(source, 'buildBannerText not found');
const buildBannerText = new Function(`${source[0]}; return buildBannerText;`)();

const trial = buildBannerText({ commit: '828a9c7634a2bf4ec5986dff5bb1258a7ea2d1b1', date: '2026-10-04T10:48:02+05:00',
  instance: 'trial', instance_title: 'Стенд (тестовые данные)' });
assert(trial.startsWith('МОЯ СБОРКА · от '), trial);
assert(trial.includes('828a9c7') && trial.endsWith('СТЕНД (ТЕСТОВЫЕ ДАННЫЕ)'), trial);
assert.strictEqual(buildBannerText({ commit: '', date: '', instance_title: '' }), 'МОЯ СБОРКА');
assert(html.includes('id="buildBanner"') && html.includes('showBuildBanner();'), 'banner is not wired into the page');
console.log('Build banner: build, date, commit and installation OK');
