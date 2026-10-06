"""Sell open positions by hand (e.g. before a shutdown), and record them properly.

STOP THE BOT FIRST (Ctrl+C), otherwise the bot and this script may both try to sell.

Usage (from the bot folder):
    SOL_PRIVATE_KEY="$(security find-generic-password -a copybot -s SOL_PRIVATE_KEY -w)" python close_all.py
    ... python close_all.py --id 42        # only position 42
Paper mode (paper: true in config) simulates the sales instead.

It lists the open positions with their current value, asks you to type YES, then sells each one
via Jupiter (slippage 30%, then 50%, then 90% if a sale fails) and marks it closed with
exit_reason 'manual_close' - so the bot won't try to resume it on the next start.
"""
import argparse
import asyncio
import logging
import time

import aiohttp
import yaml

from bot.chains.solana import SolanaAdapter, WSOL, LAMPORTS
from bot.store import Store


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("--id", type=int, default=None, help="only this position id")
    ap.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    args = ap.parse_args()
    cfg = yaml.safe_load(open(args.config))
    paper = bool(cfg.get("paper", True))
    store = Store(cfg.get("db_path", "copybot.db"))
    positions = [p for p in store.load_open(paper) if p.chain == "solana" and (args.id is None or p.id == args.id)]
    if not positions:
        print("No open positions" + (f" with id {args.id}" if args.id else "") + ". Nothing to do.")
        return
    a = SolanaAdapter(cfg["chains"]["solana"], paper)
    a.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
    try:
        print(f"{'PAPER' if paper else 'LIVE'} - wallet {a.kp.pubkey() if a.kp else '(none)'}\n")
        print(f"{'id':>4}  {'token':<12} {'cost':>8}  {'value now':>10}  {'result':>8}")
        for p in positions:
            try:
                q = await a._quote(p.token, WSOL, p.token_amount, priority=True)
                v = int(q["outAmount"]) / LAMPORTS
                print(f"{p.id:>4}  {p.token[:10]:<12} {p.cost:>8.4f}  {v:>10.4f}  {v / p.cost - 1:>+8.1%}")
            except Exception as e:
                print(f"{p.id:>4}  {p.token[:10]:<12} {p.cost:>8.4f}  {'no quote':>10}  ({e})")
        if not args.yes and input(f"\nSell {len(positions)} position(s) now? Type YES: ").strip() != "YES":
            print("Cancelled. Nothing sold.")
            return
        for p in positions:
            for slip in (3000, 5000, 9000):
                fill = await a.sell(p, slip)
                if fill.ok:
                    p.status, p.exit_value, p.closed_at = "closed", fill.quote_amount, time.time()
                    p.exit_reason, p.exit_sig = "manual_close", fill.signature
                    p.timing.update(exit_filled_at=p.closed_at, exit_model=fill.model or "manual")
                    store.close_position(p)
                    print(f"SOLD {p.id} {p.token[:10]}: {fill.quote_amount:.4f} SOL ({fill.quote_amount / p.cost - 1:+.1%})"
                          f"  {fill.signature or ''}")
                    break
                print(f"  sale of {p.id} failed at {slip / 100:.0f}% slippage: {fill.error}")
            else:
                print(f"COULD NOT SELL {p.id} {p.token}. Still open in the bot. Try again, or sell it in Phantom "
                      f"and then run:  sqlite3 copybot.db \"UPDATE positions SET status='closed', "
                      f"exit_reason='manual_close', closed_at={int(time.time())} WHERE id={p.id};\"")
    finally:
        await a.session.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(main())
