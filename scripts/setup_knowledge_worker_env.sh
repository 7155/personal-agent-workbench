#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_ROOT="${RAG_IME_KNOWLEDGE_RUNTIME_ROOT:-$HOME/Library/Application Support/RagIme/KnowledgeRuntime}"
VENV="${RAG_IME_KNOWLEDGE_VENV:-$RUNTIME_ROOT/.venv}"
PYTHON_VERSION="${RAG_IME_KNOWLEDGE_PYTHON_VERSION:-3.13}"
MODEL="${RAG_IME_EMBEDDING_MODEL:-$HOME/Library/Application Support/RagIme/Models/bge-base-zh-v1.5-mlx-q8}"
UV_BIN="${UV_BIN:-$(command -v uv || true)}"

[[ -n "$UV_BIN" ]] || { echo "uv is required" >&2; exit 1; }
mkdir -p "$RUNTIME_ROOT"
chmod 700 "$RUNTIME_ROOT"

if [[ ! -x "$VENV/bin/python" ]]; then
  "$UV_BIN" venv --python "$PYTHON_VERSION" "$VENV"
fi

REQUIREMENTS="$RUNTIME_ROOT/knowledge-worker-requirements.txt"
"$UV_BIN" export \
  --project "$ROOT" \
  --frozen \
  --no-dev \
  --no-emit-project \
  --extra embedding-mlx \
  --extra embedding-local \
  --extra knowledge-ann \
  --output-file "$REQUIREMENTS" \
  >/dev/null
chmod 600 "$REQUIREMENTS"
"$UV_BIN" pip install --python "$VENV/bin/python" --requirement "$REQUIREMENTS"

PYTHONPATH="$ROOT" \
RAG_IME_EMBEDDING_PROVIDER="local-bge-mlx" \
RAG_IME_EMBEDDING_MODEL="$MODEL" \
RAG_IME_EMBEDDING_BITS="${RAG_IME_EMBEDDING_BITS:-8}" \
RAG_IME_EMBEDDING_GROUP_SIZE="${RAG_IME_EMBEDDING_GROUP_SIZE:-32}" \
RAG_IME_EMBEDDING_LOCAL_FILES_ONLY=1 \
"$VENV/bin/python" - <<'PY'
import json

import numpy
import pypdf
import usearch
import torch
import sentence_transformers

from rag_ime.embeddings import embedding_provider_from_env

provider = embedding_provider_from_env()
vector = provider.embed_query("知识库运行时自检")
if not vector or len(vector) != 768:
    raise SystemExit(f"unexpected embedding dimensions: {len(vector)}")
print(json.dumps({
    "ok": True,
    "python": __import__("sys").executable,
    "embeddingFingerprint": provider.fingerprint,
    "dimensions": len(vector),
    "numpy": numpy.__version__,
    "pypdf": pypdf.__version__,
    "usearch": usearch.__version__,
    "torch": torch.__version__,
    "sentenceTransformers": sentence_transformers.__version__,
}, ensure_ascii=False))
PY

printf '%s\n' "$VENV/bin/python"
