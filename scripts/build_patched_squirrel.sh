#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SQUIRREL_WORKDIR="${RAG_IME_SQUIRREL_WORKDIR:-/tmp/rag-ime-squirrel}"
PROJECT_PATH="${RAG_IME_SQUIRREL_PROJECT:-$SQUIRREL_WORKDIR/Squirrel.xcodeproj}"
SCHEME="${RAG_IME_SQUIRREL_SCHEME:-Squirrel}"
CONFIGURATION="${RAG_IME_SQUIRREL_CONFIGURATION:-Release}"
DERIVED_DATA="${RAG_IME_SQUIRREL_DERIVED_DATA:-/tmp/rag-ime-squirrel-derived-data}"
INSTALL_DIR="${RAG_IME_SQUIRREL_INSTALL_DIR:-$HOME/Library/Input Methods}"
INSTALL_APP_NAME="${RAG_IME_SQUIRREL_INSTALL_APP_NAME:-Squirrel}"
ACTION="${1:-${RAG_IME_SQUIRREL_BUILD_ACTION:-build}}"
DRY_RUN="${RAG_IME_SQUIRREL_BUILD_DRY_RUN:-0}"
XCODEBUILD="${RAG_IME_XCODEBUILD:-$(command -v xcodebuild || true)}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
CLT_SDK_ROOT="${RAG_IME_CLT_SDK_ROOT:-/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk}"
INSTALL_NAME_TOOL="${RAG_IME_INSTALL_NAME_TOOL:-/usr/bin/install_name_tool}"
DITTO="${RAG_IME_DITTO:-/usr/bin/ditto}"
PLISTBUDDY="${RAG_IME_PLISTBUDDY:-/usr/libexec/PlistBuddy}"
PREINSTALL="${RAG_IME_SQUIRREL_PREINSTALL:-auto}"
NO_DOWNLOAD="${RAG_IME_SQUIRREL_NO_DOWNLOAD:-0}"
SKIP_POSTINSTALL="${RAG_IME_SQUIRREL_SKIP_POSTINSTALL:-0}"
SKIP_CODESIGN="${RAG_IME_SQUIRREL_SKIP_CODESIGN:-0}"
CODESIGN_IDENTITY="${RAG_IME_SQUIRREL_CODESIGN_IDENTITY:--}"
ENABLE_PREF_REPAIR="${RAG_IME_SQUIRREL_ENABLE_PREF_REPAIR:-0}"
AUTO_SELECT="${RAG_IME_SQUIRREL_AUTO_SELECT:-0}"
HANDOFF_INPUT_SOURCE_ID="${RAG_IME_SQUIRREL_HANDOFF_INPUT_SOURCE_ID:-com.apple.keylayout.ABC}"
ALLOW_SOURCE_ROOT_CHANGE="${RAG_IME_SQUIRREL_ALLOW_SOURCE_ROOT_CHANGE:-0}"
CANONICALIZE_INPUT_METHODS="${RAG_IME_SQUIRREL_CANONICALIZE_INPUT_METHODS:-1}"
CANONICAL_INPUT_METHOD_ALIASES="${RAG_IME_SQUIRREL_CANONICAL_INPUT_METHOD_ALIASES:-RAG-IME.app:RagIme.app:Squirrel.app}"
CANONICAL_QUARANTINE_DIR="${RAG_IME_SQUIRREL_CANONICAL_QUARANTINE_DIR:-$HOME/Library/Application Support/RagIme/disabled-input-method-backups}"
SYSTEM_INPUT_METHOD_DIR="${RAG_IME_SQUIRREL_SYSTEM_INPUT_METHOD_DIR:-/Library/Input Methods}"
LSREGISTER="${RAG_IME_LSREGISTER:-/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister}"
CHECK_INPUT_SOURCE_SCRIPT="${RAG_IME_CHECK_INPUT_SOURCE_SCRIPT:-$ROOT/scripts/check_macos_input_source.sh}"
SELECT_INPUT_SOURCE_SCRIPT="${RAG_IME_SELECT_INPUT_SOURCE_SCRIPT:-$ROOT/scripts/select_macos_input_source.sh}"
DEFAULT_BUNDLE_ID="im.rime.inputmethod.Squirrel"
BUNDLE_ID="${RAG_IME_SQUIRREL_BUNDLE_ID:-$DEFAULT_BUNDLE_ID}"
INPUT_SOURCE_ID="${RAG_IME_SQUIRREL_INPUT_SOURCE_ID:-$BUNDLE_ID.Hans}"
HANT_INPUT_SOURCE_ID="${RAG_IME_SQUIRREL_HANT_INPUT_SOURCE_ID:-$BUNDLE_ID.Hant}"
if [[ "$BUNDLE_ID" == "$DEFAULT_BUNDLE_ID" && "$INSTALL_APP_NAME" == "Squirrel" ]]; then
  DEFAULT_DISPLAY_NAME="澄输入法"
  DEFAULT_HANS_DISPLAY_NAME="澄输入法"
  DEFAULT_HANT_DISPLAY_NAME="澄输入法（繁体）"
else
  DEFAULT_DISPLAY_NAME="$INSTALL_APP_NAME"
  DEFAULT_HANS_DISPLAY_NAME="$INSTALL_APP_NAME - Simplified"
  DEFAULT_HANT_DISPLAY_NAME="$INSTALL_APP_NAME - Traditional"
fi
DEFAULT_CONNECTION_NAME="${BUNDLE_ID}_Connection"
DISPLAY_NAME="${RAG_IME_SQUIRREL_DISPLAY_NAME:-$DEFAULT_DISPLAY_NAME}"
HANS_DISPLAY_NAME="${RAG_IME_SQUIRREL_HANS_DISPLAY_NAME:-$DEFAULT_HANS_DISPLAY_NAME}"
HANT_DISPLAY_NAME="${RAG_IME_SQUIRREL_HANT_DISPLAY_NAME:-$DEFAULT_HANT_DISPLAY_NAME}"
CONNECTION_NAME="${RAG_IME_SQUIRREL_CONNECTION_NAME:-$DEFAULT_CONNECTION_NAME}"
# Xcode otherwise runs RegisterWithLaunchServices for the DerivedData app before
# the script can unregister it, briefly creating a second Squirrel identity.
BUILD_SETTINGS_EXTRA="${RAG_IME_SQUIRREL_BUILD_SETTINGS:-CODE_SIGNING_ALLOWED=NO INFOPLIST_KEY_LSRegisterProhibited=YES}"
lock_name="${BUNDLE_ID//[^A-Za-z0-9_.-]/_}"
INSTALL_LOCK_DIR="${RAG_IME_SQUIRREL_INSTALL_LOCK_DIR:-${TMPDIR:-/tmp}/rag-ime-squirrel-install-${UID:-$(id -u)}-$lock_name.lock}"

extra_build_settings=()
if [[ -n "$BUILD_SETTINGS_EXTRA" ]]; then
  read -r -a extra_build_settings <<< "$BUILD_SETTINGS_EXTRA"
fi

# Upstream Squirrel hard-codes /Applications/Xcode.app for Tk's X11 headers.
# Resolve the header root from the selected Developer directory so a full Xcode
# installed elsewhere can still compile librime's key_table.h.
x11_header_root=""
x11_header_candidates=()
if [[ -n "${DEVELOPER_DIR:-}" && "${DEVELOPER_DIR}" != *" "* ]]; then
  x11_header_candidates+=(
    "$DEVELOPER_DIR/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk/System/Library/Frameworks/Tk.framework/Headers"
  )
fi
x11_header_candidates+=(
  "$CLT_SDK_ROOT/System/Library/Frameworks/Tk.framework/Headers"
  "/opt/homebrew/include"
  "/usr/local/include"
)
if [[ -n "${DEVELOPER_DIR:-}" && "${DEVELOPER_DIR}" == *" "* ]]; then
  # Xcode splits whitespace-delimited build-setting values even when the
  # xcodebuild argv item itself is quoted. Prefer an equivalent no-space SDK
  # header root before falling back to the selected Developer directory.
  x11_header_candidates+=(
    "$DEVELOPER_DIR/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk/System/Library/Frameworks/Tk.framework/Headers"
  )
fi
for candidate in "${x11_header_candidates[@]}"; do
  if [[ -f "$candidate/X11/keysym.h" ]]; then
    x11_header_root="$candidate"
    break
  fi
