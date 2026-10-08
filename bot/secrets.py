"""Secrets: environment first, then the macOS Keychain (same convention as SOL_PRIVATE_KEY):

    security find-generic-password -a copybot -s <NAME> -w

The value is never logged. Add a key once with
    security add-generic-password -a copybot -s RELAY_API_KEY -w '<key>' -U
Items in use: SOL_PRIVATE_KEY (live), RELAY_API_KEY, HELIUS_RPC_HTTP / HELIUS_RPC_WS (Helius URLs incl. the API key,
referenced by chains.solana.rpc_http_secret / rpc_ws_secret, see resolve_rpc).
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


RPC_KEYS = (("rpc_http", "rpc_http_secret"), ("rpc_ws", "rpc_ws_secret"))


def resolve_rpc(sol: dict) -> dict:
    """chains.solana: fill rpc_http / rpc_ws from the secrets named in rpc_http_secret / rpc_ws_secret (env, else
    the keychain; e.g. HELIUS_RPC_HTTP / HELIUS_RPC_WS) when the plain URL is not in the config. The URLs carry
    the Helius API key, so they are never logged. Mutates and returns `sol`; a plain URL in the config wins."""
    for key, ref in RPC_KEYS:
        if not sol.get(key) and sol.get(ref):
            sol[key] = get_secret(sol[ref])
    return sol


def rpc_source(sol: dict, key: str = "rpc_http") -> str:
    """Where chains.solana.<key> comes from, for start-up lines: 'config' or 'secret NAME' (no value)."""
    ref = sol.get(f"{key}_secret")
    return f"secret {ref}" if ref else "config"
