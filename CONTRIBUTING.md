# Contributing

Thanks for helping improve Personal Agent Workbench. The project combines a
local Python runtime, React Control Center, Agent/Room orchestration, optional
Provider integrations, and macOS input/voice/browser adapters. A small-looking
change can therefore cross privacy or lifecycle boundaries.

## Before You Start

- Use Python 3.12 or newer.
- Use Node.js 22 and the pinned `pnpm@11.9.0` for `control-center-web/`.
- Use Xcode 16 or newer for patched Squirrel and native release work.
- Start with the [architecture and source owners](README.md#架构与职责)
  for the area you want to change.
- Open an issue before changing persisted schemas, Provider context ordering,
  Tool approval, Room settlement/cancellation, or cross-language route
  contracts.

## Local Setup

```bash
uv sync --locked --python 3.12
corepack enable
corepack prepare pnpm@11.9.0 --activate
pnpm --dir control-center-web install --frozen-lockfile
```

Do not add local API keys, model weights, personal input history, databases,
build products, or macOS permission state to fixtures.

Native builds hydrate the lockfile-pinned Electron runtime through its official
installer and checksums. Existing HTTP/HTTPS proxy settings apply to this
download; an explicit `ELECTRON_GET_USE_PROXY` setting takes precedence (an
empty value disables proxy initialization). No proxy or credential settings
are persisted by the build.

## Change Discipline

Development plans, requirement ledgers, handoffs, and review diaries stay local
and are excluded by `.gitignore`. A fresh clone needs only the public source,
fixtures, and guides; do not force-add private development records.

- Trace one real entry point through its downstream consumer before editing.
- Keep one authoritative owner for each stateful concern.
- Migrate complete capabilities and delete the replaced path in the same change.
- Prefer typed request/result/receipt boundaries over undocumented dictionaries.
- Keep transport, application, lifecycle, persistence, Provider, and UI
  responsibilities separate.
- Do not split a file only because it is large.
- Do not introduce forwarding-only facades, service locators, universal base
  classes, or a second event/message protocol.

The Control Center has one product entry: `src/app/App.tsx` mounts PAWOS.
`src/paw-os` owns the desktop, windows and workspace composition;
`src/features` owns shared domain state, API access and renderers. Hash routes
resolve through the PAWOS App registry. Standalone report, screen-assistant and
portable Agent surfaces reuse these owners. Do not add another frontend
selector or a parallel page/router implementation. Product browser tests use
this same entry with an isolated mock transport; component fixtures remain
explicitly synthetic.

## Verification

Run focused tests while working. Before a substantial pull request, run:

```bash
uv run --locked python scripts/check_project_harness.py
uv run --locked python -m compileall -q rag_ime scripts tests
uv run --locked python scripts/check_owner_boundaries.py
uv run --locked python scripts/check_import_boundaries.py
uv run --locked python scripts/check_route_ownership.py
uvx --from ruff==0.14.2 ruff check rag_ime scripts tests
uvx --from mypy==2.3.0 mypy
uv run --locked python scripts/check_product_status.py --json
uv run --locked python scripts/check_public_release.py --repository-only
uv run --locked python -m unittest discover -s tests
pnpm --dir control-center-web typecheck
pnpm --dir control-center-web test
pnpm --dir control-center-web build
```

Native or input-method changes also require the relevant build and an attended
foreground test. A backend JSON response is not proof that a candidate was
visible and selectable in Squirrel.

For a slow or apparently stuck Python suite, use
`uv run --locked python scripts/run_unit_tests.py --timing-jsonl /tmp/paw-unit-times.jsonl`.
It runs the ordinary suite, records active tests and module timings, and prints
periodic thread stacks without killing or skipping a test. An interrupted run
reports incomplete status. Diagnose resource owners and setup cost before
changing a timeout.

Behavior fixtures may use `tests.sqlite_fixtures.copy_current_database` to copy
a pristine database produced by the real migration chain. Each test still owns
its database and runs normal product initialization. Empty/legacy migration,
initialization, crash and durability tests must retain their specific starting
state. Close services, workers and streams before removing temporary data.
Preserve unique recovery assertions when consolidating tests; implementation
shape or test count is not the acceptance criterion.

For Jev Room changes, run `python3 -m unittest discover -s tests -p
'test_jev*py'` and `python3 -m unittest tests.test_pi_exact_turn_cancellation`,
then the affected frontend tests. The opt-in [live Jev canary](integrations/jev/README.md#room-api-and-verification)
uses actual Provider calls and a separately supplied Pi payload; preserve its
reported evidence boundary and do not count a synthetic UI fixture as live
multi-Agent acceptance.

### macOS 本地接续与验收

先核对本地 `git status --short` 和 `git rev-parse HEAD`。接续指定提交时，
可以在现有仓库中 `git fetch origin`，再用
`git worktree add --detach "$PAW_LOCAL_CHECKOUT" "$PAW_REVISION"` 建立独立目录。
`PAW_LOCAL_CHECKOUT` 必须是新目录，`PAW_REVISION` 使用交付的精确 commit；
不 reset、clean 或覆盖原工作树。进入新目录后，设置
`PAW_WORKTREE="$(pwd -P)"`，按上面的 Local Setup 安装锁定依赖。
这会更新源码开发副本，不会更新已经安装的 PAW.app 或激活 Runtime。

当前入口的覆盖范围如下；`scripts/run_paw_acceptance.py` **没有 `--mock`
或 `--offline` 参数**，省略 `--live` 就是离线检查。

| 入口 | 实际运行的层 | 未证明的层 |
| --- | --- | --- |
| 前端 `controlTransport=mock` | 同一 PAWOS 页面与演示 transport | HTTP、Pi、真实文件/模型、原生 Electron |
| 默认 acceptance CLI | 8 项任务 fixture 与 oracle 自检 | 任务模型执行、桌面交互 |
| acceptance storage/trace 单测 | 临时 SQLite、生产 Memory/Gateway owner；Runtime 关闭 | 完整 Agent 执行 |
| staged deterministic Session/resilience smoke | 实际配对 Pi Host、Session、Steer/Stop、压缩继续与重复工具失败终止；模型为确定性适配器 | PAW HTTP、完整 Room/Jev/子任务工作流及前端 |
| 显式 live acceptance | 实际 AgentLab/Agent/Pi/工具与精确回合结算 | 原生前台；另需费用与授权证据 |

尚无统一的“HTTP → Agent → 工具落盘/回读 → 事件/前端”确定性 mock
入口。Classic/PTC fixture、Room/Jev/子任务契约测试均不能代替这条集成路径；
多 Agent 依赖长流程、重复请求、停止后恢复仍需在配对 Runtime 上接续验证。
前端逐页记录见
[现有验收表](design-system/rag-ime-control-center/frontend-acceptance-20261006.md)，
其中演示截图、真实后端读取和未验状态分别标明。

**先运行无需模型的检查与界面：**

```bash
uv run --locked python scripts/run_paw_acceptance.py
PAW_PYTHON="$(uv run --locked python -c 'import sys; print(sys.executable)')"
PAW_OFFLINE_STATE="$(mktemp -d "${TMPDIR:-/tmp}/paw-offline.XXXXXX")"
mkdir -p "$PAW_OFFLINE_STATE/home"
env -i PATH="$PATH" HOME="$PAW_OFFLINE_STATE/home" TMPDIR="${TMPDIR:-/tmp}" \
  RAG_IME_APP_SUPPORT_DIR="$PAW_OFFLINE_STATE/support" \
  RAG_IME_KNOWLEDGE_ROOT="$PAW_OFFLINE_STATE/knowledge" \
  "$PAW_PYTHON" -m unittest tests.test_agent_lab_acceptance_fixtures \
  tests.test_agent_lab_acceptance_storage tests.test_agent_lab_acceptance_trace \
  tests.test_agent_lab_acceptance
node --test scripts/test_paw_acceptance_provider_guard.mjs
pnpm --dir control-center-web dev --port 5173 --strictPort
```

storage fixture 初始化真实 Knowledge owner，因此单测也明确使用临时
HOME/support/knowledge；不让测试初始化用户既有资料目录。
打开 `http://127.0.0.1:5173/?controlTransport=mock`，用 Ctrl-C 停止服务器。
这个 URL 只切换界面演示 transport，不能把真实 Runtime 切换成 mock。
生产 HTTP/native bundle 不提供失败后回退到 mock 的路径。

**有配对 Pi payload 时，单独运行真实 Host 的确定性检查：**

使用 `7155/pi` 的产品 fork，不能用未修改的上游 npm Pi 替代。
[Pi 1.0 配对构建说明](integrations/pi/pi-0.99-codemode.md) 对应 SDK/Host
`1.0.0` 与协议 `2`；上游基底为 `v1.0.0`
(`a13d35a742c6ef8462812a28fbe1d8c8b7431c32`)。
[Host 合约](integrations/pi/session-runtime-host-contract.json) 的最低 handler
commit 为 `93f517925a64ae215ba3ae3fd6b925b9a8505ef6`，它不是完整配对证明。
从明确选定的 Pi worktree 构建到新的 payload 目录，保留实际 `sourceCommit`、
`piVersion`、`protocolVersion` 和 manifest SHA；builder 会检查源码标记与
实际 `hello.piVersion`。构建不安装、不激活。已有 payload 须核对这些信息；
没有 payload 就记录这一层未执行，不使用假 Host 填补。

以下 `PAIRED_RUNTIME` 是调用者选定的绝对 payload 路径。命令用空环境、
临时 HOME 和合成工作区运行既有 smoke，既有配置与凭据不复制进来：

```bash
PAW_PYTHON="$(uv run --locked python -c 'import sys; print(sys.executable)')"
PAW_MOCK_STATE="$(mktemp -d "${TMPDIR:-/tmp}/paw-mock.XXXXXX")"
mkdir -p "$PAW_MOCK_STATE/home" "$PAW_MOCK_STATE/workspace" "$PAW_MOCK_STATE/evidence"
printf '{"name":"paw-staged-mock","private":true}\n' > "$PAW_MOCK_STATE/workspace/package.json"
env -i PATH="$PATH" HOME="$PAW_MOCK_STATE/home" TMPDIR="${TMPDIR:-/tmp}" \
  "$PAW_PYTHON" scripts/smoke_pi_session_staged_runtime.py \
  --payload "$PAIRED_RUNTIME" --workspace-root "$PAW_MOCK_STATE/workspace" \
  --deterministic-test-gate --report-path "$PAW_MOCK_STATE/evidence/session.json" \
  > "$PAW_MOCK_STATE/evidence/session.stdout.log" 2> "$PAW_MOCK_STATE/evidence/session.stderr.log"
env -i PATH="$PATH" HOME="$PAW_MOCK_STATE/home" TMPDIR="${TMPDIR:-/tmp}" \
  "$PAW_PYTHON" scripts/smoke_pi_resilience_staged_runtime.py \
  --payload "$PAIRED_RUNTIME" --workspace-root "$PAW_MOCK_STATE/workspace" \
  --deterministic-test-gate > "$PAW_MOCK_STATE/evidence/resilience.json" \
  2> "$PAW_MOCK_STATE/evidence/resilience.stderr.log"
```

每条命令单独检查退出码。成功应报告 `passed_not_installed`、
`productionEnabled: false` 及所选 manifest SHA；非零退出、缺失回执或超时
均保留为失败/未验，不能用旧报告替换。证据位于上述外部临时目录，
resilience 的 stdout 只有成功才是完整 JSON。切换到 live 时不沿用这个
临时 HOME/确定性适配器；使用下面独立的显式入口。

**live 默认关闭。** 配置只记录调用者选择的路径，不输出 `auth.json`、
`models.json` 或环境凭据内容。恢复原付费账本、run/reservation ID、逐次
用量回执及未决任务的终止证明后，才能确定可用预算。所有未知保留继续
保留；不要另开归零账本，也不要用 mock 的零费用覆盖原记录。
只在决定付费且原账本存在时，按
[显式 live 命令](eval/execution-reliability/README.md#explicit-live-run)
指定既有授权目录、配对 payload、原账本/原限额和新的外部 run 目录。
入口在收费 admission 前核对预算、catalog 与零请求 guard；任何缺口先
记录 blocked，不能自动尝试登录、换模型或收费重跑。

## Extension Contracts

Use the existing owner for each extension. Do not add another plugin registry or
an execution loop beside Pi.

| Extension | Entry and contract | Verification |
| --- | --- | --- |
| Lab scene | `TrialAdapter.prepare/execute`, registered in the supplied adapter mapping | [Runnable offline example](examples/lab/README.md); execution, cancellation, cleanup and persisted replay |
| Tool or Skill | Pi/Package resources with declared arguments and current dispatch permissions | Tool contract and permission tests; Pi keeps the model/Tool loop |
| Workbench page | `registerProductExtensionHosts`, a lazy page loader and manifest host type | Host registration/restore tests; feature code stays outside window management |
| Model on an existing protocol | Pi Provider configuration and its existing adapter | Model catalog/configuration tests; Session, Room and Lab orchestration stay unchanged |

For a page, keep rendering in the feature, interaction state with the page,
HTTP/SSE adaptation in the transport, and host wiring in `src/app`. Test and
preview transports must satisfy the same request/event contracts. Preview is an
explicit mode; a failed production request must not silently become a successful
Mock response. Start with `control-transport-variants`, `http-transport`,
`mock-transport` and `host-registry` tests when changing these boundaries.

The resume workflows under `.agents/skills` are project-scoped resources for
this workspace, discovered through the existing project Skill catalog. Their
upstream source and paths are recorded in [skills-lock.json](skills-lock.json).
They are optional for backend/frontend setup; keep real resume facts and outputs
outside the source tree, as their workflow contract requires. The product's Pi
Skills remain under `integrations/pi/skills`.

## Pull Requests

Describe:

- the user-visible entry and complete call chain affected;
- the old owner and new owner;
- compatibility and rollback behavior;
- focused and broad commands with real exit codes;
- privacy, security, Provider, Session, Room, and persistence impact;
- screenshots only when UI behavior changes.

Keep commits focused. Do not mix generated output or unrelated formatting with
the behavioral change.

## Third-Party Source

Do not copy third-party source without confirming its license and recording the
provenance in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). Distribution of
patched Squirrel must include its exact corresponding source and notices.
