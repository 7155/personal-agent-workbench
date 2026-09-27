"""TypeSafe typed-decision transport shared by explicit online workflows."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Mapping

from .keychain_secrets import MODEL_KEYCHAIN_SERVICE, TYPESAFE_ACCOUNT, read_keychain_secret

JEV_MODEL_ID = "jev-latest"
JEV_ENDPOINT = "https://api.typesafe.ai/v1/systemone"


def api_key() -> str:
    return str(os.environ.get("TYPESAFE_API_KEY") or read_keychain_secret(
        MODEL_KEYCHAIN_SERVICE, TYPESAFE_ACCOUNT
    ) or "").strip()


def evaluate(state: str, questions: Mapping[str, object], *, key: str, timeout_seconds: float = 25) -> Mapping[str, object]:
    if not key:
        raise RuntimeError("Jev key is not configured")
    request = urllib.request.Request(
        os.environ.get("TYPESAFE_API_URL", JEV_ENDPOINT),
        data=json.dumps({"model": JEV_MODEL_ID, "state": state, "questions": questions}, ensure_ascii=False).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=max(1.0, float(timeout_seconds))) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        exc.close()
        raise RuntimeError(f"Jev HTTP {exc.code}") from None
    except urllib.error.URLError:
        raise RuntimeError("Jev endpoint unavailable") from None
    if not isinstance(payload, Mapping):
        raise ValueError("Jev response is not an object")
    return payload
