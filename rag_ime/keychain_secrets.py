from __future__ import annotations

import shutil
import subprocess


MODEL_KEYCHAIN_SERVICE = "com.rag-ime.model-provider"
MODEL_KNOWLEDGE_ACCOUNT = "knowledge-api-key"
MODEL_INSTANT_ACCOUNT = "instant-api-key"
TYPESAFE_ACCOUNT = "typesafe-api-key"


def read_keychain_secret(service: str, account: str) -> str:
    security = shutil.which("security")
    if not security or not service or not account:
        return ""
    try:
        result = subprocess.run(
            [security, "find-generic-password", "-s", service, "-a", account, "-w"],
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        # A locked or busy login keychain must not prevent the local service
        # from starting. The caller treats an empty value as not configured.
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def write_keychain_secret(service: str, account: str, value: str) -> None:
    security = shutil.which("security")
    if not security:
        raise RuntimeError("macOS security command is unavailable")
    secret = str(value).strip()
    if not secret:
        return
    result = subprocess.run(
        [security, "add-generic-password", "-U", "-s", service, "-a", account, "-w", secret],
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "failed to write macOS Keychain")