done
if [[ -n "$x11_header_root" && " $BUILD_SETTINGS_EXTRA " != *" SYSTEM_HEADER_SEARCH_PATHS="* ]]; then
  extra_build_settings+=("SYSTEM_HEADER_SEARCH_PATHS=$x11_header_root")
fi

bool_true() {
  [[ "$1" == "1" || "$1" == "true" || "$1" == "TRUE" || "$1" == "yes" || "$1" == "YES" ]]
}

usage() {
  cat <<EOF
Usage: scripts/build_patched_squirrel.sh [list|build|install]

Environment:
  RAG_IME_SQUIRREL_WORKDIR       patched Squirrel checkout (default: /tmp/rag-ime-squirrel)
  RAG_IME_SQUIRREL_PROJECT       Xcode project path (default: <workdir>/Squirrel.xcodeproj)
  RAG_IME_SQUIRREL_SCHEME        Xcode scheme (default: Squirrel)
  RAG_IME_SQUIRREL_CONFIGURATION Xcode configuration (default: Release)
  RAG_IME_SQUIRREL_DERIVED_DATA  derived data path (default: /tmp/rag-ime-squirrel-derived-data)
  RAG_IME_SQUIRREL_INSTALL_DIR   install target (default: ~/Library/Input Methods)
  RAG_IME_SQUIRREL_INSTALL_APP_NAME installed app bundle name (default: Squirrel)
  RAG_IME_SQUIRREL_BUNDLE_ID     app/input-source bundle prefix (default: im.rime.inputmethod.Squirrel)
  RAG_IME_SQUIRREL_DISPLAY_NAME  app/input-source display name (default: 澄输入法)
  RAG_IME_SQUIRREL_CONNECTION_NAME input method connection name (must be <bundle id>_Connection)
  RAG_IME_SQUIRREL_PREINSTALL    auto|1|0, run Squirrel action-install when dependencies are missing
  RAG_IME_SQUIRREL_NO_DOWNLOAD   set no_download=1 for action-install
  RAG_IME_SQUIRREL_BUILD_SETTINGS extra xcodebuild settings (default: unsigned and LaunchServices-prohibited)
  RAG_IME_SQUIRREL_CODESIGN_IDENTITY codesign identity after copy (default: - for ad-hoc)
  RAG_IME_SQUIRREL_SKIP_CODESIGN skip post-copy codesign
  RAG_IME_SQUIRREL_SKIP_POSTINSTALL skip user-data bootstrap and Squirrel scripts/postinstall after install
  RAG_IME_SQUIRREL_ENABLE_PREF_REPAIR allow direct HIToolbox/inputsource plist repair after branded install
  RAG_IME_SQUIRREL_AUTO_SELECT select the branded input source after install
  RAG_IME_SQUIRREL_HANDOFF_INPUT_SOURCE_ID temporary source selected while replacing an active Squirrel bundle
  RAG_IME_SQUIRREL_ALLOW_SOURCE_ROOT_CHANGE allow a different checkout to replace the installed build marker owner
  RAG_IME_SQUIRREL_INSTALL_LOCK_DIR atomic install lock directory
  RAG_IME_SQUIRREL_CANONICALIZE_INPUT_METHODS quarantine old RAG-IME/RagIme/Squirrel aliases before install
  RAG_IME_SQUIRREL_CANONICAL_INPUT_METHOD_ALIASES colon-separated app names to keep unique in install dir
  RAG_IME_SQUIRREL_SYSTEM_INPUT_METHOD_DIR system Input Methods directory scanned for same-bundle conflicts
  RAG_IME_SQUIRREL_INPUT_SOURCE_ID input source checked after install
  RAG_IME_XCODEBUILD             xcodebuild executable override
  RAG_IME_CLT_SDK_ROOT           Command Line Tools SDK root override
  RAG_IME_INSTALL_NAME_TOOL      no-space install_name_tool executable override
  RAG_IME_DITTO                 archive tool executable override
  RAG_IME_PLISTBUDDY            plist tool executable override
  RAG_IME_SQUIRREL_BUILD_DRY_RUN print resolved commands without requiring Xcode/workdir
EOF
}

xcodebuild_base_args=(
  -project "$PROJECT_PATH"
)

xcodebuild_build_args=(
  -project "$PROJECT_PATH"
  -scheme "$SCHEME"
  -configuration "$CONFIGURATION"
  -derivedDataPath "$DERIVED_DATA"
  "${extra_build_settings[@]}"
  build
)

PRODUCT_APP="$DERIVED_DATA/Build/Products/$CONFIGURATION/Squirrel.app"
TARGET_APP="$INSTALL_DIR/$INSTALL_APP_NAME.app"
CONFIG_SNIPPET="$SQUIRREL_WORKDIR/rag-ime.squirrel.custom.yaml"

if [[ "$ACTION" != "list" && "$ACTION" != "build" && "$ACTION" != "install" ]]; then
  usage >&2
  echo "unknown action: $ACTION" >&2
  exit 1
fi

if bool_true "$DRY_RUN"; then
  cat <<EOF
repo_root=$ROOT
workdir=$SQUIRREL_WORKDIR
project=$PROJECT_PATH
scheme=$SCHEME
configuration=$CONFIGURATION
derived_data=$DERIVED_DATA
product_app=$PRODUCT_APP
install_dir=$INSTALL_DIR
install_app_name=$INSTALL_APP_NAME
target_app=$TARGET_APP
config_snippet=$CONFIG_SNIPPET
action=$ACTION
xcodebuild=${XCODEBUILD:-<not-found>}
preinstall=$PREINSTALL
no_download=$NO_DOWNLOAD
build_settings=$BUILD_SETTINGS_EXTRA
x11_header_root=${x11_header_root:-<not-found>}
codesign_identity=$CODESIGN_IDENTITY
skip_codesign=$SKIP_CODESIGN
enable_pref_repair=$ENABLE_PREF_REPAIR
auto_select=$AUTO_SELECT
handoff_input_source_id=$HANDOFF_INPUT_SOURCE_ID
allow_source_root_change=$ALLOW_SOURCE_ROOT_CHANGE
install_lock_dir=$INSTALL_LOCK_DIR
canonicalize_input_methods=$CANONICALIZE_INPUT_METHODS
canonical_input_method_aliases=$CANONICAL_INPUT_METHOD_ALIASES
canonical_quarantine_dir=$CANONICAL_QUARANTINE_DIR
system_input_method_dir=$SYSTEM_INPUT_METHOD_DIR
bundle_id=$BUNDLE_ID
input_source_id=$INPUT_SOURCE_ID
hant_input_source_id=$HANT_INPUT_SOURCE_ID
display_name=$DISPLAY_NAME
connection_name=$CONNECTION_NAME
list_command=${XCODEBUILD:-xcodebuild} ${xcodebuild_base_args[*]} -list
build_command=${XCODEBUILD:-xcodebuild} ${xcodebuild_build_args[*]}
install_command=$0 install
EOF
  exit 0
fi

require_file() {
  local path="$1"
  local message="$2"
  if [[ ! -f "$path" ]]; then
    echo "$message: $path" >&2
    exit 1
  fi
}

verify_generated_config_owner() {
  local config_path="$1"
  "$PYTHON_BIN" - "$config_path" "$ROOT" <<'PY'
from pathlib import Path
import sys

config_path = Path(sys.argv[1])
expected_root = Path(sys.argv[2]).resolve()
inside_rag_ime = False
configured_root = ""
for raw_line in config_path.read_text(encoding="utf-8").splitlines():
    line = raw_line.rstrip()
    if line == "rag_ime:":
        inside_rag_ime = True
        continue
    if not inside_rag_ime:
        continue
    if line and not line.startswith("  "):
        break
    key, separator, value = line.strip().partition(":")
    if separator and key == "repo_root":
        configured_root = value.strip().strip("\"'")
        break

if not configured_root:
    raise SystemExit(
        f"patched Squirrel config has no rag_ime.repo_root: {config_path}"
    )

actual_root = Path(configured_root).expanduser().resolve()
if actual_root != expected_root:
    raise SystemExit(
        "patched Squirrel config repo_root is stale: "
        f"{actual_root}; expected current product source root: {expected_root}. "
        "Prepare a fresh workspace with scripts/prepare_squirrel_workspace.sh."
    )
PY
}

