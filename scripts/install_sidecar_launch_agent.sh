#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="${RAG_IME_LAUNCH_AGENT_LABEL:-com.rag-ime.sidecar}"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST_PATH="$PLIST_DIR/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs/RagIme"
APP_SUPPORT_DIR="${RAG_IME_APP_SUPPORT_DIR:-$HOME/Library/Application Support/RagIme}"
APP_CODE_DIR="$APP_SUPPORT_DIR/app"
MODEL_REGISTRY_EXPLICIT="${RAG_IME_MODEL_REGISTRY+x}"
MODEL_REGISTRY_ORIGIN="${RAG_IME_MODEL_REGISTRY_ORIGIN:-$([[ -n "$MODEL_REGISTRY_EXPLICIT" ]] && printf explicit || printf default)}"
MODEL_REGISTRY_PATH="${RAG_IME_MODEL_REGISTRY:-$APP_SUPPORT_DIR/models.json}"
INSTALL_MARKER="$APP_CODE_DIR/rag-ime-install-marker.json"
LAUNCH_WRAPPER="$APP_CODE_DIR/sidecar_launch.py"
RESTORE_SUPERVISOR="$APP_CODE_DIR/portable_restore_supervisor.py"
PI_INTEGRATION_SOURCE_DIR="$ROOT/integrations/pi"
PI_INTEGRATION_DIR="$APP_CODE_DIR/integrations/pi"
PI_EXTENSION_SOURCE="$PI_INTEGRATION_SOURCE_DIR/rag-ime-control.ts"
PI_NATIVE_SESSION_SOURCE="$PI_INTEGRATION_SOURCE_DIR/pi-native-session.ts"
PI_EXTENSION_TARGET="$PI_INTEGRATION_DIR/rag-ime-control.ts"
PI_SKILLS_SOURCE_DIR="$PI_INTEGRATION_SOURCE_DIR/skills"
PI_INIT_PROMPT_SOURCE="$PI_INTEGRATION_SOURCE_DIR/prompts/init.md"
VERTICAL_AGENT_SOURCE_DIR="$ROOT/examples/vertical_agents"
VERTICAL_AGENT_INSTALL_DIR="$APP_CODE_DIR/examples/vertical_agents"
EVAL_LAB_SOURCE_DIR="$ROOT/eval/interview-metrics"
EVAL_LAB_INSTALL_DIR="$APP_CODE_DIR/eval/interview-metrics"
EVAL_LAB_IMPORTER_SOURCE="$ROOT/scripts/import_agent_lab_experiments.py"
EVAL_LAB_IMPORTER_TARGET="$APP_CODE_DIR/scripts/import_agent_lab_experiments.py"
EVAL_LAB_RECEIPT_MANIFEST_SOURCE="$ROOT/scripts/list_agent_lab_install_receipts.py"
MANAGED_PI_SKILLS_DIR="$APP_SUPPORT_DIR/Agent/config/skills"
MANAGED_PI_PROMPTS_DIR="$APP_SUPPORT_DIR/Agent/config/prompts"
DB_PATH="${RAG_IME_DB_PATH:-$APP_SUPPORT_DIR/rag-ime.sqlite}"
PROJECT="${RAG_IME_PROJECT:-wisdom-weasel-rag-ime}"
HOST="${RAG_IME_SIDECAR_HOST:-127.0.0.1}"
PORT="${RAG_IME_SIDECAR_PORT:-8766}"
KNOWLEDGE_WORKER_PORT="${RAG_IME_KNOWLEDGE_WORKER_PORT:-8769}"
CORE_MODE="${RAG_IME_CORE_MODE:-local}"
CORE_COMMAND="${RAG_MEMORY_CORE_COMMAND:-}"
NO_SEED="${RAG_IME_SIDECAR_NO_SEED:-0}"
DRY_RUN="${RAG_IME_LAUNCH_AGENT_DRY_RUN:-0}"
RUNTIME_PROFILE="${RAG_IME_RUNTIME_PROFILE:-foreground-rag-proof}"
HEALTH_TIMEOUT_SECONDS="${RAG_IME_SIDECAR_HEALTH_TIMEOUT_SECONDS:-45}"
source "$ROOT/scripts/support/prebuilt_product.sh"
if ! paw_prebuilt_identity; then
SOURCE_COMMIT="$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || printf 'unknown')"
SOURCE_DIRTY="false"
if [[ -n "$(git -C "$ROOT" status --porcelain --untracked-files=no 2>/dev/null)" ]]; then
  SOURCE_DIRTY="true"
fi
fi

EMBEDDING_PROVIDER_HINT="${RAG_IME_EMBEDDING_PROVIDER:-}"
if [[ -z "$EMBEDDING_PROVIDER_HINT" && -f "$PLIST_PATH" ]]; then
  EMBEDDING_PROVIDER_HINT="$(
    /usr/libexec/PlistBuddy \
      -c "Print :EnvironmentVariables:RAG_IME_EMBEDDING_PROVIDER" \
      "$PLIST_PATH" 2>/dev/null || true
  )"
fi
REQUIRE_MLX_EMBEDDING=0
REQUIRE_LOCAL_EMBEDDING="${RAG_IME_REQUIRE_LOCAL_EMBEDDING:-0}"
EMBEDDING_PROVIDER_HINT_LOWER="$(printf '%s' "$EMBEDDING_PROVIDER_HINT" | tr '[:upper:]' '[:lower:]')"
case "$EMBEDDING_PROVIDER_HINT_LOWER" in
  mlx-bert|local-bge-mlx|mlx-bge) REQUIRE_MLX_EMBEDDING=1 ;;
  sentence-transformers|sentence_transformers) REQUIRE_LOCAL_EMBEDDING=1 ;;
esac

if [[ ! "$HEALTH_TIMEOUT_SECONDS" =~ ^[0-9]+$ ]] || (( HEALTH_TIMEOUT_SECONDS < 1 )); then
  echo "RAG_IME_SIDECAR_HEALTH_TIMEOUT_SECONDS must be a positive integer" >&2
  exit 1
fi

is_dry_run() {
  case "$DRY_RUN" in
    1|true|TRUE) return 0 ;;
    *) return 1 ;;
  esac
}

