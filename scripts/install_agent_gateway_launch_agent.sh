#!/usr/bin/env bash
set -euo pipefail

WEB_ONLY=0
if [[ "${1:-}" == "--web-only" ]]; then
  WEB_ONLY=1
  shift
fi
if [[ "$#" != "0" ]]; then
  echo "usage: $0 [--web-only]" >&2
  exit 2
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_SUPPORT_DIR="${RAG_IME_APP_SUPPORT_DIR:-$HOME/Library/Application Support/RagIme}"
APP_CODE_DIR="$APP_SUPPORT_DIR/app"
SIDE_LABEL="${RAG_IME_LAUNCH_AGENT_LABEL:-com.rag-ime.sidecar}"
LABEL="${RAG_IME_AGENT_GATEWAY_LABEL:-com.rag-ime.agent-gateway}"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST_PATH="$PLIST_DIR/$LABEL.plist"
SIDE_PLIST="$PLIST_DIR/$SIDE_LABEL.plist"
LOG_DIR="$HOME/Library/Logs/RagIme"
WRAPPER="$APP_CODE_DIR/sidecar_launch.py"
INSTALL_MARKER="$APP_CODE_DIR/rag-ime-install-marker.json"
DB_PATH="${RAG_IME_DB_PATH:-$APP_SUPPORT_DIR/rag-ime.sqlite}"
HOST="${RAG_IME_AGENT_GATEWAY_HOST:-127.0.0.1}"
PORT="${RAG_IME_AGENT_GATEWAY_PORT:-8768}"
PROJECT="${RAG_IME_PROJECT:-wisdom-weasel-rag-ime}"
DRY_RUN="${RAG_IME_LAUNCH_AGENT_DRY_RUN:-0}"
MANAGED_RUNTIME_POINTER="$APP_SUPPORT_DIR/PiRuntime/current.json"
WEB_SOURCE_DIR="$ROOT/control-center-web/dist"
WEB_INSTALL_DIR="$APP_CODE_DIR/control-center-web/dist"
ALLOWED_LOGINS="${RAG_IME_REMOTE_ALLOWED_LOGINS:-}"
DEBUG_CONTEXT_DIR="${RAG_IME_PI_DEBUG_CONTEXT_DIR:-}"
DEBUG_CONTEXT_MAX_BYTES="${RAG_IME_PI_DEBUG_CONTEXT_MAX_BYTES:-5368709120}"
DEBUG_CONTEXT_MAX_CALLS="${RAG_IME_PI_DEBUG_CONTEXT_MAX_CALLS:-128}"
WEB_SOURCE_BACKUP=""
WEB_SOURCE_PRESENT=0
WEB_INSTALL_TEMP=""

restore_web_source_dist() {
  [[ -n "$WEB_SOURCE_BACKUP" ]] || return 0
  rm -rf "$WEB_SOURCE_DIR"
  if [[ "$WEB_SOURCE_PRESENT" == "1" ]]; then
    ditto "$WEB_SOURCE_BACKUP/dist" "$WEB_SOURCE_DIR"
  fi
  rm -rf "$WEB_SOURCE_BACKUP"
  WEB_SOURCE_BACKUP=""
}

cleanup_web_install_temp() {
  if [[ -n "$WEB_INSTALL_TEMP" && -e "$WEB_INSTALL_TEMP" ]]; then
    rm -rf -- "$WEB_INSTALL_TEMP"
  fi
  WEB_INSTALL_TEMP=""
}

cleanup_web_install_state() {
  cleanup_web_install_temp
  restore_web_source_dist
}

verify_copied_web_dist() {
  python3 - "$ROOT" "$1" "$2" <<'PY'
import json
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from rag_ime.release_staging import control_center_dist_digest

source_root, installed_root = map(Path, sys.argv[2:])


source_marker = json.loads(
    (source_root / "rag-ime-control-web-build.json").read_text(encoding="utf-8")
)
installed_marker = json.loads(
    (installed_root / "rag-ime-control-web-build.json").read_text(encoding="utf-8")
)
if source_marker != installed_marker:
    raise ValueError("copied control-center dist marker does not match the source")
if source_marker.get("buildChannel") != "production":
    raise ValueError("copied control-center dist is not a production build")
if source_marker.get("frontendProduct") != "paw-os":
    raise ValueError("copied control-center dist is not the PAWOS frontend")
expected_digest = source_marker.get("distTreeDigest")
if not isinstance(expected_digest, str) or len(expected_digest) != 64:
    raise ValueError("copied control-center dist marker has no tree digest")
source_digest = control_center_dist_digest(source_root)
installed_digest = control_center_dist_digest(installed_root)
if source_digest != installed_digest or installed_digest != expected_digest:
    raise ValueError("copied control-center dist tree digest does not match the marker")
PY
}

