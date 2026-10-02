# Native Pi codemode and the Pi 1.0 compatibility candidate

The original reviewed product baseline is Pi 0.99.2. The isolated Pi 1.0.0
candidate combines upstream tag `v1.0.0` (`a13d35a742c6ef8462812a28fbe1d8c8b7431c32`)
with the product fork's Runtime Host and existing fixes. It is not a replacement
with the unmodified upstream npm package. PAW loads its native
`createCodemodeExtension`; it does not add a second JavaScript executor or
another Room execution owner.

## Explicit development build selection

The managed builder requires `--pi-worktree`; this compatibility work does not
change an installed Runtime, a release source pin, or any default application
data. Select the separate Pi 1.0 candidate checkout explicitly. In the current
development workspace it is the sibling `pi-1.0-candidate` directory; on another
computer, use the candidate checkout's actual absolute path, not the old `pi`
directory and not an unmodified upstream checkout.

```bash
PI_WORKTREE=/absolute/path/to/pi-1.0-candidate
PAW_WORKTREE=/absolute/path/to/personal-agent-workbench
PAYLOAD=/absolute/path/to/new-pi-1.0-payload

cd "$PI_WORKTREE"
npm ci --ignore-scripts
npm run hydrate:model-data
npm run check:model-data
npm run check
npm run build:offline
npm run build:rag-ime-runtime-host

cd "$PAW_WORKTREE"
RAG_IME_ALLOW_DIRTY_INSTALL=1 python3 scripts/build_managed_pi_runtime_v2.py \
  --pi-worktree "$PI_WORKTREE" --output "$PAYLOAD" --node "$(command -v node)"

python3 scripts/smoke_pi_session_staged_runtime.py \
  --payload "$PAYLOAD" --workspace-root "$PAW_WORKTREE" --deterministic-test-gate
python3 scripts/smoke_pi_packages_staged_runtime.py \
  --payload "$PAYLOAD" --workspace-root "$PAW_WORKTREE"
python3 scripts/smoke_pi_resilience_staged_runtime.py \
  --payload "$PAYLOAD" --workspace-root "$PAW_WORKTREE" --deterministic-test-gate
```

Use a new output directory. A fresh checkout must hydrate the ignored model
catalog data before `check:model-data` and the offline build. Hydration downloads
public model metadata; it does not invoke a model. Existing valid data can be
checked without downloading again. Dependency installation and hydration may
need network access, but these staged checks use the local
deterministic adapter and do not need model credentials. Run them with an
isolated HOME and no Provider credentials. `RAG_IME_ALLOW_DIRTY_INSTALL=1` is an
explicit development-source allowance: the payload records the full dirty
source digest and is not a clean release artifact. Despite the historical flag
name, the builder above only creates the selected payload and does not install
or activate it.

The builder requires the Runtime's actual `hello.piVersion` to match the SDK
package version. The Pi 1.0 candidate reports `1.0.0` with protocol `2`, and the
payload includes the relocated codemode worker, QuickJS WASM and the Pi 1.0
codemode reference. A successful build is not proof that the app is using it;
activation and foreground acceptance remain separate, explicitly selected steps.

## Durable scope

`@earendil-works/pi-durable@1.0.0` is still marked experimental upstream. Its
Session, task state and JSONL/SQLite storage are separate from the classic
coding-agent `SessionManager`. The compatibility candidate preserves the
classic JSONL product path. Durable recovery is verified separately with a
faux model and newly created isolated storage; no existing Session data is
migrated and no Room execution owner is replaced. Upstream 1.0.0 does not include
the candidate's legacy SQLite schema rejection guard. Never point a new durable
runtime at an existing product or experimental database without a separately
verified migration.

## Native codemode product behavior

`codemodeMode` has three effective Session settings: `on` exposes ordinary
tools and codemode, `only` exposes the code tool with native tool discovery,
and `off` disables codemode. The existing mode update API changes idle Sessions
through `session.codemode.set`, requires the Host response, and persists the
preference with the exact Runtime binding. Busy Sessions reject the change.
Hosts that do not declare the capability retain their older behavior.

The sandbox calls the same authorized native/product tools, including the
original Gateway admission and cancellation path. Optional direct model
helpers are disabled: Pi/Room model routing still owns model execution.
Nested calls retain their parent ID, exact arguments, and actual status in
Pi's transcript. PAW groups them under the code card and restores them on
reopen. Missing or unfinished results remain unfinished; a caught internal
error can coexist with a successfully completed outer script.

The managed payload ships the native worker and QuickJS WASM. Its source
checks cover parallel Gateway requests, invalid parameters without execution,
failed tools, exact cancellation with physical drain, mode updates and cold
history. Deterministic staged payload checks establish packaging and RPC
behavior; they do not establish real provider or multi-partner Room acceptance.
Installed version and foreground acceptance must be checked independently.

Normal execution uses the user's selected model and thinking level. Enabling
codemode does not change reasoning to max, increase Room partners, or promise
token or latency savings for a complete task.

The managed SDK loader also loads native MCP and `tool_search`. The built-in
MCP owner reads this Session's managed agent directory and trusted project
configuration. Its read-only status callback publishes connection states and
per-tool MCP exposure without command arguments, transport URLs or credentials.
`tools.list` supplies that projection to the existing Session command catalog;
the frontend's MCP tab offers inspection, refresh and idle native login/reconnect.
The MCP server exposure and Pi core tool exposure are distinct: native MCP's
`codemode` maps to deferred core discovery. The UI uses the MCP owner's per-tool
value, including overrides, instead of guessing from the core value or name.
An unavailable or replaced native owner remains unavailable in the inspector.