ensure_assistant_overlay_v2_sources() {
  local source_dir="$ROOT/squirrel-patches/sources"
  local file
  for file in \
    RagImeAssistantSurfaceState.swift \
    RagImeSuggestionCardView.swift \
    RagImeSuggestionRowView.swift \
    RagImeNonActivatingPanel.swift \
    RagImeAssistantPanelController.swift \
    RagImeInputCaptureOutbox.swift; do
    [[ -f "$source_dir/$file" ]] || continue
    cp "$source_dir/$file" "$SQUIRREL_WORKDIR/sources/$file"
  done
}

sha256_file() {
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  elif command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    "$PYTHON_BIN" - "$1" <<'PY'
import hashlib
import sys
from pathlib import Path

print(hashlib.sha256(Path(sys.argv[1]).read_bytes()).hexdigest())
PY
  fi
}

write_rag_ime_build_marker() {
  local app_path="$1"
  local marker_path="$app_path/Contents/Resources/rag-ime-build-marker.json"
  local patch_path="$ROOT/squirrel-patches/0001-add-rag-ime-sidecar.patch"
  local git_commit="unknown"
  local git_branch="unknown"
  local git_dirty="unknown"
  local patch_sha256="missing"
  local overlay_sha256="missing"
  local generated_at

  mkdir -p "$(dirname "$marker_path")"
  generated_at="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  if command -v git >/dev/null 2>&1 && git -C "$ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    git_commit="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || printf unknown)"
    git_branch="$(git -C "$ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || printf unknown)"
    if [[ -n "$(git -C "$ROOT" status --short --untracked-files=no 2>/dev/null)" ]]; then
      git_dirty="true"
    else
      git_dirty="false"
    fi
  fi
  if [[ -f "$patch_path" ]]; then
    patch_sha256="$(sha256_file "$patch_path")"
  fi
  overlay_sha256="$($PYTHON_BIN - "$ROOT/squirrel-patches/sources" <<'PY'
import hashlib
import sys
from pathlib import Path

root = Path(sys.argv[1])
names = (
    "RagImeAssistantSurfaceState.swift",
    "RagImeSuggestionCardView.swift",
    "RagImeSuggestionRowView.swift",
    "RagImeNonActivatingPanel.swift",
    "RagImeAssistantPanelController.swift",
)
digest = hashlib.sha256()
for name in names:
    path = root / name
    if not path.is_file():
        print("missing")
        raise SystemExit(0)
    digest.update(name.encode("utf-8"))
    digest.update(b"\0")
    digest.update(path.read_bytes())
    digest.update(b"\0")
print(digest.hexdigest())
PY
)"

  "$PYTHON_BIN" - "$marker_path" <<PY
import json
import sys
from pathlib import Path

payload = {
    "schemaVersion": "rag-ime.squirrel-build-marker.v2",
    "generatedAt": "$generated_at",
    "repoRoot": "$ROOT",
    "gitCommit": "$git_commit",
    "gitBranch": "$git_branch",
    "gitDirty": "$git_dirty",
    "patchPath": "squirrel-patches/0001-add-rag-ime-sidecar.patch",
    "patchSha256": "$patch_sha256",
    "overlaySha256": "$overlay_sha256",
    "installAppName": "$INSTALL_APP_NAME",
    "bundleId": "$BUNDLE_ID",
    "inputSourceId": "$INPUT_SOURCE_ID",
    "hantInputSourceId": "$HANT_INPUT_SOURCE_ID",
    "displayName": "$DISPLAY_NAME",
    "features": {
        "displayTextPrefersCleanText": True,
        "sourceCommentsHiddenByDefault": True,
        "sourceSuffixStripper": True,
        "foregroundTrace": "rag-ime.foreground-trace.v2",
    },
}
Path(sys.argv[1]).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
  printf '[OK] wrote RAG-IME build marker: %s\n' "$marker_path"
}

require_text() {
  local path="$1"
  local text="$2"
  local description="$3"
  if ! grep -Fq -- "$text" "$path"; then
    echo "patched Squirrel workdir is missing $description in $path" >&2
    exit 1
  fi
}

require_trace_event_field() {
  local path="$1"
  local event_name="$2"
  local field_text="$3"
  local description="$4"
  "$PYTHON_BIN" - "$path" "$event_name" "$field_text" "$description" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
event_name = sys.argv[2]
field_text = sys.argv[3]
description = sys.argv[4]
text = path.read_text(encoding="utf-8")
needle = f'traceRagImeFrontendEvent("{event_name}"'
start = text.find(needle)
if start < 0:
    raise SystemExit(f"patched Squirrel workdir is missing {event_name} trace event in {path}")
end = text.find("])", start)
block = text[start : end + 2 if end >= 0 else start + 2400]
if field_text not in block:
    raise SystemExit(f"patched Squirrel workdir is missing {description} in {event_name} trace block: {path}")
PY
}

if [[ ! -d "$SQUIRREL_WORKDIR/.git" ]]; then
  echo "Squirrel workdir not prepared: $SQUIRREL_WORKDIR" >&2
  echo "Run scripts/prepare_squirrel_workspace.sh first." >&2
  exit 1
fi

if [[ ! -d "$PROJECT_PATH" || ! -f "$PROJECT_PATH/project.pbxproj" ]]; then
  echo "patched Squirrel Xcode project missing: $PROJECT_PATH" >&2
  echo "Run scripts/prepare_squirrel_workspace.sh first and verify the Squirrel checkout." >&2
  exit 1
fi