atomically_install_web_dist() {
  local install_parent
  install_parent="$(dirname "$WEB_INSTALL_DIR")"
  mkdir -p "$install_parent"
  WEB_INSTALL_TEMP="$(mktemp -d "$install_parent/.control-center-web-dist.XXXXXX")"
  if ! ditto "$WEB_SOURCE_DIR/." "$WEB_INSTALL_TEMP"; then
    echo "could not copy control-center web dist to a staging directory" >&2
    return 1
  fi
  verify_copied_web_dist "$WEB_SOURCE_DIR" "$WEB_INSTALL_TEMP"
  WEB_INSTALL_TEMP_PATH="$WEB_INSTALL_TEMP" WEB_INSTALL_DEST="$WEB_INSTALL_DIR" \
    python3 - <<'PY'
import os
import shutil
import sys
import uuid
from pathlib import Path

staged = Path(os.environ["WEB_INSTALL_TEMP_PATH"])
destination = Path(os.environ["WEB_INSTALL_DEST"])
backup = destination.parent / f".{destination.name}.previous-{uuid.uuid4().hex}"


def remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


moved_previous = False
try:
    if destination.is_symlink() or destination.exists():
        os.replace(destination, backup)
        moved_previous = True
    # Both paths live under the same app-support directory, so this rename is
    # the atomic cutover after the staged copy and digest verification.
    os.replace(staged, destination)
except Exception:
    if moved_previous and not (destination.is_symlink() or destination.exists()) and backup.exists():
        os.replace(backup, destination)
    raise
else:
    if backup.exists() or backup.is_symlink():
        remove_path(backup)
PY
  WEB_INSTALL_TEMP=""
}

if [[ ! -f "$WRAPPER" || ! -d "$APP_CODE_DIR/rag_ime" ]]; then
  echo "installed app code is missing; run scripts/install_sidecar_launch_agent.sh first" >&2
  exit 1
fi
if ! python3 - \
  "$INSTALL_MARKER" \
  "$ROOT/rag_ime" \
  "$APP_CODE_DIR/rag_ime" \
  "$ROOT/examples/vertical_agents" \
  "$APP_CODE_DIR/examples/vertical_agents" \
  "$ROOT/scripts/sidecar_launch.py" \
  "$WRAPPER" "$WEB_ONLY" <<'PY'
import hashlib
import json
import sys
from pathlib import Path


def tree_digest(root: Path) -> str:
    if not root.is_dir() or root.is_symlink():
        raise ValueError(f"invalid runtime tree: {root}")
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if "__pycache__" in relative.parts or path.suffix == ".pyc":
            continue
        if path.is_symlink():
            raise ValueError(f"runtime tree contains a symlink: {relative}")
        if not path.is_file():
            continue
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


try:
    (
        marker_path,
        source_tree,
        installed_tree,
        source_vertical_agents,
        installed_vertical_agents,
        source_wrapper,
        installed_wrapper,
    ) = map(
        Path,
        sys.argv[1:8],
    )
    web_only = sys.argv[8] == "1"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    provenance_valid = (
        marker.get("component") == "sidecar-runtime"
        and bool(marker.get("sourceCommit"))
    )
    # A frontend-only update consumes the installed Runtime through HTTP. It
    # must not require rolling back a separately updated backend to this checkout.
    # Validate the installed component, then retain its code, plist and process.
    if web_only:
        tree_digest(installed_tree)
        tree_digest(installed_vertical_agents)
        raise SystemExit(0 if provenance_valid and installed_wrapper.is_file() else 1)
    runtime_matches = tree_digest(source_tree) == tree_digest(installed_tree)
    vertical_agents_match = (
        tree_digest(source_vertical_agents)
        == tree_digest(installed_vertical_agents)
    )
    wrapper_matches = (
        source_wrapper.is_file()
        and installed_wrapper.is_file()
        and source_wrapper.read_bytes() == installed_wrapper.read_bytes()
    )
