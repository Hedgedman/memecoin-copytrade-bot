from __future__ import annotations
import logging
import os
import time
from .store import Store

log = logging.getLogger("risk")


class RiskManager:
    """Hard limits checked before every buy. Any failure blocks the trade."""

    def __init__(self, cfg: dict, store: Store, paper: bool):
        self.cfg, self.store, self.paper = cfg, store, paper
        self._limit_logged: str | None = None      # chain + UTC day the daily-loss warning was logged for

    def can_open(self, chain: str, token: str, open_positions: list, in_flight: set,
                 chain_cfg: dict) -> tuple[bool, str]:
        if os.path.exists(self.cfg.get("kill_switch_file", "STOP")):
            return False, "kill switch file present"
        if len(open_positions) + len(in_flight) >= int(self.cfg.get("max_open_positions", 3)):
            return False, "max open positions"
        if any(p.token == token for p in open_positions) or (chain, token) in in_flight:
            return False, "already in this token"
        # one_entry_per_token: a token we held once (any exit, win or loss) is never bought again. The positions
        # table is the record, so this survives restarts.
        if chain_cfg.get("one_entry_per_token") and self.store.has_traded(chain, token, self.paper):
            return False, "already traded this token"
        last = self.store.last_exit(chain, token)
        if last and time.time() - last < float(self.cfg.get("reentry_cooldown_s", 1800)):
            return False, "re-entry cooldown"
        limit = float(chain_cfg.get("daily_loss_limit", 0))
        if limit:
            lost = self.store.realized_pnl_today(chain, self.paper)     # closed normal positions since 00:00 UTC
            if lost <= -limit:
                day = f"{chain}:{time.strftime('%Y-%m-%d', time.gmtime())}"
                if self._limit_logged != day:
                    self._limit_logged = day
                    log.warning("DAILY LOSS LIMIT HIT on %s: realized today %+.4f <= -%.2f (%s). No new buys until "
                                "00:00 UTC; open positions keep their exits.", chain, lost, limit,
                                "paper" if self.paper else "LIVE")
                return False, "daily loss limit hit"
        return True, "ok"
