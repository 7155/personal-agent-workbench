#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WEB="$ROOT/control-center-web"
ELECTRON_APP="$WEB/node_modules/electron/dist/Electron.app"
ELECTRON_INSTALLER="$WEB/node_modules/electron/install.js"
ACTION="${1:-build}"

case "$ACTION" in
  build|install-preview)
    APP="$ROOT/build/RagImeControlElectronPreview.app"
    INSTALL_DEST="$HOME/Applications/RagImeControlWebPreview.app"
    EXECUTABLE="RagImeControlWebPreview"
    BUNDLE_ID="com.rag-ime.control.web-preview"
    DISPLAY_NAME="PAW Preview"
    CHANNEL="preview"
    FRONTEND_CHANNEL="preview"
    ;;
  build-release|install-release)
    APP="$ROOT/build/RagImeControlElectron.app"
    INSTALL_DEST="$HOME/Applications/Personal Agent Workbench.app"
    EXECUTABLE="RagImeControl"
    BUNDLE_ID="com.rag-ime.control"
    DISPLAY_NAME="Personal Agent Workbench"
    CHANNEL="release"
    FRONTEND_CHANNEL="production"
    ;;
  *)
    echo "usage: $0 [build|install-preview|build-release|install-release]" >&2
    exit 2
    ;;
esac

DEVELOPMENT_INSTALL="false"
case "${RAG_IME_ALLOW_DEVELOPMENT_INSTALL:-0}" in
  0) ;;
  1)
    [[ "$CHANNEL" == "release" ]] || {
      echo "RAG_IME_ALLOW_DEVELOPMENT_INSTALL=1 supports build-release or install-release only" >&2
      exit 2
    }
    DEVELOPMENT_INSTALL="true"
    ;;
  *)
    echo "RAG_IME_ALLOW_DEVELOPMENT_INSTALL must be 0 or 1" >&2
    exit 2
    ;;
esac

hydrate_electron_runtime() {
  [[ -d "$ELECTRON_APP" ]] && return 0
  [[ -f "$ELECTRON_INSTALLER" ]] || {
    echo "Electron package is unavailable; run pnpm install in control-center-web" >&2
    exit 1
  }
  echo "Hydrating the pinned Electron runtime..." >&2
  local use_proxy="${ELECTRON_GET_USE_PROXY-}"
  if [[ -z "${ELECTRON_GET_USE_PROXY+x}" &&
    -n "${HTTPS_PROXY:-}${HTTP_PROXY:-}${https_proxy:-}${http_proxy:-}" ]]; then
    use_proxy=1
  fi
  ELECTRON_GET_USE_PROXY="$use_proxy" node "$ELECTRON_INSTALLER"
  [[ -d "$ELECTRON_APP" ]] || {
    echo "Electron runtime hydration did not produce Electron.app" >&2
    exit 1
  }
}

source "$ROOT/scripts/support/prebuilt_product.sh"
if paw_prebuilt_identity; then
  [[ "$DEVELOPMENT_INSTALL" == "false" ]] || {
    echo "RAG_IME_ALLOW_DEVELOPMENT_INSTALL is for local source builds, not prebuilt payloads" >&2
    exit 2
  }
  [[ "$ACTION" == "install-release" ]] || { echo "prebuilt payload supports install-release only" >&2; exit 2; }
  APP="$PAW_BINARY_PAYLOAD/apps/RagImeControlElectron.app"
  codesign --verify --deep --strict "$APP"
else

SOURCE_COMMIT="$(git -C "$ROOT" rev-parse HEAD)"
SOURCE_BRANCH="$(git -C "$ROOT" symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
SOURCE_DIRTY="false"
if [[ -n "$(git -C "$ROOT" status --porcelain --untracked-files=all)" ]]; then
  SOURCE_DIRTY="true"
fi
if [[ "$CHANNEL" == "release" && "$DEVELOPMENT_INSTALL" != "true" ]]; then
  if [[ "$SOURCE_BRANCH" != "main" ]]; then
    echo "refusing formal Electron release from non-main source: ${SOURCE_BRANCH:-detached HEAD}" >&2
    exit 1
  fi