require_file "$CONFIG_SNIPPET" "patched Squirrel workdir is missing generated config snippet"
verify_generated_config_owner "$CONFIG_SNIPPET"
ensure_assistant_overlay_v2_sources
require_file "$SQUIRREL_WORKDIR/sources/RagImeSidecarModels.swift" "patched Squirrel workdir is missing RAG-IME model file"
require_file "$SQUIRREL_WORKDIR/sources/RagImeSidecarClient.swift" "patched Squirrel workdir is missing RAG-IME client file"
require_file "$SQUIRREL_WORKDIR/sources/RagImeSelectedTextProvider.swift" "patched Squirrel workdir is missing RAG-IME selected text provider file"
require_file "$SQUIRREL_WORKDIR/sources/RagImeAssistantSurfaceState.swift" "patched Squirrel workdir is missing Assistant Overlay state file"
require_file "$SQUIRREL_WORKDIR/sources/RagImeSuggestionCardView.swift" "patched Squirrel workdir is missing Assistant Overlay card file"
require_file "$SQUIRREL_WORKDIR/sources/RagImeSuggestionRowView.swift" "patched Squirrel workdir is missing Assistant Overlay row file"
require_file "$SQUIRREL_WORKDIR/sources/RagImeNonActivatingPanel.swift" "patched Squirrel workdir is missing non-activating panel file"
require_file "$SQUIRREL_WORKDIR/sources/RagImeAssistantPanelController.swift" "patched Squirrel workdir is missing Assistant Overlay controller file"
require_file "$SQUIRREL_WORKDIR/sources/SquirrelInputController.swift" "patched Squirrel workdir is missing patched SquirrelInputController"
require_file "$SQUIRREL_WORKDIR/sources/SquirrelPanel.swift" "patched Squirrel workdir is missing patched SquirrelPanel"
require_text "$SQUIRREL_WORKDIR/sources/RagImeSidecarModels.swift" "RagImeWindowContextSnapshot" "Accessibility-only window context contract"
require_text "$SQUIRREL_WORKDIR/sources/RagImeSidecarClient.swift" "rime-rank-feedback" "native Rime selection feedback hook"
require_text "$SQUIRREL_WORKDIR/sources/RagImeSidecarModels.swift" "let privacyDisposition: String" "explicit foreground privacy disposition contract"
require_text "$SQUIRREL_WORKDIR/sources/RagImeSidecarClient.swift" 'request.privacyDisposition == "allowed"' "sidecar allowed-transaction send gate"
require_text "$SQUIRREL_WORKDIR/sources/SquirrelInputController.swift" 'ragImePrivacyDisposition == "allowed"' "foreground privacy assessment propagation"
require_text "$SQUIRREL_WORKDIR/sources/SquirrelInputController.swift" "privacyDisposition: ragImePrivacyDisposition" "foreground request privacy disposition"
require_text "$SQUIRREL_WORKDIR/sources/SquirrelInputController.swift" "ragImePanelForcesHorizontalLayout" "mixed LLM horizontal-lane guard"
require_text "$SQUIRREL_WORKDIR/sources/SquirrelInputController.swift" "ragImePanelUsesSideDisplay" "RAG-IME side-display panel marker"
require_text "$SQUIRREL_WORKDIR/sources/SquirrelInputController.swift" "traceRagImeFrontendEvent" "foreground frontend trace hook"
require_text "$SQUIRREL_WORKDIR/sources/SquirrelInputController.swift" "guard ragImeSidecarClient?.frontendTrace == true || isCaptureDeliveryEvent else { return }" "frontend trace configuration gate"
require_text "$SQUIRREL_WORKDIR/sources/SquirrelInputController.swift" "guard !ragImeSensitiveFieldActive || sensitiveSafeEvents.contains(event) else { return }" "sensitive-field trace suppression"
require_text "$SQUIRREL_WORKDIR/sources/SquirrelInputController.swift" "ragImePrepareFrontendTraceLog" "bounded frontend trace log"
require_text "$SQUIRREL_WORKDIR/sources/SquirrelInputController.swift" ".posixPermissions: 0o600" "private frontend trace permissions"
require_text "$SQUIRREL_WORKDIR/sources/SquirrelInputController.swift" "appendingPathExtension(\"1\")" "frontend trace rotation"
require_text "$SQUIRREL_WORKDIR/sources/SquirrelInputController.swift" "let contextAnchor = ragImeStableTextHash(context)" "hashed local action context anchor"
require_text "$SQUIRREL_WORKDIR/sources/SquirrelInputController.swift" "queryAnchor: contextAnchor" "hashed query anchor"
require_text "$SQUIRREL_WORKDIR/sources/SquirrelInputController.swift" "displayAnchor: contextAnchor" "hashed display anchor"
require_text "$SQUIRREL_WORKDIR/sources/SquirrelInputController.swift" "panel_text_layout" "actual frontend mixed-layout trace event"
require_text "$SQUIRREL_WORKDIR/sources/SquirrelInputController.swift" "sidecar_request_scheduled" "real foreground sidecar request trace event"
require_text "$SQUIRREL_WORKDIR/sources/SquirrelInputController.swift" "sidecar_empty_response_cleared" "empty sidecar response guard"
require_text "$SQUIRREL_WORKDIR/sources/SquirrelInputController.swift" "rag-ime.foreground-trace.v2" "foreground trace v2 marker"
require_text "$SQUIRREL_WORKDIR/sources/SquirrelInputController.swift" "let forceSideCandidates = rawInput.isEmpty && preedit.isEmpty" "foreground post-commit-only LLM/RAG candidate request"
require_text "$SQUIRREL_WORKDIR/sources/SquirrelInputController.swift" "forceSideCandidates: forceSideCandidates" "foreground dynamic LLM/RAG candidate request"
require_text "$SQUIRREL_WORKDIR/sources/SquirrelInputController.swift" "composition_ai_suppressed" "composition-phase AI overlay suppression"
require_text "$SQUIRREL_WORKDIR/sources/SquirrelInputController.swift" "rime_composition_started" "composition ownership trace"
require_text "$SQUIRREL_WORKDIR/sources/SquirrelInputController.swift" "rime_composition_candidates_visible" "Rime composition candidate trace"
require_text "$SQUIRREL_WORKDIR/sources/SquirrelInputController.swift" "打开澄控制中心..." "native control center menu entry"
require_text "$SQUIRREL_WORKDIR/sources/SquirrelInputController.swift" "配置由控制中心管理" "native configuration ownership copy"
require_text "$SQUIRREL_WORKDIR/sources/SquirrelInputController.swift" "重新启动后台服务" "native backend restart copy"
require_text "$SQUIRREL_WORKDIR/sources/SquirrelInputController.swift" "诊断与修复..." "native diagnostics copy"
require_text "$SQUIRREL_WORKDIR/sources/SquirrelInputController.swift" "Applications/RagImeControl.app" "canonical control center app launch"
require_text "$SQUIRREL_WORKDIR/sources/SquirrelInputController.swift" "--paw-session=" "exact Electron Session launch"
require_text "$SQUIRREL_WORKDIR/sources/SquirrelInputController.swift" "--paw-capture" "Electron screen capture launch"
require_text "$SQUIRREL_WORKDIR/sources/SquirrelInputController.swift" "RAG_IME_ASSISTANT_OVERLAY_AUTO_PENDING" "post-commit assistant overlay opt-in guard"
require_text "$SQUIRREL_WORKDIR/sources/SquirrelInputController.swift" "assistant_overlay_local_placeholder_suppressed" "post-commit local placeholder suppression"
require_text "$SQUIRREL_WORKDIR/sources/SquirrelInputController.swift" "ragImeForegroundContextResolver.captureFromAccessibility(" "queued foreground Accessibility snapshot request"
require_text "$SQUIRREL_WORKDIR/sources/SquirrelInputController.swift" "assistant_overlay_active_rag_thinking" "Active RAG assistant overlay thinking event"
require_trace_event_field "$SQUIRREL_WORKDIR/sources/SquirrelInputController.swift" "active_rag_thinking_displayed" '"selectedTextChars": request.selectedTextChars' "Active RAG selected-text count trace anchor"
require_trace_event_field "$SQUIRREL_WORKDIR/sources/SquirrelInputController.swift" "active_rag_thinking_displayed" '"frontAppBundleId": currentApp' "Active RAG front-app trace anchor"
require_trace_event_field "$SQUIRREL_WORKDIR/sources/SquirrelInputController.swift" "active_rag_thinking_displayed" '"traceIncludesText": false' "Active RAG privacy trace marker"
require_trace_event_field "$SQUIRREL_WORKDIR/sources/SquirrelInputController.swift" "active_rag_status_poll_scheduled" '"selectedTextChars": request.selectedTextChars' "Active RAG poll selected-text count trace anchor"
require_trace_event_field "$SQUIRREL_WORKDIR/sources/SquirrelInputController.swift" "active_rag_status_poll_scheduled" '"frontAppBundleId": request.frontAppBundleId' "Active RAG poll front-app trace anchor"
require_trace_event_field "$SQUIRREL_WORKDIR/sources/SquirrelInputController.swift" "active_rag_status_poll_scheduled" '"traceIncludesText": false' "Active RAG poll privacy trace marker"
require_text "$SQUIRREL_WORKDIR/sources/RagImeSelectedTextProvider.swift" "kAXSelectedTextRangeAttribute" "focused text selected range accessibility capture"
require_text "$SQUIRREL_WORKDIR/sources/RagImeSelectedTextProvider.swift" "kAXStringForRangeParameterizedAttribute" "focused text surrounding range accessibility capture"
require_text "$SQUIRREL_WORKDIR/sources/RagImeSelectedTextProvider.swift" "RagImeForegroundContextResolver" "delayed IMK to Accessibility foreground context resolver"
require_text "$SQUIRREL_WORKDIR/sources/RagImeSelectedTextProvider.swift" "captureWindowContext" "bounded Accessibility window semantic capture"
require_text "$SQUIRREL_WORKDIR/sources/RagImeSelectedTextProvider.swift" "AXUIElementCopyMultipleAttributeValues" "batched Accessibility attribute reads"
require_text "$SQUIRREL_WORKDIR/sources/RagImeSelectedTextProvider.swift" "AXManualAccessibility" "Electron Accessibility tree activation fallback"
require_text "$SQUIRREL_WORKDIR/sources/RagImeSelectedTextProvider.swift" "AXUIElementGetPid" "focused Accessibility element app-identity guard"
require_text "$SQUIRREL_WORKDIR/sources/RagImeSelectedTextProvider.swift" "privacy_unknown_app_bundle_missing" "missing app identity privacy fail-closed guard"
require_text "$SQUIRREL_WORKDIR/sources/RagImeSelectedTextProvider.swift" "isSensitive: false, reason: \"privacy_unknown_ax_not_trusted\"" "optional accessibility metadata fallback"
require_text "$SQUIRREL_WORKDIR/sources/RagImeSelectedTextProvider.swift" "privacy_unknown_focused_element_missing" "missing focused element privacy fail-closed guard"
require_text "$SQUIRREL_WORKDIR/sources/RagImeSelectedTextProvider.swift" "privacy_unknown_metadata_read_failed" "accessibility metadata privacy fail-closed guard"
require_text "$SQUIRREL_WORKDIR/sources/RagImeSelectedTextProvider.swift" "privacy_unknown_text_field_metadata_missing" "empty text-field metadata privacy fail-closed guard"
require_text "$SQUIRREL_WORKDIR/sources/RagImeSelectedTextProvider.swift" "sensitive_application_bundle" "sensitive app bundle denylist"
require_text "$SQUIRREL_WORKDIR/sources/RagImeSelectedTextProvider.swift" "RAG_IME_SENSITIVE_APP_BUNDLE_IDS" "sensitive app bundle environment configuration"
require_text "$SQUIRREL_WORKDIR/sources/RagImeSelectedTextProvider.swift" "RagImeSensitiveAppBundleTokens" "sensitive app bundle defaults configuration"
require_text "$SQUIRREL_WORKDIR/sources/SquirrelInputController.swift" "foreground_context_capture_resolved" "foreground context capture success trace"
require_text "$SQUIRREL_WORKDIR/sources/SquirrelInputController.swift" "foreground_context_capture_failed" "foreground context capture failure trace"
require_text "$SQUIRREL_WORKDIR/sources/SquirrelInputController.swift" "side_candidate_feedback_recorded" "selection feedback receipt trace"
require_text "$SQUIRREL_WORKDIR/sources/SquirrelInputController.swift" "ragImeNativeSelectionSnapshot" "native Rime selection snapshot"
require_text "$SQUIRREL_WORKDIR/sources/SquirrelInputController.swift" "native_rime_rank_feedback_recorded" "native Rime selection feedback trace"
require_text "$SQUIRREL_WORKDIR/sources/SquirrelInputController.swift" "guard !enforceRagImeFastPrivacyGuard()" "native Rime feedback non-blocking privacy guard"
require_text "$SQUIRREL_WORKDIR/sources/SquirrelInputController.swift" "probeRagImeForegroundPrivacyAndContext" "background Accessibility privacy probe"
require_text "$SQUIRREL_WORKDIR/sources/SquirrelInputController.swift" "privacy_probe_timeout" "background privacy timeout fail-closed path"
require_text "$SQUIRREL_WORKDIR/sources/SquirrelInputController.swift" "active_rag_window_context_capture_scheduled" "quick-generate asynchronous window context capture"
require_text "$SQUIRREL_WORKDIR/sources/SquirrelInputController.swift" '"usesScreenCapture": false' "quick-generate semantic-only capture marker"
require_text "$SQUIRREL_WORKDIR/sources/SquirrelInputController.swift" "discardRagImeSensitiveNativeLearningTransaction" "sensitive native Rime learning rollback"
require_text "$SQUIRREL_WORKDIR/sources/SquirrelInputController.swift" "guard rimeAPI.get_status(session, &status) else { return false }" "native learning rollback composition-state check"
require_text "$SQUIRREL_WORKDIR/sources/SquirrelInputController.swift" "guard !isComposing else { return false }" "native learning rollback requires completed commit"
require_text "$SQUIRREL_WORKDIR/sources/SquirrelInputController.swift" "rimeAPI.process_key(session, Int32(XK_BackSpace), 0)" "isolated librime pending-transaction rollback"
require_text "$SQUIRREL_WORKDIR/sources/SquirrelInputController.swift" "return !backspaceHandled" "native learning rollback requires unhandled BackSpace"
require_text "$SQUIRREL_WORKDIR/sources/SquirrelInputController.swift" '"forwardedToClient": false' "native learning rollback is not forwarded to IMK"
require_text "$SQUIRREL_WORKDIR/sources/SquirrelInputController.swift" "assistant_overlay_candidate_visible" "assistant overlay candidate trace"
require_text "$SQUIRREL_WORKDIR/sources/RagImeSelectedTextProvider.swift" "kAXValueAttribute" "focused text whole-value fallback"
require_text "$SQUIRREL_WORKDIR/sources/SquirrelInputController.swift" "candidate.sourceType" "compact model inline candidate comments"
require_text "$SQUIRREL_WORKDIR/sources/Main.swift" "traceRagImeProcessEvent" "foreground process trace hook"
require_text "$SQUIRREL_WORKDIR/sources/SquirrelPanel.swift" "candidateSeparator" "mixed inline/block candidate separator"
require_text "$SQUIRREL_WORKDIR/sources/SquirrelPanel.swift" "ragImePanelLinear" "forced horizontal layout for inline LLM candidates"
require_text "$SQUIRREL_WORKDIR/sources/SquirrelPanel.swift" "ragImePanelUsesSideDisplay" "RAG-IME side-display panel clamp"
require_text "$SQUIRREL_WORKDIR/sources/SquirrelPanel.swift" "return NSView()" "plain non-glass macOS 26 panel background"
require_text "$SQUIRREL_WORKDIR/sources/SquirrelPanel.swift" "traceRagImePanelTextLayout" "actual frontend mixed-layout trace"
require_text "$SQUIRREL_WORKDIR/sources/RagImeAssistantPanelController.swift" "same_snapshot_stable_ids" "Assistant Overlay snapshot diff"
require_text "$SQUIRREL_WORKDIR/sources/RagImeAssistantPanelController.swift" "passive_mouse_fallback" "passive missing-anchor fallback"
require_text "$SQUIRREL_WORKDIR/sources/RagImeAssistantPanelController.swift" "assistant_panel_created" "Assistant Overlay lifecycle tracing"
require_text "$SQUIRREL_WORKDIR/sources/RagImeSuggestionCardView.swift" "actionPredictionWidth" "content-sized prediction panel width"
require_text "$SQUIRREL_WORKDIR/sources/RagImeSuggestionRowView.swift" "shortcutPlate" "stable shortcut keycap lane"
if [[ -f "$SQUIRREL_WORKDIR/sources/InputSource.swift" ]]; then
  require_text "$SQUIRREL_WORKDIR/sources/InputSource.swift" "static var inputSourceIDPrefix: String" "brandable input-source prefix"
