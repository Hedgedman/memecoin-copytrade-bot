from __future__ import annotations
import os
import time
from .store import Store


class RiskManager:
    """Hard limits checked before every buy. Any failure blocks the trade."""

    def __init__(self, cfg: dict, store: Store, paper: bool):
        self.cfg, self.store, self.paper = cfg, store, paper

    def can_open(self, chain: str, token: str, open_positions: list, in_flight: set,
                 chain_cfg: dict) -> tuple[bool, str]:
        if os.path.exists(self.cfg.get("kill_switch_file", "STOP")):
            return False, "kill switch file present"
        if len(open_positions) + len(in_flight) >= int(self.cfg.get("max_open_positions", 3)):
            return False, "max open positions"
        if any(p.token == token for p in open_positions) or (chain, token) in in_flight:
            return False, "already in this token"
        last = self.store.last_exit(chain, token)
        if last and time.time() - last < float(self.cfg.get("reentry_cooldown_s", 1800)):
            return False, "re-entry cooldown"
        limit = float(chain_cfg.get("daily_loss_limit", 0))
        if limit and self.store.realized_pnl_today(chain, self.paper) <= -limit:
            return False, "daily loss limit hit"
        return True, "ok"
