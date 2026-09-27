import fs from 'node:fs';
import path from 'node:path';

/** Reject aliases as well as lexical traversal, including dangling symlinks. */
export function workspacePath(root, relative, { allowMissing = false } = {}) {
  if (typeof relative !== 'string' || !relative || relative.includes('\0') || path.isAbsolute(relative)) {
    throw new TypeError('GIS path must be relative to the workspace.');
  }
  const target = path.resolve(root, relative), rel = path.relative(root, target);
  if (!rel || rel === '..' || rel.startsWith(`..${path.sep}`) || path.isAbsolute(rel)) throw new Error('GIS path escapes the workspace.');
  let cursor = root;
  for (const part of rel.split(path.sep)) {
    cursor = path.join(cursor, part);
    try { if (fs.lstatSync(cursor).isSymbolicLink()) throw new Error('GIS workspace paths cannot contain a symlink.'); }
    catch (error) { if (error.code !== 'ENOENT' || !allowMissing) throw error; }
  }
  return target;
}