fi

deps_ready() {
  [[ -f "$SQUIRREL_WORKDIR/lib/librime.1.dylib" ]] &&
    [[ -d "$SQUIRREL_WORKDIR/Frameworks/Sparkle.framework" ]] &&
    [[ -f "$SQUIRREL_WORKDIR/bin/rime-install" ]]
}

sync_librime_headers() {
  local source_dir="$SQUIRREL_WORKDIR/librime/dist/include"
  local target_dir="$SQUIRREL_WORKDIR/librime/include"
  if [[ ! -d "$source_dir" ]]; then
    return 0
  fi
  mkdir -p "$target_dir"
  cp -R "$source_dir"/. "$target_dir"/
}

ensure_librime_source_headers() {
  if [[ -f "$SQUIRREL_WORKDIR/librime/src/rime/key_table.h" ]]; then
    return 0
  fi
  if [[ ! -f "$SQUIRREL_WORKDIR/.gitmodules" ]] || ! grep -Fq 'path = librime' "$SQUIRREL_WORKDIR/.gitmodules"; then
    return 0
  fi

  local restore_dir
  restore_dir="$(mktemp -d /tmp/rag-ime-librime-restore.XXXXXX)"
  for name in dist share include; do
    if [[ -e "$SQUIRREL_WORKDIR/librime/$name" ]]; then
      mv "$SQUIRREL_WORKDIR/librime/$name" "$restore_dir/$name"
    fi
  done
  rm -rf "$SQUIRREL_WORKDIR/librime"
  git -C "$SQUIRREL_WORKDIR" submodule update --init librime
  for name in dist share include; do
    if [[ -e "$restore_dir/$name" ]]; then
      rm -rf "$SQUIRREL_WORKDIR/librime/$name"
      mv "$restore_dir/$name" "$SQUIRREL_WORKDIR/librime/$name"
    fi
  done
  rm -rf "$restore_dir"
}

run_squirrel_action_install() {
  local action_makeflags="${MAKEFLAGS:-}"
  local action_status=0

  if [[ "${DEVELOPER_DIR:-}" == *" "* ]]; then
    if [[ "$INSTALL_NAME_TOOL" == *[[:space:]]* || ! -x "$INSTALL_NAME_TOOL" ]]; then
      echo "spaceful DEVELOPER_DIR requires an executable install_name_tool path without whitespace: $INSTALL_NAME_TOOL" >&2
      return 1
    fi
    # GNU make 3.81 treats a bare MAKEFLAGS value as single-letter flags. Keep
    # command-line variable overrides after the standalone `--` separator so
    # INSTALL_NAME_TOOL does not accidentally enable `-n` (dry-run).
    if [[ " $action_makeflags " == *" -- "* ]]; then
      action_makeflags="${action_makeflags} INSTALL_NAME_TOOL=$INSTALL_NAME_TOOL"
    else
      action_makeflags="${action_makeflags:+$action_makeflags }-- INSTALL_NAME_TOOL=$INSTALL_NAME_TOOL"
    fi
    printf '[INFO] overriding Squirrel dependency install_name_tool for a spaceful Xcode path\n'
  fi

  if bool_true "$NO_DOWNLOAD"; then
    (cd "$SQUIRREL_WORKDIR" && MAKEFLAGS="$action_makeflags" no_download=1 ./action-install.sh) || action_status=$?
  else
    (cd "$SQUIRREL_WORKDIR" && MAKEFLAGS="$action_makeflags" ./action-install.sh) || action_status=$?
  fi
  return "$action_status"
}

