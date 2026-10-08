"""whatif_ticksv2 profile filter: python -m tests.test_whatif_profiles

Positions opened under a leader profile (timing["profile"]) are excluded from the what-if and the venue split by
default (--exclude-profiles), listed as (id, leader, profile); --profile NAME keeps only that profile;
--no-exclude-profiles includes everything. The same filter runs in load() and venue_groups()."""
import json
import os
import tempfile

from bot.store import Store
from whatif_ticksv2 import keep_profile, load, venue_groups

L1, L2 = "LEADER_ONE_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "Leader-B"
T0 = 1_700_000_000.0
FEES = {"pumpfun_curve": 125, "pumpswap_amm": 100}


def seed(path):
    db = Store(path).db
    rows = [  # id, leader, timing: #231 and #233 carry a profile stamp
        (230, L1, {"fill_model": "pumpswap_amm+0.8s"}),
        (231, L2, {"fill_model": "pumpswap_amm+0.8s", "profile": "trail_Leader-B", "exits": {"mode": "trail"}}),
        (232, L1, {"fill_model": "jupiter+0.8s"}),
        (233, L1, {"fill_model": "pumpswap_amm+0.8s", "profile": "mirror_Leader-A", "exits": {"mode": "mirror"}}),
    ]
    for pid, leader, timing in rows:
        tok = f"TOK{pid}"
        db.execute("INSERT INTO positions(id,chain,token,leader,token_amount,cost,opened_at,paper,status,exit_value,"
                   "closed_at,exit_reason,timing) VALUES(?,'solana',?,?,'1000',2.0,?,1,'closed',1.6,?,'stop_loss',?)",
                   (pid, tok, leader, T0 + pid, T0 + pid + 60, json.dumps(timing)))
        for dt in (0, 10, 30):
            db.execute("INSERT INTO ticks VALUES(?,?,?,?,?,?,?,?)", (T0 + pid + dt, 1, tok, "pumpswap_amm", 10**12, 10**11, 0, 0))
    db.commit()
    db.close()


def main():
    assert keep_profile('{"profile": "trail_Leader-B"}', "", True) is False
    assert keep_profile('{}', "", True) is True and keep_profile(None, "", True) is True
    assert keep_profile('{"profile": "trail_Leader-B"}', "", False) is True            # --no-exclude-profiles
    assert keep_profile('{"profile": "trail_Leader-B"}', "trail_Leader-B", True) is True   # --profile wins
    assert keep_profile('{}', "trail_Leader-B", False) is False and keep_profile('{}', "default", True) is True

    path = os.path.join(tempfile.mkdtemp(), "t.db")
    seed(path)
    ids = lambda data: sorted(p.pid for p in data)
    data, skipped, mc_out, excluded, _ = load(path, 230, FEES, 0.01, 3600, exclude_profiles=True)
    assert ids(data) == [230, 232] and skipped == 0 and mc_out == 0
    assert excluded == [(231, L2, "trail_Leader-B"), (233, L1, "mirror_Leader-A")], excluded
    data, _, _, excluded, _ = load(path, 230, FEES, 0.01, 3600, exclude_profiles=False)
    assert ids(data) == [230, 231, 232, 233] and excluded == []
    data, _, _, excluded, _ = load(path, 230, FEES, 0.01, 3600, profile="trail_Leader-B", exclude_profiles=True)
    assert ids(data) == [231] and len(excluded) == 3
    data, _, _, _, _ = load(path, 230, FEES, 0.01, 3600, profile="default")
    assert ids(data) == [230, 232]
    data, _, _, _, _ = load(path, 232, FEES, 0.01, 3600, exclude_profiles=True)      # --from-id still applies
    assert ids(data) == [232]

    # venue split: the same filter, the same list
    data, *_ = load(path, 230, FEES, 0.01, 3600, exclude_profiles=True)
    groups, excluded = venue_groups(path, 230, False, data, None, None, exclude_profiles=True)
    assert sorted(pid for g in groups.values() for pid, *_ in g) == [230, 232]
    assert excluded == [(231, L2, "trail_Leader-B"), (233, L1, "mirror_Leader-A")]
    assert [pid for pid, *_ in groups["jupiter"]] == [232]
    groups, excluded = venue_groups(path, 230, False, data, None, None, exclude_profiles=False)
    assert sorted(pid for g in groups.values() for pid, *_ in g) == [230, 231, 232, 233] and excluded == []
    print("whatif profile filter: excluded by default + listed, --no-exclude-profiles, --profile, venue split OK")
    print("ALL PASSED")


if __name__ == "__main__":
    main()