fi
if [[ "$CHANNEL" == "release" \
  && "$SOURCE_DIRTY" == "true" \
  && "$DEVELOPMENT_INSTALL" != "true" \
  && ! ( "$ACTION" == "install-release" \
    && "${RAG_IME_ALLOW_DIRTY_INSTALL:-0}" == "1" ) ]]; then
  echo "refusing formal Electron release build from dirty source" >&2
  exit 1
fi

if [[ "$ACTION" == "install-release" ]]; then
  if [[ "$SOURCE_DIRTY" == "true" ]]; then
    # The legacy dirty-main opt-in passed the same guards above. Mark it just
    # as honestly as the explicit development-source path.
    DEVELOPMENT_INSTALL="true"
  fi
fi
if [[ "$DEVELOPMENT_INSTALL" == "true" ]]; then
  echo "Development build/install: explicitly selected ${SOURCE_BRANCH:-detached HEAD} source (dirty=$SOURCE_DIRTY)." >&2
fi

# Source/opt-in rejection must precede runtime hydration and build side effects.
hydrate_electron_runtime
PACKAGE_VERSION="$(cd "$ROOT" && node -p "require('./control-center-web/package.json').version")"
PRODUCT_VERSION="${RAG_IME_PRODUCT_VERSION:-$PACKAGE_VERSION}"
BUILD_NUMBER="${RAG_IME_BUILD_NUMBER:-$(git -C "$ROOT" rev-list --count "$SOURCE_COMMIT" 2>/dev/null || echo 0)}"

if [[ "$ACTION" == "install-release" ]]; then
  curl --silent --show-error --fail --max-time 5 \
    -H 'Origin: http://127.0.0.1:8766' \
    -H 'Content-Type: application/json' \
    --data '{}' \
    http://127.0.0.1:8766/api/browser/managed/stop >/dev/null 2>&1 || true
fi

RAG_IME_CONTROL_TRANSPORT=http \
RAG_IME_CONTROL_BUILD_CHANNEL="$FRONTEND_CHANNEL" \
RAG_IME_PRODUCT_VERSION="$PRODUCT_VERSION" \
RAG_IME_BUILD_COMMIT="$SOURCE_COMMIT" \
RAG_IME_BUILD_NUMBER="$BUILD_NUMBER" \
RAG_IME_SOURCE_DIRTY="$SOURCE_DIRTY" \
  "$ROOT/scripts/build_control_center_web.sh" >/dev/null
# `pnpm install --frozen-lockfile` may replace the Electron package link and
# remove its downloaded `dist/` after the preflight above. Reconcile that
# mutable package payload again at the exact copy boundary so an update cannot
# pass preflight and then fail midway through installation.
hydrate_electron_runtime
DIST_TREE_DIGEST="$(python3 - "$ROOT" "$WEB/dist" <<'PY'
import sys
from pathlib import Path

root, dist = sys.argv[1:]
sys.path.insert(0, root)
from rag_ime.release_staging import content_tree_digest

print(content_tree_digest(Path(dist), excluded_paths=("rag-ime-control-web-build.json",)))
PY
)"
"$ROOT/scripts/check_control_center_web_dist.sh" \
  "$WEB/dist" http "$FRONTEND_CHANNEL" "$SOURCE_COMMIT" "$DIST_TREE_DIGEST" >/dev/null

rm -rf "$APP"
ditto "$ELECTRON_APP" "$APP"
CONTENTS="$APP/Contents"
MACOS="$CONTENTS/MacOS"
RESOURCES="$CONTENTS/Resources"
rm -f "$RESOURCES/default_app.asar"
/usr/libexec/PlistBuddy -c 'Delete :ElectronAsarIntegrity' "$CONTENTS/Info.plist" 2>/dev/null || true
mv "$MACOS/Electron" "$MACOS/$EXECUTABLE"
/usr/libexec/PlistBuddy -c "Set :CFBundleExecutable $EXECUTABLE" "$CONTENTS/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier $BUNDLE_ID" "$CONTENTS/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleDisplayName $DISPLAY_NAME" "$CONTENTS/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleName $DISPLAY_NAME" "$CONTENTS/Info.plist"

