import { execFile } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
export async function readOfficialDocs(root: string, value: string, signal?: AbortSignal) {
  const url = new URL(value);
  if (url.protocol !== 'https:' || url.hostname !== 'developers.google.com' || !url.pathname.startsWith('/earth-engine/') || url.username || url.password) throw new Error('Only official developers.google.com/earth-engine/ documents are supported.');
  const html = await new Promise<string>((resolve, reject) => {
    execFile('curl', ['--fail', '--silent', '--show-error', '--location', '--max-redirs', '2', '--proto', '=https', '--max-time', '30', url.href], { encoding: 'utf8', maxBuffer: 2 * 1024 * 1024, signal }, (error, stdout) => error ? reject(new Error('Google 文档读取失败；请检查网络或文档地址。')) : resolve(stdout));
  });
  const decode = (text: string) => text.replace(/&nbsp;/g, ' ').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, '&');
  const title = decode(html.match(/<title[^>]*>([\s\S]*?)<\/title>/i)?.[1] || url.pathname);
  const main = html.match(/<main\b[^>]*>([\s\S]*?)<\/main>/i)?.[1] || html;
  const excerpt = decode(main.replace(/<(script|style)\b[^>]*>[\s\S]*?<\/\1>/gi, '').replace(/<\/(p|div|h[1-6]|pre|li|tr)>/gi, '\n').replace(/<[^>]+>/g, ' ').replace(/[ \t]+/g, ' ').replace(/\n\s*\n/g, '\n')).trim().slice(0, 14000);
  if (excerpt.length < 80) throw new Error('Google 文档没有返回可用正文。');
  const ref = { title, url: url.href, retrievedAt: new Date().toISOString() };
  const directory = path.join(root, '.earth'); if (fs.existsSync(directory) && fs.lstatSync(directory).isSymbolicLink()) throw new Error('.earth must not be a symlink.');
  fs.mkdirSync(directory, { recursive: true });
  const file = path.join(directory, 'sources.json'); if (fs.existsSync(file) && fs.lstatSync(file).isSymbolicLink()) throw new Error('Source record cannot be a symlink.');
  const prior = fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, 'utf8')) : [];
  fs.writeFileSync(file, JSON.stringify([...prior.filter((x: { url: string }) => x.url !== ref.url), ref].slice(-30), null, 2), { mode: 0o600 });
  return { ...ref, excerpt, truncated: excerpt.length === 14000 };
}
