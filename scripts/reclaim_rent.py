"""Close EMPTY token accounts of the bot wallet and reclaim their rent (~0.002 SOL each).

    python -m scripts.reclaim_rent                 # DRY RUN (default): lists the empty accounts and the SOL to reclaim
    python -m scripts.reclaim_rent --send          # closes them: signs with the wallet key, sends the txs, waits
    python -m scripts.reclaim_rent --send --max-per-tx 1   # one account per tx (if a batch fails on-chain)

Run it with the bot STOPPED. Key: env SOL_PRIVATE_KEY, else the macOS keychain item SOL_PRIVATE_KEY
(account copybot) - the same lookup as the bot (bot/secrets.py); the value is never printed.
Only accounts with a token balance of 0 are touched (open positions and dust keep their accounts); frozen
accounts are skipped (they cannot be closed). SPL Token and Token-2022 accounts are both handled.
The CloseAccount instruction (index 9) sends the account's lamports to the owner."""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.request

import yaml
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.transaction import Transaction

from bot.secrets import get_secret, resolve_rpc

TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
COMPUTE_BUDGET = "ComputeBudget111111111111111111111111111111"
LAMPORTS = 1_000_000_000


def rpc(url: str, method: str, params: list):
    req = urllib.request.Request(url, data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                                                       "params": params}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        j = json.loads(r.read())
    if "error" in j:
        raise RuntimeError(f"{method}: {j['error']}")
    return j["result"]


def load_config(path: str) -> dict:
    """config.yaml with chains.solana.rpc_http / rpc_ws resolved from the keychain (rpc_*_secret)."""
    cfg = yaml.safe_load(open(path))
    resolve_rpc(((cfg.get("chains") or {}).get("solana")) or {})
    return cfg


def load_keypair(sol_cfg: dict) -> Keypair:
    name = sol_cfg.get("private_key_env", "SOL_PRIVATE_KEY")
    key = get_secret(name, use_keychain=bool(sol_cfg.get("private_key_keychain", False)) or sys.platform == "darwin")
    if not key:
        raise SystemExit(f"no wallet key: export {name} or put it in the keychain (account copybot, service {name})")
    return Keypair.from_base58_string(key)


def token_accounts(url: str, owner: str) -> list[dict]:
    """All token accounts of `owner` (SPL Token + Token-2022): pubkey, mint, amount (raw), lamports, state, program."""
    rows = []
    for program in (TOKEN, TOKEN_2022):
        res = rpc(url, "getTokenAccountsByOwner", [owner, {"programId": program}, {"encoding": "jsonParsed"}])
        for v in res["value"]:
            info = v["account"]["data"]["parsed"]["info"]
            rows.append({"pubkey": v["pubkey"], "mint": info["mint"], "amount": int(info["tokenAmount"]["amount"]),
                         "lamports": int(v["account"]["lamports"]), "state": info.get("state", "initialized"),
                         "program": program})
    return rows


def empty_accounts(rows: list[dict]) -> list[dict]:
    """The accounts that can be closed: zero token balance and not frozen."""
    return [r for r in rows if r["amount"] == 0 and r["state"] != "frozen"]


def close_instructions(program: str, accounts: list[str], owner: Pubkey, priority_micro_lamports: int = 0) -> list[Instruction]:
    """CloseAccount (index 9) for each account: [account (w), destination = owner (w), owner (signer)]."""
    ixs = []
    if priority_micro_lamports > 0:    # ComputeBudget SetComputeUnitPrice (index 3, u64 micro-lamports per CU)
        ixs.append(Instruction(Pubkey.from_string(COMPUTE_BUDGET),
                               bytes([3]) + int(priority_micro_lamports).to_bytes(8, "little"), []))
    pid = Pubkey.from_string(program)
    for a in accounts:
        ixs.append(Instruction(pid, bytes([9]), [AccountMeta(Pubkey.from_string(a), is_signer=False, is_writable=True),
                                                  AccountMeta(owner, is_signer=False, is_writable=True),
                                                  AccountMeta(owner, is_signer=True, is_writable=False)]))
    return ixs


def build_tx(kp: Keypair, ixs: list[Instruction], blockhash: str) -> Transaction:
    bh = Hash.from_string(blockhash)
    return Transaction([kp], Message.new_with_blockhash(ixs, kp.pubkey(), bh), bh)


def confirm(url: str, sig: str, timeout_s: float = 60) -> str:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        st = rpc(url, "getSignatureStatuses", [[sig], {"searchTransactionHistory": False}])["value"][0]
        if st:
            if st.get("err"):
                return f"failed on-chain: {st['err']}"
            if st.get("confirmationStatus") in ("confirmed", "finalized"):
                return "ok"
        time.sleep(0.5)
    return "not confirmed in time (check the explorer)"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("--send", action="store_true", help="really close the accounts (default: dry run)")
    ap.add_argument("--max-per-tx", type=int, default=20)
    ap.add_argument("--priority-fee", type=int, default=10_000, help="micro-lamports per CU (0 = none)")
    a = ap.parse_args()
    sol = load_config(a.config)["chains"]["solana"]
    url = sol["rpc_http"]
    kp = load_keypair(sol)
    owner = kp.pubkey()
    bal = rpc(url, "getBalance", [str(owner)])["value"] / LAMPORTS
    rows = token_accounts(url, str(owner))
    empty = empty_accounts(rows)
    held = [r for r in rows if r["amount"] > 0]
    rent = sum(r["lamports"] for r in empty) / LAMPORTS
    print(f"wallet {owner}: {bal:.4f} SOL, {len(rows)} token accounts ({len(held)} with a balance, "
          f"{len(empty)} empty, {len(rows) - len(held) - len(empty)} frozen/empty skipped)")
    for r in empty:
        print(f"  close {r['pubkey']}  mint {r['mint'][:8]}...  {r['lamports'] / LAMPORTS:.6f} SOL  "
              f"{'token2022' if r['program'] == TOKEN_2022 else 'token'}")
    print(f"reclaimable rent: {rent:.4f} SOL in {len(empty)} accounts")
    if not empty:
        return
    if not a.send:
        print("DRY RUN - nothing sent. Add --send to close them.")
        return
    done = 0
    for program in (TOKEN, TOKEN_2022):
        accts = [r["pubkey"] for r in empty if r["program"] == program]
        for i in range(0, len(accts), max(1, a.max_per_tx)):
            batch = accts[i:i + max(1, a.max_per_tx)]
            bh = rpc(url, "getLatestBlockhash", [{"commitment": "finalized"}])["value"]["blockhash"]
            tx = build_tx(kp, close_instructions(program, batch, owner, a.priority_fee), bh)
            try:
                sig = rpc(url, "sendTransaction", [base64.b64encode(bytes(tx)).decode(),
                                                   {"encoding": "base64", "skipPreflight": False, "maxRetries": 3}])
            except RuntimeError as e:
                print(f"  batch of {len(batch)} REJECTED: {e}  (retry with --max-per-tx 1 to find the bad account)")
                continue
            st = confirm(url, sig)
            print(f"  {len(batch)} accounts -> {sig}: {st}")
            if st == "ok":
                done += len(batch)
    bal2 = rpc(url, "getBalance", [str(owner)])["value"] / LAMPORTS
    print(f"closed {done} of {len(empty)} accounts; balance {bal:.4f} -> {bal2:.4f} SOL")


if __name__ == "__main__":
    main()
