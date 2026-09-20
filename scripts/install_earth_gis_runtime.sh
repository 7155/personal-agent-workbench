#!/usr/bin/env bash
set -euo pipefail

WARM_CACHE_ONLY=0
if [[ "${1:-}" == "--warm-cache-only" ]]; then WARM_CACHE_ONLY=1; shift; fi
if [[ "$#" != "0" ]]; then
  echo "usage: $0 [--warm-cache-only]" >&2
  exit 2
fi
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_SUPPORT_DIR="${RAG_IME_APP_SUPPORT_DIR:-${HOME}/Library/Application Support/RagIme}"
RUNTIME_ROOT="${APP_SUPPORT_DIR}/EarthGISRuntime"
VENV="${RUNTIME_ROOT}/.venv"
if [[ "$WARM_CACHE_ONLY" == "0" ]]; then
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

mkdir -p "$RUNTIME_ROOT"
if [[ ! -x "$VENV/bin/python" ]]; then
  uv venv --python "$PYTHON_EXECUTABLE" "$VENV"
fi
uv pip install --python "$VENV/bin/python" \
  geopandas rasterio fiona shapely pyproj pandas numpy scikit-learn 'matplotlib>=3.11' 'psycopg[binary]>=3.2,<4'
fi

if [[ ! -x "$VENV/bin/python" ]]; then
  echo "Earth GIS runtime is missing; run this installer without --warm-cache-only first." >&2
  exit 2
fi
FONT_CACHE="${RUNTIME_ROOT}/cache/matplotlib"
if [[ -L "${RUNTIME_ROOT}/cache" || -L "$FONT_CACHE" ]]; then
  echo "GIS font cache must be a regular directory." >&2
  exit 2
fi
mkdir -p -m 700 "$FONT_CACHE"
# The map uses Matplotlib's bundled DejaVu Sans. Excluding system fonts avoids
# macOS system_profiler startup work, including on a fresh runtime installation.
MPLCONFIGDIR="$FONT_CACHE" MPLBACKEND=Agg MPL_IGNORE_SYSTEM_FONTS=1 "$VENV/bin/python" - <<'PY'
import io
import matplotlib
matplotlib.use('Agg')
matplotlib.rcParams['font.family'] = 'DejaVu Sans'
import matplotlib.pyplot as plt
fig, ax = plt.subplots(figsize=(1, 1))
ax.text(0.5, 0.5, 'GIS 123', ha='center')
fig.savefig(io.BytesIO(), format='pdf')
fig.savefig(io.BytesIO(), format='svg')
plt.close(fig)
print(f"GIS renderer cache ready: {matplotlib.get_cachedir()}")
PY

if [[ "$WARM_CACHE_ONLY" == "1" ]]; then exit 0; fi
cat <<EOF
Earth GIS runtime installed.
workspace: $ROOT
python: $VENV/bin/python
packages: geopandas rasterio fiona shapely pyproj pandas numpy scikit-learn matplotlib psycopg
EOF
