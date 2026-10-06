#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP="${RAG_IME_CONTROL_APP:-$ROOT/build/RagImeControlElectron.app}"
BINARY="$APP/Contents/MacOS/RagImeControl"
FOOTPRINT_LIMIT_MB="${RAG_IME_CONTROL_FOOTPRINT_LIMIT_MB:-250}"
RSS_LIMIT_MB="${RAG_IME_CONTROL_RSS_LIMIT_MB:-350}"
IDLE_CPU_LIMIT="${RAG_IME_CONTROL_IDLE_CPU_LIMIT:-0.5}"
if [[ -n "${RAG_IME_CONTROL_EXPECTED_COMMIT:-}" ]]; then
  EXPECTED_COMMIT="$RAG_IME_CONTROL_EXPECTED_COMMIT"
else
  EXPECTED_COMMIT="$(git -C "$ROOT" rev-parse HEAD)"
fi
pid=""

cleanup() {
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    osascript -e 'tell application id "com.rag-ime.control" to quit' >/dev/null 2>&1 || kill "$pid"
    for _ in $(seq 1 20); do
      kill -0 "$pid" 2>/dev/null || return 0
      sleep 0.1
    done
    kill "$pid" 2>/dev/null || true
  fi
  return 0
}
trap cleanup EXIT

[[ -x "$BINARY" ]] || "$ROOT/scripts/build_control_center.sh" >/dev/null

WEB_RESOURCES="$APP/Contents/Resources/app/dist"
MARKER="$APP/Contents/Resources/rag-ime-control-web-build-marker.json"
[[ -d "$APP/Contents/Frameworks/Electron Framework.framework" ]]
otool -L "$BINARY" | grep -q 'Electron Framework.framework'
[[ -f "$WEB_RESOURCES/index.html" ]]
[[ -f "$WEB_RESOURCES/manifest.webmanifest" ]]
[[ -d "$WEB_RESOURCES/assets" ]]
[[ -f "$MARKER" ]]
[[ ! -d "$WEB_RESOURCES/node_modules" ]]
grep -q 'Content-Security-Policy' "$WEB_RESOURCES/index.html"
! grep -R -E -q 'unsafe-eval|new Function|require\("|eval\(' "$WEB_RESOURCES"
"$ROOT/scripts/check_control_center_web_dist.sh" \
  "$WEB_RESOURCES" http production "$EXPECTED_COMMIT" >/dev/null
python3 - "$ROOT" "$WEB_RESOURCES" "$MARKER" "$EXPECTED_COMMIT" "${RAG_IME_ALLOW_DIRTY_INSTALL:-0}" <<'PY'
import json
import sys
from pathlib import Path

root, dist_path, marker_path, expected_commit, allow_dirty = sys.argv[1:]
expected_dirty = allow_dirty == "1"
sys.path.insert(0, root)
from rag_ime.release_staging import control_center_dist_digest

with open(marker_path, encoding="utf-8") as handle:
    marker = json.load(handle)
if marker.get("bundleId") != "com.rag-ime.control":
    raise SystemExit("web control bundle marker has the wrong bundle id")
if marker.get("sourceCommit") != expected_commit:
    raise SystemExit("web control bundle marker has the wrong source commit")
if marker.get("sourceDirty") is not expected_dirty or marker.get("gitDirty") is not expected_dirty:
    raise SystemExit("web control bundle marker dirty state does not match the requested install mode")
if marker.get("ui") != "control-center-web" or marker.get("channel") != "release":
    raise SystemExit("web control bundle marker is not a release build")
if marker.get("frontendProduct") != "paw-os":
    raise SystemExit("web control bundle marker is not the PAW OS frontend")
if marker.get("frontendTransport") != "http":
    raise SystemExit("web control bundle marker is not the Electron HTTP frontend")
if marker.get("browserHost") != "electron-webview":
    raise SystemExit("web control bundle marker is not the Electron Browser host")
