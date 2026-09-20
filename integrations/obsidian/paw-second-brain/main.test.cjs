const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');
const crypto = require('node:crypto');
const scope = { module: { exports: {} }, require: name => name === 'obsidian' ? { Plugin: class {}, PluginSettingTab: class {}, Modal: class {} } : require(name) };
vm.runInNewContext(readFileSync(__dirname + '/main.js', 'utf8'), scope);
const checked = scope.module.exports.checkedBody;
const hash = s => crypto.createHash('sha256').update(s).digest('hex');
const intent = { beforeRevision: hash('original'), afterRevision: hash('approved'), markdown: 'approved' };
test('applies only approved body and unchanged buffers', () => assert.equal(checked('original', intent, ['original']), 'approved'));
test('rejects external edits and dirty editor buffer', () => {
  assert.throws(() => checked('changed', intent));
  assert.throws(() => checked('original', intent, ['unsaved change']));
});
test('rejects tampered approved body', () => assert.throws(() => checked('original', { ...intent, markdown: 'different' })));
