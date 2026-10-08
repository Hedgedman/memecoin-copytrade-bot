"""Per-leader profiles (PAPER only): own exits and proportional sizing for selected leaders.

    leader_profiles:
      <solana wallet>:
        name: trail_Leader-B
        mode: trail                # no take-profit; trailing stop + stop-loss; his sells are NOT followed
        trailing_activate: 0.5     # trail starts once the position is worth +50% (1.5x)
        trailing_pct: 0.35         # ... and sells when it falls 35% from its peak
        stop_loss: 0.60            # before the trail is active
        target_usd: 200            # our size = target_usd / his_median_usd x his USD size
        his_median_usd: 913
        max_usd_per_token: 1000    # incl. his add-ons (xchain); Solana holds one position per token
        min_usd: 25                # skip copies smaller than this
        mark_interval_s: 30        # xchain: Relay quote per position for the trail checks

      <solana wallet>:
        name: mirror_Leader-A
        mode: mirror               # copy his buys AND his sells: he sells X% of his holding -> we sell X% of
                                   # ours (Solana: pre/post balance of his sell tx; xchain: balanceOf / tracked).
                                   # No take-profit, no trailing. stop_loss = safety stop on the cost of what
                                   # we still hold (collapses only). Sizing fields as above.
        stop_loss: 0.60
        target_usd: 200
        his_median_usd: 500
        max_usd_per_token: 500
        min_usd: 25
        mark_interval_s: 120

      <solana wallet>:
        name: mirror_ladder_Leader-B
        mode: mirror_ladder        # mirror (his sells pro rata) PLUS an own ladder: at each level x our average
        ladder_levels: [10, 25, 50, 100]   # entry (cost / tokens, adds included) sell ladder_pct of what we
        ladder_pct: 0.25           # still hold; each level once; no trailing, no take-profit; safety stop -60%
        stop_loss: 0.60            # on the cost of what we still hold (confirmed quotes only, bot/positions.py)
        ... sizing fields as above

    Every profile block also takes (Oct 8 2026):
        max_open_tokens: 10        # open positions of this profile, Solana + xchain together (own limit)
        daily_loss_usd: 500        # no new positions once the profile lost this much today (USD; SOL converted)
    Profile positions are keyed by (leader, token): they never block a normal leader's position in the same
    token and vice versa, they do not count toward max_open_positions / daily_loss_limit (Solana) or the
    xchain 50 open / 5 per leader limits, and his ADDS are copied (merged into the position, weighted cost)
    up to max_usd_per_token; the Solana min_leader_buy_sol filter does not apply (min_usd does).

Every other leader keeps the global exits / buy_amount / xchain paper settings. The profile's exits are
copied into each position (timing["exits"]) together with timing["profile"] and timing["size_factor"],
so a restart, the what-ifs and the reports all see what a position was opened with. Positions opened
before a profile existed keep the rules they were opened with.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger("profiles")

MODES = ("trail", "mirror", "mirror_ladder")


@dataclass(frozen=True)
class Profile:
    leader: str
    name: str
    mode: str
    trailing_activate: float
    trailing_pct: float
    stop_loss: float
    target_usd: float
    his_median_usd: float
    max_usd_per_token: float
    min_usd: float
    mark_interval_s: float
    max_open_tokens: int = 10
    daily_loss_usd: float = 500.0
    ladder_levels: tuple = ()
    ladder_pct: float = 0.0

    @property
    def factor(self) -> float:
        return self.target_usd / self.his_median_usd

    @property
    def mirrors(self) -> bool:
        """mirror / mirror_ladder: his sells are copied pro rata; trail: we exit on our own trail/stop."""
        return self.mode in ("mirror", "mirror_ladder")

    def exits(self) -> dict:
        """Stored per position; PositionManager / XchainPaperTrader read it instead of the global exits."""
        e = {"mode": self.mode, "take_profit": 0.0, "trailing_activate": self.trailing_activate,
             "trailing_pct": self.trailing_pct, "stop_loss": self.stop_loss, "max_hold_s": 0.0,
             "follow_leader_exit": self.mirrors}
        if self.mode == "mirror_ladder":
            e.update(ladder_levels=list(self.ladder_levels), ladder_pct=self.ladder_pct, stop_basis="cost")
        return e

    def stamp(self) -> dict:
        return {"profile": self.name, "size_factor": round(self.factor, 6), "exits": self.exits()}

    def size_usd(self, his_usd: float, already_usd: float = 0.0) -> tuple[float, str | None]:
        """-> (our USD size, None) or (0, why we skip)."""
        want = self.factor * his_usd
        size = min(want, self.max_usd_per_token - already_usd)
        if size < self.min_usd:
            if want < self.min_usd:
                return 0.0, f"profile {self.name}: our size ${want:,.2f} < ${self.min_usd:,.0f}"
            return 0.0, f"profile {self.name}: cap ${self.max_usd_per_token:,.0f}/token reached"
        return size, None


def load_profiles(cfg: dict, paper: bool) -> dict[str, Profile]:
    raw = cfg.get("leader_profiles") or {}
    if not raw:
        return {}
    if not paper:
        log.error("leader_profiles ignored: they are paper-only and this bot runs with paper: false")
        return {}
    out = {}
    for leader, p in raw.items():
        mode = p.get("mode", "trail")
        if mode not in MODES:
            raise ValueError(f"leader_profiles.{leader}: unknown mode {mode!r} (known: {MODES})")
        trail, ladder = mode == "trail", mode == "mirror_ladder"
        prof = Profile(leader=leader, name=str(p.get("name") or f"{mode}_{leader[:4]}"), mode=mode,
                       trailing_activate=float(p["trailing_activate"]) if trail else 0.0,
                       trailing_pct=float(p["trailing_pct"]) if trail else 0.0,
                       stop_loss=float(p["stop_loss"]), target_usd=float(p["target_usd"]),
                       his_median_usd=float(p["his_median_usd"]),
                       max_usd_per_token=float(p.get("max_usd_per_token", 1000)),
                       min_usd=float(p.get("min_usd", 25)), mark_interval_s=float(p.get("mark_interval_s", 30)),
                       max_open_tokens=int(p.get("max_open_tokens", 10)),
                       daily_loss_usd=float(p.get("daily_loss_usd", 500)),
                       ladder_levels=tuple(sorted(float(x) for x in p.get("ladder_levels", [10, 25, 50, 100]))) if ladder else (),
                       ladder_pct=float(p.get("ladder_pct", 0.25)) if ladder else 0.0)
        if not (0 < prof.stop_loss < 1 and prof.his_median_usd > 0) or (trail and not 0 < prof.trailing_pct < 1):
            raise ValueError(f"leader_profiles.{leader}: trailing_pct / stop_loss must be in (0, 1)")
        if not trail and ("trailing_pct" in p or "trailing_activate" in p or "take_profit" in p):
            raise ValueError(f"leader_profiles.{leader}: mode {mode} has no trailing / take-profit")
        if ladder and not (0 < prof.ladder_pct <= 1 and prof.ladder_levels and min(prof.ladder_levels) > 1):
            raise ValueError(f"leader_profiles.{leader}: ladder_pct must be in (0, 1], ladder_levels > 1")
        if prof.max_open_tokens < 1 or prof.daily_loss_usd <= 0:
            raise ValueError(f"leader_profiles.{leader}: max_open_tokens >= 1, daily_loss_usd > 0")
        out[leader] = prof
        rule = (f"trail {100 * prof.trailing_pct:.0f}% after +{100 * prof.trailing_activate:.0f}%, stop" if trail
                else f"his sells mirrored pro rata + ladder {100 * prof.ladder_pct:.0f}% at "
                     f"{'/'.join(f'{x:g}' for x in prof.ladder_levels)}x entry, safety stop" if ladder
                else "his sells mirrored pro rata, safety stop")
        log.info("leader profile %s for %s: %s, %s -%.0f%%, size %.3fx his ($%.0f / $%.0f), cap $%.0f/token incl. "
                 "adds, min $%.0f, max %d open tokens, daily loss $%.0f", prof.name, leader[:8], mode, rule,
                 100 * prof.stop_loss, prof.factor, prof.target_usd, prof.his_median_usd, prof.max_usd_per_token,
                 prof.min_usd, prof.max_open_tokens, prof.daily_loss_usd)
    return out
