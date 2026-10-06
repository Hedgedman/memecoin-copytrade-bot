import asyncio, sys, yaml
from bot.chains.solana import SolanaAdapter

async def main(sig, wallet):
    cfg = yaml.safe_load(open("config.yaml"))["chains"]["solana"]
    a = SolanaAdapter(cfg, paper=True)
    await a.start()
    try:
        q = asyncio.Queue()
        await a._handle(sig, wallet, q)
        if q.empty():
            print("1. detection: NOT recognised (or below min_leader_buy_sol)")
            return
        s = q.get_nowait()
        print(f"1. detection: {s.side} {s.token}  ~{s.quote_amount:.3f} SOL-equiv  OK")
        if s.side != "buy":
            return
        ok, why = await a.pre_trade_check(s.token, float(cfg["buy_amount"]))
        print(f"2. safety check: {'PASS' if ok else 'SKIP'} - {why}")
        if ok:
            fill = await a.buy(s.token, float(cfg["buy_amount"]))
            print(f"3. paper buy: {'OK' if fill.ok else 'FAILED'} - tokens {fill.token_amount}, "
                  f"cost {fill.quote_amount:.4f} SOL {fill.error or ''}")
    finally:
        await a.close()

asyncio.run(main(sys.argv[1], sys.argv[2]))