python_has_required_stdlib() {
  local candidate="$1"
  [[ "$candidate" == /* && -f "$candidate" && -x "$candidate" ]] || return 1
  "$candidate" - <<'PY' >/dev/null 2>&1
import hashlib
import sqlite3
import ssl
import sys

hashlib.md5(b"rag-ime").hexdigest()
raise SystemExit(0 if sys.version_info >= (3, 12) else 1)
PY
}

python_has_required_runtime() {
  local candidate="$1"
  RAG_IME_INSTALL_REQUIRE_MLX_EMBEDDING="$REQUIRE_MLX_EMBEDDING" \
  RAG_IME_INSTALL_REQUIRE_LOCAL_EMBEDDING="$REQUIRE_LOCAL_EMBEDDING" \
    "$candidate" - <<'PY' >/dev/null 2>&1
import os
import pypdf
import yaml

if os.environ.get("RAG_IME_INSTALL_REQUIRE_MLX_EMBEDDING") == "1":
    import mlx
    import transformers
if os.environ.get("RAG_IME_INSTALL_REQUIRE_LOCAL_EMBEDDING") == "1":
    import torch
    import sentence_transformers
PY
}

detect_python() {
  local candidate
  local candidates=()
  if [[ "$REQUIRE_MLX_EMBEDDING" == "1" ]]; then
    candidates+=("$APP_SUPPORT_DIR/KnowledgeRuntime/.venv/bin/python")
    candidates+=("$ROOT/.venv-mlx313/bin/python")
    candidates+=("$ROOT/.venv-mlx314sys/bin/python")
    candidates+=("$ROOT/.venv/bin/python")
  else
    candidates+=("$APP_SUPPORT_DIR/KnowledgeRuntime/.venv/bin/python")
    candidates+=("$ROOT/.venv/bin/python")
    candidates+=("$ROOT/.venv-mlx313/bin/python")
    candidates+=("$ROOT/.venv-mlx314sys/bin/python")
  fi
  candidates+=("/opt/homebrew/bin/python3")
  candidates+=("/opt/homebrew/opt/python@3.14/bin/python3.14")
  candidates+=("$(command -v python3 2>/dev/null || true)")
  candidates+=("/usr/local/bin/python3")

  for candidate in "${candidates[@]}"; do
    if ! python_has_required_stdlib "$candidate"; then
      continue
    fi
    if ! is_dry_run && ! python_has_required_runtime "$candidate"; then
      continue
    fi
    printf '%s\n' "$candidate"
    return 0
  done
  return 1
}

detect_ssl_cert_file() {
  local candidate
  local cert_path
  local candidates=()
  candidates+=("$PYTHON_EXECUTABLE")
  candidates+=("$(command -v python3 2>/dev/null || true)")
  candidates+=("/opt/homebrew/bin/python3")
  candidates+=("/opt/homebrew/opt/python@3.14/bin/python3.14")

  for candidate in "${candidates[@]}"; do
    if [[ -z "$candidate" || ! -x "$candidate" ]]; then
      continue
    fi
    cert_path="$("$candidate" - <<'PY' 2>/dev/null || true
try:
    import certifi
except Exception:
    raise SystemExit(1)
print(certifi.where())
PY
)"
    if [[ -n "$cert_path" && -f "$cert_path" ]]; then
      printf '%s\n' "$cert_path"
      return 0
    fi
  done
  return 1
}

if [[ -n "${RAG_IME_PYTHON:-}" ]] \
  && [[ "$RAG_IME_PYTHON" != /* || ! -f "$RAG_IME_PYTHON" || ! -x "$RAG_IME_PYTHON" ]]; then
  echo "RAG_IME_PYTHON must be an absolute executable file: $RAG_IME_PYTHON" >&2
  exit 1
fi
if [[ -n "${RAG_IME_KNOWLEDGE_PYTHON:-}" ]] \
  && [[ "$RAG_IME_KNOWLEDGE_PYTHON" != /* || ! -f "$RAG_IME_KNOWLEDGE_PYTHON" || ! -x "$RAG_IME_KNOWLEDGE_PYTHON" ]]; then
  echo "RAG_IME_KNOWLEDGE_PYTHON must be an absolute executable file: $RAG_IME_KNOWLEDGE_PYTHON" >&2
  exit 1
fi

PYTHON_EXECUTABLE="${RAG_IME_PYTHON:-$(detect_python || true)}"

if ! python_has_required_stdlib "$PYTHON_EXECUTABLE"; then
  echo "python executable must be an absolute executable Python 3.12+ with stdlib sqlite3/hashlib/ssl: $PYTHON_EXECUTABLE" >&2
  exit 1
fi

if ! is_dry_run && ! python_has_required_runtime "$PYTHON_EXECUTABLE"; then
  if [[ "$REQUIRE_MLX_EMBEDDING" == "1" ]]; then
    echo "python executable must be Python 3.12+ and import the project runtime plus MLX modules (pypdf/yaml/mlx/transformers): $PYTHON_EXECUTABLE" >&2
    echo "Run scripts/setup_knowledge_worker_env.sh or set RAG_IME_PYTHON to a compatible Python." >&2
  else
    echo "python executable must be Python 3.12+ and import the project runtime modules (pypdf/yaml) plus stdlib (sqlite3/hashlib/ssl): $PYTHON_EXECUTABLE" >&2
  fi
  echo "Set RAG_IME_PYTHON to a healthy Homebrew or project virtualenv Python." >&2
  exit 1
fi

EXISTING_KNOWLEDGE_PYTHON=""
if [[ -f "$PLIST_PATH" ]]; then
  EXISTING_KNOWLEDGE_PYTHON="$("$PYTHON_EXECUTABLE" - "$PLIST_PATH" <<'PY' 2>/dev/null || true
import plistlib
import sys

try:
    with open(sys.argv[1], "rb") as source:
        payload = plistlib.load(source)
    value = payload.get("EnvironmentVariables", {}).get("RAG_IME_KNOWLEDGE_PYTHON", "")
    if isinstance(value, str):
        print(value)
except Exception:
    pass
PY
)"
fi
KNOWLEDGE_PYTHON="${RAG_IME_KNOWLEDGE_PYTHON:-${EXISTING_KNOWLEDGE_PYTHON:-$PYTHON_EXECUTABLE}}"
if [[ "$KNOWLEDGE_PYTHON" != /* || ! -f "$KNOWLEDGE_PYTHON" || ! -x "$KNOWLEDGE_PYTHON" ]]; then
  echo "RAG_IME_KNOWLEDGE_PYTHON must be an absolute executable file: $KNOWLEDGE_PYTHON" >&2
  exit 1
fi
if ! "$KNOWLEDGE_PYTHON" - <<'PY' >/dev/null 2>&1; then
import sys

raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
  echo "RAG_IME_KNOWLEDGE_PYTHON must run Python 3.11 or newer: $KNOWLEDGE_PYTHON" >&2
  exit 1
fi
export RAG_IME_KNOWLEDGE_PYTHON="$KNOWLEDGE_PYTHON"

set -a
eval "$(PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_EXECUTABLE" -m rag_ime.runtime_profile --profile "$RUNTIME_PROFILE" --format shell)"
set +a
export RAG_IME_RUNTIME_PROFILE="$RUNTIME_PROFILE"

# The model registry is the product's authoritative hot-model selection. A
# clean install must not depend on a previous LaunchAgent having happened to
# contain predictor variables: without provider/base/model the Sidecar
# deliberately constructs NullPredictionProvider and the healthy resident
# MiniMind service never receives a request. Import the resolved plan here,
# while preserving any explicit non-empty install-time override.
if [[ "$MODEL_REGISTRY_ORIGIN" == "explicit" && ! -f "$MODEL_REGISTRY_PATH" ]]; then
  echo "Explicit model registry does not exist: $MODEL_REGISTRY_PATH" >&2
  exit 1
fi
if [[ -f "$MODEL_REGISTRY_PATH" ]]; then
  if ! MODEL_RUNTIME_ENV="$(
    PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" \
      "$PYTHON_EXECUTABLE" -m rag_ime.model_runtime \
      --registry "$MODEL_REGISTRY_PATH" \
      --lane hot \
      --format shell
  )"; then
    echo "Active hot model registry entry is invalid or its artifact is missing: $MODEL_REGISTRY_PATH" >&2
    exit 1
  fi
  while IFS= read -r assignment; do
    [[ -n "$assignment" ]] || continue
    key="${assignment%%=*}"
    case "$key" in
      RAG_IME_PREDICTOR_*|RAG_IME_MLX_*|RAG_IME_MODEL_ID|RAG_IME_MODEL_FINGERPRINT|RAG_IME_MODEL_RUNTIME)
        if [[ -z "$(printenv "$key" 2>/dev/null || true)" ]]; then
          eval "export $assignment"
        fi
        ;;
    esac
  done <<< "$MODEL_RUNTIME_ENV"
  export RAG_IME_MODEL_REGISTRY="$MODEL_REGISTRY_PATH"
  export RAG_IME_PREDICTOR_TIMEOUT_MS="${RAG_IME_PREDICTOR_TIMEOUT_MS:-3000}"
  export RAG_IME_PREDICTOR_MAX_TOKENS="${RAG_IME_PREDICTOR_MAX_TOKENS:-8}"
  export RAG_IME_PREDICTOR_FAILURE_COOLDOWN_MS="${RAG_IME_PREDICTOR_FAILURE_COOLDOWN_MS:-0}"
fi

if [[ ! -f "$ROOT/scripts/sidecar_launch.py" ]]; then
  echo "sidecar launch wrapper not found: $ROOT/scripts/sidecar_launch.py" >&2
  exit 1
fi

for source_file in "$PI_EXTENSION_SOURCE" "$PI_NATIVE_SESSION_SOURCE"; do
  if [[ ! -f "$source_file" || -L "$source_file" ]]; then
    echo "controlled Pi integration source not found or is a symlink: $source_file" >&2
    exit 1
  fi
done
if [[ ! -d "$PI_SKILLS_SOURCE_DIR" || -L "$PI_SKILLS_SOURCE_DIR" ]]; then
  echo "controlled Pi skills source not found or is a symlink: $PI_SKILLS_SOURCE_DIR" >&2
  exit 1
fi
if [[ ! -f "$PI_INIT_PROMPT_SOURCE" || -L "$PI_INIT_PROMPT_SOURCE" ]]; then
  echo "controlled Pi init prompt not found or is a symlink: $PI_INIT_PROMPT_SOURCE" >&2
  exit 1
fi
if [[ -n "$(find "$PI_INTEGRATION_SOURCE_DIR" -type l -print -quit)" ]]; then
  echo "controlled Pi integration source must not contain symlinks: $PI_INTEGRATION_SOURCE_DIR" >&2
  exit 1
fi
if [[ ! -d "$VERTICAL_AGENT_SOURCE_DIR" || -L "$VERTICAL_AGENT_SOURCE_DIR" ]]; then
  echo "vertical Agent fixture source not found or is a symlink: $VERTICAL_AGENT_SOURCE_DIR" >&2
  exit 1
fi
if [[ -n "$(find "$VERTICAL_AGENT_SOURCE_DIR" -type l -print -quit)" ]]; then
  echo "vertical Agent fixture source must not contain symlinks: $VERTICAL_AGENT_SOURCE_DIR" >&2
  exit 1
fi
if [[ ! -f "$EVAL_LAB_SOURCE_DIR/agent-experiments.v1.json" ]]; then
  echo "missing Agent Lab projection ledger: $EVAL_LAB_SOURCE_DIR/agent-experiments.v1.json" >&2
  exit 1
fi
if [[ ! -f "$EVAL_LAB_IMPORTER_SOURCE" ]]; then
  echo "missing Agent Lab public projection module: $EVAL_LAB_IMPORTER_SOURCE" >&2
  exit 1
fi
if [[ ! -f "$EVAL_LAB_RECEIPT_MANIFEST_SOURCE" ]]; then
  echo "missing Agent Lab receipt manifest helper: $EVAL_LAB_RECEIPT_MANIFEST_SOURCE" >&2
  exit 1
fi
# Resolve and validate every bound receipt before replacing the installed
# projection. A plain assignment preserves the helper's non-zero exit status;
# process substitution would let a failed manifest silently produce a partial
# installation.
EVAL_LAB_RECEIPTS="$(
  "$PYTHON_EXECUTABLE" \
    "$EVAL_LAB_RECEIPT_MANIFEST_SOURCE" \
    "$EVAL_LAB_SOURCE_DIR/agent-experiments.v1.json"
)"

SSL_CERT_FILE_DEFAULT="${SSL_CERT_FILE:-$(detect_ssl_cert_file || true)}"

# Lab freezes these compiled controls into each exported App. Install them
# with the runtime, including when this script runs before the main web build.
PORTABLE_UI_SOURCE="$ROOT/control-center-web/.generated/portable-agent-ui"
if [[ ! -s "$PORTABLE_UI_SOURCE/agent-ui.js" || ! -s "$PORTABLE_UI_SOURCE/agent-ui.css" ]]; then
  (cd "$ROOT/control-center-web" && pnpm run build:app-ui)
fi
PORTABLE_UI_TARGET="$APP_CODE_DIR/control-center-web/.generated/portable-agent-ui"

mkdir -p "$PLIST_DIR" "$LOG_DIR" "$(dirname "$DB_PATH")" "$APP_CODE_DIR"
mkdir -p "$PORTABLE_UI_TARGET"
cp "$PORTABLE_UI_SOURCE/agent-ui.js" "$PORTABLE_UI_SOURCE/agent-ui.css" "$PORTABLE_UI_TARGET/"
rm -f "$INSTALL_MARKER"
rm -rf "$APP_CODE_DIR/rag_ime"
cp -R "$ROOT/rag_ime" "$APP_CODE_DIR/rag_ime"
rm -rf -- "$EVAL_LAB_INSTALL_DIR"
mkdir -p "$EVAL_LAB_INSTALL_DIR/runs"
mkdir -p "$(dirname "$EVAL_LAB_IMPORTER_TARGET")"
cp "$EVAL_LAB_SOURCE_DIR/agent-experiments.v1.json" "$EVAL_LAB_INSTALL_DIR/agent-experiments.v1.json"
cp "$EVAL_LAB_IMPORTER_SOURCE" "$EVAL_LAB_IMPORTER_TARGET"
if [[ -n "$EVAL_LAB_RECEIPTS" ]]; then
  while IFS= read -r receipt; do
    cp "$receipt" "$EVAL_LAB_INSTALL_DIR/runs/$(basename "$receipt")"
  done <<< "$EVAL_LAB_RECEIPTS"
fi
rm -rf "$VERTICAL_AGENT_INSTALL_DIR"
mkdir -p "$(dirname "$VERTICAL_AGENT_INSTALL_DIR")"
cp -R "$VERTICAL_AGENT_SOURCE_DIR" "$VERTICAL_AGENT_INSTALL_DIR"
cp "$ROOT/scripts/sidecar_launch.py" "$LAUNCH_WRAPPER"
cp "$ROOT/scripts/portable_restore_supervisor.py" "$RESTORE_SUPERVISOR"
chmod 700 "$RESTORE_SUPERVISOR"
rm -rf "$PI_INTEGRATION_DIR"
mkdir -p "$PI_INTEGRATION_DIR"
cp -R "$PI_INTEGRATION_SOURCE_DIR"/. "$PI_INTEGRATION_DIR"/
chmod 644 "$PI_EXTENSION_TARGET" "$PI_INTEGRATION_DIR/pi-native-session.ts"

# Protocol v2 loads Agent-dir skills before payload-bundled copies. Refreshing
# these product-owned directories makes skill updates part of the normal stack
# install even when the verified Pi executable payload itself is reused.
mkdir -p "$MANAGED_PI_SKILLS_DIR"
# Remove only retired product-owned names. Unrecognized directories remain
# user-owned, while old aliases must not compete with the compact native catalog.
RETIRED_MANAGED_PI_SKILLS=(
  grill-me
  grill-me-docs
  implementation-execution
  managed-task-execution
  project-devlog
  quality-gate
  rag-ime-memory-curator
  rag-ime-plugin-creator
  requirement-alignment
  review-feedback-resolution
  room-delivery-closure
  room-delivery-self-check
  room-implementation-execution
  room-implementation-planning
  room-independent-vision-review
  room-managed-task-execution
  room-requirement-clarification
  room-review-feedback-resolution
  room-solution-convergence
  room-structured-handoff
  room-systematic-debugging
  room-test-driven-implementation
  solution-convergence
  structured-handoff
  structured-result-presentation
  work-document-archive
)
for retired_skill_name in "${RETIRED_MANAGED_PI_SKILLS[@]}"; do
  rm -rf "$MANAGED_PI_SKILLS_DIR/$retired_skill_name"
done
for skill_source in "$PI_SKILLS_SOURCE_DIR"/*; do
  if [[ ! -d "$skill_source" || -L "$skill_source" ]]; then
    continue
  fi
  skill_name="$(basename "$skill_source")"
  skill_target="$MANAGED_PI_SKILLS_DIR/$skill_name"
  rm -rf "$skill_target"
  cp -R "$skill_source" "$skill_target"
done

# `/init` is a Pi-native prompt template, not a PAW command reimplementation.
# Replace only the product-owned filename and preserve every unrelated user
# prompt already installed in the Agent directory.
mkdir -p "$MANAGED_PI_PROMPTS_DIR"
rm -f "$MANAGED_PI_PROMPTS_DIR/init.md"
cp "$PI_INIT_PROMPT_SOURCE" "$MANAGED_PI_PROMPTS_DIR/init.md"
chmod 644 "$MANAGED_PI_PROMPTS_DIR/init.md"

write_install_marker() {
  "$PYTHON_EXECUTABLE" - "$INSTALL_MARKER" "$ROOT" "$SOURCE_COMMIT" "$SOURCE_DIRTY" "$PYTHON_EXECUTABLE" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

target = Path(sys.argv[1])
target.write_text(
    json.dumps(
        {
            "schemaVersion": "rag-ime.component-install-marker.v1",
            "component": "sidecar-runtime",
            "sourceRoot": sys.argv[2],
            "sourceCommit": sys.argv[3],
            "sourceDirty": sys.argv[4] == "true",
            "installedAt": datetime.now(timezone.utc).isoformat(),
            "pythonExecutable": sys.argv[5],
        },
        ensure_ascii=False,
        indent=2,
    )
    + "\n",
    encoding="utf-8",
)
PY
}

# Keep the explicit high-intelligence route usable after every reinstall. The
# LaunchAgent cannot inherit an interactive shell's secrets, so install one
# stable, permission-restricted env file and point the service at it.
MODEL_ENV_SOURCE="${RAG_IME_DEEPSEEK_ENV:-${RAG_IME_MODEL_ENV:-}}"
if [[ -z "$MODEL_ENV_SOURCE" ]]; then
  for candidate in "$APP_SUPPORT_DIR/deepseek.env" "$ROOT/.rag-ime-data/deepseek.env"; do
    if [[ -f "$candidate" ]]; then
      MODEL_ENV_SOURCE="$candidate"
      break
    fi
  done
fi
if [[ -n "$MODEL_ENV_SOURCE" && -f "$MODEL_ENV_SOURCE" ]]; then
  INSTALLED_MODEL_ENV="$APP_SUPPORT_DIR/deepseek.env"
  if [[ "$MODEL_ENV_SOURCE" != "$INSTALLED_MODEL_ENV" ]]; then
    cp "$MODEL_ENV_SOURCE" "$INSTALLED_MODEL_ENV"
  fi
  chmod 600 "$INSTALLED_MODEL_ENV"
  export RAG_IME_DEEPSEEK_ENV="$INSTALLED_MODEL_ENV"
  export RAG_IME_DEEPSEEK_ACTIVE_RAG="${RAG_IME_DEEPSEEK_ACTIVE_RAG:-1}"
fi

# Optional Pi-only provider catalogs (for example an OpenCode JSON export) are
# installed beside the model env with the same owner-only permissions. The
# Python runtime translates their credentials to child-process environment
# references; generated Pi models.json files never contain the literal keys.
PI_PROVIDER_CONFIG_SOURCE="${RAG_IME_PI_PROVIDER_CONFIG:-}"
if [[ -z "$PI_PROVIDER_CONFIG_SOURCE" && -f "$APP_SUPPORT_DIR/pi-providers.json" ]]; then
  PI_PROVIDER_CONFIG_SOURCE="$APP_SUPPORT_DIR/pi-providers.json"
fi
if [[ -n "$PI_PROVIDER_CONFIG_SOURCE" && -f "$PI_PROVIDER_CONFIG_SOURCE" ]]; then
  INSTALLED_PI_PROVIDER_CONFIG="$APP_SUPPORT_DIR/pi-providers.json"
  if [[ "$PI_PROVIDER_CONFIG_SOURCE" != "$INSTALLED_PI_PROVIDER_CONFIG" ]]; then
    cp "$PI_PROVIDER_CONFIG_SOURCE" "$INSTALLED_PI_PROVIDER_CONFIG"
  fi
  chmod 600 "$INSTALLED_PI_PROVIDER_CONFIG"
  export RAG_IME_PI_PROVIDER_CONFIG="$INSTALLED_PI_PROVIDER_CONFIG"
fi

ROOT="$ROOT" \
LABEL="$LABEL" \
PLIST_PATH="$PLIST_PATH" \
LOG_DIR="$LOG_DIR" \
APP_SUPPORT_DIR="$APP_SUPPORT_DIR" \
APP_CODE_DIR="$APP_CODE_DIR" \
PI_EXTENSION_TARGET="$PI_EXTENSION_TARGET" \
PYTHON_EXECUTABLE="$PYTHON_EXECUTABLE" \
LAUNCH_WRAPPER="$LAUNCH_WRAPPER" \
DB_PATH="$DB_PATH" \
PROJECT="$PROJECT" \
HOST="$HOST" \
PORT="$PORT" \
CORE_MODE="$CORE_MODE" \
CORE_COMMAND="$CORE_COMMAND" \
NO_SEED="$NO_SEED" \
SSL_CERT_FILE_DEFAULT="$SSL_CERT_FILE_DEFAULT" \
"$PYTHON_EXECUTABLE" - <<'PY'
import os
import plistlib
from pathlib import Path

root = os.environ["ROOT"]
app_support_dir = os.environ["APP_SUPPORT_DIR"]
app_code_dir = os.environ["APP_CODE_DIR"]
label = os.environ["LABEL"]
existing_env = {}
try:
    with open(os.environ["PLIST_PATH"], "rb") as fh:
        existing_payload = plistlib.load(fh)
    maybe_env = existing_payload.get("EnvironmentVariables")
    if isinstance(maybe_env, dict):
        existing_env = {str(key): str(value) for key, value in maybe_env.items() if value is not None}
except Exception:
    existing_env = {}

args = [
    os.environ["PYTHON_EXECUTABLE"],
    os.environ["LAUNCH_WRAPPER"],
    "--core-mode",
    os.environ["CORE_MODE"],
    "--db-path",
    os.environ["DB_PATH"],
]
core_command = os.environ.get("CORE_COMMAND", "")
if core_command:
    args.extend(["--core-command", core_command])
args.extend(
    [
        "sidecar-server",
        "--host",
        os.environ["HOST"],
        "--port",
        os.environ["PORT"],
        "--project",
        os.environ["PROJECT"],
    ]
)
if os.environ.get("NO_SEED") in {"1", "true", "TRUE", "yes", "YES"}:
    args.append("--no-seed")

env_vars = {
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONUNBUFFERED": "1",
    "RAG_IME_ROOT": app_code_dir,
    "RAG_IME_INSTALL_MARKER": str(Path(app_code_dir) / "rag-ime-install-marker.json"),
    "RAG_IME_SOURCE_ROOT": root,
    "RAG_IME_APP_SUPPORT_DIR": app_support_dir,
    "RAG_IME_DB_PATH": os.environ["DB_PATH"],
    "RAG_IME_CORE_MODE": os.environ["CORE_MODE"],
    "RAG_IME_RUNTIME_PROFILE": os.environ.get("RAG_IME_RUNTIME_PROFILE", "foreground-rag-proof"),
    "RAG_IME_KNOWLEDGE_PYTHON": os.environ["RAG_IME_KNOWLEDGE_PYTHON"],
    "RAG_IME_ENABLE_POST_COMMIT_ASYNC_COMPLETION": "1",
    "RAG_IME_ENABLE_POST_COMMIT_AUTO_MODEL": "1",
    "RAG_IME_LOCAL_MODEL_QUALITY_GATE_MODE": os.environ.get(
        "RAG_IME_LOCAL_MODEL_QUALITY_GATE_MODE",
        "observe" if os.environ.get("RAG_IME_RUNTIME_PROFILE") == "foreground-rag-proof" else "strict",
    ),
    "RAG_IME_ENABLE_COMPOSING_MODEL": os.environ.get("RAG_IME_ENABLE_COMPOSING_MODEL", os.environ["RAG_IME_PROFILE_COMPOSITION_AI"]),
    "RAG_IME_ENABLE_PINYIN_CONSTRAINED_MODEL": os.environ.get("RAG_IME_ENABLE_PINYIN_CONSTRAINED_MODEL", os.environ["RAG_IME_PROFILE_PINYIN_CONSTRAINED_MODEL"]),
    "RAG_IME_POST_COMMIT_FIRST_RESPONSE_MS": os.environ.get("RAG_IME_POST_COMMIT_FIRST_RESPONSE_MS", "180"),
    "RAG_IME_PROGRESSIVE_FOLLOW_UP_RETRY_MS": os.environ.get("RAG_IME_PROGRESSIVE_FOLLOW_UP_RETRY_MS", "180"),
    "RAG_IME_POST_COMMIT_COMPLETION_TTL_MS": os.environ.get("RAG_IME_POST_COMMIT_COMPLETION_TTL_MS", os.environ["RAG_IME_PROFILE_POST_COMMIT_COMPLETION_TTL_MS"]),
    "RAG_IME_POST_COMMIT_COMPLETION_CACHE_MAX_JOBS": "8",
    "RAG_IME_POST_COMMIT_MODEL_HARD_TIMEOUT_MS": os.environ.get("RAG_IME_POST_COMMIT_MODEL_HARD_TIMEOUT_MS", os.environ["RAG_IME_PROFILE_POST_COMMIT_MODEL_HARD_TIMEOUT_MS"]),
    "RAG_IME_POST_COMMIT_MODEL_BUDGET_MS": os.environ.get("RAG_IME_POST_COMMIT_MODEL_BUDGET_MS", os.environ["RAG_IME_PROFILE_POST_COMMIT_MODEL_BUDGET_MS"]),
    "RAG_IME_REQUIRE_FOREGROUND_CONTEXT_FOR_POST_COMMIT": os.environ.get("RAG_IME_REQUIRE_FOREGROUND_CONTEXT_FOR_POST_COMMIT", os.environ["RAG_IME_PROFILE_REQUIRE_FOREGROUND_CONTEXT"]),
    "RAG_IME_FOREGROUND_CONTEXT_MAX_FRESHNESS_MS": os.environ.get("RAG_IME_FOREGROUND_CONTEXT_MAX_FRESHNESS_MS", os.environ["RAG_IME_PROFILE_FOREGROUND_CONTEXT_MAX_FRESHNESS_MS"]),
    "RAG_IME_PROGRESSIVE_FOREGROUND_CONTEXT_MAX_FRESHNESS_MS": os.environ.get("RAG_IME_PROGRESSIVE_FOREGROUND_CONTEXT_MAX_FRESHNESS_MS", "2500"),
    "RAG_IME_POST_COMMIT_PENDING_PREVIEW": os.environ.get("RAG_IME_POST_COMMIT_PENDING_PREVIEW", os.environ["RAG_IME_PROFILE_ASSISTANT_PENDING_PREVIEW"]),
    # The resident MiniMind model returns a complete Top-3 batch in the first
    # foreground window. Token-prefix presentation used to expose only the
    # first growing phrase (for example "先记录") and hid the other branches.
    "RAG_IME_POST_COMMIT_PRESENTATION_STREAM": os.environ.get("RAG_IME_POST_COMMIT_PRESENTATION_STREAM", "0"),
    "RAG_IME_ENABLE_DEMO_SAFE_FALLBACK": "0",
    "RAG_IME_MODEL_HOLDOVER_MAX_ENTRIES": "32",
    "RAG_IME_PREDICTION_MANAGER_MAX_ENTRIES": "16",
    "RAG_IME_REFRESH_DEBOUNCE_MAX_ENTRIES": "128",
    "RAG_IME_POST_COMMIT_PRESENTATION_STREAM_MAX_ENTRIES": "32",
    "RAG_IME_SUGGESTION_CACHE_SIZE": "32",
    "RAG_IME_HYBRID_RAG_CORE": os.environ.get("RAG_IME_HYBRID_RAG_CORE", os.environ["RAG_IME_PROFILE_HYBRID_RAG_CORE"]),
    "RAG_IME_RAG_DIRECT_DISPLAY": os.environ.get("RAG_IME_RAG_DIRECT_DISPLAY", os.environ["RAG_IME_PROFILE_RAG_DIRECT_DISPLAY"]),
    "RAG_IME_AUTO_PREDICT_IDLE_MS": os.environ.get("RAG_IME_AUTO_PREDICT_IDLE_MS", "180"),
    "RAG_IME_AUTO_PREDICT_MIN_DELTA_CHARS": os.environ.get("RAG_IME_AUTO_PREDICT_MIN_DELTA_CHARS", "3"),
    "RAG_IME_AUTO_PREDICT_MAX_CALLS_PER_10S": os.environ.get("RAG_IME_AUTO_PREDICT_MAX_CALLS_PER_10S", "6"),
    "RAG_IME_AUTO_PREDICT_IGNORE_COOLDOWN_MS": os.environ.get("RAG_IME_AUTO_PREDICT_IGNORE_COOLDOWN_MS", "600"),
    "RAG_IME_T0_DIRECT_MEMORY_THRESHOLD": os.environ.get("RAG_IME_T0_DIRECT_MEMORY_THRESHOLD", "0.86"),
    "RAG_IME_POST_COMMIT_ACTIVE_RAG_BUTTON": "1",
    "RAG_IME_DEEPSEEK_THINKING": "disabled",
    "RAG_IME_DEEPSEEK_REASONING_EFFORT": "low",
    "RAG_IME_DEEPSEEK_MAX_TOKENS": "96",
    "RAG_IME_DEEPSEEK_ACTIVE_RAG_MAX_TOKENS": "4096",
    "RAG_IME_PINYIN_FUZZY_ENABLED": "1",
    "RAG_IME_PINYIN_FUZZY_PROFILE": "sichuan-mild",
    "RAG_IME_PINYIN_FUZZY_Z_ZH": "1",
    "RAG_IME_PINYIN_FUZZY_C_CH": "1",
    "RAG_IME_PINYIN_FUZZY_S_SH": "1",
    "RAG_IME_PINYIN_FUZZY_EN_ENG": "1",
    "RAG_IME_PINYIN_FUZZY_IN_ING": "1",
    "RAG_IME_PINYIN_FUZZY_ONG_ON": "1",
    "RAG_IME_PINYIN_FUZZY_N_L": "0",
    "RAG_IME_PINYIN_FUZZY_F_H": "0",
}
ssl_cert_file = os.environ.get("SSL_CERT_FILE_DEFAULT", "")
if ssl_cert_file:
    env_vars["SSL_CERT_FILE"] = ssl_cert_file
preserve_existing_keys = {
    "SSL_CERT_FILE",
    "RAG_IME_MODEL_ENV",
    "RAG_IME_MEMORY_GENERATOR_ENV",
    "RAG_IME_VCP_REBUILD_ENV",
    "RAG_IME_AI_PROVIDER",
    "RAG_IME_AI_BASE_URL",
    "RAG_IME_AI_MODEL",
    "RAG_IME_AI_WIRE_API",
    "RAG_IME_AI_TIMEOUT_SECONDS",
    "RAG_IME_AI_REASONING_EFFORT",
    "RAG_IME_AI_DISABLE_RESPONSE_STORAGE",
    "RAG_IME_AI_API_KEY",
    "RAG_IME_DEEPSEEK_ENV",
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_BASE_URL",
    "DEEPSEEK_MODEL",
    "DEEPSEEK_WIRE_API",
    "DEEPSEEK_STREAM",
    "DEEPSEEK_JSON",
    "RAG_IME_DEEPSEEK_API_KEY",
    "RAG_IME_DEEPSEEK_BASE_URL",
    "RAG_IME_DEEPSEEK_MODEL",
    "RAG_IME_DEEPSEEK_WIRE_API",
    "RAG_IME_DEEPSEEK_STREAM",
    "RAG_IME_DEEPSEEK_JSON",
    "RAG_IME_DEEPSEEK_TIMEOUT_SECONDS",
    "RAG_IME_DEEPSEEK_THINKING",
    "RAG_IME_DEEPSEEK_REASONING_EFFORT",
    "RAG_IME_DEEPSEEK_MAX_TOKENS",
    "RAG_IME_DEEPSEEK_ACTIVE_RAG_MAX_TOKENS",
    "RAG_IME_DEEPSEEK_MEMORY_BOOK_MAX_TOKENS",
    "RAG_IME_DEEPSEEK_KNOWLEDGE_MAX_TOKENS",
    "RAG_IME_DEEPSEEK_ACTIVE_RAG",
    "RAG_IME_DEEPSEEK_POST_COMMIT",
    "RAG_IME_DEEPSEEK_PREVIEW_TOKEN",
    "RAG_IME_PI_ENABLED",
    "RAG_IME_PI_EXECUTABLE",
    "RAG_IME_PI_NODE",
    "RAG_IME_PI_VERSION",
    "RAG_IME_PI_PROVIDER",
    "RAG_IME_PI_MODEL",
    "RAG_IME_PI_PROVIDER_CONFIG",
    "RAG_IME_PI_TOOLS",
    "RAG_IME_PI_IDLE_TIMEOUT_SECONDS",
    "RAG_IME_NOTION_ENV",
    "RAG_IME_NOTION_WORKER_URL",
    "RAG_IME_NOTION_STATUS_URL",
    "RAG_IME_NOTION_STATUS_TOKEN",
    "RAG_IME_NOTION_TOKEN",
    "RAG_IME_NOTION_DATA_SOURCE_ID",
    "RAG_IME_NOTION_WEBHOOK_SECRET",
    "RAG_IME_NOTION_POLL_INTERVAL_MS",
    "RAG_IME_NOTION_TIMEOUT_MS",
    "RAG_IME_PREDICTOR_PROVIDER",
    "RAG_IME_PREDICTOR_ENV",
    "RAG_IME_PREDICTOR_BASE_URL",
    "RAG_IME_PREDICTOR_MODEL",
    "RAG_IME_PREDICTOR_PROFILE",
    "RAG_IME_PREDICTOR_PROMPT_MODE",
    "RAG_IME_PREDICTOR_TIMEOUT_MS",
    "RAG_IME_PREDICTOR_MAX_TOKENS",
    "RAG_IME_PREDICTOR_TEMPERATURE",
    "RAG_IME_PREDICTOR_TOP_P",
    "RAG_IME_PREDICTOR_DISABLE_THINKING",
    "RAG_IME_PREDICTOR_FAILURE_COOLDOWN_MS",
    "RAG_IME_PREDICTOR_FAILURE_LATENCY_MS",
    "RAG_IME_PREDICTOR_EXTRA_BODY_JSON",
    "RAG_IME_PREDICTOR_EXTRA_HEADERS_JSON",
    "RAG_IME_PREDICTOR_API_KEY",
    "RAG_IME_MLX_MODEL",
    "RAG_IME_MODEL_REGISTRY",
    "RAG_IME_MODEL_ID",
    "RAG_IME_MODEL_FINGERPRINT",
    "RAG_IME_PINYIN_FUZZY_ENABLED",
    "RAG_IME_PINYIN_FUZZY_PROFILE",
    "RAG_IME_PINYIN_FUZZY_Z_ZH",
    "RAG_IME_PINYIN_FUZZY_C_CH",
    "RAG_IME_PINYIN_FUZZY_S_SH",
    "RAG_IME_PINYIN_FUZZY_EN_ENG",
    "RAG_IME_PINYIN_FUZZY_IN_ING",
    "RAG_IME_PINYIN_FUZZY_ONG_ON",
    "RAG_IME_PINYIN_FUZZY_N_L",
    "RAG_IME_PINYIN_FUZZY_F_H",
    "RAG_IME_RIME_CACHE_TTL_MS",
    "RAG_IME_SUGGESTION_CACHE_SIZE",
    "RAG_IME_MODEL_HOLDOVER_MAX_ENTRIES",
    "RAG_IME_LOCAL_MODEL_QUALITY_GATE_MODE",
    "RAG_IME_PREDICTION_MANAGER_MAX_ENTRIES",
    "RAG_IME_REFRESH_DEBOUNCE_MAX_ENTRIES",
    "RAG_IME_POST_COMMIT_COMPLETION_CACHE_MAX_JOBS",
    "RAG_IME_POST_COMMIT_PRESENTATION_STREAM_MAX_ENTRIES",
    "RAG_IME_EMBEDDING_PROVIDER",
    "RAG_IME_EMBEDDING_BASE_URL",
    "RAG_IME_EMBEDDING_MODEL",
    "RAG_IME_EMBEDDING_BITS",
    "RAG_IME_EMBEDDING_GROUP_SIZE",
    "RAG_IME_EMBEDDING_QUERY_PREFIX",
    "RAG_IME_EMBEDDING_DOCUMENT_PREFIX",
    "RAG_IME_EMBEDDING_CACHE_DIR",
    "RAG_IME_EMBEDDING_LOCAL_FILES_ONLY",
    "RAG_IME_EMBEDDING_API_KEY",
    "RAG_IME_EMBEDDING_TIMEOUT_MS",
    "RAG_IME_EMBEDDING_DIMENSIONS",
    "RAG_IME_EMBEDDING_CACHE_SIZE",
    "RAG_IME_EMBEDDING_WARMUP",
    "RAG_IME_EMBEDDING_EXTRA_BODY_JSON",
    "RAG_IME_EMBEDDING_EXTRA_HEADERS_JSON",
    "RAG_IME_VECTOR_CANDIDATES",
    "RAG_IME_VECTOR_WEIGHT",
    "RAG_IME_VECTOR_AUTO_REBUILD_LIMIT",
    "RAG_IME_KNOWLEDGE_DENSE_BACKEND",
}
for key in (
    "RAG_IME_RIME_CACHE_TTL_MS",
    "RAG_IME_SUGGESTION_CACHE_SIZE",
    "RAG_IME_HISTORY_CONTEXT_EVENTS",
    "RAG_IME_HISTORY_CONTEXT_CHARS",
    "RAG_IME_MODEL_CONTEXT_EVENTS",
    "RAG_IME_MODEL_CONTEXT_CHARS",
    "RAG_IME_MODEL_LANE_MAX_CANDIDATES",
    "RAG_IME_MODEL_LANE_LEASE_TTL_MS",
    "RAG_IME_ENABLE_POST_COMMIT_ASYNC_COMPLETION",
    "RAG_IME_ENABLE_POST_COMMIT_AUTO_MODEL",
    "RAG_IME_LOCAL_MODEL_QUALITY_GATE_MODE",
    "RAG_IME_ENABLE_COMPOSING_MODEL",
    "RAG_IME_ENABLE_PINYIN_CONSTRAINED_MODEL",
    "RAG_IME_POST_COMMIT_FIRST_RESPONSE_MS",
    "RAG_IME_PROGRESSIVE_FOLLOW_UP_RETRY_MS",
    "RAG_IME_POST_COMMIT_COMPLETION_TTL_MS",
    "RAG_IME_POST_COMMIT_COMPLETION_CACHE_MAX_JOBS",
    "RAG_IME_POST_COMMIT_MODEL_HARD_TIMEOUT_MS",
    "RAG_IME_POST_COMMIT_MODEL_BUDGET_MS",
    "RAG_IME_REQUIRE_FOREGROUND_CONTEXT_FOR_POST_COMMIT",
    "RAG_IME_POST_COMMIT_PENDING_PREVIEW",
    "RAG_IME_POST_COMMIT_PRESENTATION_STREAM",
    "RAG_IME_ENABLE_DEMO_SAFE_FALLBACK",
    "RAG_IME_MODEL_HOLDOVER_MAX_ENTRIES",
    "RAG_IME_PREDICTION_MANAGER_MAX_ENTRIES",
    "RAG_IME_REFRESH_DEBOUNCE_MAX_ENTRIES",
    "RAG_IME_POST_COMMIT_PRESENTATION_STREAM_MAX_ENTRIES",
    "RAG_IME_RAG_DIRECT_DISPLAY",
    "RAG_IME_POST_COMMIT_ACTIVE_RAG_BUTTON",
    "RAG_IME_POST_COMMIT_PENDING_PREVIEW",
    "RAG_IME_ENABLE_DEMO_SAFE_FALLBACK",
    "SSL_CERT_FILE",
    "RAG_IME_MODEL_ENV",
    "RAG_IME_MEMORY_GENERATOR_ENV",
    "RAG_IME_VCP_REBUILD_ENV",
    "RAG_IME_AI_PROVIDER",
    "RAG_IME_AI_BASE_URL",
    "RAG_IME_AI_MODEL",
    "RAG_IME_AI_WIRE_API",
    "RAG_IME_AI_TIMEOUT_SECONDS",
    "RAG_IME_AI_REASONING_EFFORT",
    "RAG_IME_AI_DISABLE_RESPONSE_STORAGE",
    "RAG_IME_AI_API_KEY",
    "RAG_IME_DEEPSEEK_ENV",
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_BASE_URL",
    "DEEPSEEK_MODEL",
    "DEEPSEEK_WIRE_API",
    "DEEPSEEK_STREAM",
    "DEEPSEEK_JSON",
    "RAG_IME_DEEPSEEK_API_KEY",
    "RAG_IME_DEEPSEEK_BASE_URL",
    "RAG_IME_DEEPSEEK_MODEL",
    "RAG_IME_DEEPSEEK_WIRE_API",
    "RAG_IME_DEEPSEEK_STREAM",
    "RAG_IME_DEEPSEEK_JSON",
    "RAG_IME_DEEPSEEK_TIMEOUT_SECONDS",
    "RAG_IME_DEEPSEEK_THINKING",
    "RAG_IME_DEEPSEEK_REASONING_EFFORT",
    "RAG_IME_DEEPSEEK_MAX_TOKENS",
    "RAG_IME_DEEPSEEK_ACTIVE_RAG_MAX_TOKENS",
    "RAG_IME_DEEPSEEK_MEMORY_BOOK_MAX_TOKENS",
    "RAG_IME_DEEPSEEK_KNOWLEDGE_MAX_TOKENS",
    "RAG_IME_DEEPSEEK_ACTIVE_RAG",
    "RAG_IME_DEEPSEEK_POST_COMMIT",
    "RAG_IME_DEEPSEEK_PREVIEW_TOKEN",
    "RAG_IME_PI_ENABLED",
    "RAG_IME_PI_EXECUTABLE",
    "RAG_IME_PI_NODE",
    "RAG_IME_PI_VERSION",
    "RAG_IME_PI_PROVIDER",
    "RAG_IME_PI_MODEL",
    "RAG_IME_PI_PROVIDER_CONFIG",
    "RAG_IME_PI_TOOLS",
    "RAG_IME_PI_IDLE_TIMEOUT_SECONDS",
    "RAG_IME_NOTION_ENV",
    "RAG_IME_NOTION_WORKER_URL",
    "RAG_IME_NOTION_STATUS_URL",
    "RAG_IME_NOTION_STATUS_TOKEN",
    "RAG_IME_NOTION_TOKEN",
    "RAG_IME_NOTION_DATA_SOURCE_ID",
    "RAG_IME_NOTION_WEBHOOK_SECRET",
    "RAG_IME_NOTION_POLL_INTERVAL_MS",
    "RAG_IME_NOTION_TIMEOUT_MS",
    "RAG_IME_PREDICTOR_PROVIDER",
    "RAG_IME_PREDICTOR_ENV",
    "RAG_IME_PREDICTOR_BASE_URL",
    "RAG_IME_PREDICTOR_MODEL",
    "RAG_IME_PREDICTOR_PROFILE",
    "RAG_IME_PREDICTOR_PROMPT_MODE",
    "RAG_IME_PREDICTOR_TIMEOUT_MS",
    "RAG_IME_PREDICTOR_MAX_TOKENS",
    "RAG_IME_PREDICTOR_TEMPERATURE",
    "RAG_IME_PREDICTOR_TOP_P",
    "RAG_IME_PREDICTOR_DISABLE_THINKING",
    "RAG_IME_PREDICTOR_STREAM_FIRST",
    "RAG_IME_PREDICTOR_FAILURE_COOLDOWN_MS",
    "RAG_IME_PREDICTOR_FAILURE_LATENCY_MS",
    "RAG_IME_PREDICTOR_EXTRA_BODY_JSON",
    "RAG_IME_PREDICTOR_EXTRA_HEADERS_JSON",
    "RAG_IME_PREDICTOR_API_KEY",
    "RAG_IME_MLX_MODEL",
    "RAG_IME_MODEL_REGISTRY",
    "RAG_IME_MODEL_ID",
    "RAG_IME_MODEL_FINGERPRINT",
    "RAG_IME_PINYIN_FUZZY_ENABLED",
    "RAG_IME_PINYIN_FUZZY_PROFILE",
    "RAG_IME_PINYIN_FUZZY_Z_ZH",
    "RAG_IME_PINYIN_FUZZY_C_CH",
    "RAG_IME_PINYIN_FUZZY_S_SH",
    "RAG_IME_PINYIN_FUZZY_EN_ENG",
    "RAG_IME_PINYIN_FUZZY_IN_ING",
    "RAG_IME_PINYIN_FUZZY_ONG_ON",
    "RAG_IME_PINYIN_FUZZY_N_L",
    "RAG_IME_PINYIN_FUZZY_F_H",
    "RAG_IME_RAG_DIRECT_DISPLAY",
    "RAG_IME_POST_COMMIT_ACTIVE_RAG_BUTTON",
    "RAG_IME_EMBEDDING_PROVIDER",
    "RAG_IME_EMBEDDING_BASE_URL",
    "RAG_IME_EMBEDDING_MODEL",
    "RAG_IME_EMBEDDING_BITS",
    "RAG_IME_EMBEDDING_GROUP_SIZE",
    "RAG_IME_EMBEDDING_QUERY_PREFIX",
    "RAG_IME_EMBEDDING_DOCUMENT_PREFIX",
    "RAG_IME_EMBEDDING_CACHE_DIR",
    "RAG_IME_EMBEDDING_LOCAL_FILES_ONLY",
    "RAG_IME_EMBEDDING_API_KEY",
    "RAG_IME_EMBEDDING_TIMEOUT_MS",
    "RAG_IME_EMBEDDING_DIMENSIONS",
    "RAG_IME_EMBEDDING_CACHE_SIZE",
    "RAG_IME_EMBEDDING_WARMUP",
    "RAG_IME_EMBEDDING_EXTRA_BODY_JSON",
    "RAG_IME_EMBEDDING_EXTRA_HEADERS_JSON",
    "RAG_IME_VECTOR_CANDIDATES",
    "RAG_IME_VECTOR_WEIGHT",
    "RAG_IME_VECTOR_AUTO_REBUILD_LIMIT",
    "RAG_IME_KNOWLEDGE_DENSE_BACKEND",
):
    value = os.environ.get(key)
    if not value and key in preserve_existing_keys:
        value = existing_env.get(key)
    if value:
        env_vars[key] = value
# Do not inherit the old one-candidate stream mode across model upgrades. An
# explicit install-time override remains supported, but the product default is
# the full resident-model batch so all three completions arrive together.
env_vars["RAG_IME_PREDICTOR_STREAM_FIRST"] = os.environ.get(
    "RAG_IME_PREDICTOR_STREAM_FIRST",
    "0",
)
# Presentation streaming has the same compatibility boundary as predictor
# streaming: an older launch agent may contain "1", but carrying that value
# into a new Top-3 batch model makes the native panel show only the first
# growing branch. Reset it unless the installer receives an explicit override.
env_vars["RAG_IME_POST_COMMIT_PRESENTATION_STREAM"] = os.environ.get(
    "RAG_IME_POST_COMMIT_PRESENTATION_STREAM",
    "0",
)
# Let both HTTP services bind before the Sidecar begins heavyweight embedding
# initialization. An explicit install-time value remains authoritative.
env_vars["RAG_IME_EMBEDDING_WARMUP_DELAY_SECONDS"] = (
    os.environ.get("RAG_IME_EMBEDDING_WARMUP_DELAY_SECONDS", "").strip()
    or "60"
)
for key in (
    "RAG_IME_PI_EXECUTABLE",
    "RAG_IME_PI_NODE",
    "RAG_IME_PI_EXTENSION",
    "RAG_IME_PI_PROTOCOL_VERSION",
    "RAG_IME_PI_TOOLS",
    "RAG_IME_PI_VERSION",
):
    env_vars.pop(key, None)
env_vars["RAG_IME_PI_ENABLED"] = "0"
env_vars["RAG_IME_AGENT_GATEWAY_ENABLED"] = "1"
env_vars["RAG_IME_KNOWLEDGE_SHARED_WORKER"] = "1"
enable_local_vector = os.environ.get("RAG_IME_ENABLE_LOCAL_VECTOR", "").strip().lower() in {"1", "true", "yes", "on"}
if enable_local_vector:
    env_vars.setdefault("RAG_IME_EMBEDDING_PROVIDER", "local-hash")
    env_vars.setdefault("RAG_IME_EMBEDDING_DIMENSIONS", "96")
    env_vars.setdefault("RAG_IME_VECTOR_CANDIDATES", "80")
    env_vars.setdefault("RAG_IME_VECTOR_WEIGHT", "1.4")
    env_vars.setdefault("RAG_IME_VECTOR_AUTO_REBUILD_LIMIT", "5000")
if core_command:
    env_vars["RAG_MEMORY_CORE_COMMAND"] = core_command

payload = {
    "Label": label,
    "ProgramArguments": args,
    "RunAtLoad": True,
    "KeepAlive": os.environ.get("RAG_IME_LAUNCH_KEEP_ALIVE", "1").strip().lower()
    not in {"0", "false", "no", "off"},
    "ThrottleInterval": 10,
    "StandardOutPath": str(Path(os.environ["LOG_DIR"]) / "sidecar.out.log"),
    "StandardErrorPath": str(Path(os.environ["LOG_DIR"]) / "sidecar.err.log"),
    "WorkingDirectory": app_support_dir,
    "EnvironmentVariables": env_vars,
}

with open(os.environ["PLIST_PATH"], "wb") as fh:
    plistlib.dump(payload, fh)
PY

if command -v plutil >/dev/null 2>&1; then
  plutil -lint "$PLIST_PATH" >/dev/null
fi

if [[ "$DRY_RUN" == "1" || "$DRY_RUN" == "true" || "$DRY_RUN" == "TRUE" ]]; then
  write_install_marker
  echo "$PLIST_PATH"
  echo "dry-run: not loading launch agent"
  exit 0
fi

DOMAIN="gui/$(id -u)"

kill_stale_sidecar_processes() {
  if [[ "${RAG_IME_KILL_STALE_SIDECAR_ON_INSTALL:-1}" == "0" ]]; then
    return 0
  fi
  pkill -f 'sidecar_launch.py.*sidecar-server' >/dev/null 2>&1 || true
  pkill -f 'rag_ime.cli.*sidecar-server' >/dev/null 2>&1 || true
  # The knowledge worker is a child of the Sidecar but may survive an abrupt
  # LaunchAgent replacement. Never let a stale dev/test worker on the fixed
  # production port make the new Sidecar adopt the wrong Knowledge root.
  pkill -f "rag_ime.knowledge_library.worker.*--port $KNOWLEDGE_WORKER_PORT" >/dev/null 2>&1 || true
  if command -v lsof >/dev/null 2>&1; then
    local pid
    while read -r pid; do
      [[ -n "$pid" ]] || continue
      ps -p "$pid" -o command= | grep -E 'sidecar_launch.py|rag_ime.cli' >/dev/null 2>&1 || continue
      kill "$pid" >/dev/null 2>&1 || true
    done < <(lsof -nP -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)
  fi
}

wait_for_sidecar_port_release() {
  if ! command -v lsof >/dev/null 2>&1; then
    return 0
  fi
  local attempt
  for attempt in {1..25}; do
    if ! lsof -nP -tiTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.2
  done
  echo "sidecar port $PORT is still occupied after stale-process cleanup" >&2
  lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >&2 || true
  return 1
}

bootstrap_launch_agent() {
  local attempt
  local delay
  local error_log

  error_log="$(mktemp "${TMPDIR:-/tmp}/rag-ime-launchctl-bootstrap.XXXXXX")"
  trap 'rm -f "$error_log"' RETURN

  for attempt in 1 2 3 4 5; do
    if launchctl bootstrap "$DOMAIN" "$PLIST_PATH" 2>"$error_log"; then
      return 0
    fi
    if [[ "$attempt" == "5" ]]; then
      break
    fi
    delay="$(awk "BEGIN { printf \"%.1f\", $attempt * 0.4 }")"
    sleep "$delay"
  done

  echo "launchctl bootstrap failed for $DOMAIN/$LABEL" >&2
  cat "$error_log" >&2
  return 1
}

launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
kill_stale_sidecar_processes
wait_for_sidecar_port_release
launchctl enable "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
sleep 0.2
bootstrap_launch_agent
if ! launchctl kickstart "$DOMAIN/$LABEL" >/dev/null 2>&1; then
  echo "launchctl could not start $DOMAIN/$LABEL after bootstrap" >&2
  launchctl print "$DOMAIN/$LABEL" >&2 || true
  exit 1
fi

echo "$PLIST_PATH"
DISPLAY_HOST="$HOST"
if [[ "$DISPLAY_HOST" == *:* && "$DISPLAY_HOST" != \[*\] ]]; then
  DISPLAY_HOST="[$DISPLAY_HOST]"
fi
echo "http://$DISPLAY_HOST:$PORT/"
echo "Logs: $LOG_DIR/sidecar.out.log and $LOG_DIR/sidecar.err.log"

health_deadline=$((SECONDS + HEALTH_TIMEOUT_SECONDS))
while (( SECONDS < health_deadline )); do
  if "$PYTHON_EXECUTABLE" - \
    "$HOST" \
    "$PORT" \
    "${RAG_IME_PREDICTOR_PROVIDER:-}" \
    "${RAG_IME_PREDICTOR_MODEL:-}" \
    "${RAG_IME_PREDICTOR_PROFILE:-}" \
    "${RAG_IME_PREDICTOR_PROMPT_MODE:-}" \
    "${RAG_IME_PREDICTOR_MAX_TOKENS:-}" \
    "${RAG_IME_PREDICTOR_TEMPERATURE:-}" \
    "${RAG_IME_PREDICTOR_TOP_P:-}" >/dev/null 2>&1 <<'PY'
import json
import sys
import urllib.request

host = sys.argv[1]
port = sys.argv[2]
expected_predictor_provider = sys.argv[3]
expected_predictor_model = sys.argv[4]
expected_predictor_profile = sys.argv[5]
expected_prompt_mode = sys.argv[6]
expected_max_tokens = sys.argv[7]
expected_temperature = sys.argv[8]
expected_top_p = sys.argv[9]
url_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
with opener.open(f"http://{url_host}:{port}/health", timeout=1.0) as response:
    payload = json.loads(response.read().decode("utf-8"))
if not payload.get("ok"):
    raise SystemExit(1)
if expected_predictor_provider and expected_predictor_model:
    predictor = payload.get("predictor")
    if not isinstance(predictor, dict) or not predictor.get("configured"):
        raise SystemExit(1)
    if str(predictor.get("model") or "") != expected_predictor_model:
        raise SystemExit(1)
    if str(predictor.get("providerProfile") or "") != expected_predictor_profile:
        raise SystemExit(1)
    if str(predictor.get("promptMode") or "") != expected_prompt_mode:
        raise SystemExit(1)
    try:
        runtime_max_tokens = int(predictor.get("maxTokens"))
        configured_max_tokens = int(expected_max_tokens)
    except (TypeError, ValueError):
        raise SystemExit(1)
    if runtime_max_tokens != configured_max_tokens:
        raise SystemExit(1)
    try:
        runtime_temperature = float(predictor.get("temperature"))
        runtime_top_p = float(predictor.get("topP"))
        configured_temperature = float(expected_temperature)
        configured_top_p = float(expected_top_p)
    except (TypeError, ValueError):
        raise SystemExit(1)
    if abs(runtime_temperature - configured_temperature) > 1e-9:
        raise SystemExit(1)
    if abs(runtime_top_p - configured_top_p) > 1e-9:
        raise SystemExit(1)
PY
  then
    echo "health: OK"
    write_install_marker
    if [[ "${RAG_IME_INSTALL_AGENT_GATEWAY:-1}" != "0" ]]; then
      RAG_IME_APP_SUPPORT_DIR="$APP_SUPPORT_DIR" \
      RAG_IME_DB_PATH="$DB_PATH" \
      RAG_IME_PYTHON="$PYTHON_EXECUTABLE" \
      "$ROOT/scripts/install_agent_gateway_launch_agent.sh"
    fi
    exit 0
  fi
  sleep 0.5
done

echo "health: not ready after ${HEALTH_TIMEOUT_SECONDS}s; inspect $LOG_DIR/sidecar.err.log" >&2
exit 1
