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

For Jev Room changes, run `python3 -m unittest discover -s tests -p
'test_jev*py'` and `python3 -m unittest tests.test_pi_exact_turn_cancellation`,
then the affected frontend tests. The opt-in [live Jev canary](integrations/jev/README.md#room-api-and-verification)
uses actual Provider calls and a separately supplied Pi payload; preserve its
reported evidence boundary and do not count a synthetic UI fixture as live
multi-Agent acceptance.

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
