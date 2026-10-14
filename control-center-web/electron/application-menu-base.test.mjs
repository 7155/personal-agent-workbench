import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createApplicationMenuBase } from './application-menu-base.mjs';

function boundary(existing = null) {
  class Menu {
    static getApplicationMenu() { return existing; }
    static buildFromTemplate(items) { return new Menu(items); }
    constructor(items = []) {
      this.items = items.map((item) => Array.isArray(item.submenu) ? { ...item, submenu: new Menu(item.submenu) } : item);
    }
    append(item) { this.items.push(item); }
  }
  return Menu;
}

test('a new macOS application menu retains standard editing and window shortcuts', () => {
  const menu = createApplicationMenuBase(boundary(), 'darwin');
  assert.equal(menu.items[0]?.role, 'appMenu');
  for (const role of ['fileMenu', 'editMenu', 'viewMenu', 'windowMenu']) {
    assert.ok(menu.items.some((item) => item.role === role), `missing ${role}`);
  }
});

test('augmenting a supplied menu preserves callbacks without mutating the returned native menu', () => {
  const action = { label: 'Existing action', click() {} };
  const existing = { items: [action], append() { throw new Error('returned native menu is not mutable'); } };
  const menu = createApplicationMenuBase(boundary(existing), 'darwin');
  assert.notEqual(menu, existing);
  assert.equal(menu.items.find((item) => item.label === action.label)?.click, action.click);
  menu.append({ label: 'PAW action' });
  assert.deepEqual(existing.items, [action]);
  assert.ok(menu.items.some((item) => item.role === 'editMenu'));
});

test('the copied File submenu can receive PAW actions without changing the native submenu', () => {
  const callback = () => {};
  const original = { items: [{ label: 'Existing file action', click: callback }], append() { throw new Error('native submenu is not mutable'); } };
  const menu = createApplicationMenuBase(boundary({ items: [{ label: 'File', submenu: original }] }), 'darwin');
  const files = menu.items.find((item) => item.label === 'File').submenu;
  files.append({ label: 'PAW action' });
  assert.equal(files.items[0].click, callback);
  assert.equal(original.items.length, 1);
  assert.equal(files.items.length, 2);
});

test('non-macOS menus keep editing shortcuts without inserting a macOS application menu', () => {
  const menu = createApplicationMenuBase(boundary(), 'linux');
  assert.ok(menu.items.some((item) => item.role === 'editMenu'));
  assert.equal(menu.items.some((item) => item.role === 'appMenu'), false);
});
