# PAW Second Brain editor adapter

This desktop-only Obsidian plugin edits the same Markdown files PAW reads. It
uses the public Vault API (`read` / `process`), not Obsidian's DOM or private
storage. Obsidian itself is independently installed and is not bundled here.

Copy `manifest.json` and `main.js` into `<vault>/.obsidian/plugins/paw-second-brain/`.
Enable the plugin in Obsidian's community plugin settings. In PAW, open
**Knowledge → 本地笔记 → 设置 → 生成配对码**. In this plugin's settings enter the
vault ID and pairing code. The local endpoint defaults to `http://127.0.0.1:8769`.
The pairing code is held only in memory; re-enter it after restarting Obsidian.
It is not a Jev API key. PAW can revoke it; generating a new code revokes the old one.

Read and approve a proposal in PAW, then run **PAW Second Brain: 查看笔记更新**
in Obsidian's command palette. The plugin checks disk content and every open
Markdown editor buffer, then calls `Vault.process` with the same checks in its
synchronous callback. It sends the application receipt only after the write.
A changed file or buffer leaves the original intact and requires a fresh proposal.

If the process stops after writing, run the command again. A persisted application
intent and matching after-hash permit receipt recovery; the body is not appended
a second time. `saved_index_pending` means the body was saved and indexing can retry.
No plugin means PAW can show a diff or save a new draft, but cannot update old files.

To uninstall, revoke pairing in PAW, disable this plugin, then remove only this
plugin directory. All user Markdown remains. Neither PAW nor this plugin installs
Obsidian Sync or uploads the vault. Use PAW's existing secure provider settings
for Jev; remote note processing is independently off by default.

`node --test integrations/obsidian/paw-second-brain/main.test.cjs` checks the pure
write guard. This does not replace an attended test of Obsidian's real editor.
