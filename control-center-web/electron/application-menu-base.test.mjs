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

test('PAW actions can be added while leaving existing native submenus untouched', () => {
  const callback = () => {};
  const original = { items: [{ label: 'Existing file action', click: callback }], append() { throw new Error('native submenu is not mutable'); } };
  const menu = createApplicationMenuBase(boundary({ items: [{ label: 'File', submenu: original }] }), 'darwin');
  const files = menu.items.find((item) => item.label === 'File').submenu;
  menu.append({ label: 'PAW', submenu: [{ label: 'PAW action' }] });
  assert.equal(files, original);
  assert.equal(files.items[0].click, callback);
  assert.equal(original.items.length, 1);
  assert.equal(files.items.length, 1);
});

test('non-macOS menus keep editing shortcuts without inserting a macOS application menu', () => {
  const menu = createApplicationMenuBase(boundary(), 'linux');
  assert.ok(menu.items.some((item) => item.role === 'editMenu'));
  assert.equal(menu.items.some((item) => item.role === 'appMenu'), false);
});

test('native item activation keeps event arguments, checkbox state and the original instance', () => {
  let receipt;
  class NativeItem {
    constructor(options) {
      Object.assign(this, options);
      const callback = options.click;
      // Electron wraps constructor callbacks; its instance click takes the
      // keyboard event first, rather than a constructor callback's menu item.
      this.click = (event, window, contents) => {
        if (this.type === 'checkbox') this.checked = !this.checked;
        callback?.(this, window, event);
      };
    }
  }
  const original = new NativeItem({ label: 'Existing toggle', type: 'checkbox', checked: false,
    click: (item, window, event) => { receipt = { item, window, event }; },
  });
  class Menu {
    static getApplicationMenu() { return { items: [original] }; }
    static buildFromTemplate(items) { return { items: items.map((item) => item instanceof NativeItem ? item : new NativeItem(item)) }; }
  }
  const menu = createApplicationMenuBase(Menu, 'darwin');
  const action = menu.items.find((item) => item.label === original.label);
  const event = { triggeredByAccelerator: true };
  const window = { id: 42 };
  action.click(event, window, { id: 7 });
  assert.equal(receipt.event, event);
  assert.equal(receipt.window, window);
  assert.equal(receipt.item, original);
  assert.equal(action, original);
  assert.equal(original.checked, true);
});
