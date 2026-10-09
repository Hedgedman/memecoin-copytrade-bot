"""Read-only wallet screener: is this Solana wallet worth paper-trading as a leader?

Usage (from the copybot folder):
    python -m scripts.scan_wallet <address> [<address> ...] [--days 30] [--gt-max 60] [--refresh]

Data
- His transactions over --days via Helius getTransactionsForAddress (100 full txs per call; URL from the keychain
  like the bot, chains.solana.rpc_http_secret; never printed). Raw pages cached gzipped in
  backtests/wallet_scans/raw/ (reused for --cache-h hours).
- Swaps parsed with the bot's own parsers (parse_wallet_swap: SOL/WSOL-quoted, parse_stable_swap: USDC/USDT-quoted,
  converted to SOL at that day's SOL/USD close from GeckoTerminal).
- Per token: average-cost accounting inside the window. Tokens he sold more of than he bought in the window (bought
  before it) are left out of PnL (counted as `pre_window`). Closed = what he still holds <= 1% of what he bought.
- Price paths after his buys: GeckoTerminal 1-minute candles (cached in backtests/wallet_scans/gt/, 1 call / 6.5 s),
  on the pool of his own swap (pump.fun curve / PumpSwap pool from the tx; other venues: GT's top SOL pool), for
  a fixed random sample of up to --gt-max tokens (first buy each). 1-minute resolution: "+5 s" is the close of
  the minute of his buy (includes his own impact), "+60 s" the next minute's close, "+5 min" the close 5 minutes on.
- Non-trading income: SOL / WSOL / USDC he receives in txs that are not his swaps, split into fee shares
  (inspect_wallet.classify c_fee_share), program claims (creator fees etc.: a trading program is involved) and
  plain transfers in. Check 10 uses fee shares + program claims; transfers in are shown separately.
- Funding: his oldest transactions (getTransactionsForAddress ascending): who sent the first SOL; a funder with
  >= 1000 signatures within 6 h is called exchange-like (a heuristic, not a label).
- If the address is one of our leaders: our own paper scorecard for him (scripts.leader_report.collect).

Output: a table per wallet (PASS / FAIL / FLAG / UNKNOWN / INFO with the number), a verdict (REJECT if any FAIL,
else PAPER CANDIDATE with the flags and unknowns listed), a ranking for several wallets, and
backtests/wallet_scans/<address>.json.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import random
import sqlite3
import statistics
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

import yaml

from bot.chains.solana import USDC, USDT, WSOL, parse_stable_swap, parse_wallet_swap, venues_in_tx
from bot.pricing.pools import curve_pda, find_pool_vaults
from bot.secrets import resolve_rpc
from scripts.inspect_wallet import classify, owner_deltas

L = 1_000_000_000
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
OUT = os.path.join(ROOT, "backtests", "wallet_scans")
GT = "https://api.geckoterminal.com/api/v2"
NET = "/networks/solana"
TRADING_PROGRAMS = {"6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P", "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",
                    "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ"}       # pump.fun, PumpSwap, pump fee program
STABLE = {USDC, USDT}


# ---------------------------------------------------------------- HTTP (no URL ever printed) -------------------
class Rpc:
    def __init__(self, url: str, min_interval: float = 0.25):
        self.url, self.min_interval, self._last, self.calls = url, min_interval, 0.0, 0

    def __call__(self, method: str, params: list, tries: int = 6):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        for i in range(tries):
            wait = self.min_interval - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            self.calls += 1
            try:
                req = urllib.request.Request(self.url, body, {"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=90) as r:
                    j = json.load(r)
            except urllib.error.HTTPError as e:
                if e.code == 429 and i < tries - 1:
                    time.sleep(2 * (i + 1)); continue
                raise RuntimeError(f"{method}: HTTP {e.code}") from None
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                if i < tries - 1:
                    time.sleep(2 * (i + 1)); continue
                raise RuntimeError(f"{method}: {type(e).__name__}") from None
            if "error" in j:
                raise RuntimeError(f"{method}: {j['error'].get('message', '')[:200]}")
            return j["result"]
        raise RuntimeError(f"{method}: still failing")


class Gecko:
    """GeckoTerminal, 1 call per 6.5 s (stricter than documented), JSON cached on disk."""
    def __init__(self, cache: str, interval: float = 6.5):
        self.cache, self.interval, self._last, self.calls = cache, interval, 0.0, 0
        os.makedirs(cache, exist_ok=True)

    def get(self, path: str, name: str):
        f = os.path.join(self.cache, name)
        if os.path.exists(f):
            with open(f) as fh:
                return json.load(fh)
        data, answered = None, False
        for i in range(6):
            wait = self.interval - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            self.calls += 1
            try:
                with urllib.request.urlopen(urllib.request.Request(GT + path, headers={"Accept": "application/json"}),
                                            timeout=30) as r:
                    data = json.load(r)
                answered = True
                break
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    answered = True          # a real "no such pool / token": cache it
                    break
                if e.code == 429:
                    time.sleep(30 * (i + 1)); continue
                raise RuntimeError(f"GeckoTerminal HTTP {e.code}") from None
            except (urllib.error.URLError, TimeoutError, OSError):
                time.sleep(5)
        if answered:                         # never cache a rate-limit / network failure as "no data"
            with open(f, "w") as fh:
                json.dump(data, fh)
        return data


# ---------------------------------------------------------------- fetching ---------------------------------------
def fetch_window(rpc: Rpc, addr: str, days: float, cache_h: float, refresh: bool, max_pages: int) -> tuple[list, bool]:
    """All his txs of the last `days` (newest first). -> (txs, truncated)."""
    os.makedirs(os.path.join(OUT, "raw"), exist_ok=True)
    path = os.path.join(OUT, "raw", f"{addr}_{days:g}d.json.gz")
    if not refresh and os.path.exists(path) and time.time() - os.path.getmtime(path) < cache_h * 3600:
        with gzip.open(path, "rt") as fh:
            d = json.load(fh)
        return d["txs"], d["truncated"]
    since = int(time.time() - days * 86400)
    txs, token, truncated = [], None, False
    for page in range(max_pages):
        opts = {"transactionDetails": "full", "encoding": "jsonParsed", "maxSupportedTransactionVersion": 1,
                "limit": 100, "sortOrder": "desc", "filters": {"blockTime": {"gte": since}}}
        if token:
            opts["paginationToken"] = token
        res = rpc("getTransactionsForAddress", [addr, opts])
        txs += res.get("data") or []
        token = res.get("paginationToken")
        if not token or not res.get("data"):
            break
        if page % 20 == 19:
            print(f"  {addr[:8]}: {len(txs)} txs fetched ...", file=sys.stderr, flush=True)
    else:
        truncated = True
    with gzip.open(path, "wt") as fh:
        json.dump({"txs": txs, "truncated": truncated, "fetched": time.time()}, fh)
    return txs, truncated


def fetch_oldest(rpc: Rpc, addr: str) -> list:
    path = os.path.join(OUT, "raw", f"{addr}_oldest.json.gz")
    if os.path.exists(path):
        with gzip.open(path, "rt") as fh:
            return json.load(fh)
    res = rpc("getTransactionsForAddress", [addr, {"transactionDetails": "full", "encoding": "jsonParsed",
                                                   "maxSupportedTransactionVersion": 1, "limit": 20,
                                                   "sortOrder": "asc"}])
    data = res.get("data") or []
    with gzip.open(path, "wt") as fh:
        json.dump(data, fh)
    return data


def sol_usd_daily(gk: Gecko) -> dict[int, float]:
    """{UTC day start: SOL/USD close} from GeckoTerminal's top SOL/USDC pool."""
    d = gk.get(f"{NET}/tokens/{WSOL}/pools?page=1", "sol_pools.json")
    pool = None
    for p in (d or {}).get("data", []):
        r = p["relationships"]
        if r["base_token"]["data"]["id"] == f"solana_{WSOL}" and r["quote_token"]["data"]["id"] == f"solana_{USDC}":
            pool = p["attributes"]["address"]
            break
    if not pool:
        return {}
    day = int(time.time() // 86400)
    rows = (gk.get(f"{NET}/pools/{pool}/ohlcv/day?aggregate=1&limit=120", f"solusd_day_{day}.json") or {}) \
        .get("data", {}).get("attributes", {}).get("ohlcv_list") or []
    return {int(r[0]) // 86400 * 86400: float(r[4]) for r in rows}


def usd_at(daily: dict[int, float], ts: float) -> float | None:
    if not daily:
        return None
    k = int(ts) // 86400 * 86400
    return daily.get(k) or daily[min(daily, key=lambda x: abs(x - k))]


def mint_infos(rpc: Rpc, mints: list[str]) -> dict[str, dict]:
    out = {}
    for i in range(0, len(mints), 100):
        chunk = mints[i:i + 100]
        res = rpc("getMultipleAccounts", [chunk, {"encoding": "jsonParsed"}])
        for m, v in zip(chunk, res["value"]):
            info = ((v or {}).get("data") or {})
            info = info.get("parsed", {}).get("info", {}) if isinstance(info, dict) else {}
            if info:
                out[m] = {"supply": int(info.get("supply") or 0), "decimals": int(info.get("decimals") or 0)}
    return out


# ---------------------------------------------------------------- parsing ----------------------------------------
def venue_of(names: str) -> str:
    n = names.split(",")
    return "curve" if "pumpfun_curve" in n else "pumpswap" if "pumpswap_amm" in n else "other"


def extract(txs: list, addr: str, daily: dict) -> tuple[list, dict]:
    """-> (swaps, income). swap = dict(ts, side, mint, raw, sol, quote, venue, pool, sig)."""
    swaps = []
    inc = {"fee_share": 0.0, "program_claims": 0.0, "transfers_in": 0.0, "transfers_out": 0.0, "relay_txs": 0,
           "n_fee_share": 0, "n_program_claims": 0, "n_transfers_in": 0, "failed": 0, "unpriced_usd": 0}
    for tx in txs:
        if not tx or not tx.get("meta"):
            continue
        if tx["meta"].get("err") is not None:
            inc["failed"] += 1
            continue
        ts = tx.get("blockTime") or 0
        s = parse_wallet_swap(tx, addr)
        quote, amt = "SOL", None
        if s:
            side, mint, raw, amt = s
        else:
            st = parse_stable_swap(tx, addr)
            if st:
                side, mint, raw, usd = st
                px = usd_at(daily, ts)
                if not px:
                    inc["unpriced_usd"] += 1
                    continue
                amt, quote = usd / px, "USD"
        if amt is not None:
            names = venues_in_tx(tx)
            v = venue_of(names)
            pool = None
            if v == "curve":
                pool = curve_pda(mint)
            elif v == "pumpswap":
                pv = find_pool_vaults(tx, mint, addr)
                pool = pv[0] if pv else None
            swaps.append(dict(ts=ts, side=side, mint=mint, raw=int(raw), sol=float(amt), quote=quote, venue=v,
                              pool=pool, sig=tx["transaction"]["signatures"][0]))
            continue
        c = classify(tx, addr)
        if c["cls"] == "d_relay":
            inc["relay_txs"] += 1
            continue
        mine = owner_deltas(tx).get(addr, {})
        sol = (mine.get("SOL", 0) + mine.get(WSOL, 0)) / L
        usd = sum(v for m, v in mine.items() if m in STABLE) / 1e6
        px = usd_at(daily, ts) or 0
        val = sol + (usd / px if px else 0)
        others = [m for m, v in mine.items() if m not in ("SOL", WSOL) and m not in STABLE and v != 0]
        if others:                      # token moves without a swap (airdrops, transfers): not income
            continue
        if val > 0:
            if c["cls"] == "c_fee_share":
                inc["fee_share"] += val; inc["n_fee_share"] += 1
            elif _programs(tx) & TRADING_PROGRAMS:
                inc["program_claims"] += val; inc["n_program_claims"] += 1
            else:
                inc["transfers_in"] += val; inc["n_transfers_in"] += 1
        elif val < -0.001:
            inc["transfers_out"] += -val
    swaps.sort(key=lambda x: x["ts"])
    return swaps, inc


def _programs(tx: dict) -> set:
    ids = {ix.get("programId") for ix in tx["transaction"]["message"].get("instructions", []) or []}
    for inner in tx["meta"].get("innerInstructions") or []:
        ids |= {ix.get("programId") for ix in inner.get("instructions", []) or []}
    return ids


def tokens_book(swaps: list) -> dict[str, dict]:
    """Average-cost book per mint inside the window."""
    book: dict[str, dict] = {}
    for s in swaps:
        b = book.setdefault(s["mint"], dict(bought=0, sold_raw=0, cost=0.0, proceeds=0.0, held=0, basis=0.0,
                                            realized=0.0, first_buy=None, last_sell=None, buys=[], sells=[],
                                            pre_window=False, venue=None))
        if s["side"] == "buy":
            b["bought"] += s["raw"]; b["cost"] += s["sol"]; b["held"] += s["raw"]; b["basis"] += s["sol"]
            b["buys"].append(s)
            if b["first_buy"] is None:
                b["first_buy"], b["venue"] = s["ts"], s["venue"]
        else:
            b["sells"].append(s)
            b["sold_raw"] += s["raw"]; b["proceeds"] += s["sol"]; b["last_sell"] = s["ts"]
            if b["held"] <= 0 or s["raw"] > b["held"] * 1.01:
                b["pre_window"] = True          # sells more than bought in the window: cost basis unknown
            take = min(s["raw"], b["held"])
            frac = take / b["held"] if b["held"] else 0
            b["realized"] += s["sol"] * (take / s["raw"] if s["raw"] else 0) - b["basis"] * frac
            b["basis"] -= b["basis"] * frac
            b["held"] -= take
    for b in book.values():
        b["closed"] = (not b["pre_window"] and b["bought"] > 0 and b["sells"] != []
                       and b["held"] <= 0.01 * b["bought"])
    return book


# ---------------------------------------------------------------- checks -----------------------------------------
def med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def check(status, value, rule):
    return {"status": status, "value": value, "rule": rule}


def copyability(gk: Gecko, book: dict, infos: dict, gt_max: int, seed: str) -> dict:
    """Price after his FIRST buy of a sample of tokens, from 1-minute candles (SOL per UI token)."""
    cands = [m for m, b in book.items() if b["buys"] and infos.get(m)]
    rnd = random.Random(seed)
    rnd.shuffle(cands)
    rows = []
    for m in cands[:gt_max]:
        s = book[m]["buys"][0]
        dec = infos[m]["decimals"]
        his = s["sol"] / (s["raw"] / 10 ** dec)
        pool = s["pool"]
        if not pool:
            d = gk.get(f"{NET}/tokens/{m}/pools?page=1", f"pools_{m}.json")
            for p in (d or {}).get("data", []):
                r = p["relationships"]
                if r["base_token"]["data"]["id"] == f"solana_{m}" and r["quote_token"]["data"]["id"] == f"solana_{WSOL}":
                    pool = p["attributes"]["address"]; break
        if not pool:
            continue
        t0 = int(s["ts"]) // 60 * 60
        d = gk.get(f"{NET}/pools/{pool}/ohlcv/minute?aggregate=1&before_timestamp={t0 + 361}&limit=10&currency=token&token={m}",
                   f"ohlcv_{pool}_{t0}.json")
        c = {int(r[0]): r for r in ((d or {}).get("data") or {}).get("attributes", {}).get("ohlcv_list") or []}
        if t0 not in c:
            continue
        def close_at(k):            # last candle at or before t0 + k*60 (no trade in a minute = unchanged price)
            ks = [x for x in c if t0 <= x <= t0 + k * 60]
            return c[max(ks)][4] if ks else None
        hi60 = max(c[x][2] for x in c if t0 <= x <= t0 + 60)
        rows.append(dict(mint=m, venue=s["venue"], his=his, p5s=close_at(0) / his - 1,
                         p60=(close_at(1) or close_at(0)) / his - 1, p5m=(close_at(5) or close_at(0)) / his - 1,
                         up20=hi60 / his - 1 > 0.20))
    return {"n": len(rows), "sampled": min(gt_max, len(cands)), "tokens_with_buys": len(cands), "rows": rows}


def our_paper(addr: str) -> dict | None:
    cfg = yaml.safe_load(open(os.path.join(ROOT, "config.yaml")))
    if addr not in ((cfg.get("chains") or {}).get("solana") or {}).get("leaders", []):
        return None
    from scripts import leader_report as LR
    from scripts.flags import flagged_positions
    db = sqlite3.connect(f"file:{os.path.join(ROOT, 'copybot.db')}?mode=ro", uri=True)
    st = LR.collect(db, flagged=flagged_positions(db)).get((addr, "solana"))
    return st


def funding(rpc: Rpc, addr: str, oldest: list) -> dict:
    out = {"wallet_age_days": None, "funder": None, "funder_kind": None, "first_sol": None}
    if not oldest:
        return out
    out["wallet_age_days"] = (time.time() - (oldest[0].get("blockTime") or time.time())) / 86400
    for tx in oldest:
        d = owner_deltas(tx)
        mine = d.get(addr, {}).get("SOL", 0)
        if mine > 0 and not parse_wallet_swap(tx, addr):
            src = min(((o, ms.get("SOL", 0)) for o, ms in d.items() if o != addr), key=lambda x: x[1], default=None)
            if src and src[1] < 0:
                out["funder"], out["first_sol"] = src[0], mine / L
                try:
                    sigs = rpc("getSignaturesForAddress", [src[0], {"limit": 1000}])
                    span = (sigs[0]["blockTime"] - sigs[-1]["blockTime"]) / 3600 if len(sigs) > 1 else None
                    out["funder_kind"] = ("exchange-like (>= 1000 txs within %.1f h)" % span
                                          if len(sigs) >= 1000 and span is not None and span < 6
                                          else f"wallet ({len(sigs)} recent txs over {span or 0:.0f} h)")
                except RuntimeError as e:
                    out["funder_kind"] = f"unknown ({e})"
            break
    return out


def scan(addr: str, args, rpc: Rpc, gk: Gecko, daily: dict) -> dict:
    t0 = time.time()
    txs, truncated = fetch_window(rpc, addr, args.days, args.cache_h, args.refresh, args.max_pages)
    swaps, inc = extract(txs, addr, daily)
    book = tokens_book(swaps)
    mints = sorted(book)
    infos = mint_infos(rpc, mints)
    closed = {m: b for m, b in book.items() if b["closed"]}
    open_ = {m: b for m, b in book.items() if not b["closed"] and not b["pre_window"] and b["held"] > 0}
    pnl = sorted(((b["proceeds"] - b["cost"], m) for m, b in closed.items()), reverse=True)
    realized = sum(p for p, _ in pnl)
    wo3 = sum(p for p, _ in pnl[3:])
    # unrealized at the last price (GeckoTerminal simple prices, USD -> SOL)
    unreal, priced = 0.0, 0
    now_usd = usd_at(daily, time.time())
    for i in range(0, len(open_), 30):
        chunk = sorted(open_)[i:i + 30]
        d = gk.get(f"/simple{NET}/token_price/{','.join(chunk)}", f"price_{addr}_{int(time.time() // 3600)}_{i}.json")
        prices = ((d or {}).get("data") or {}).get("attributes", {}).get("token_prices") or {}
        for m in chunk:
            px = prices.get(m)
            if px and now_usd and infos.get(m):
                unreal += open_[m]["held"] / 10 ** infos[m]["decimals"] * float(px) / now_usd - open_[m]["basis"]
                priced += 1
    span = (swaps[-1]["ts"] - swaps[0]["ts"]) / 86400 if len(swaps) > 1 else 0
    days_active = len({int(s["ts"]) // 86400 for s in swaps})
    wins = sum(p > 0 for p, _ in pnl)
    wr = wins / len(pnl) if pnl else None
    holds = [b["last_sell"] - b["first_buy"] for b in closed.values() if b["first_buy"] and b["last_sell"]]
    buys = [s for s in swaps if s["side"] == "buy"]
    mcs = []
    for m, b in book.items():
        if b["buys"] and infos.get(m) and infos[m]["supply"]:
            s = b["buys"][0]
            px = usd_at(daily, s["ts"])
            if px:
                mcs.append(s["sol"] / s["raw"] * infos[m]["supply"] * px)
    vm = defaultdict(int)
    for s in buys:
        vm[s["venue"]] += 1
    cp = copyability(gk, book, infos, args.gt_max, addr)
    r = cp["rows"]
    up20 = sum(x["up20"] for x in r) / len(r) if r else None
    quick = [m for m, b in book.items() if b["buys"] and not b["pre_window"] and any(
        s2["ts"] - b["first_buy"] <= 300 and s2["raw"] and b["cost"] and
        (s2["sol"] / s2["raw"]) / (b["cost"] / b["bought"]) > 1.2 for s2 in b["sells"])]
    sold_any = [m for m, b in book.items() if b["sells"] and not b["pre_window"]]
    pump_share = len(quick) / len(sold_any) if sold_any else None
    nti = inc["fee_share"] + inc["program_claims"]
    fund = funding(rpc, addr, fetch_oldest(rpc, addr))
    checks = {}
    checks["1 activity"] = check(
        "PASS" if span >= min(30, args.days) - 1 and len(book) >= 50 else "FAIL",
        f"{span:.1f} d span, {days_active} active days, {len(book)} tokens{' (TRUNCATED fetch)' if truncated else ''}",
        f">= {min(30, args.days) - 1:g} d span and >= 50 tokens")
    checks["2 realized PnL"] = check("UNKNOWN" if not pnl else "PASS" if realized > 0 else "FAIL",
                                     f"{realized:+.2f} SOL on {len(pnl)} closed tokens; unrealized {unreal:+.2f} SOL on "
                                     f"{priced}/{len(open_)} open tokens (last price); {sum(b['pre_window'] for b in book.values())} "
                                     f"tokens bought before the window left out", "> 0")
    checks["3 w/o best 3"] = check("UNKNOWN" if len(pnl) < 4 else "PASS" if wo3 > 0 else "FAIL",
                                   f"{wo3:+.2f} SOL (best 3: " + ", ".join(f"{m[:6]} {p:+.2f}" for p, m in pnl[:3]) + ")", "> 0")
    checks["4 win rate"] = check("UNKNOWN" if wr is None else "FLAG" if wr > 0.85 else "PASS" if 0.40 <= wr <= 0.75 else "FAIL",
                                 None if wr is None else f"{wr:.0%} ({wins}/{len(pnl)} tokens)", "40-75% (flag > 85%)")
    mh = med(holds)
    checks["5 holding time"] = check("UNKNOWN" if mh is None else "FAIL" if mh < 60 else "PASS",
                                     None if mh is None else f"median {mh / 60:.1f} min ({len(holds)} closed tokens)", ">= 60 s")
    mb = med(s["sol"] for s in buys)
    checks["6 buy size"] = check("UNKNOWN" if mb is None else "PASS" if 0.5 <= mb <= 10 else "FAIL",
                                 None if mb is None else f"median {mb:.2f} SOL ({len(buys)} buys, "
                                 f"{sum(s['quote'] == 'USD' for s in buys)} USDC-quoted)", "0.5-10 SOL")
    tot = sum(vm.values()) or 1
    checks["7 entry mc / venues"] = check("INFO", f"median entry mc ${med(mcs) or 0:,.0f} ({len(mcs)} tokens); buys: curve "
                                          f"{vm['curve'] / tot:.0%}, PumpSwap {vm['pumpswap'] / tot:.0%}, other {vm['other'] / tot:.0%}",
                                          "info")
    checks["8 copyability"] = check(
        "UNKNOWN" if up20 is None else "FAIL" if up20 > 0.40 else "PASS",
        None if not r else f"after his buy: +5 s~ {med(x['p5s'] for x in r):+.1%}, +60 s {med(x['p60'] for x in r):+.1%}, "
        f"+5 min {med(x['p5m'] for x in r):+.1%}; > +20% within 60 s: {up20:.0%} ({len(r)} of {cp['sampled']} sampled tokens)",
        "<= 40% of buys > +20% within 60 s")
    checks["9 sells into own pump"] = check("UNKNOWN" if pump_share is None else "FLAG" if pump_share > 0.5 else "PASS",
                                            None if pump_share is None else f"{pump_share:.0%} ({len(quick)}/{len(sold_any)} tokens)",
                                            "flag > 50% (sold within 5 min at > +20%)")
    pct_nti = nti / realized if realized > 0 else None
    checks["10 non-trading income"] = check(
        "FLAG" if (pct_nti is not None and pct_nti > 0.30) or (realized <= 0 and nti > 0.05) else "PASS",
        f"fee shares {inc['fee_share']:.2f} SOL ({inc['n_fee_share']} txs) + program claims {inc['program_claims']:.2f} "
        f"({inc['n_program_claims']}) = {nti:.2f} SOL = " + (f"{pct_nti:.0%} of realized PnL" if pct_nti is not None
                                                             else "n/a (realized PnL <= 0)")
        + f"; transfers in {inc['transfers_in']:.2f} SOL ({inc['n_transfers_in']}), out {inc['transfers_out']:.2f}; "
          f"Relay txs {inc['relay_txs']}", "flag > 30% of realized PnL")
    checks["11 funding"] = check("INFO", f"wallet age {fund['wallet_age_days'] or 0:.0f} d; first SOL "
                                 f"{fund['first_sol'] or 0:.3f} from {(fund['funder'] or '?')[:8]} ({fund['funder_kind']})",
                                 "info")
    fails = [k for k, c in checks.items() if c["status"] == "FAIL"]
    flags = [k for k, c in checks.items() if c["status"] == "FLAG"]
    unknown = [k for k, c in checks.items() if c["status"] == "UNKNOWN"]
    verdict = "REJECT" if fails else "PAPER CANDIDATE"
    res = dict(address=addr, scanned_at=time.time(), days=args.days, txs=len(txs), truncated=truncated, swaps=len(swaps),
               tokens=len(book), checks=checks, verdict=verdict, fails=fails, flags=flags, unknown=unknown,
               ours=our_paper(addr), realized=realized, wo3=wo3, copy_rows=r, income=inc, funding=fund,
               rpc_calls=rpc.calls, gt_calls=gk.calls, seconds=round(time.time() - t0))
    with open(os.path.join(OUT, f"{addr}.json"), "w") as fh:
        json.dump(res, fh, indent=1, default=str)
    return res


def show(res: dict) -> None:
    print(f"\n### {res['address']}  ({res['days']:g} days: {res['txs']} txs, {res['swaps']} swaps, {res['tokens']} tokens)\n")
    print("| check | result | value | rule |\n|---|---|---|---|")
    for k, c in res["checks"].items():
        print(f"| {k} | **{c['status']}** | {c['value']} | {c['rule']} |")
    o = res.get("ours")
    if o:
        print(f"\nOur paper results for him (leader_report, flagged out): {o['trades']} closed, {o['wins']} wins, PnL "
              f"{(o['pnl'] or 0):+.2f} SOL, w/o best {(o['wo_best'] or 0):+.2f}, median {(o['med_pct'] or 0):+.1f}%, "
              f"peak median {(o['peak'] or 0):+.0f}%, entry gap median {(o['gap'] or 0):+.1f}%, venues {o['venues']}")
    why = (f"FAIL: {', '.join(res['fails'])}" if res["fails"] else "no FAIL") + \
          (f"; FLAG: {', '.join(res['flags'])}" if res["flags"] else "") + \
          (f"; UNKNOWN: {', '.join(res['unknown'])}" if res["unknown"] else "")
    print(f"\n**Verdict: {res['verdict']}** ({why})")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("addresses", nargs="+")
    ap.add_argument("--days", type=float, default=30)
    ap.add_argument("--gt-max", type=int, default=60, help="tokens sampled for the copyability check (GeckoTerminal)")
    ap.add_argument("--max-pages", type=int, default=600, help="getTransactionsForAddress pages (100 txs each)")
    ap.add_argument("--cache-h", type=float, default=12, help="reuse cached transactions younger than this")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("-c", "--config", default=os.path.join(ROOT, "config.yaml"))
    a = ap.parse_args()
    url = resolve_rpc(yaml.safe_load(open(a.config))["chains"]["solana"]).get("rpc_http")
    if not url:
        sys.exit("no Solana RPC URL (config rpc_http or the keychain item named by rpc_http_secret)")
    os.makedirs(OUT, exist_ok=True)
    rpc, gk = Rpc(url), Gecko(os.path.join(OUT, "gt"))
    daily = sol_usd_daily(gk)
    results = []
    for addr in a.addresses:
        print(f"scanning {addr} ...", file=sys.stderr, flush=True)
        res = scan(addr, a, rpc, gk, daily)
        show(res)
        results.append(res)
    if len(results) > 1:
        print("\n### Ranking (verdict, then number of PASS, then realized PnL without the best 3)\n")
        print("| rank | wallet | verdict | PASS | FAIL | FLAG | realized SOL | w/o best 3 |\n|---|---|---|---|---|---|---|---|")
        key = lambda r: (r["verdict"] == "PAPER CANDIDATE",
                         sum(c["status"] == "PASS" for c in r["checks"].values()), r["wo3"])
        for i, r in enumerate(sorted(results, key=key, reverse=True), 1):
            n = lambda s: sum(c["status"] == s for c in r["checks"].values())
            print(f"| {i} | {r['address'][:8]} | {r['verdict']} | {n('PASS')} | {n('FAIL')} | {n('FLAG')} | "
                  f"{r['realized']:+.2f} | {r['wo3']:+.2f} |")


if __name__ == "__main__":
    main()
