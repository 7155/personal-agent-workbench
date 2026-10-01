"""Explicit Pi catalog additions for models newer than the managed bundle."""
from __future__ import annotations


def with_current_codex_models(providers):
    # Pi's normal models.json extension keeps OAuth and the Responses transport
    # owned by Pi. No credentials, endpoint override, or alternate client here.
    # Source: https://developers.openai.com/api/docs/models/gpt-6.1-sol (2026-09-30).
    result = dict(providers)
    codex = dict(result.get("openai-codex") or {})
    models = list(codex.get("models") or [])
    if not any(model.get("id") == "gpt-6.1-sol" for model in models):
        models.append({
            "id": "gpt-6.1-sol", "name": "GPT-6.1 Sol",
            "api": "openai-codex-responses", "reasoning": True,
            "input": ["text", "image"],
            # Retain the managed Codex provider's conservative context budget.
            # The public API's larger limit does not establish account access.
            "contextWindow": 272000, "maxTokens": 128000,
            "thinkingLevelMap": {"off": None, "minimal": None,
                "low": "low", "medium": "medium", "high": "high",
                "xhigh": "xhigh", "max": "max"},
            "cost": {"input": 2, "output": 10, "cacheRead": 0.1, "cacheWrite": 2.5},
        })
    codex["models"] = models
    result["openai-codex"] = codex
    return result
