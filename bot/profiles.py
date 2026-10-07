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

Every other leader keeps the global exits / buy_amount / xchain paper settings. The profile's exits are
copied into each position (timing["exits"]) together with timing["profile"] and timing["size_factor"],
so a restart, the what-ifs and the reports all see what a position was opened with.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger("profiles")

MODES = ("trail",)


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

    @property
    def factor(self) -> float:
        return self.target_usd / self.his_median_usd

    def exits(self) -> dict:
        """Stored per position; PositionManager / XchainPaperTrader read it instead of the global exits."""
        return {"mode": self.mode, "take_profit": 0.0, "trailing_activate": self.trailing_activate,
                "trailing_pct": self.trailing_pct, "stop_loss": self.stop_loss, "max_hold_s": 0.0,
                "follow_leader_exit": False}

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
        prof = Profile(leader=leader, name=str(p.get("name") or f"{mode}_{leader[:4]}"), mode=mode,
                       trailing_activate=float(p["trailing_activate"]), trailing_pct=float(p["trailing_pct"]),
                       stop_loss=float(p["stop_loss"]), target_usd=float(p["target_usd"]),
                       his_median_usd=float(p["his_median_usd"]),
                       max_usd_per_token=float(p.get("max_usd_per_token", 1000)),
                       min_usd=float(p.get("min_usd", 25)), mark_interval_s=float(p.get("mark_interval_s", 30)))
        if not (0 < prof.trailing_pct < 1 and 0 < prof.stop_loss < 1 and prof.his_median_usd > 0):
            raise ValueError(f"leader_profiles.{leader}: trailing_pct / stop_loss must be in (0, 1)")
        out[leader] = prof
        log.info("leader profile %s for %s: %s, trail %.0f%% after +%.0f%%, stop -%.0f%%, size %.3fx his "
                 "($%.0f / $%.0f), cap $%.0f/token, min $%.0f", prof.name, leader[:8], mode, 100 * prof.trailing_pct,
                 100 * prof.trailing_activate, 100 * prof.stop_loss, prof.factor, prof.target_usd,
                 prof.his_median_usd, prof.max_usd_per_token, prof.min_usd)
    return out
