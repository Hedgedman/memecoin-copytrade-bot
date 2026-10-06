"""Inspect what a wallet's recent transactions really are (read-only, no keys needed).

Usage (from the copybot folder):
    python -m scripts.inspect_wallet <wallet> [--limit 300] [--cache data/txcache] [--refresh]

Fetches the last N signatures via the Helius RPC in config.yaml (getSignaturesForAddress +
getTransaction jsonParsed), caches every tx as JSON under --cache, and classifies each one
from the wallet's point of view:

  a_signer      he signed it (what the bot can copy today)
  b_unsigned    his OWN balances change on both sides of a swap (token in + USDC/SOL out or
                the reverse) but he did not sign (session signer / smart wallet / delegate)
  c_fee_share   he only receives a small USDC/USDT/SOL amount (fee share / referral)
  d_relay       Relay deposit (cross-chain buy) or Relay solver transfer (cross-chain sell)
  e_other       anything else

For each tx it also records whether his pubkey is an ACCOUNT KEY (static or via a lookup
table). The bot's transactionSubscribe uses accountInclude=[leader], so txs where he only
appears as the owner of a token account are never delivered to us.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone

import aiohttp
import yaml

from bot.chains.solana import (USDC, USDT, WSOL, LAMPORTS, parse_stable_swap, parse_wallet_swap,
                               venues_in_tx)

FOMO_COSIGNER = "AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51"
RELAY_DEPOSITORY = "99vQwtBwYtrqqD9YSXbdum3KBdxPAVxYTaQ3cfnJSrN2"
RELAY_SOLVER = "F7p3dFrjRTbtRp8FRF6qHLomXbKRBzpvBLjtQcfcgmNe"
STABLES = {USDC: "USDC", USDT: "USDT"}
FEE_SHARE_MAX_RATIO = 0.05      # he gets <= 5% of the biggest stable/SOL flow in the tx


def _keys(tx: dict) -> list[dict]:
    out = []
    for k in tx["transaction"]["message"].get("accountKeys", []):
        out.append(k if isinstance(k, dict) else {"pubkey": k, "signer": False, "source": "transaction"})
    return out


def owner_deltas(tx: dict) -> dict[str, dict[str, int]]:
    """{owner: {mint: raw delta}} from pre/post token balances. SOL lamports under 'SOL'."""
    meta = tx["meta"]
    d: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for balances, sign in ((meta.get("preTokenBalances") or [], -1), (meta.get("postTokenBalances") or [], 1)):
        for b in balances:
            if b.get("owner"):
                d[b["owner"]][b["mint"]] += sign * int(b["uiTokenAmount"]["amount"])
    for i, k in enumerate(_keys(tx)):
        lam = meta["postBalances"][i] - meta["preBalances"][i]
        if lam:
            d[k["pubkey"]]["SOL"] += lam
    return {o: {m: v for m, v in ms.items() if v} for o, ms in d.items()}


def _usd_like(mint: str, raw: int) -> float:
    """Stable amounts in USD, SOL/WSOL in SOL (only used for size ratios inside one tx)."""
    if mint in STABLES:
        return raw / 1e6
    if mint in ("SOL", WSOL):
        return raw / LAMPORTS
    return 0.0


def is_two_sided(ms: dict[str, int]) -> tuple[str, str, int, float, str] | None:
    """(side, token, token_raw, quote_amount, quote) if these deltas look like one swap."""
    quote_sol = (ms.get("SOL", 0) + ms.get(WSOL, 0)) / LAMPORTS
    quote_usd = (ms.get(USDC, 0) + ms.get(USDT, 0)) / 1e6
    tokens = {m: v for m, v in ms.items() if m not in ("SOL", WSOL) and m not in STABLES}
    if len(tokens) != 1:
        return None
    mint, v = next(iter(tokens.items()))
    for q, name, thr in ((quote_usd, "USD", 0.5), (quote_sol, "SOL", 0.002)):
        if v > 0 and q < -thr:
            return ("buy", mint, v, -q, name)
        if v < 0 and q > thr:
            return ("sell", mint, -v, q, name)
    return None


def classify(tx: dict, wallet: str) -> dict:
    keys = _keys(tx)
    key_map = {k["pubkey"]: k for k in keys}
    me = key_map.get(wallet)
    signers = [k["pubkey"] for k in keys if k.get("signer")]
    deltas = owner_deltas(tx)
    mine = deltas.get(wallet, {})
    programs = set()
    for ix in tx["transaction"]["message"].get("instructions", []) or []:
        programs.add(ix.get("programId"))
    for inner in tx["meta"].get("innerInstructions") or []:
        for ix in inner.get("instructions", []) or []:
            programs.add(ix.get("programId"))

    r = {
        "sig": tx["transaction"]["signatures"][0],
        "time": tx.get("blockTime"),
        "slot": tx.get("slot"),
        "err": tx["meta"].get("err") is not None,
        "version": tx.get("version", "legacy"),
        "signer": bool(me and me.get("signer")),
        "in_keys": me is not None,
        "key_source": (me or {}).get("source", "transaction") if me else None,
        "signers": signers,
        "other_signers": [s for s in signers if s not in (wallet, FOMO_COSIGNER)],
        "cosigned_by_fomo": FOMO_COSIGNER in signers,
        "my_deltas": mine,
        "venues": venues_in_tx(tx),
        "swap": None,
        "swappers": {},          # other owners whose balances show a two-sided swap
    }
    for o, ms in deltas.items():
        if o != wallet:
            s = is_two_sided(ms)
            if s:
                r["swappers"][o] = s

    relay_solver_out = any(v < 0 for v in deltas.get(RELAY_SOLVER, {}).values())
    if RELAY_DEPOSITORY in programs or relay_solver_out or RELAY_SOLVER in key_map:
        r["cls"] = "d_relay"
        r["relay"] = "deposit" if RELAY_DEPOSITORY in programs else "solver_transfer"
        return r
    if r["err"]:
        r["cls"] = "e_other"
        return r
    swap = is_two_sided(mine)
    if swap:
        r["swap"] = swap
    if r["signer"]:
        r["cls"] = "a_signer"
        r["bot_parses"] = bool(parse_wallet_swap(tx, wallet) or parse_stable_swap(tx, wallet))
        return r
    if swap:
        r["cls"] = "b_unsigned"
        return r
    received = {m: v for m, v in mine.items() if v > 0}
    sent = {m: v for m, v in mine.items() if v < 0}
    if received and not sent and all(m in STABLES or m in ("SOL", WSOL) for m in received):
        flows = [abs(_usd_like(m, v)) for ms in deltas.values() for m, v in ms.items()
                 if (m in STABLES) == (next(iter(received)) in STABLES)]
        got = sum(_usd_like(m, v) for m, v in received.items())
        biggest = max(flows) if flows else 0
        r["fee_ratio"] = got / biggest if biggest else None
        if biggest and got / biggest <= FEE_SHARE_MAX_RATIO:
            r["cls"] = "c_fee_share"
            return r
    r["cls"] = "e_other"
    return r


# ---------------- fetching ----------------
async def rpc(session, url, method, params, tries=6):
    for i in range(tries):
        try:
            async with session.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}) as resp:
                if resp.status == 429:
                    await asyncio.sleep(1.5 * (i + 1)); continue
                j = await resp.json(content_type=None)
            if "error" in j:
                raise RuntimeError(f"{method}: {j['error']}")
            return j["result"]
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            if i == tries - 1:   # never re-raise as-is: aiohttp messages contain the URL (= API key)
                raise RuntimeError(f"{method}: {type(e).__name__}") from None
            await asyncio.sleep(1.0 * (i + 1))
    raise RuntimeError(f"{method}: still rate-limited")


async def fetch(url: str, wallet: str, limit: int, cache: str, refresh: bool) -> list[dict]:
    os.makedirs(cache, exist_ok=True)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
        sigs, before = [], None
        while len(sigs) < limit:
            p = {"limit": min(1000, limit - len(sigs))}
            if before:
                p["before"] = before
            page = await rpc(s, url, "getSignaturesForAddress", [wallet, p])
            if not page:
                break
            sigs += page
            before = page[-1]["signature"]
        sem = asyncio.Semaphore(8)

        async def one(sig):
            path = os.path.join(cache, sig + ".json")
            if os.path.exists(path) and not refresh:
                with open(path) as f:
                    return json.load(f)
            async with sem:
                tx = await rpc(s, url, "getTransaction", [sig, {"encoding": "jsonParsed",
                                                                "maxSupportedTransactionVersion": 1,
                                                                "commitment": "confirmed"}])
            if tx:
                with open(path, "w") as f:
                    json.dump(tx, f)
            return tx

        txs = await asyncio.gather(*(one(x["signature"]) for x in sigs))
        holdings = await rpc(s, url, "getTokenAccountsByOwner",
                             [wallet, {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
                              {"encoding": "jsonParsed"}])
    return [t for t in txs if t], holdings


def _t(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%m-%d %H:%M:%S") if ts else "?"


def _short(x, n=8):
    return x[:n] if x else "-"


def report(rows: list[dict], wallet: str, holdings=None):
    print(f"\nwallet {wallet}: {len(rows)} transactions "
          f"({_t(min(r['time'] or 0 for r in rows))} .. {_t(max(r['time'] or 0 for r in rows))} UTC)\n")
    by = defaultdict(list)
    for r in rows:
        by[r["cls"]].append(r)
    print("class            n   signed  pubkey-in-keys  fomo-cosigned")
    for c in ("a_signer", "b_unsigned", "c_fee_share", "d_relay", "e_other"):
        g = by.get(c, [])
        print(f"{c:<14} {len(g):>4}   {sum(r['signer'] for r in g):>6}  {sum(r['in_keys'] for r in g):>14}"
              f"  {sum(r['cosigned_by_fomo'] for r in g):>13}")
    print("tx versions per class: " + "; ".join(
        f"{c}: {dict(Counter(r['version'] for r in g))}" for c, g in sorted(by.items())))
    src = Counter(r["key_source"] for r in rows if r["in_keys"])
    print(f"\npubkey present as account key in {sum(r['in_keys'] for r in rows)}/{len(rows)} txs "
          f"(source: {dict(src)})")
    others = Counter(s for r in rows for s in r["other_signers"])
    print(f"distinct other signers: {len(others)}; top: "
          + ", ".join(f"{_short(k)}x{v}" for k, v in others.most_common(8)))

    for c in ("a_signer", "b_unsigned"):
        g = by.get(c, [])
        if not g:
            continue
        print(f"\n--- {c}: trades in HIS balances ---")
        for r in sorted(g, key=lambda r: r["time"] or 0):
            sw = r["swap"]
            what = f"{sw[0]:<4} {_short(sw[1])} {sw[3]:>10.3f} {sw[4]}" if sw else f"deltas {_fmt(r['my_deltas'])}"
            extra = f" bot_parses={r.get('bot_parses')}" if c == "a_signer" else ""
            print(f"  {_t(r['time'])}  {what}  keys={r['in_keys']}({r['key_source']}) "
                  f"signers={[_short(s) for s in r['signers']]}{extra}  {r['sig'][:20]}..")

    g = by.get("c_fee_share", [])
    if g:
        ratios = sorted(r["fee_ratio"] for r in g if r.get("fee_ratio") is not None)
        tot = sum(_usd_like(m, v) for r in g for m, v in r["my_deltas"].items() if m in STABLES)
        print(f"\n--- c_fee_share: {len(g)} txs, he received {tot:.2f} USD in stables; ratio to biggest flow "
              f"median {ratios[len(ratios) // 2]:.4f} (min {ratios[0]:.4f}, max {ratios[-1]:.4f})")
        sw = Counter(o for r in g for o in r["swappers"])
        print("   who actually swapped (two-sided balances): "
              + ", ".join(f"{_short(k)}x{v}" for k, v in sw.most_common(8)))
        same = sum(1 for r in g if any(o in r["signers"] for o in r["swappers"]))
        print(f"   swapper is also a signer in {same}/{len(g)} txs (= someone else's own trade)")
        for r in g[:5]:
            print(f"   e.g. {_t(r['time'])} got {_fmt(r['my_deltas'])} ratio {r['fee_ratio']:.4f} "
                  f"signers={[_short(s) for s in r['signers']]} {r['sig']}")

    g = by.get("d_relay", [])
    if g:
        print(f"\n--- d_relay: {len(g)} ---")
        for r in g[:10]:
            print(f"   {_t(r['time'])} {r['relay']} signed={r['signer']} mine={_fmt(r['my_deltas'])} {r['sig']}")

    g = by.get("e_other", [])
    if g:
        print(f"\n--- e_other: {len(g)} (venues: {Counter(r['venues'] for r in g).most_common(5)}) ---")
        for r in g[:8]:
            print(f"   {_t(r['time'])} err={r['err']} signed={r['signer']} mine={_fmt(r['my_deltas'])} "
                  f"swappers={[_short(o) for o in r['swappers']]} {r['sig']}")

    if holdings is not None:
        held = [(a["account"]["data"]["parsed"]["info"]["mint"],
                 a["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"]) for a in holdings.get("value", [])]
        nonzero = [(m, a) for m, a in held if a]
        print(f"\ncurrent SPL token accounts: {len(held)}, non-zero: "
              + ", ".join(f"{STABLES.get(m, _short(m))}={a:g}" for m, a in nonzero[:12]))


def _fmt(ms: dict) -> str:
    out = []
    for m, v in ms.items():
        if m in STABLES:
            out.append(f"{STABLES[m]}{v / 1e6:+.4f}")
        elif m in ("SOL", WSOL):
            out.append(f"{'SOL' if m == 'SOL' else 'wSOL'}{v / LAMPORTS:+.5f}")
        else:
            out.append(f"{_short(m, 6)}{v:+d}")
    return " ".join(out) or "none"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("wallet")
    ap.add_argument("--limit", type=int, default=300)
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("--cache", default="data/txcache")
    ap.add_argument("--refresh", action="store_true", help="re-download cached txs")
    a = ap.parse_args()
    url = yaml.safe_load(open(a.config))["chains"]["solana"]["rpc_http"]
    t0 = time.time()
    txs, holdings = asyncio.run(fetch(url, a.wallet, a.limit, os.path.join(a.cache, a.wallet[:8]), a.refresh))
    print(f"fetched {len(txs)} txs in {time.time() - t0:.1f}s", file=sys.stderr)
    report([classify(t, a.wallet) for t in txs], a.wallet, holdings)


if __name__ == "__main__":
    main()