# Electron's stock atom icon/version must never leak into the PAW product.
# Rebuild the branded resource for every host build and expose the same
# semantic version in Finder, the native host and the PAWOS shell.
RAG_IME_ICON_SOURCE="${RAG_IME_ICON_SOURCE:-$ROOT/assets/brand/paw-os-icon.png}" \
  "$ROOT/scripts/support/build_app_icon.sh" "$RESOURCES/RagImeIcon.icns"
# Electron's atom.icns is an implementation detail, not a PAW identity. Keep
# only the resource named by CFBundleIconFile so Finder cannot cache or expose
# the stock Electron artwork after a reinstall.
rm -f "$RESOURCES/electron.icns"
/usr/libexec/PlistBuddy -c 'Delete :CFBundleIconFile' "$CONTENTS/Info.plist" 2>/dev/null || true
/usr/libexec/PlistBuddy -c 'Add :CFBundleIconFile string RagImeIcon' "$CONTENTS/Info.plist" 2>/dev/null || \
  /usr/libexec/PlistBuddy -c 'Set :CFBundleIconFile RagImeIcon' "$CONTENTS/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $PRODUCT_VERSION" "$CONTENTS/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleVersion $BUILD_NUMBER" "$CONTENTS/Info.plist"

mkdir -p "$RESOURCES/app/electron" "$RESOURCES/app/dist"
ditto "$WEB/electron" "$RESOURCES/app/electron"
ditto "$WEB/dist" "$RESOURCES/app/dist"
python3 - "$RESOURCES/app/package.json" <<'PY'
import json
import sys
from pathlib import Path

Path(sys.argv[1]).write_text(json.dumps({
    "name": "personal-agent-workbench",
    "private": True,
    "type": "module",
    "main": "electron/main.mjs",
}, indent=2) + "\n", encoding="utf-8")
PY

python3 - "$RESOURCES/rag-ime-control-web-build-marker.json" "$BUNDLE_ID" "$CHANNEL" "$FRONTEND_CHANNEL" "$SOURCE_COMMIT" "$SOURCE_DIRTY" "$DIST_TREE_DIGEST" "$PRODUCT_VERSION" "$BUILD_NUMBER" "$SOURCE_BRANCH" "$DEVELOPMENT_INSTALL" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