except (OSError, ValueError, json.JSONDecodeError):
    raise SystemExit(1)
raise SystemExit(
    0
    if provenance_valid
    and runtime_matches
    and vertical_agents_match
    and wrapper_matches
    else 1
)
PY
then
  echo "installed sidecar code does not match this source checkout; reinstall the sidecar first" >&2
  exit 1
fi
if [[ ! -f "$MANAGED_RUNTIME_POINTER" && "${RAG_IME_ALLOW_UNMANAGED_PI:-0}" != "1" ]]; then
  echo "managed Pi Runtime is missing; install a verified protocol-v2 payload first" >&2
  exit 1
fi

if [[ -z "$ALLOWED_LOGINS" && -f "$PLIST_PATH" ]]; then
  ALLOWED_LOGINS="$(python3 - "$PLIST_PATH" <<'PY' 2>/dev/null || true
import plistlib
import sys

with open(sys.argv[1], "rb") as source:
    payload = plistlib.load(source)
print(str((payload.get("EnvironmentVariables") or {}).get("RAG_IME_REMOTE_ALLOWED_LOGINS") or ""))
PY
)"
fi

PYTHON_EXECUTABLE="${RAG_IME_PYTHON:-}"
if [[ -z "$PYTHON_EXECUTABLE" && -f "$SIDE_PLIST" ]]; then
  PYTHON_EXECUTABLE="$(python3 - "$SIDE_PLIST" <<'PY'
import plistlib
import sys
with open(sys.argv[1], "rb") as source:
    payload = plistlib.load(source)
args = payload.get("ProgramArguments") or []
print(args[0] if args else "")
PY
)"
fi
if [[ -z "$PYTHON_EXECUTABLE" ]]; then
  for candidate in "$ROOT/.venv/bin/python" /opt/homebrew/bin/python3 "$(command -v python3 2>/dev/null || true)"; do
    if [[ -n "$candidate" && -x "$candidate" ]]; then PYTHON_EXECUTABLE="$candidate"; break; fi
  done
fi
if [[ -z "$PYTHON_EXECUTABLE" || ! -x "$PYTHON_EXECUTABLE" ]]; then
  echo "Python executable is unavailable: $PYTHON_EXECUTABLE" >&2
  exit 1
fi

if [[ "$DRY_RUN" != "1" && "$DRY_RUN" != "true" && "$DRY_RUN" != "TRUE" ]]; then
  WEB_SOURCE_BACKUP="$(mktemp -d "${TMPDIR:-/tmp}/rag-ime-agent-gateway-web.XXXXXX")"
  if [[ -d "$WEB_SOURCE_DIR" ]]; then
    WEB_SOURCE_PRESENT=1
    ditto "$WEB_SOURCE_DIR" "$WEB_SOURCE_BACKUP/dist"
  fi
  trap cleanup_web_install_state EXIT

  RAG_IME_CONTROL_TRANSPORT=http \
  RAG_IME_CONTROL_BUILD_CHANNEL=production \
    "$ROOT/scripts/build_control_center_web.sh" >/dev/null
  "$ROOT/scripts/check_control_center_web_dist.sh" \
    "$WEB_SOURCE_DIR" http production >/dev/null
  atomically_install_web_dist
  restore_web_source_dist
  trap - EXIT
fi

# Static assets are read from the installed tree on each request. Updating
# only that tree must not rewrite the plist or interrupt active Pi Sessions.
if [[ "$WEB_ONLY" == "1" ]]; then
  if [[ "$DRY_RUN" == "1" || "$DRY_RUN" == "true" || "$DRY_RUN" == "TRUE" ]]; then
    echo "dry-run: web assets, Agent Gateway and Pi unchanged"
  else
    echo "control-center web assets updated; Agent Gateway and Pi left running"
  fi
  exit 0
fi

mkdir -p "$PLIST_DIR" "$LOG_DIR"
if [[ -n "$DEBUG_CONTEXT_DIR" && "$DRY_RUN" != "1" && "$DRY_RUN" != "true" && "$DRY_RUN" != "TRUE" ]]; then
  mkdir -p "$DEBUG_CONTEXT_DIR"
  chmod 700 "$DEBUG_CONTEXT_DIR"
