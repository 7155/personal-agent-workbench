#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST="${1:-$ROOT/control-center-web/dist}"
EXPECTED_TRANSPORT="${2:-native}"
EXPECTED_CHANNEL="${3:-production}"
EXPECTED_COMMIT="${4:-}"
EXPECTED_DIST_DIGEST="${5:-${RAG_IME_CONTROL_EXPECTED_DIST_DIGEST:-}}"
MARKER="$DIST/rag-ime-control-web-build.json"

[[ -f "$DIST/index.html" ]] || {
  echo "missing control-center-web dist index: $DIST/index.html" >&2
  exit 1
}
[[ -f "$DIST/manifest.webmanifest" ]] || {
  echo "missing control-center-web manifest: $DIST/manifest.webmanifest" >&2
  exit 1
}
[[ -d "$DIST/assets" ]] || {
  echo "missing control-center-web assets: $DIST/assets" >&2
  exit 1
}
[[ -f "$MARKER" ]] || {
  echo "missing control-center-web build boundary marker: $MARKER" >&2
  exit 1
}

python3 - "$ROOT" "$DIST" "$MARKER" "$EXPECTED_TRANSPORT" "$EXPECTED_CHANNEL" "$EXPECTED_COMMIT" "$EXPECTED_DIST_DIGEST" <<'PY'
import json
import sys
from pathlib import Path

root, dist_path, marker_path, expected_transport, expected_channel, expected_commit, expected_dist_digest = sys.argv[1:]
sys.path.insert(0, root)
from rag_ime.release_staging import content_tree_digest

with open(marker_path, encoding="utf-8") as handle:
    marker = json.load(handle)

if marker.get("schemaVersion") != "rag-ime.control-web-build.v1":
    raise SystemExit("control-center web build marker has the wrong schema")
if marker.get("transport") != expected_transport:
    raise SystemExit(
        f"control-center transport is {marker.get('transport')!r}, expected {expected_transport!r}"
    )
if marker.get("buildChannel") != expected_channel:
    raise SystemExit(
        f"control-center channel is {marker.get('buildChannel')!r}, expected {expected_channel!r}"
    )
if expected_transport == "native":
    if marker.get("nativeOnly") is not True:
        raise SystemExit("native control-center build is not marked native-only")
    if marker.get("forbiddenTransportModulesExcluded") is not True:
        raise SystemExit("native control-center build did not exclude mock/http modules")
    if expected_channel == "production" and marker.get("previewFixturesExcluded") is not True:
        raise SystemExit("production control-center build did not exclude preview fixtures")
elif expected_transport == "http":
    if marker.get("httpOnly") is not True:
        raise SystemExit("http control-center build is not marked http-only")
    if marker.get("forbiddenTransportModulesExcluded") is not True:
        raise SystemExit("http control-center build did not exclude native/mock modules")
    if expected_channel == "production" and marker.get("previewFixturesExcluded") is not True:
        raise SystemExit("production http control-center build did not exclude preview fixtures")
if expected_commit and marker.get("sourceCommit") != expected_commit:
    raise SystemExit(
        f"control-center source commit is {marker.get('sourceCommit')!r}, expected {expected_commit!r}"
    )
dist_digest = content_tree_digest(
    Path(dist_path),
    excluded_paths=("rag-ime-control-web-build.json",),
)
if expected_transport == "http" and marker.get("frontendProduct") != "paw-os":
    raise SystemExit("http control-center build is not the PAWOS frontend")
if marker.get("distTreeDigest") != dist_digest:
    raise SystemExit("control-center dist tree digest does not match the marker")
if expected_dist_digest and dist_digest != expected_dist_digest:
    raise SystemExit(
        f"control-center dist tree digest is {dist_digest!r}, expected {expected_dist_digest!r}"
    )
print(f"distTreeDigest={dist_digest}")
PY

if [[ "$EXPECTED_TRANSPORT" == "native" ]]; then
  if grep -R -E -q \
    --include='*.html' --include='*.js' \
    'ControlTransportHttpError|No mock response registered for|mock-subscription-' \
    "$DIST"; then
    echo "native control-center dist contains a mock/http transport sentinel" >&2
    exit 1
  fi
  if [[ "$EXPECTED_CHANNEL" == "production" ]] && grep -R -E -q \
    --include='*.html' --include='*.js' \
    'session-preview|room-preview|迁移作战室|control-center-fixture\.json' \
    "$DIST"; then
    echo "production control-center dist contains a preview fixture sentinel" >&2
    exit 1
  fi
  grep -q "connect-src 'self' http://127.0.0.1:18875;" "$DIST/index.html" || {
    echo "native control-center CSP differs from approved workspace probe origin" >&2
    exit 1
  }
elif [[ "$EXPECTED_TRANSPORT" == "http" ]]; then
  if grep -R -E -q \
    --include='*.html' --include='*.js' \
    'NativeBridgeUnavailableError|No mock response registered for|mock-subscription-|http://127\.0\.0\.1:8766' \
    "$DIST"; then
    echo "http control-center dist contains a native/mock/loopback sentinel" >&2
    exit 1
  fi
  if [[ "$EXPECTED_CHANNEL" == "production" ]] && grep -R -E -q \
    --include='*.html' --include='*.js' \
    'session-preview|room-preview|迁移作战室|control-center-fixture\.json' \
    "$DIST"; then
    echo "production http control-center dist contains a preview fixture sentinel" >&2
    exit 1
  fi
  grep -q "connect-src 'self' http://127.0.0.1:18875;" "$DIST/index.html" || {
    echo "http control-center CSP differs from approved workspace probe origin" >&2
    exit 1
  }
fi

echo "control center web dist boundary: PASS ($EXPECTED_CHANNEL/$EXPECTED_TRANSPORT)"