target, bundle_id, channel, frontend_channel, commit, dirty, dist_digest, product_version, build_number, branch, development = sys.argv[1:]
provenance = {
    "sourceCommit": commit,
    "sourceDirty": dirty == "true",
    "frontendProduct": "paw-os",
    "bundleId": bundle_id,
    "frontendTransport": "http",
    "browserHost": "electron-webview",
    "browserControl": "ego-browser",
    "browserTransport": "cdp",
    "browserPartition": "persist:paw-browser",
    "sameOriginControlProxy": True,
    "distTreeDigest": dist_digest,
}
Path(target).write_text(json.dumps({
    "schemaVersion": "rag-ime.control-build-marker.v1",
    "bundleId": bundle_id,
    "gitCommit": commit,
    "gitDirty": dirty == "true",
    "sourceCommit": commit,
    "sourceDirty": dirty == "true",
    "sourceBranch": branch,
    "developmentInstall": development == "true",
    "builtAt": datetime.now(timezone.utc).isoformat(),
    "ui": "control-center-web",
    "channel": channel,
    "frontendTransport": "http",
    "frontendBuildChannel": frontend_channel,
    "forbiddenTransportModulesExcluded": True,
    "browserHost": "electron-webview",
    "browserControl": "ego-browser",
    "browserTransport": "cdp",
    "browserPartition": "persist:paw-browser",
    "sameOriginControlProxy": True,
    "frontendProduct": "paw-os",
    "productVersion": product_version,
    "buildNumber": build_number,
    "buildCommit": commit,
    "distTreeDigest": dist_digest,
    "provenance": provenance,
}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY

[[ ! -d "$RESOURCES/app/node_modules" ]] || {
  echo "node_modules must not enter the PAWOS app resources" >&2
  exit 1
}
[[ -f "$RESOURCES/app/dist/index.html" \
  && -f "$RESOURCES/app/electron/main.mjs" \
  && -f "$RESOURCES/app/electron/preload.cjs" ]] || {
  echo "PAWOS Electron host resources are incomplete" >&2
  exit 1
}
codesign --force --deep --sign - "$APP" >/dev/null
codesign --verify --deep --strict "$APP"
# Electron's archive gives the bundle directory a fixed 1980 timestamp.
# Refresh it after assembly so LaunchServices sees the new branded icon even
# when an update replaces the same app path. This changes no signed content.
touch "$APP"

fi

verify_release_provenance() {
  local candidate="$1"
  # The existing checker interprets this legacy flag as the expected actual
  # dirty value, not as permission to falsify the bundle's source identity.
  local expected_dirty="0"
  if [[ "$SOURCE_DIRTY" == "true" ]]; then
    expected_dirty="1"
  fi
  RAG_IME_CONTROL_APP="$candidate" \
  RAG_IME_CONTROL_EXPECTED_COMMIT="$SOURCE_COMMIT" \
  RAG_IME_CONTROL_SKIP_LIVE=1 \
  RAG_IME_ALLOW_DIRTY_INSTALL="$expected_dirty" \
    "$ROOT/scripts/check_control_center_footprint.sh" >/dev/null
}

if [[ "$CHANNEL" == "release" ]]; then
  verify_release_provenance "$APP"
fi

if [[ "$ACTION" == install-* ]]; then
  if [[ "$CHANNEL" == "release" ]]; then
    for existing in "$INSTALL_DEST" "$HOME/Applications/RagImeControl.app"; do
      if [[ -e "$existing" ]]; then
        existing_id="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$existing/Contents/Info.plist")"
        [[ "$existing_id" == "$BUNDLE_ID" ]] || { echo "App path belongs to another product: $existing" >&2; exit 1; }
      fi
    done
  fi
  LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
  # A hung old app must not hold the installer in Apple Events' two-minute
  # default timeout. The bounded process-exit fallback below still owns cutover.
  osascript -e 'with timeout of 5 seconds' \
    -e "tell application id \"$BUNDLE_ID\" to quit" \
    -e 'end timeout' >/dev/null 2>&1 || true
  INSTALLED_EXECUTABLE="$INSTALL_DEST/Contents/MacOS/$EXECUTABLE"
  INSTALLED_PID="$(pgrep -f "^${INSTALLED_EXECUTABLE}$" | head -n 1 || true)"
  [[ -z "$INSTALLED_PID" ]] || kill -TERM "$INSTALLED_PID" 2>/dev/null || true
  for _ in {1..25}; do
    [[ -z "$INSTALLED_PID" ]] || ! kill -0 "$INSTALLED_PID" 2>/dev/null || {
      sleep 0.2
      continue
    }
    break
  done
  [[ -z "$INSTALLED_PID" ]] || ! kill -0 "$INSTALLED_PID" 2>/dev/null || kill -KILL "$INSTALLED_PID" 2>/dev/null || true
  mkdir -p "$HOME/Applications"
  # Keep the previous bundle for recovery, including the pre-rename install.
  BACKUP_DIR="$(mktemp -d "$HOME/Applications/.paw-update.XXXXXX")"
  if [[ -e "$INSTALL_DEST" || -L "$INSTALL_DEST" ]]; then
    mv "$INSTALL_DEST" "$BACKUP_DIR/$(basename "$INSTALL_DEST")"
  fi
  ditto "$APP" "$INSTALL_DEST"
  codesign --verify --deep --strict "$INSTALL_DEST"
  if [[ "$CHANNEL" == "release" ]]; then
    verify_release_provenance "$INSTALL_DEST"
  fi
  if [[ "$CHANNEL" == "release" ]]; then
    LEGACY_DEST="$HOME/Applications/RagImeControl.app"
    if [[ -e "$LEGACY_DEST" || -L "$LEGACY_DEST" ]]; then
      mv "$LEGACY_DEST" "$BACKUP_DIR/RagImeControl.app"
    fi
    ln -s "Personal Agent Workbench.app" "$LEGACY_DEST"
    python3 "$ROOT/scripts/refresh_paw_dock.py" "$INSTALL_DEST" "$BACKUP_DIR"
  fi
  echo "Previous application retained at $BACKUP_DIR" >&2
  touch "$INSTALL_DEST"
  "$LSREGISTER" -f "$INSTALL_DEST" >/dev/null
  echo "$INSTALL_DEST"
else
  echo "$APP"
fi
