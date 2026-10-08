"""Block 5 (Oct 8 2026) - secrets from env or the macOS keychain: python -m tests.test_secrets

get_secret(name): the env var wins; else `security find-generic-password -a copybot -s NAME -w` (never run here:
subprocess.run is faked); a missing item or a failing command gives '' without raising; the value is not logged.
The xchain watcher reads RELAY_API_KEY through it."""
import json
import logging
import os
import subprocess
import tempfile

from bot import secrets
from bot.store import Store
from bot.xchain.watcher import XchainWatcher


class FakeRun:
    def __init__(self, items, fail=False):
        self.items, self.fail, self.calls = items, fail, []

    def __call__(self, cmd, capture_output, text, timeout):
        self.calls.append(cmd)
        if self.fail:
            raise OSError("no security binary")
        assert cmd[:3] == ["security", "find-generic-password", "-a"] and cmd[-1] == "-w", cmd
        account, service = cmd[3], cmd[5]
        v = self.items.get((account, service))
        return subprocess.CompletedProcess(cmd, 0 if v is not None else 44, stdout=(v or "") + "\n", stderr="")


def main():
    real = secrets.subprocess.run
    fake = FakeRun({("copybot", "RELAY_API_KEY"): "relay-secret-123"})
    secrets.subprocess.run = fake
    try:
        os.environ.pop("RELAY_API_KEY", None)
        # keychain hit; a logging handler must never see the value
        seen = []
        h = logging.Handler(); h.emit = lambda r: seen.append(r.getMessage())
        logging.getLogger("secrets").addHandler(h)
        logging.getLogger("secrets").setLevel(logging.INFO)
        assert secrets.get_secret("RELAY_API_KEY", use_keychain=True) == "relay-secret-123"
        assert fake.calls[-1] == ["security", "find-generic-password", "-a", "copybot", "-s", "RELAY_API_KEY", "-w"]
        assert all("relay-secret" not in m for m in seen) and any("keychain" in m for m in seen)
        # env wins, keychain not consulted
        n = len(fake.calls)
        os.environ["RELAY_API_KEY"] = "from-env"
        assert secrets.get_secret("RELAY_API_KEY", use_keychain=True) == "from-env" and len(fake.calls) == n
        os.environ.pop("RELAY_API_KEY")
        # missing item / broken command -> ''
        assert secrets.get_secret("NOPE_KEY", use_keychain=True) == ""
        secrets.subprocess.run = FakeRun({}, fail=True)
        assert secrets.get_secret("RELAY_API_KEY", use_keychain=True) == ""
        assert secrets.get_secret("RELAY_API_KEY", use_keychain=False) == ""
        # the watcher picks the key up from the keychain
        secrets.subprocess.run = fake
        w = XchainWatcher({"leaders": {}}, Store(tempfile.mktemp()))
        assert w.key == "relay-secret-123" or (secrets.sys.platform != "darwin" and w.key == "")
        logging.getLogger("secrets").removeHandler(h)
    finally:
        secrets.subprocess.run = real
    print("secrets: env first, keychain (account copybot, service = name) second, failures -> '', value never logged, "
          "watcher wired")


def test_rpc():
    """Oct 9 2026: Helius URLs (with the API key) from the keychain via chains.solana.rpc_http_secret / rpc_ws_secret."""
    from bot.chains.solana import SolanaAdapter
    from scripts.check_config import validate
    http, ws = "https://rpc.example/?api-key=SECRET-xyz", "wss://rpc.example/?api-key=SECRET-xyz"
    real = secrets.subprocess.run
    secrets.subprocess.run = FakeRun({("copybot", "HELIUS_RPC_HTTP"): http, ("copybot", "HELIUS_RPC_WS"): ws})
    seen = []
    h = logging.Handler(); h.emit = lambda r: seen.append(r.getMessage())
    for name in ("secrets", "solana"):
        logging.getLogger(name).addHandler(h); logging.getLogger(name).setLevel(logging.INFO)
    try:
        for k in ("HELIUS_RPC_HTTP", "HELIUS_RPC_WS"):
            os.environ.pop(k, None)
        sol = {"rpc_http_secret": "HELIUS_RPC_HTTP", "rpc_ws_secret": "HELIUS_RPC_WS"}
        if secrets.sys.platform == "darwin":
            assert secrets.resolve_rpc(dict(sol)) | {} == sol | {"rpc_http": http, "rpc_ws": ws}
            a = SolanaAdapter(dict(sol, leaders=[], paper_land_delay_s=0), paper=True)
            assert (a.rpc, a.ws_url) == (http, ws)
        # a plain URL in the config wins (tests / other folders keep working)
        assert secrets.resolve_rpc({"rpc_http": "http://x", "rpc_http_secret": "HELIUS_RPC_HTTP"})["rpc_http"] == "http://x"
        assert secrets.rpc_source(sol) == "secret HELIUS_RPC_HTTP" and secrets.rpc_source({}) == "config"
        # missing item -> clear error naming the item, no URL
        secrets.subprocess.run = FakeRun({})
        try:
            SolanaAdapter(dict(sol, leaders=[]), paper=True)
            raise AssertionError("adapter started without an RPC URL")
        except ValueError as e:
            assert "HELIUS_RPC_HTTP" in str(e), e
        assert all("SECRET-xyz" not in m for m in seen), seen
        # check_config: plain-text API key is a problem; a secret reference is fine; missing secret is a problem
        base = {"paper": True, "exits": {}, "risk": {}, "chains": {"solana": {"enabled": True, "leaders": ["L"],
                                                                         "buy_amount": 0.5, "daily_loss_limit": 1}}}
        def problems(sol_extra):
            cfg = json.loads(json.dumps(base)); cfg["chains"]["solana"].update(sol_extra)
            return [b for b in validate(cfg)[0] if "rpc" in b]
        assert any("plain text" in b for b in problems({"rpc_http": http, "rpc_ws": ws}))
        assert problems({"rpc_http": http, "rpc_ws": ws, "rpc_http_secret": "HELIUS_RPC_HTTP",
                         "rpc_ws_secret": "HELIUS_RPC_WS"}) == []
        assert any("HELIUS_RPC_WS" in b for b in problems({"rpc_http": "http://x", "rpc_ws_secret": "HELIUS_RPC_WS"}))
    finally:
        secrets.subprocess.run = real
        for name in ("secrets", "solana"):
            logging.getLogger(name).removeHandler(h)
    print("rpc from the keychain: resolved, plain URL wins, missing item -> clear error, never logged, check_config flags "
          "plain-text keys")


if __name__ == "__main__":
    main()
    test_rpc()
    print("ALL PASSED")
