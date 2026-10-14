export function createApplicationMenuBase(Menu, platform = process.platform) {
  // A returned native menu is not a mutable builder. Preserve its items and
  // callbacks in a fresh menu, including the standard macOS editing commands.
  const items = (Menu.getApplicationMenu()?.items ?? []).map(menuItemTemplate);
  const role = (item) => String(item.role ?? '').toLowerCase();
  const hasRole = (name, childRole, labels = []) => items.some((item) => (
    role(item) === name.toLowerCase()
    || labels.includes(item.label)
    || item.submenu?.some((child) => role(child) === childRole)
  ));
  if (platform === 'darwin' && !hasRole('appMenu', 'about')) items.unshift({ role: 'appMenu' });
  for (const [name, childRole, labels] of [
    ['fileMenu', undefined, ['File', '文件']],
    ['editMenu', 'selectall', []],
    ['viewMenu', 'resetzoom', []],
    ['windowMenu', 'minimize', []],
  ]) {
    if (!hasRole(name, childRole, labels)) items.push({ role: name });
  }
  return Menu.buildFromTemplate(items);
}

function menuItemTemplate(item) {
  const template = {};
  for (const key of ['id', 'label', 'sublabel', 'toolTip', 'type', 'role', 'accelerator', 'enabled', 'visible', 'checked', 'icon', 'acceleratorWorksWhenHidden', 'registerAccelerator', 'sharingItem', 'click']) {
    if (item[key] !== undefined) template[key] = item[key];
  }
  if (item.submenu) template.submenu = item.submenu.items.map(menuItemTemplate);
  return template;
}
