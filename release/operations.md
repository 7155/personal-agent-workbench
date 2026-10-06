# Local Operations

[Project overview](../README.md) · [Build and installation](README.md)

These optional maintenance workflows apply to a configured local PAW installation.
Keep personal data and receipts outside Git.

## Archive Old App Install Backups

With an external archive configured, each successful release install keeps at
least the two newest `.paw-update.*` recovery directories in `~/Applications`. To
offload older ones, create an archive directory on a mounted external disk,
then put its absolute path on one line in
`~/Library/Application Support/RagIme/app-backup-archive-root.txt`. The
`RAG_IME_APP_BACKUP_ARCHIVE_ROOT` environment variable can override that file.

After a verified install, the existing installer copies inactive older backups
with `ditto`, compares contents with `rsync`, records each move in
`migration.jsonl`, and leaves a link at the original path. If the disk is
unmounted, full, or a backup is open, the local backup remains. The installer
does not archive application data, databases, Lab packages, or Pi generations.

## Import Existing Conversations

Completed local Codex conversations can be staged and then registered as
ordinary PAW conversation records without copying them into governed Memory:

```bash
python3 scripts/import_codex_conversation.py <codex-session-id>
python3 scripts/import_codex_conversation.py <codex-session-id> --write
```

The first command is a dry-run. The written record keeps its original title,
uses a provenance-marked Pi v3 transcript, and can be restored through the
existing Agent Session path. Its Codex origin stays in provenance rather than
being added to the visible title. Import intentionally keeps visible
user/assistant text and omits developer bootstrap, private reasoning, Tool
calls/results, and Runtime events; the receipt records that fidelity boundary.
Incomplete Codex rollouts are rejected unless `--allow-incomplete` is
explicitly supplied.

Bulk discovery keys every JSONL by its embedded `session_meta.id`, so local
symlinks, migrated copies, and stale Codex state paths do not create duplicate
PAW records. Repeat `--source-root` to include external archives; currently
open Codex rollouts are skipped by default:

```bash
python3 scripts/import_codex_conversations.py \
  --paw-python-root "$HOME/Library/Application Support/RagIme/app" \
  --source-root ~/.codex/sessions \
  --source-root ~/.codex/archived_sessions \
  --source-root ~/.codex/history_sync_backups \
  --source-root "/path/to/CodexData" \
  --allow-incomplete

# After reviewing the dry-run summary:
python3 scripts/import_codex_conversations.py \
  --source-root ~/.codex/sessions \
  --source-root ~/.codex/archived_sessions \
  --source-root ~/.codex/history_sync_backups \
  --source-root "/path/to/CodexData" \
  --allow-incomplete --write \
  --receipt /path/to/new-import-receipt.json
```

`--paw-python-root` makes live writes use the installed PAW store implementation
and schema while retaining the importer from the current checkout. Omit it for
an isolated database initialized by the same checkout.

## Review And Migrate A Legacy Memory Database

Historical curation is manual, copy-first, and fail-closed. Do not let a local
large model rewrite the production database. First export an immutable private
snapshot, make an explicit decision for every logical input and every existing
Atom, Book, and Phrase, and validate the complete manifest. Questions without a
durable assertion, workflow noise, failed receipts, duplicates, and one-turn
commands stay in Evidence/audit storage as `not_for_memory`; they do not become
Atoms, Books, Timelines, Phrases, or retrieval documents.

A production candidate additionally requires the configured embedding provider,
complete document/vector parity, caught-up projection checkpoints, an empty
failed/dead Outbox, and exact source fingerprints. No Timeline or Role Book draft
is auto-approved.

The `wisdom-weasel-rag-ime` value in the migration command below is a legacy
persisted project scope, not the current product name. Existing databases,
schema identifiers, `rag_ime` imports, and `RAG_IME_*` environment variables
retain compatibility until a separately versioned data migration can update
them without losing memory provenance.

```bash
DB="$HOME/Library/Application Support/RagIme/rag-ime.sqlite"
SNAPSHOT="/private/path/rag-ime-reviewed-source.sqlite"
EXPORT="/private/path/rag-ime-review-export.json"
MANIFEST="/private/path/rag-ime-review-manifest.json"
CANDIDATE="/private/path/rag-ime-reviewed-candidate.sqlite"
ROLLBACK="/path/to/rag-ime-before-semantic-v2.sqlite"

python3 scripts/review_memory_history.py export \
  --source "$DB" --snapshot "$SNAPSHOT" --output "$EXPORT" \
  --project wisdom-weasel-rag-ime --timezone Asia/Shanghai

# Review every exported logical input and existing memory item. Then assemble
# the reviewed parts and catalog audit into one complete manifest.
python3 scripts/review_memory_history.py assemble \
  --export "$EXPORT" --part /private/path/review-part-01.json \
  --existing-audit /private/path/existing-memory-audit.json \
  --output "$MANIFEST"
python3 scripts/review_memory_history.py validate \
  --export "$EXPORT" --manifest "$MANIFEST"

python3 scripts/apply_manual_memory_review.py \
  --source "$SNAPSHOT" --export "$EXPORT" --manifest "$MANIFEST" \
  --output "$CANDIDATE" --embedding-from-env

scripts/stop_rag_ime_runtime.sh
python3 scripts/activate_semantic_memory_candidate.py \
  --target "$DB" --candidate "$CANDIDATE" \
  --report "${CANDIDATE}.manual-review-report.json" --rollback "$ROLLBACK" \
  --confirm ACTIVATE_SEMANTIC_MEMORY_V2
scripts/build_control_center.sh install-stack --include-squirrel --include-pi --include-mlx
```

The activation report is bound to the candidate SHA-256 and all four reviewed
source fingerprints. They are revalidated immediately before the atomic
replacement, and the activated database is verified again afterward. Keep all
writers stopped from the final drift check through activation.

## Multi-Device Agent Gateway

The Agent Gateway remains a control-plane process on loopback `127.0.0.1:8768`.
It serves the production HTTP Control Center and the existing REST/SSE API from
one origin. Squirrel and the foreground typing path continue to use the
separate `127.0.0.1:8766` Sidecar; remote devices never enter that hot path.

For local development:

```bash
RAG_IME_CONTROL_TRANSPORT=http \
RAG_IME_CONTROL_BUILD_CHANNEL=production \
  scripts/build_control_center_web.sh
python3 -m rag_ime.cli agent-gateway \
  --host 127.0.0.1 \
  --port 8768 \
  --web-dist control-center-web/dist
```

To expose the already installed gateway to phones, tablets, and other Macs on
the same tailnet:

```bash
scripts/configure_agent_gateway_tailscale.sh enable --login you@example.com
scripts/configure_agent_gateway_tailscale.sh status
scripts/configure_agent_gateway_tailscale.sh disable
```

This route uses tailnet-only Tailscale Serve, never public Funnel. The gateway
accepts remote requests only when Serve supplies an allowlisted
`Tailscale-User-Login`; it then applies the canonical remote-safe route and
scope policy. Provider credentials, plugin installation, arbitrary files,
database apply operations, and input-method configuration remain local-only.
Session prompts and Room messages use durable `clientMessageId` receipts, so a
network retry cannot silently start the same Agent turn twice.

Installing an input method changes user-level macOS state. Use an attended
foreground session and verify the actual UI rather than trusting an HTTP
response:

```bash
scripts/doctor_squirrel_integration.sh
scripts/verify_squirrel_foreground_trace.sh
```

The optional voice lane, Active RAG provider, and Notion Worker each have
separate setup in the Web Control Center; none is required for the local
core.
