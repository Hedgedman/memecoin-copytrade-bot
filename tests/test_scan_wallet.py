"""Wallet screener, offline parts: python -m tests.test_scan_wallet

Average-cost token book (closed / open / bought-before-the-window), venue classification, the SOL/USD lookup, and
the copy simulation (check 12) + check 8 numbers on synthetic 1-minute candles.
No network: fetching, GeckoTerminal and the checks that need them are exercised by running the script."""
from scripts.scan_wallet import copy_sim, path_numbers, tokens_book, usd_at, venue_of

L = 1_000_000_000


def sw(ts, side, mint, raw, sol, venue="curve"):
    return dict(ts=ts, side=side, mint=mint, raw=raw, sol=sol, quote="SOL", venue=venue, pool=None, sig=f"s{ts}")


def test_book():
    swaps = [
        sw(0, "buy", "WIN", 1000, 1.0), sw(10, "buy", "WIN", 1000, 1.0),        # 2 SOL for 2000
        sw(100, "sell", "WIN", 1500, 2.4), sw(200, "sell", "WIN", 500, 0.9),    # all out for 3.3 SOL: +1.3
        sw(0, "buy", "OPEN", 1000, 2.0), sw(50, "sell", "OPEN", 500, 1.5),      # half sold: realized +0.5, 500 held
        sw(5, "sell", "OLD", 700, 0.7),                                          # bought before the window
        sw(1, "buy", "DUST", 1000, 1.0), sw(30, "sell", "DUST", 995, 0.5),       # 0.5% left = closed
    ]
    b = tokens_book(sorted(swaps, key=lambda s: s["ts"]))
    assert b["WIN"]["closed"] and abs(b["WIN"]["proceeds"] - b["WIN"]["cost"] - 1.3) < 1e-9
    assert abs(b["WIN"]["realized"] - 1.3) < 1e-9 and b["WIN"]["last_sell"] - b["WIN"]["first_buy"] == 200
    assert not b["OPEN"]["closed"] and b["OPEN"]["held"] == 500 and abs(b["OPEN"]["realized"] - 0.5) < 1e-9
    assert abs(b["OPEN"]["basis"] - 1.0) < 1e-9
    assert b["OLD"]["pre_window"] and not b["OLD"]["closed"]
    assert b["DUST"]["closed"] and abs(b["DUST"]["proceeds"] - b["DUST"]["cost"] + 0.5) < 1e-9
    print("token book: closed / partial / pre-window / dust ok")


def test_misc():
    assert venue_of("?ALPHAQ,pumpfun_curve") == "curve" and venue_of("jupiter,pumpswap_amm") == "pumpswap"
    assert venue_of("jupiter,meteora_damm_v2") == "other" and venue_of("") == "other"
    daily = {0: 100.0, 86400: 110.0}
    assert usd_at(daily, 86400 + 5) == 110.0 and usd_at(daily, 10 * 86400) == 110.0 and usd_at({}, 0) is None
    print("venues + SOL/USD lookup ok")


def c(ts, o, h, lo, cl, v=1.0):
    return [ts, o, h, lo, cl, v]


def test_copy_sim():
    t = 600_000 + 17                                     # his buy at second 17 of minute 600000
    base = {600_000: c(600_000, 1.0, 1.1, 0.9, 1.0)}
    # entry = open of the NEXT candle (1.2); TP at 1.56 hit in the minute after
    tp = base | {600_060: c(600_060, 1.2, 1.3, 1.15, 1.25), 600_120: c(600_120, 1.25, 1.6, 1.2, 1.5)}
    r = copy_sim(tp, t)
    assert r["out"] == "tp" and abs(r["ret"] - 0.28) < 1e-9 and r["entry"] == 1.2 and r["entry_ts"] == 600_060
    # both levels in one candle -> stop first
    both = base | {600_060: c(600_060, 1.0, 1.4, 0.7, 1.0)}
    assert copy_sim(both, t)["out"] == "stop" and abs(copy_sim(both, t)["ret"] + 0.22) < 1e-9
    # gap below the stop: filled at the open (worse than -20%)
    gap = base | {600_060: c(600_060, 1.0, 1.05, 0.95, 1.0), 600_180: c(600_180, 0.5, 0.55, 0.4, 0.45)}
    r = copy_sim(gap, t)
    assert r["out"] == "stop(gap)" and abs(r["ret"] - (0.5 - 1 - 0.02)) < 1e-9
    # a minute without trades: the next existing candle is the entry; nothing hit -> last close, open
    quiet = base | {600_180: c(600_180, 2.0, 2.1, 1.9, 2.2)}
    r = copy_sim(quiet, t)
    assert r["entry"] == 2.0 and r["out"] == "open" and abs(r["ret"] - 0.08) < 1e-9
    assert copy_sim(base, t) is None                     # no candle after his minute
    # check 8 numbers vs his fill 0.8: +5 s = close of his minute, drift = +5 min close vs our entry
    pn = path_numbers(tp | {600_300: c(600_300, 1.5, 1.5, 1.4, 1.44)}, t, 0.8)
    assert abs(pn["p5s"] - 0.25) < 1e-9 and abs(pn["p60"] - (1.25 / 0.8 - 1)) < 1e-9
    assert abs(pn["p5m"] - 0.8) < 1e-9 and pn["up20"] and abs(pn["drift"] - 0.2) < 1e-9
    print("copy simulation: next-candle entry, TP / stop (stop first), gaps, quiet minutes, check 8 numbers ok")


if __name__ == "__main__":
    test_book(); test_misc(); test_copy_sim()
    print("ALL PASSED")
