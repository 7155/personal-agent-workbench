#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_SUPPORT_DIR="${RAG_IME_APP_SUPPORT_DIR:-${HOME}/Library/Application Support/RagIme}"
PYTHON_EXECUTABLE="${PAW_EARTH_GIS_PYTHON_BOOTSTRAP:-}"
if [[ -z "$PYTHON_EXECUTABLE" ]]; then
  for candidate in "${HOME}/.local/bin/python3.12" "$(command -v python3.12 2>/dev/null || true)" "$(command -v python3.13 2>/dev/null || true)"; do
    if [[ -x "$candidate" ]]; then PYTHON_EXECUTABLE="$candidate"; break; fi
  done
fi
if [[ -z "$PYTHON_EXECUTABLE" ]]; then
  echo "需要 Python 3.12 或 3.13；可用 PAW_EARTH_GIS_PYTHON_BOOTSTRAP 指定路径。" >&2
  exit 2
fi
if ! command -v uv >/dev/null 2>&1; then
  echo "需要 uv 来创建可回滚的隔离 GIS 环境。" >&2
  exit 2
fi

RUNTIME_ROOT="${APP_SUPPORT_DIR}/EarthGISRuntime"
VENV="${RUNTIME_ROOT}/.venv"
mkdir -p "$RUNTIME_ROOT"
if [[ ! -x "$VENV/bin/python" ]]; then
  uv venv --python "$PYTHON_EXECUTABLE" "$VENV"
fi
uv pip install --python "$VENV/bin/python" \
  geopandas rasterio fiona shapely pyproj pandas numpy

cat <<EOF
Earth GIS runtime installed.
workspace: $ROOT
python: $VENV/bin/python
packages: geopandas rasterio fiona shapely pyproj pandas numpy
EOF