if marker.get("browserControl") != "ego-browser" or marker.get("browserTransport") != "cdp":
    raise SystemExit("web control bundle marker is not the Ego/CDP Browser authority")
if "swiftFallback" in marker:
    raise SystemExit("web control bundle marker must not advertise a Swift fallback")
if marker.get("frontendBuildChannel") != "production":
    raise SystemExit("web control bundle marker is not a production frontend build")
if marker.get("forbiddenTransportModulesExcluded") is not True:
    raise SystemExit("web control bundle marker did not exclude native/mock transport modules")
if marker.get("browserPartition") != "persist:paw-browser" or marker.get("sameOriginControlProxy") is not True:
    raise SystemExit("web control bundle marker has an invalid Browser session boundary")
if marker.get("provenance") != {
    "sourceCommit": expected_commit,
    "sourceDirty": expected_dirty,
    "frontendProduct": "paw-os",
    "bundleId": "com.rag-ime.control",
    "frontendTransport": "http",
    "browserHost": "electron-webview",
    "browserControl": "ego-browser",
    "browserTransport": "cdp",
    "browserPartition": "persist:paw-browser",
    "sameOriginControlProxy": True,
    "distTreeDigest": marker.get("distTreeDigest"),
}:
    raise SystemExit("web control bundle marker provenance is incomplete or inconsistent")
expected_digest = marker.get("distTreeDigest")
actual_digest = control_center_dist_digest(Path(dist_path))
if not isinstance(expected_digest, str) or expected_digest != actual_digest:
    raise SystemExit("web control bundle marker dist tree digest does not match its content")
print(f"distTreeDigest={actual_digest}")
PY

if [[ "${RAG_IME_CONTROL_SKIP_LIVE:-0}" == "1" ]]; then
  echo "control center web static footprint gate: PASS"
  exit 0
fi

existing_pid="$(pgrep -f "$BINARY" | head -1 || true)"
if [[ -n "$existing_pid" ]]; then
  kill "$existing_pid"
  for _ in $(seq 1 30); do
    kill -0 "$existing_pid" 2>/dev/null || break
    sleep 0.1
  done
fi

open "$APP"
for _ in $(seq 1 50); do
  pid="$(pgrep -f "$BINARY" | head -1 || true)"
  [[ -n "$pid" ]] && break
  sleep 0.1
done
[[ -n "${pid:-}" ]] || { echo "control center did not launch" >&2; exit 1; }
sleep 30
rss_kb="$(ps -o rss= -p "$pid" | tr -d ' ')"
cpu="$(ps -o %cpu= -p "$pid" | tr -d ' ')"
physical="$(vmmap -summary "$pid" 2>/dev/null | awk '/^Physical footprint:/ {print $3; exit}')"
python3 - "$rss_kb" "$RSS_LIMIT_MB" "$physical" "$FOOTPRINT_LIMIT_MB" "$cpu" "$IDLE_CPU_LIMIT" <<'PY'
import sys
rss_mb = int(sys.argv[1]) / 1024
rss_limit = float(sys.argv[2])
physical_raw = sys.argv[3].strip().upper()
physical_limit = float(sys.argv[4])
cpu = float(sys.argv[5])
cpu_limit = float(sys.argv[6])
scale = 1024 if physical_raw.endswith("G") else (1 / 1024 if physical_raw.endswith("K") else 1)
physical_mb = float(physical_raw[:-1]) * scale
if rss_mb > rss_limit:
    raise SystemExit(f"RSS {rss_mb:.1f} MB exceeds {rss_limit:.1f} MB")
if physical_mb > physical_limit:
    raise SystemExit(f"physical footprint {physical_mb:.1f} MB exceeds {physical_limit:.1f} MB")
if cpu > cpu_limit:
    raise SystemExit(f"idle CPU {cpu:.2f}% exceeds {cpu_limit:.2f}%")
print(f"physicalFootprint={physical_mb:.1f}MB RSS={rss_mb:.1f}MB idleCPU={cpu:.2f}%")
PY
cleanup
pid=""
echo "control center web live footprint gate: PASS"
