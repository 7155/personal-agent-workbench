const { Plugin, PluginSettingTab, Setting, Notice, Modal, requestUrl, TFile, MarkdownView } = require('obsidian');
const { createHash } = require('crypto');
const hash = text => createHash('sha256').update(text.replace(/^\uFEFF/, '')).digest('hex');

// Pure write precondition, also exercised by the Node regression suite.
function checkedBody(current, intent, buffers = []) {
  if (hash(current) !== intent.beforeRevision) throw new Error('原文已变化，请回 PAW 重新审核。');
  if (buffers.some(buffer => hash(buffer) !== intent.beforeRevision)) throw new Error('此笔记存在尚未保存的编辑，请先保存后重新审核。');
  if (hash(intent.markdown) !== intent.afterRevision) throw new Error('批准内容校验失败。');
  return intent.markdown;
}
class Review extends Modal {
  constructor(plugin, proposals) { super(plugin.app); this.plugin = plugin; this.proposals = proposals; }
  onOpen() {
    this.titleEl.setText('PAW 笔记更新');
    for (const p of this.proposals) {
      const box = this.contentEl.createDiv();
      box.createEl('h3', { text: p.path || '来源不可用' });
      box.createEl('p', { text: `${p.reason} · ${p.state}` });
      box.createEl('pre', { text: p.diff });
      if (['waiting_editor', 'applying', 'saved_index_pending'].includes(p.state)) {
        const button = box.createEl('button', { text: '应用已批准修改 / 恢复回执' });
        button.onclick = async () => { button.disabled = true; try { await this.plugin.apply(p); box.createEl('p', { text: '正文已保存，回执已核对。' }); } catch (e) { new Notice(e.message); button.disabled = false; } };
      } else box.createEl('p', { text: '请先在 PAW 审核并批准这一版提案。' });
    }
    if (!this.proposals.length) this.contentEl.createEl('p', { text: '没有待处理提案。' });
  }
}
module.exports = class PawSecondBrain extends Plugin {
  async onload() {
    this.settings = { endpoint: 'http://127.0.0.1:8769', vaultId: '', ...(await this.loadData()) };
    this.token = ''; // Session-only pairing credential; never in the vault or plugin JSON.
    this.addSettingTab(new Settings(this.app, this));
    this.addCommand({ id: 'review-proposals', name: '查看笔记更新', callback: async () => {
      try { const result = await this.call({ action: 'pending' }); new Review(this, result.items).open(); } catch (e) { new Notice(e.message); }
    }});
    this.busy = new Set();
  }
  async call(payload) {
    const url = new URL(this.settings.endpoint);
    if (url.protocol !== 'http:' || !['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname) || url.username || url.password || url.pathname !== '/' || url.search || url.hash) throw new Error('只允许本机 PAW 服务。');
    if (!this.token) throw new Error('请在设置中输入 PAW 配对码（仅本次运行保留）。');
    const root = this.app.vault.adapter.getBasePath();
    const result = await requestUrl({ url: `${url.origin}/v1/knowledge/vault/editor`, method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${this.token}` }, body: JSON.stringify({ ...payload, root, vaultId: this.settings.vaultId }), throw: false });
    if (result.status !== 200) throw new Error(result.json?.error?.message || 'PAW 连接失败');
    return result.json;
  }
  async apply(p) {
    if (this.busy.has(p.id)) return;
    this.busy.add(p.id);
    try {
      const file = this.app.vault.getAbstractFileByPath(p.path);
      if (!(file instanceof TFile) || file.extension !== 'md') throw new Error('笔记已移动，请在 PAW 刷新。');
      const current = await this.app.vault.read(file);
      // Recover only a persisted intent whose after hash is already on disk.
      if (['applying', 'saved_index_pending'].includes(p.state) && hash(current) === p.afterRevision) {
        await this.call({ action: 'receipt', proposalId: p.id, applicationId: p.applicationId }); return;
      }
      const intent = await this.call({ action: 'begin', proposalId: p.id });
      if (intent.path !== p.path) throw new Error('目标路径已变化，请重新打开提案。');
      await this.app.vault.process(file, body => {
        const buffers = this.app.workspace.getLeavesOfType('markdown').map(leaf => leaf.view).filter(view => view instanceof MarkdownView && view.file?.path === file.path).map(view => view.editor.getValue());
        return checkedBody(body, intent, buffers);
      });
      await this.call({ action: 'receipt', proposalId: p.id, applicationId: intent.applicationId });
    } finally { this.busy.delete(p.id); }
  }
  onunload() { this.token = ''; }
};
class Settings extends PluginSettingTab {
  constructor(app, plugin) { super(app, plugin); this.plugin = plugin; }
  display() {
    const { containerEl } = this; containerEl.empty();
    containerEl.createEl('h2', { text: '连接 PAW' });
    containerEl.createEl('p', { text: '在 PAW 本地笔记设置中生成配对码。插件不接收 Jev Key，不运行另一个 Agent。' });
    new Setting(containerEl).setName('服务地址').addText(t => t.setValue(this.plugin.settings.endpoint).onChange(async v => { this.plugin.settings.endpoint = v; await this.plugin.saveData(this.plugin.settings); }));
    new Setting(containerEl).setName('笔记库 ID').addText(t => t.setValue(this.plugin.settings.vaultId).onChange(async v => { this.plugin.settings.vaultId = v; await this.plugin.saveData(this.plugin.settings); }));
    new Setting(containerEl).setName('配对码（仅本次运行）').addText(t => { t.inputEl.type = 'password'; t.onChange(v => { this.plugin.token = v.trim(); }); });
  }
}
module.exports.checkedBody = checkedBody;
