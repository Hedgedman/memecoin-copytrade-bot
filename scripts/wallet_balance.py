"""Wallet balance of the bot wallet (read-only): python -m scripts.wallet_balance [-c config.yaml] [--pubkey <address>]

Prints the address, the SOL balance, the token accounts (held / empty, rent locked) and the open positions
in the DB. Without --pubkey the address comes from the wallet key (env SOL_PRIVATE_KEY or the keychain, as the
bot); the key itself is never printed."""
from __future__ import annotations

import argparse
import sqlite3

from scripts.reclaim_rent import LAMPORTS, empty_accounts, load_config, load_keypair, rpc, token_accounts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("--pubkey", help="wallet address (skips the key lookup)")
    a = ap.parse_args()
    cfg = load_config(a.config)
    sol = cfg["chains"]["solana"]
    owner = a.pubkey or str(load_keypair(sol).pubkey())
    url = sol["rpc_http"]
    bal = rpc(url, "getBalance", [owner])["value"] / LAMPORTS
    rows = token_accounts(url, owner)
    empty = empty_accounts(rows)
    held = [r for r in rows if r["amount"] > 0]
    print(f"wallet {owner}")
    print(f"  SOL: {bal:.4f} (buy_amount {sol.get('buy_amount')} x max_open_positions {cfg.get('risk', {}).get('max_open_positions')} "
          f"= {float(sol.get('buy_amount', 0)) * int(cfg.get('risk', {}).get('max_open_positions', 0)):.1f} SOL needed + fees)")
    print(f"  token accounts: {len(rows)} ({len(held)} with a balance, {len(empty)} empty = "
          f"{sum(r['lamports'] for r in empty) / LAMPORTS:.4f} SOL rent reclaimable: python -m scripts.reclaim_rent)")
    for r in held:
        print(f"    held {r['mint']}: {r['amount']} raw")
    try:
        db = sqlite3.connect(f"file:{cfg.get('db_path', 'copybot.db')}?mode=ro", uri=True)
        n_open = db.execute("SELECT COUNT(*) FROM positions WHERE status!='closed' AND paper=0").fetchone()[0]
        print(f"  open LIVE positions in {cfg.get('db_path', 'copybot.db')}: {n_open}")
    except sqlite3.Error as e:
        print(f"  db: {e}")


if __name__ == "__main__":
    main()