prepare_squirrel_dependencies() {
  if [[ "$ACTION" == "list" ]]; then
    return 0
  fi

  local should_preinstall=0
  if bool_true "$PREINSTALL"; then
    should_preinstall=1
  elif [[ "$PREINSTALL" == "auto" ]]; then
    if ! deps_ready; then
      should_preinstall=1
    fi
  fi

  if [[ "$should_preinstall" == "1" ]]; then
    if [[ ! -x "$SQUIRREL_WORKDIR/action-install.sh" ]]; then
      echo "[WARN] Squirrel action-install.sh not found; skipping dependency preinstall" >&2
    else
      printf '[INFO] preparing Squirrel binary dependencies with action-install.sh\n'
      run_squirrel_action_install
    fi
  fi

  if [[ -f "$SQUIRREL_WORKDIR/package/add_data_files" ]]; then
    printf '[INFO] refreshing Squirrel bundled data files\n'
    (cd "$SQUIRREL_WORKDIR" && bash package/add_data_files)
  fi

  sync_librime_headers
  ensure_librime_source_headers
}

ensure_squirrel_input_source_enabled() {
  local app="$1"
  local output

  if [[ ! -x "$app/Contents/MacOS/Squirrel" ]]; then
    printf '[WARN] Squirrel executable missing; cannot verify input source: %s\n' "$app" >&2
    return 0
  fi
  if output="$(RAG_IME_SQUIRREL_APP="$app" \
    RAG_IME_SQUIRREL_BUNDLE_ID="$BUNDLE_ID" \
    RAG_IME_SQUIRREL_INPUT_SOURCE_ID="$INPUT_SOURCE_ID" \
    "$ROOT/scripts/refresh_squirrel_input_source_registration.sh" 2>&1)"; then
    printf '[OK] macOS input source enabled for real use: %s\n' "$output"
    return 0
  fi
  printf '[WARN] macOS input source not confirmed for real use after install: %s\n' "$output" >&2
  printf '[WARN] If thirdPartyEnabled=false, add %s from System Settings -> Keyboard -> Input Sources.\n' "$DISPLAY_NAME" >&2
}

prevent_build_product_registration() {
  local info="$PRODUCT_APP/Contents/Info.plist"
  [[ -f "$info" ]] || return 0
  "$PLISTBUDDY" -c "Set :LSRegisterProhibited true" "$info" >/dev/null 2>&1 ||
    "$PLISTBUDDY" -c "Add :LSRegisterProhibited bool true" "$info"
  if [[ -x "$LSREGISTER" ]]; then
    "$LSREGISTER" -u "$PRODUCT_APP" >/dev/null 2>&1 || true
  fi
  printf '[OK] prevented temporary build product from registering as a second input method\n'
}

should_brand_app() {
  [[ "$INSTALL_APP_NAME" != "Squirrel" ||
    "$BUNDLE_ID" != "$DEFAULT_BUNDLE_ID" ||
    "$INPUT_SOURCE_ID" != "$DEFAULT_BUNDLE_ID.Hans" ||
    "$HANT_INPUT_SOURCE_ID" != "$DEFAULT_BUNDLE_ID.Hant" ||
    "$DISPLAY_NAME" != "Squirrel" ||
    "$CONNECTION_NAME" != "${DEFAULT_BUNDLE_ID}_Connection" ]]
}

canonicalize_input_method_bundles() {
  bool_true "$CANONICALIZE_INPUT_METHODS" || return 0

  local target_base
  local timestamp
  local quarantine_dir
  local moved_count=0
  target_base="$(basename "$TARGET_APP")"
  timestamp="$(date +%Y%m%d-%H%M%S)"
  quarantine_dir="$CANONICAL_QUARANTINE_DIR/$timestamp"

  local aliases=()
  IFS=':' read -r -a aliases <<< "$CANONICAL_INPUT_METHOD_ALIASES"
  for alias in "${aliases[@]}"; do
    [[ -n "$alias" ]] || continue
    [[ "$alias" == *.app ]] || alias="$alias.app"
    [[ "$alias" != "$target_base" ]] || continue

    local candidate="$INSTALL_DIR/$alias"
    [[ -d "$candidate" ]] || continue

    mkdir -p "$quarantine_dir"
    local destination="$quarantine_dir/${alias%.app}.disabled.zip"
    if [[ -e "$destination" ]]; then
      destination="$quarantine_dir/${alias%.app}.$$.disabled.zip"
    fi
    pkill -f "$candidate/Contents/MacOS/Squirrel" >/dev/null 2>&1 || true
    if [[ -x "$LSREGISTER" ]]; then
      "$LSREGISTER" -u "$candidate" >/dev/null 2>&1 || true
    fi
    "$DITTO" -c -k --keepParent "$candidate" "$destination"
    rm -rf "$candidate"
    moved_count=$((moved_count + 1))
    printf '[OK] archived noncanonical input method app: %s -> %s\n' "$candidate" "$destination"

    if [[ -x "$LSREGISTER" ]]; then
      "$LSREGISTER" -u "$destination" >/dev/null 2>&1 || true
    fi
  done

  if [[ "$moved_count" -gt 0 ]]; then
    printf '[OK] canonical input method bundle enforced without restarting macOS input services: %s\n' "$TARGET_APP"
  fi
}

current_input_source_id() {
  local output
  output="$(RAG_IME_INPUT_SOURCE_BUNDLE_ID="$BUNDLE_ID" \
    "$CHECK_INPUT_SOURCE_SCRIPT" "$INPUT_SOURCE_ID" 2>/dev/null || true)"
  printf '%s\n' "$output" |
    sed -n 's/.* current=\([^[:space:]]*\).*/\1/p' |
    tail -n 1
}

verify_install_source_owner() {
  local marker="$TARGET_APP/Contents/Resources/rag-ime-build-marker.json"
  local installed_root=""
  [[ -f "$marker" ]] || return 0
  installed_root="$("$PYTHON_BIN" - "$marker" <<'PY'
import json
import sys
from pathlib import Path

try:
    payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
except (OSError, ValueError):
    print("")
else:
    print(str(payload.get("repoRoot") or ""))
PY
)"
  if [[ -n "$installed_root" && "$installed_root" != "$ROOT" ]] &&
    ! bool_true "$ALLOW_SOURCE_ROOT_CHANGE"; then
    echo "installed Squirrel is owned by a different checkout: $installed_root" >&2
    echo "refusing a cross-worktree overwrite that would invalidate live IMK clients" >&2
    echo "set RAG_IME_SQUIRREL_ALLOW_SOURCE_ROOT_CHANGE=1 only for an intentional owner handoff" >&2
    exit 73
  fi
}

INSTALL_STAGING_APP=""
INSTALL_PREVIOUS_APP=""
INSTALL_SWAP_COMPLETE=0
INSTALL_COMPLETE=0
INSTALL_RESTORE_INPUT_SOURCE=""

cleanup_install_transaction() {
  local status=$?
  trap - EXIT
  if [[ "$status" -ne 0 && "$INSTALL_SWAP_COMPLETE" == "1" &&
    -n "$INSTALL_PREVIOUS_APP" && -d "$INSTALL_PREVIOUS_APP" ]]; then
    pkill -f "$TARGET_APP/Contents/MacOS/Squirrel" >/dev/null 2>&1 || true
    rm -rf "$TARGET_APP"
    mv "$INSTALL_PREVIOUS_APP" "$TARGET_APP" || true
    "$LSREGISTER" -f -R -trusted "$TARGET_APP" >/dev/null 2>&1 || true
    open -a "$TARGET_APP" >/dev/null 2>&1 || true
    printf '[WARN] restored the previous Squirrel bundle after an incomplete install\n' >&2
  fi
  if [[ "$status" -ne 0 && -n "$INSTALL_RESTORE_INPUT_SOURCE" ]]; then
    "$SELECT_INPUT_SOURCE_SCRIPT" "$INSTALL_RESTORE_INPUT_SOURCE" >/dev/null 2>&1 || true
  fi
  if [[ -n "$INSTALL_STAGING_APP" ]]; then
    rm -rf "$INSTALL_STAGING_APP"
  fi
  if [[ "$INSTALL_COMPLETE" == "1" && -n "$INSTALL_PREVIOUS_APP" ]]; then
    rm -rf "$INSTALL_PREVIOUS_APP"
  fi
  rm -rf "$INSTALL_LOCK_DIR"
  return "$status"
}

