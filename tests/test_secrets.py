"""Block 5 (Oct 8 2026) - secrets from env or the macOS keychain: python -m tests.test_secrets

get_secret(name): the env var wins; else `security find-generic-password -a copybot -s NAME -w` (never run here:
subprocess.run is faked); a missing item or a failing command gives '' without raising; the value is not logged.
The xchain watcher reads RELAY_API_KEY through it."""
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
    print("ALL PASSED")


if __name__ == "__main__":
    main()
