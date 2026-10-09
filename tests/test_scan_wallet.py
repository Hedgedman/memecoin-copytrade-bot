"""Wallet screener, offline parts: python -m tests.test_scan_wallet

Average-cost token book (closed / open / bought-before-the-window), venue classification and the SOL/USD lookup.
No network: fetching, GeckoTerminal and the checks that need them are exercised by running the script."""
from scripts.scan_wallet import tokens_book, usd_at, venue_of

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


if __name__ == "__main__":
    test_book(); test_misc()
    print("ALL PASSED")