fi
SIDE_PLIST="$SIDE_PLIST" PLIST_PATH="$PLIST_PATH" LABEL="$LABEL" LOG_DIR="$LOG_DIR" \
APP_SUPPORT_DIR="$APP_SUPPORT_DIR" APP_CODE_DIR="$APP_CODE_DIR" WRAPPER="$WRAPPER" \
PYTHON_EXECUTABLE="$PYTHON_EXECUTABLE" DB_PATH="$DB_PATH" HOST="$HOST" PORT="$PORT" \
PROJECT="$PROJECT" WEB_INSTALL_DIR="$WEB_INSTALL_DIR" ALLOWED_LOGINS="$ALLOWED_LOGINS" \
DEBUG_CONTEXT_DIR="$DEBUG_CONTEXT_DIR" DEBUG_CONTEXT_MAX_BYTES="$DEBUG_CONTEXT_MAX_BYTES" \
DEBUG_CONTEXT_MAX_CALLS="$DEBUG_CONTEXT_MAX_CALLS" \
"$PYTHON_EXECUTABLE" - <<'PY'
import os
import plistlib
from pathlib import Path

environment = {}
side_plist = Path(os.environ["SIDE_PLIST"])
if side_plist.is_file():
    with side_plist.open("rb") as source:
        payload = plistlib.load(source)
    raw = payload.get("EnvironmentVariables")
    if isinstance(raw, dict):
        environment = {str(key): str(value) for key, value in raw.items() if value is not None}

for key in (
    "RAG_IME_PI_EXECUTABLE",
    "RAG_IME_PI_NODE",
    "RAG_IME_PI_EXTENSION",
    "RAG_IME_PI_PROTOCOL_VERSION",
    "RAG_IME_PI_TOOLS",
    "RAG_IME_PI_VERSION",
):
    environment.pop(key, None)

environment.update({
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONUNBUFFERED": "1",
    "RAG_IME_ROOT": os.environ["APP_CODE_DIR"],
    "RAG_IME_APP_SUPPORT_DIR": os.environ["APP_SUPPORT_DIR"],
    "RAG_IME_DB_PATH": os.environ["DB_PATH"],
    "RAG_IME_PI_ENABLED": "1",
    "RAG_IME_AGENT_GATEWAY_ENABLED": "1",
    "RAG_IME_AGENT_GATEWAY_WEB_DIST": os.environ["WEB_INSTALL_DIR"],
    "RAG_IME_AGENT_TOOL_URL": f"http://127.0.0.1:{os.environ['PORT']}/api/agent/tool/execute",
})
# The Gateway must bind immediately. It starts from the Sidecar environment
# for shared provider settings, but never inherits the Sidecar's warmup delay.
environment["RAG_IME_EMBEDDING_WARMUP_DELAY_SECONDS"] = "0"
if os.environ["DEBUG_CONTEXT_DIR"]:
    environment["RAG_IME_PI_DEBUG_CONTEXT_DIR"] = os.environ["DEBUG_CONTEXT_DIR"]
    environment["RAG_IME_PI_DEBUG_CONTEXT_MAX_BYTES"] = os.environ["DEBUG_CONTEXT_MAX_BYTES"]
    environment["RAG_IME_PI_DEBUG_CONTEXT_MAX_CALLS"] = os.environ["DEBUG_CONTEXT_MAX_CALLS"]
else:
    environment.pop("RAG_IME_PI_DEBUG_CONTEXT_DIR", None)
    environment.pop("RAG_IME_PI_DEBUG_CONTEXT_MAX_BYTES", None)
    environment.pop("RAG_IME_PI_DEBUG_CONTEXT_MAX_CALLS", None)
if os.environ["ALLOWED_LOGINS"]:
    environment["RAG_IME_REMOTE_ALLOWED_LOGINS"] = os.environ["ALLOWED_LOGINS"]
else:
    environment.pop("RAG_IME_REMOTE_ALLOWED_LOGINS", None)

