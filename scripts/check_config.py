"""Config validation (offline, no network, no secrets printed): python -m scripts.check_config [-c config.yaml] [--live]

Checks the things that must hold before a start - paper flag, leaders / leader_allowlist / leader_status,
allowed_venues vs pool venues, exits, size and limits, and the live-only rules (no leader_profiles, no xchain
paper trading, a wallet key source). --live additionally demands paper: false. Prints a summary (hosts only,
never the RPC URLs with the API key) and exits 1 on any problem."""
from __future__ import annotations

import argparse
import os
import sys
from urllib.parse import urlsplit

import yaml

MODES = ("cost", "entry_price")


def host(url: str) -> str:
    try:
        return urlsplit(url).hostname or "?"
    except ValueError:
        return "?"


def validate(cfg: dict, want_live: bool = False) -> tuple[list[str], list[str]]:
    """-> (problems, summary lines). Problems are fatal; the summary is what the bot will run with."""
    bad, out = [], []
    paper = bool(cfg.get("paper", True))
    out.append(f"paper: {paper}")
    if want_live and paper:
        bad.append("--live: config has paper: true")
    chains = cfg.get("chains") or {}
    sol = chains.get("solana") or {}
    if not sol.get("enabled"):
        bad.append("chains.solana.enabled is not true")
    for name, c in chains.items():
        if name != "solana" and c.get("enabled"):
            bad.append(f"chains.{name}: enabled (H8-LIVE is Solana only)")
    for k in ("rpc_http", "rpc_ws"):
        if not sol.get(k):
            bad.append(f"chains.solana.{k} missing")
    out.append(f"rpc: {host(sol.get('rpc_http', ''))} / {host(sol.get('rpc_ws', ''))}, detection {sol.get('detection', 'auto')}")
    leaders = sol.get("leaders") or []
    if not leaders:
        bad.append("chains.solana.leaders is empty")
    if len(set(leaders)) != len(leaders):
        bad.append("chains.solana.leaders has duplicates")
    allow = sol.get("leader_allowlist")
    status = sol.get("leader_status") or {}
    for w, st in status.items():
        if st not in ("active", "watch"):
            bad.append(f"leader_status.{w[:8]}: unknown status {st!r}")
    if allow is not None:
        if not isinstance(allow, list) or not allow:
            bad.append("chains.solana.leader_allowlist must be a non-empty list (or absent)")
        else:
            for w in allow:
                if w not in leaders:
                    bad.append(f"leader_allowlist {w[:8]} is not in chains.solana.leaders")
                if status.get(w) == "watch":
                    bad.append(f"leader_allowlist {w[:8]} is also leader_status watch")
            out.append(f"leaders: {len(leaders)} streamed, allowlist {[w[:8] for w in allow]} open positions, "
                       f"{len([w for w in leaders if w not in allow])} watch-only")
    else:
        out.append(f"leaders: {len(leaders)} streamed, no allowlist (all open positions except leader_status watch: "
                   f"{[w[:8] for w, s in status.items() if s == 'watch']})")
    pool = set(sol.get("pool_pricing_venues", ["pumpfun_curve", "pumpswap_amm"]))
    vault = set(sol.get("vault_pricing_venues", ["meteora_damm_v2"]))
    venues = sol.get("allowed_venues")
    if venues is not None:
        if not isinstance(venues, list) or not venues:
            bad.append("chains.solana.allowed_venues must be a non-empty list (or absent)")
        else:
            for v in venues:
                if v not in pool | vault:
                    bad.append(f"allowed_venues {v}: not a pool-priced venue ({sorted(pool | vault)})")
            out.append(f"allowed venues: {venues} (other venues / Jupiter-priced tokens: skipped)")
    else:
        out.append("allowed venues: none set (Jupiter-priced tokens are bought too)")
    buy = float(sol.get("buy_amount", 0) or 0)
    if buy <= 0:
        bad.append("chains.solana.buy_amount must be > 0")
    limit = float(sol.get("daily_loss_limit", 0) or 0)
    if limit <= 0:
        bad.append("chains.solana.daily_loss_limit must be > 0")
    risk = cfg.get("risk") or {}
    mop = int(risk.get("max_open_positions", 3))
    if mop < 1:
        bad.append("risk.max_open_positions must be >= 1")
    out.append(f"size: buy_amount {buy} SOL, max_open_positions {mop}, daily_loss_limit {limit} SOL, "
               f"reentry_cooldown_s {risk.get('reentry_cooldown_s', 1800)}, kill switch file {risk.get('kill_switch_file', 'STOP')}")
    out.append(f"filters: market cap ${float(sol.get('min_market_cap_usd', 0) or 0):,.0f} - "
               f"${float(sol.get('max_market_cap_usd', 0) or 0):,.0f} (0 = off), min_leader_buy_sol {sol.get('min_leader_buy_sol', 0.5)}, "
               f"max_signal_age_s {sol.get('max_signal_age_s', 5)}, only_first_leader_buy {bool(sol.get('only_first_leader_buy'))}, "
               f"max_roundtrip_loss {sol.get('max_roundtrip_loss', 0.08)}")
    buy_slip = int(sol.get("buy_slippage_bps", sol.get("slippage_bps", 1500)))
    if not 0 < buy_slip <= 10_000:
        bad.append("chains.solana.buy_slippage_bps must be in (0, 10000]")
    out.append(f"execution: buy_slippage_bps {buy_slip} (buys never retry with more; a buy that fails on it is a skip "
               f"'buy slippage exceeded'), sell slippage_bps {sol.get('slippage_bps', 1500)} -> 3000/5000/9000 on retries, max_priority_fee_lamports "
               f"{sol.get('max_priority_fee_lamports', 2_000_000)}, confirm_timeout_s {sol.get('confirm_timeout_s', 60)}, "
               f"jupiter {host(sol.get('jupiter_base', 'https://lite-api.jup.ag/swap/v1'))} at {sol.get('jupiter_max_rps', 1.0)} rps, "
               f"Jupiter API key {'set (env)' if os.environ.get(sol.get('jupiter_api_key_env', 'JUPITER_API_KEY')) else 'NOT in env'}")
    e = cfg.get("exits") or {}
    tp, sl = float(e.get("take_profit", 0.2)), float(e.get("stop_loss", 0.04))
    tr_a, tr_p = float(e.get("trailing_activate", 0)), float(e.get("trailing_pct", 0))
    basis = e.get("stop_basis", "cost")
    if not 0 < sl < 1:
        bad.append("exits.stop_loss must be in (0, 1)")
    if tp < 0 or not 0 <= tr_p < 1 or tr_a < 0:
        bad.append("exits.take_profit >= 0, trailing_pct in [0, 1), trailing_activate >= 0")
    if basis not in MODES:
        bad.append(f"exits.stop_basis must be one of {MODES}")
    out.append(f"exits: TP {f'+{100 * tp:.0f}% on cost' if tp > 0 else 'off'}, stop -{100 * sl:.0f}% (basis {basis}), "
               f"trailing {f'{100 * tr_p:.0f}% after +{100 * tr_a:.0f}%' if tr_p > 0 else 'off'}, "
               f"max_hold_s {e.get('max_hold_s', 600)}, follow_leader_exit {bool(e.get('follow_leader_exit', True))}, "
               f"poll {e.get('poll_interval_s', 1.0)}s")
    # live-only rules
    if not paper:
        if cfg.get("leader_profiles"):
            bad.append("leader_profiles set: profiles are paper-only (the bot ignores them, but a live config must not carry them)")
        xc = cfg.get("xchain") or {}
        if (xc.get("paper_trading") or {}).get("enabled"):
            bad.append("xchain.paper_trading.enabled with paper: false (paper-only)")
        if xc.get("enabled"):
            out.append("xchain: enabled (watch-only, Relay calls)")
        else:
            out.append("xchain: disabled")
        key_env = sol.get("private_key_env", "SOL_PRIVATE_KEY")
        if os.environ.get(key_env):
            out.append(f"wallet key: {key_env} is in the environment")
        elif sol.get("private_key_keychain"):
            out.append(f"wallet key: {key_env} from the macOS keychain (account copybot) at start")
        else:
            bad.append(f"wallet key: {key_env} not in the environment and private_key_keychain is not true")
    else:
        out.append(f"profiles: {len(cfg.get('leader_profiles') or {})} | "
                   f"xchain: {'enabled' if (cfg.get('xchain') or {}).get('enabled') else 'disabled'}")
    return bad, out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("--live", action="store_true", help="demand paper: false")
    a = ap.parse_args()
    cfg = yaml.safe_load(open(a.config))
    bad, out = validate(cfg, a.live)
    print(f"config {a.config}:")
    for line in out:
        print("  " + line)
    if bad:
        print(f"PROBLEMS ({len(bad)}):")
        for b in bad:
            print("  - " + b)
        sys.exit(1)
    print("OK: no problems")


if __name__ == "__main__":
    main()