begin_install_transaction() {
  if ! mkdir "$INSTALL_LOCK_DIR" 2>/dev/null; then
    local owner="unknown"
    [[ -f "$INSTALL_LOCK_DIR/pid" ]] && owner="$(cat "$INSTALL_LOCK_DIR/pid" 2>/dev/null || printf unknown)"
    if [[ "$owner" =~ ^[0-9]+$ ]] && ! kill -0 "$owner" >/dev/null 2>&1; then
      rm -rf "$INSTALL_LOCK_DIR"
      mkdir "$INSTALL_LOCK_DIR"
      printf '[WARN] recovered stale Squirrel install lock owned by exited pid %s\n' "$owner" >&2
    else
      echo "another Squirrel install transaction is active (pid=$owner): $INSTALL_LOCK_DIR" >&2
      exit 73
    fi
  fi
  printf '%s\n' "$$" >"$INSTALL_LOCK_DIR/pid"
  trap cleanup_install_transaction EXIT
}

handoff_active_squirrel_input_source() {
  local current_source
  current_source="$(current_input_source_id)"
  if [[ "$current_source" == "$BUNDLE_ID".* ]]; then
    if [[ "$HANDOFF_INPUT_SOURCE_ID" == "$current_source" ]]; then
      echo "temporary handoff source must differ from the active Squirrel source: $current_source" >&2
      return 1
    fi
    if ! "$SELECT_INPUT_SOURCE_SCRIPT" "$HANDOFF_INPUT_SOURCE_ID" >/dev/null 2>&1; then
      echo "could not switch away from active Squirrel before bundle replacement" >&2
      echo "refusing to invalidate live application input channels; select another input source and retry" >&2
      return 1
    fi
    INSTALL_RESTORE_INPUT_SOURCE="$current_source"
    printf '[OK] handed active input source to %s before replacing Squirrel\n' "$HANDOFF_INPUT_SOURCE_ID"
  fi
}

restore_input_source_after_install() {
  local restore_source="$INSTALL_RESTORE_INPUT_SOURCE"
  local output=""
  if [[ -z "$restore_source" ]] && bool_true "$AUTO_SELECT"; then
    restore_source="$INPUT_SOURCE_ID"
  fi
  if [[ -z "$restore_source" ]]; then
    printf '[INFO] preserved the previously selected non-Squirrel input source\n'
    return 0
  fi
  if output="$("$SELECT_INPUT_SOURCE_SCRIPT" "$restore_source" 2>&1)"; then
    printf '[OK] restored input source after the new IMK endpoint started: %s\n' "$output"
  else
    printf '[WARN] could not restore input source %s after install: %s\n' "$restore_source" "$output" >&2
    return 1
  fi
}

run_branded_postinstall() {
  local app="$1"
  local output
  local registration_confirmed=0

  if output="$(RAG_IME_SQUIRREL_APP="$app" \
    RAG_IME_SQUIRREL_BUNDLE_ID="$BUNDLE_ID" \
    RAG_IME_SQUIRREL_INPUT_SOURCE_ID="$INPUT_SOURCE_ID" \
    RAG_IME_SQUIRREL_AUTO_SELECT=0 \
    RAG_IME_SQUIRREL_BUNDLE_REPLACED=1 \
    "$ROOT/scripts/refresh_squirrel_input_source_registration.sh" 2>&1)"; then
    printf '[OK] branded macOS input source enabled for real use: %s\n' "$output"
    registration_confirmed=1
  else
    printf '[WARN] branded macOS input source not confirmed after install: %s\n' "$output" >&2
    if bool_true "$ENABLE_PREF_REPAIR"; then
      RAG_IME_SQUIRREL_APP="$app" \
        RAG_IME_SQUIRREL_BUNDLE_ID="$BUNDLE_ID" \
        RAG_IME_SQUIRREL_INPUT_SOURCE_ID="$INPUT_SOURCE_ID" \
        "$ROOT/scripts/enable_squirrel_hitoolbox_input_source.sh" >/dev/null 2>&1 || true
      if output="$("$ROOT/scripts/check_macos_input_source.sh" --require-hitoolbox-enabled "$INPUT_SOURCE_ID" 2>&1)"; then
        printf '[OK] branded macOS input source enabled after HIToolbox repair: %s\n' "$output"
        registration_confirmed=1
      else
        printf '[WARN] branded macOS input source still needs manual System Settings add: %s\n' "$output" >&2
      fi
    else
      printf '[WARN] preference repair is disabled; add %s from System Settings -> Keyboard -> Input Sources.\n' "$HANS_DISPLAY_NAME" >&2
      printf '[WARN] To allow the old direct-preference repair helper, set RAG_IME_SQUIRREL_ENABLE_PREF_REPAIR=1.\n' >&2
    fi
  fi

  if [[ "$registration_confirmed" != "1" ]]; then
    echo "refusing to keep an unregistered replacement; the install transaction will restore the previous bundle" >&2
    return 1
  fi
  printf '[INFO] input-source selection is deferred until the final signed app has opened.\n'
}