arguments = [
    os.environ["PYTHON_EXECUTABLE"],
    os.environ["WRAPPER"],
    "--core-mode", environment.get("RAG_IME_CORE_MODE", "local"),
    "--db-path", os.environ["DB_PATH"],
    "agent-gateway",
    "--host", os.environ["HOST"],
    "--port", os.environ["PORT"],
    "--project", os.environ["PROJECT"],
    "--web-dist", os.environ["WEB_INSTALL_DIR"],
    "--no-seed",
]
payload = {
    "Label": os.environ["LABEL"],
    "ProgramArguments": arguments,
    "RunAtLoad": True,
    "KeepAlive": {"SuccessfulExit": False},
    "ProcessType": "Background",
    "ThrottleInterval": 3,
    "WorkingDirectory": os.environ["APP_SUPPORT_DIR"],
    "StandardOutPath": str(Path(os.environ["LOG_DIR"]) / "agent-gateway.out.log"),
    "StandardErrorPath": str(Path(os.environ["LOG_DIR"]) / "agent-gateway.err.log"),
    "EnvironmentVariables": environment,
}
with open(os.environ["PLIST_PATH"], "wb") as target:
    plistlib.dump(payload, target)
PY

if command -v plutil >/dev/null 2>&1; then plutil -lint "$PLIST_PATH" >/dev/null; fi
echo "$PLIST_PATH"
if [[ "$DRY_RUN" == "1" || "$DRY_RUN" == "true" || "$DRY_RUN" == "TRUE" ]]; then
  echo "dry-run: not loading Agent Gateway"
  exit 0
fi

DOMAIN="gui/$(id -u)"

# A product update must not stop source candidates on other ports/workspaces.
stop_stale_gateway_listener() {
  command -v lsof >/dev/null 2>&1 || return 0
  local pid process_command
  while IFS= read -r pid; do
    [[ "$pid" =~ ^[0-9]+$ ]] || continue
    process_command="$(ps -p "$pid" -o command= 2>/dev/null || true)"
    if [[ "$process_command" == *" $WRAPPER "* && "$process_command" == *" agent-gateway "* ]]; then
      kill "$pid" 2>/dev/null || true
    fi
  done < <(lsof -nP -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)
}

wait_for_gateway_port_release() {
  command -v lsof >/dev/null 2>&1 || return 0
  local attempt
  for attempt in {1..25}; do
    if ! lsof -nP -tiTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.2
  done
  echo "Agent Gateway port $PORT is still occupied after shutdown" >&2
  return 1
}

bootstrap_launch_agent() {
  local attempt
  local error_log
  error_log="$(mktemp "${TMPDIR:-/tmp}/rag-ime-agent-gateway-bootstrap.XXXXXX")"
  trap 'rm -f "$error_log"' RETURN

  for attempt in 1 2 3 4 5; do
    if launchctl bootstrap "$DOMAIN" "$PLIST_PATH" 2>"$error_log"; then
      return 0
    fi
    [[ "$attempt" == "5" ]] || sleep "$(awk "BEGIN { printf \"%.1f\", $attempt * 0.4 }")"
  done

  echo "launchctl bootstrap failed for $DOMAIN/$LABEL" >&2
  cat "$error_log" >&2
  return 1
}

launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
stop_stale_gateway_listener
wait_for_gateway_port_release
launchctl enable "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
sleep 0.2
bootstrap_launch_agent
if ! launchctl kickstart "$DOMAIN/$LABEL" >/dev/null 2>&1; then
  echo "launchctl could not start $DOMAIN/$LABEL after bootstrap" >&2
  launchctl print "$DOMAIN/$LABEL" >&2 || true
  exit 1
fi

deadline=$((SECONDS + 45))
while (( SECONDS < deadline )); do
  if "$PYTHON_EXECUTABLE" - "$HOST" "$PORT" >/dev/null 2>&1 <<'PY'
import json
import sys
import urllib.request
host, port = sys.argv[1:]
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
with opener.open(f"http://{host}:{port}/api/health", timeout=1.0) as response:
    payload = json.loads(response.read())
if not payload.get("ok"):
    raise SystemExit(1)
PY
  then
    echo "Agent Gateway health: OK (http://$HOST:$PORT/)"
    exit 0
  fi
  sleep 0.5
done

echo "Agent Gateway health did not become ready; inspect $LOG_DIR/agent-gateway.err.log" >&2
exit 1
