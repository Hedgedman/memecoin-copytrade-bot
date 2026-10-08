#!/bin/zsh
# Full offline test suite (each test module prints ALL PASSED or its own final line). Exit 1 if any fails.
cd "$(dirname "$0")" && source .venv/bin/activate
fail=0; n=0
for t in core pricing detection live_correction trailing confirm vault min_mc first_buy events evm xchain xchain_paper xchain_multi whatif_leader profiles mirror leader_report whatif_profiles inspect transfer_hook v1 data_quality ticks_data h6 profile_separation leader_status secrets live_prep pumpswap_virtual; do
  [ -f tests/test_$t.py ] || continue
  n=$((n+1))
  out=$(python -m tests.test_$t 2>&1); rc=$?
  last=$(echo "$out" | tail -1)
  if [ $rc -ne 0 ]; then fail=$((fail+1)); echo "FAIL $t"; echo "$out" | tail -5; else echo "ok   $t: $last"; fi
done
echo "$n test modules, $fail failed"; [ $fail -eq 0 ]