install_squirrel_app() {
  if [[ ! -d "$PRODUCT_APP" ]]; then
    echo "built Squirrel.app not found: $PRODUCT_APP" >&2
    exit 1
  fi

  sign_app() {
    local phase="$1"
    local app="$2"
    if bool_true "$SKIP_CODESIGN"; then
      printf '[WARN] skipped Squirrel %s codesign; input source registration may fail\n' "$phase" >&2
      return 0
    fi
    if command -v codesign >/dev/null 2>&1; then
      if [[ "$CODESIGN_IDENTITY" == "-" ]]; then
        # Keep the local designated requirement stable across rebuilds so
        # Accessibility authorization is not tied to a changing cdhash.
        # First repair every nested dylib/tool/framework, then sign only the
        # outer bundle with the stable requirement. Applying the outer
        # requirement recursively would leave existing nested signatures
        # sealed against pre-bootstrap binaries.
        codesign --force --deep --sign - "$app"
        codesign --force --sign - \
          --requirements "=designated => identifier \"$BUNDLE_ID\"" \
          "$app"
      else
        codesign --force --deep --sign "$CODESIGN_IDENTITY" "$app"
      fi
      printf '[OK] %s signed patched Squirrel.app with identity: %s\n' "$phase" "$CODESIGN_IDENTITY"
    else
      printf '[WARN] codesign not found during %s; input source registration may fail\n' "$phase" >&2
    fi
  }

  begin_install_transaction
  verify_install_source_owner
  mkdir -p "$INSTALL_DIR"
  "$PYTHON_BIN" "$ROOT/scripts/audit_canonical_squirrel_bundles.py" \
    --preinstall \
    --canonical-app "$TARGET_APP" \
    --user-dir "$INSTALL_DIR" \
    --system-dir "$SYSTEM_INPUT_METHOD_DIR" \
    --bundle-id "$BUNDLE_ID" >/dev/null
  printf '[OK] no conflicting same-bundle Squirrel app found before install\n'
  canonicalize_input_method_bundles

  # Build, brand, bootstrap and seal a hidden staging bundle. LaunchServices
  # never sees a partially modified or multiply signed canonical input method.
  INSTALL_STAGING_APP="$INSTALL_DIR/.${INSTALL_APP_NAME}.installing.$$"
  INSTALL_PREVIOUS_APP="$INSTALL_DIR/.${INSTALL_APP_NAME}.previous.$$"
  rm -rf "$INSTALL_STAGING_APP" "$INSTALL_PREVIOUS_APP"
  cp -R "$PRODUCT_APP" "$INSTALL_STAGING_APP"
  mkdir -p "$INSTALL_STAGING_APP/Contents/Resources"
  cp "$ROOT/macos/Shared/Assets/CompanionStates/"*.png "$INSTALL_STAGING_APP/Contents/Resources/"
  "$ROOT/scripts/support/build_input_menu_icon.sh" \
    "$INSTALL_STAGING_APP/Contents/Resources/RagImeInputMenuIcon.png"
  "$PLISTBUDDY" -c "Delete :LSRegisterProhibited" "$INSTALL_STAGING_APP/Contents/Info.plist" >/dev/null 2>&1 || true
  "$ROOT/scripts/support/build_app_icon.sh" "$INSTALL_STAGING_APP/Contents/Resources/RagImeIcon.icns"
  "$PLISTBUDDY" -c "Set :CFBundleIconFile RagImeIcon" "$INSTALL_STAGING_APP/Contents/Info.plist" >/dev/null 2>&1 || \
    "$PLISTBUDDY" -c "Add :CFBundleIconFile string RagImeIcon" "$INSTALL_STAGING_APP/Contents/Info.plist"

  if should_brand_app; then
    RAG_IME_SQUIRREL_APP="$INSTALL_STAGING_APP" \
      RAG_IME_SQUIRREL_BUNDLE_ID="$BUNDLE_ID" \
      RAG_IME_SQUIRREL_INPUT_SOURCE_ID="$INPUT_SOURCE_ID" \
      RAG_IME_SQUIRREL_HANT_INPUT_SOURCE_ID="$HANT_INPUT_SOURCE_ID" \
      RAG_IME_SQUIRREL_DISPLAY_NAME="$DISPLAY_NAME" \
      RAG_IME_SQUIRREL_HANS_DISPLAY_NAME="$HANS_DISPLAY_NAME" \
      RAG_IME_SQUIRREL_HANT_DISPLAY_NAME="$HANT_DISPLAY_NAME" \
      RAG_IME_SQUIRREL_CONNECTION_NAME="$CONNECTION_NAME" \
      "$ROOT/scripts/brand_squirrel_app.sh" "$INSTALL_STAGING_APP"
    printf '[OK] branded patched Squirrel.app as %s (%s)\n' "$DISPLAY_NAME" "$BUNDLE_ID"
  fi

  write_rag_ime_build_marker "$INSTALL_STAGING_APP"

  sign_app "initial" "$INSTALL_STAGING_APP"

  RAG_IME_SQUIRREL_WORKDIR="$SQUIRREL_WORKDIR" \
    RAG_IME_SQUIRREL_CONFIG_SNIPPET="$SQUIRREL_WORKDIR/rag-ime.squirrel.custom.yaml" \
    RAG_IME_SQUIRREL_APP="$INSTALL_STAGING_APP" \
    RAG_IME_SQUIRREL_DEPLOY=0 \
    "$ROOT/scripts/install_squirrel_rag_config.sh"
  printf '[OK] installed RAG-IME Squirrel config\n'

  if bool_true "$SKIP_POSTINSTALL"; then
    printf '[WARN] skipped Squirrel user-data bootstrap and postinstall; input source may need manual registration\n' >&2
  else
    RAG_IME_SQUIRREL_WORKDIR="$SQUIRREL_WORKDIR" \
      RAG_IME_SQUIRREL_APP="$INSTALL_STAGING_APP" \
      "$ROOT/scripts/bootstrap_squirrel_user_data.sh"
  fi

  # Bootstrap can generate SharedSupport/build files inside the staged bundle.
  # Seal that final state before the canonical path changes.
  sign_app "final" "$INSTALL_STAGING_APP"

  # Changing the selected input source forces every open application to close
  # its old IMK channel before the canonical bundle changes. After the new
  # signed bundle is registered, branded postinstall restarts only
  # imklaunchagent once so it cannot retain the old connection-name cache.
  # TextInputMenuAgent and cfprefsd remain untouched.
  handoff_active_squirrel_input_source
  pkill -f "$TARGET_APP/Contents/MacOS/Squirrel" >/dev/null 2>&1 || true
  for ((attempt = 0; attempt < 40; attempt++)); do
    if ! pgrep -f "$TARGET_APP/Contents/MacOS/Squirrel" >/dev/null 2>&1; then
      break
    fi
    sleep 0.05
  done
  if pgrep -f "$TARGET_APP/Contents/MacOS/Squirrel" >/dev/null 2>&1; then
    echo "old Squirrel process did not exit; refusing to replace its live bundle" >&2
    return 1
  fi

  if [[ -d "$TARGET_APP" ]]; then
    mv "$TARGET_APP" "$INSTALL_PREVIOUS_APP"
  fi
  mv "$INSTALL_STAGING_APP" "$TARGET_APP"
  INSTALL_STAGING_APP=""
  INSTALL_SWAP_COMPLETE=1
  printf '[OK] atomically installed final signed Squirrel.app: %s\n' "$TARGET_APP"

  if ! bool_true "$SKIP_POSTINSTALL"; then
    if should_brand_app; then
      run_branded_postinstall "$TARGET_APP"
    elif [[ -f "$SQUIRREL_WORKDIR/scripts/postinstall" ]]; then
      (cd "$SQUIRREL_WORKDIR" && DSTROOT="$INSTALL_DIR" bash scripts/postinstall)
      printf '[OK] Squirrel postinstall completed\n'
      sign_app "postinstall" "$TARGET_APP"
      ensure_squirrel_input_source_enabled "$TARGET_APP"
    else
      printf '[WARN] Squirrel postinstall script not found; input source may need manual registration\n' >&2
    fi
  fi

  if ! bool_true "$SKIP_POSTINSTALL" || [[ -n "$INSTALL_RESTORE_INPUT_SOURCE" ]] || bool_true "$AUTO_SELECT"; then
    open -a "$TARGET_APP" >/dev/null 2>&1 || true
    sleep 0.75
  fi
  INSTALL_COMPLETE=1
  if ! restore_input_source_after_install; then
    printf '[WARN] keeping the registered replacement installed; input-source selection can be retried without rolling back the IMK endpoint\n' >&2
  fi
}

if [[ -z "$XCODEBUILD" || ! -x "$XCODEBUILD" ]]; then
  active_developer_dir="$(xcode-select -p 2>/dev/null || true)"
  if [[ "$active_developer_dir" == *CommandLineTools* ]]; then
    echo "xcodebuild is not available from full Xcode; active developer directory is CommandLineTools." >&2
  else
    echo "xcodebuild is not available." >&2
  fi
  echo "Install/select full Xcode with scripts/setup_xcode_for_squirrel.sh." >&2
  exit 1
fi

if ! xcodebuild_version="$("$XCODEBUILD" -version 2>&1)"; then
  active_developer_dir="$(xcode-select -p 2>/dev/null || true)"
  echo "xcodebuild -version failed." >&2
  if [[ "$active_developer_dir" == *CommandLineTools* ]]; then
    echo "active developer directory is CommandLineTools; full Xcode is required for Squirrel." >&2
  fi
  printf '%s\n' "$xcodebuild_version" >&2
  echo "Install/select full Xcode with scripts/setup_xcode_for_squirrel.sh." >&2
  exit 1
fi

printf 'RAG-IME patched Squirrel build\n'
printf 'repo: %s\n' "$ROOT"
printf 'workdir: %s\n' "$SQUIRREL_WORKDIR"
printf 'project: %s\n' "$PROJECT_PATH"
printf 'scheme: %s\n' "$SCHEME"
printf 'configuration: %s\n' "$CONFIGURATION"
printf 'derived_data: %s\n' "$DERIVED_DATA"
printf 'product_app: %s\n' "$PRODUCT_APP"
printf 'install_dir: %s\n' "$INSTALL_DIR"
printf 'target_app: %s\n' "$TARGET_APP"
printf 'bundle_id: %s\n' "$BUNDLE_ID"
printf 'input_source_id: %s\n' "$INPUT_SOURCE_ID"
printf 'xcodebuild: %s\n' "$XCODEBUILD"
printf 'xcodebuild_version: %s\n\n' "$(printf '%s' "$xcodebuild_version" | tr '\n' ' ')"

"$XCODEBUILD" "${xcodebuild_base_args[@]}" -list >/dev/null
printf '[OK] xcodebuild can inspect patched Squirrel project\n'

if [[ "$ACTION" == "list" ]]; then
  exit 0
fi

prepare_squirrel_dependencies
"$XCODEBUILD" "${xcodebuild_build_args[@]}"
printf '[OK] xcodebuild build succeeded\n'
printf '[OK] built patched Squirrel.app: %s\n' "$PRODUCT_APP"
prevent_build_product_registration

if [[ "$ACTION" == "install" ]]; then
  install_squirrel_app
fi
