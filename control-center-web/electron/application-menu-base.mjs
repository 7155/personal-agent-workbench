export function createApplicationMenuBase(Menu, platform = process.platform) {
  // Reuse MenuItem instances, not their already-wrapped click functions.
  // The fresh top-level menu is mutable; existing submenus remain untouched.
  const items = [...(Menu.getApplicationMenu()?.items ?? [])];
  const role = (item) => String(item.role ?? '').toLowerCase();
  const hasRole = (name, childRole, labels = []) => items.some((item) => (
    role(item) === name.toLowerCase()
    || labels.includes(item.label)
    || item.submenu?.items?.some((child) => role(child) === childRole)
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
