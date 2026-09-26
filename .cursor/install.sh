#!/usr/bin/env bash
# Idempotent Cloud Agent bootstrap for Personal Agent Workbench.
#
# Layers:
#   1. uv (pinned to the version CI uses and the committed uv.lock was made with)
#   2. Python runtime dependencies from the committed lockfile
#   3. Node/pnpm dependencies for the React Control Center
#   4. The same-origin Control Center bundle the Agent Gateway serves
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# --- 1. uv toolchain -------------------------------------------------------
# CI installs uv==0.11.1 and the committed uv.lock was resolved with it. Newer
# uv normalises platform markers differently, which would report the lock as
# out of date, so pin the same version here.
UV_VERSION="0.11.1"
export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1 \
  || [ "$(uv --version 2>/dev/null | awk '{print $2}')" != "$UV_VERSION" ]; then
  curl -LsSf "https://astral.sh/uv/${UV_VERSION}/install.sh" | sh
fi
export PATH="$HOME/.local/bin:$PATH"
uv --version

# --- 2. Python dependencies ------------------------------------------------
# --frozen installs exactly what uv.lock pins without re-resolving or rewriting
# it. The committed lock omits the optional `tui`/textual extra declared in
# pyproject.toml, so `uv sync --locked` would fail on a lock/pyproject drift
# that is a pre-existing repository state, not an environment problem. The
# optional TUI client is not needed for the core runtime, Control Center, or
# tests.
uv sync --frozen --python 3.12

# --- 3. Control Center (React) dependencies --------------------------------
corepack enable
corepack prepare pnpm@11.9.0 --activate
pnpm --dir control-center-web install --frozen-lockfile

# --- 4. Same-origin Control Center bundle ----------------------------------
# The Agent Gateway serves this http/production bundle from control-center-web/
# dist (git-ignored). Building it here keeps the gateway able to serve the full
# UI on first boot.
VITE_CONTROL_TRANSPORT=http VITE_BUILD_CHANNEL=production \
  pnpm --dir control-center-web build

echo "PAW Cloud Agent environment ready."
