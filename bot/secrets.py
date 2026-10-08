"""Secrets: environment first, then the macOS Keychain (same convention as SOL_PRIVATE_KEY):

    security find-generic-password -a copybot -s <NAME> -w

The value is never logged. Add a key once with
    security add-generic-password -a copybot -s RELAY_API_KEY -w '<key>' -U
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys

log = logging.getLogger("secrets")

KEYCHAIN_ACCOUNT = "copybot"


def get_secret(name: str, account: str = KEYCHAIN_ACCOUNT, use_keychain: bool | None = None) -> str:
    """The env var `name`, else the Keychain item (service = name, account = `account`) on macOS, else ''."""
    v = os.environ.get(name, "")
    if v:
        return v
    if use_keychain is None:
        use_keychain = sys.platform == "darwin"
    if not use_keychain:
        return ""
    try:
        r = subprocess.run(["security", "find-generic-password", "-a", account, "-s", name, "-w"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("%s: keychain lookup failed (%s)", name, type(e).__name__)
        return ""
    if r.returncode != 0:
        log.info("%s: not in the environment and not in the keychain (account %s)", name, account)
        return ""
    v = r.stdout.strip()
    if v:
        log.info("%s loaded from the keychain (account %s)", name, account)
    return v
