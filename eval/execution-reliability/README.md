# Agent execution reliability

The fault matrix exercises task/attempt identity, idempotent dispatch, Worker
leases, process recovery, cancellation, and late-result handling. It uses local
processes, SQLite, loopback networking, and controlled failure fixtures.

Run from the repository root:

```bash
python3 scripts/check_agent_execution_fault_matrix.py
```

The checker reports the results of the current source tests. Passing this matrix
does not establish remote deployment behavior, Provider availability, or native
foreground acceptance. Implementation plans and historical acceptance diaries
are maintained locally.

## Portable acceptance tasks

`paw-acceptance.v1.json` defines eight synthetic cases: authorized file handoff,
discussion without writes, PTC parallel reads with an explicit barrier, PTC
partial error, personal-profile correction, source withdrawal, multi-file
invoice reconciliation, and A/B workspace isolation. It pins fixture hashes and
upstream design references. These are adapted PAW tasks, not upstream benchmark
results. Expected outputs and oracle code stay outside the model workspace.

There are three distinct evidence layers:

- **Offline:** deterministic oracle tests plus actual SQLite/Gateway owner tests;
  no provider requests. This is the default CLI behavior.
- **Live runtime:** explicitly requested model runs through the existing
  `AgentLabTrialApplication` / `TrialAdapter`, exact Session/turn settlement, and
  the opt-in provider guard. Offline success does not imply live success.
- **Native UI:** not covered by these tasks. A passed CLI task is not a desktop
  foreground acceptance result.

The original short-delay concurrency observation remains a separate diagnostic;
barrier-v2 does not overwrite it. Missing synthetic Memory opt-in and initial
fixture setup errors are fixture issues, not claimed product vulnerabilities.

### Offline (no credentials, network or model)

```bash
python3 scripts/run_paw_acceptance.py
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
```

The storage tests create isolated temporary databases through the production
migrations and use the actual Memory/Gateway owners. Model runtime is disabled.
Tests do not change production Memory defaults or use a user's database.

### Explicit live run

Live execution needs an already-installed, verified paired Runtime, an existing
authorized Pi agent config, and the official Pi SDK module. This command never
installs software, copies credentials, logs in, or searches the home directory.
All sensitive/local paths are supplied explicitly. The guard currently supports
only `openai-codex/gpt-6.1-sol` at `xhigh`, using the verified Responses fetch/SSE
path. Other transports/providers fail closed; this hook is not a general network
sandbox for arbitrary subprocesses or imported networking libraries.

Example using caller-supplied path variables:

```bash
test -f "$ROUND_LEDGER" || { echo "Restore the original budget ledger before live execution" >&2; exit 1; }
python3 scripts/run_paw_acceptance.py --live \
  --repo "$REPO" --runtime-payload "$PAIRED_RUNTIME" \
  --state-root "$FRESH_RUN_STATE" --agent-dir "$EXISTING_AGENT_CONFIG" \
  --model gpt-6.1-sol \
  --case personal-profile-update-v1 --max-provider-calls 1 \
  --budget-usd "$ORIGINAL_ROUND_LIMIT_USD" --budget-ledger "$ROUND_LEDGER" \
  --enable-provider-guard
```

Use `--inherit-proxy-env` only when the existing configured proxy is needed.
No credential-bearing environment defaults are imported. `--help` makes no
provider request. Omitting `--live` always stays offline.
The example resumes an **existing ledger** and deliberately omits new-ledger
opening flags. See [macOS local acceptance](../../CONTRIBUTING.md#macos-本地接续与验收)
for the separate UI mock, staged deterministic Pi checks and their boundaries.

For a **new ledger**, opening known/unknown balances are mandatory, including
explicit `0` values for a truly new round. Carry prior spend and outstanding
reservations forward; creating another file does not create additional approved
budget. Opening balances are caller-provided, not independently verified. They
cannot later be reset in that ledger. For later runs, retain its original limit
and omit the opening-balance flags or repeat the identical opening values.

The SQLite ledger admits one active run atomically with `BEGIN IMMEDIATE`, checks
known + unknown + reserved against the explicit limit (at most USD 100), and
rejects a competing active run. Each case uses fresh state. A crash leaves its
reservation active; timeouts and uncertain transport/usage retain the full bound.
There is no automatic replay or blanket reservation release. Inspect the saved
receipts and reconcile uncertainty before continuing; do not delete the ledger
or reset opening balances to evade an unresolved reservation.

Reservation uses the **full model catalog context/output bounds and conservative
price tiers**, not a requested `maxTokens` field that a provider may ignore. A
small actual-cost target may therefore be insufficient to admit even one call.
Catalog metadata comes from the verified payload's own provider bridge, using
the same existing `models.json` as the Host. Its manifest and model-config digest
are bound to admission; provider/model/API/context/output limits must match the
executing Host. Older payloads whose bridge omits cost metadata fail closed.
No independent npm SDK or current-price web lookup is used. These are catalog
API-equivalent cost estimates, not measurements of a subscription invoice.
The guard must pass a zero-network proof through the selected production Host
before any paid case is admitted. Proof state has a permanently zero-call guard
separate from paid state. HTTP attempts, successful responses and exact-turn
transcript usage must agree before settlement. Failed tasks still retain costs;
only proven pre-network local errors can be excluded from billed-call counts.
Reconciliation occurs only after the Host, guard processes and gateway are
closed with no pending work. Even zero claims cannot release a reservation
before this proof; failed shutdown retains the full bound as unknown.

Run artifacts are written only to the supplied external state directory. Do not
commit them, authentication files, native logs, screenshots or machine paths.
The repository contains synthetic definitions and tests only.
