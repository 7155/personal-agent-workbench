#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WEB="$ROOT/control-center-web"
source "$ROOT/scripts/support/prebuilt_product.sh"
if paw_prebuilt_identity; then
  "$ROOT/scripts/check_control_center_web_dist.sh" "$WEB/dist" http production "$SOURCE_COMMIT"
  echo "$WEB/dist"
  exit 0
fi
CONTROL_TRANSPORT="${RAG_IME_CONTROL_TRANSPORT:-mock}"
BUILD_CHANNEL="${RAG_IME_CONTROL_BUILD_CHANNEL:-preview}"
FRONTEND_PRODUCT=paw-os

[[ "$CONTROL_TRANSPORT" == "mock" || "$CONTROL_TRANSPORT" == "http" || "$CONTROL_TRANSPORT" == "native" ]] || {
  echo "RAG_IME_CONTROL_TRANSPORT must be mock, http, or native" >&2
  exit 2
}
[[ "$BUILD_CHANNEL" == "preview" || "$BUILD_CHANNEL" == "production" ]] || {
  echo "RAG_IME_CONTROL_BUILD_CHANNEL must be preview or production" >&2
  exit 2
}
if [[ "$BUILD_CHANNEL" == "production" \
  && "$CONTROL_TRANSPORT" != "native" \
  && "$CONTROL_TRANSPORT" != "http" ]]; then
  echo "production control-center builds require native or http transport" >&2
  exit 2
fi

if [[ "${RAG_IME_SKIP_WEB_INSTALL:-0}" != "1" ]]; then
  (
    cd "$WEB"
    CI=true pnpm --config.manage-package-manager-versions=true \
      install --frozen-lockfile
  )
fi

node "$ROOT/scripts/generate_control_center_contracts.mjs" --check
(
  cd "$WEB"
  pnpm --config.manage-package-manager-versions=true typecheck
  if [[ "${RAG_IME_SKIP_WEB_TESTS:-0}" != "1" ]]; then
    pnpm --config.manage-package-manager-versions=true test
  else
    echo "Skipping full web tests by explicit RAG_IME_SKIP_WEB_TESTS=1" >&2
  fi
  VITE_CONTROL_TRANSPORT="$CONTROL_TRANSPORT" \
  VITE_BUILD_CHANNEL="$BUILD_CHANNEL" \
  VITE_PAW_PRODUCT_VERSION="${RAG_IME_PRODUCT_VERSION:-$(node -p "require('./package.json').version")}" \
  VITE_PAW_BUILD_COMMIT="${RAG_IME_BUILD_COMMIT:-$(git -C "$ROOT" rev-parse HEAD)}" \
  VITE_PAW_BUILD_NUMBER="${RAG_IME_BUILD_NUMBER:-$(git -C "$ROOT" rev-list --count HEAD 2>/dev/null || echo 0)}" \
  VITE_PAW_SOURCE_DIRTY="${RAG_IME_SOURCE_DIRTY:-false}" \
    pnpm --config.manage-package-manager-versions=true build
)

[[ -f "$WEB/dist/index.html" ]] || {
  echo "missing control-center-web/dist/index.html" >&2
  exit 1
}
[[ -f "$WEB/dist/manifest.webmanifest" ]] || {
  echo "missing control-center-web/dist/manifest.webmanifest" >&2
  exit 1
}
if grep -R -E -q 'unsafe-eval|new Function|require\("|eval\(' "$WEB/dist"; then
  echo "control-center bundle contains runtime code generation or unresolved CommonJS" >&2
  exit 1
fi
python3 - "$ROOT" "$WEB/dist/rag-ime-control-web-build.json" "$WEB/dist" "$FRONTEND_PRODUCT" <<'PY'
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from rag_ime.release_staging import control_center_dist_digest

marker_path = Path(sys.argv[2])
dist_path = Path(sys.argv[3])
frontend_product = sys.argv[4]


try:
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as error:
    raise SystemExit(f"control-center web build marker is unreadable: {error}") from error

if marker.get("schemaVersion") != "rag-ime.control-web-build.v1":
    raise SystemExit("control-center web build marker has the wrong schema")
marker["frontendProduct"] = frontend_product
marker["distTreeDigest"] = control_center_dist_digest(dist_path)
temporary = marker_path.with_name(f".{marker_path.name}.tmp")
temporary.write_text(
    json.dumps(marker, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
os.replace(temporary, marker_path)
PY
"$ROOT/scripts/check_control_center_web_dist.sh" \
  "$WEB/dist" "$CONTROL_TRANSPORT" "$BUILD_CHANNEL" >/dev/null

echo "$WEB/dist"
