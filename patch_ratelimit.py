import re

def edit(path, old, new, marker=None):
    s = open(path).read()
    if (marker or new) in s:
        print(f"{path}: already done"); return
    assert old in s, f"{path}: could not find the spot -> send this error to Claude:\n{old[:80]}"
    open(path, "w").write(s.replace(old, new, 1)); print(f"{path}: ok")

# 1. base.py: a shared RateLimited error type
edit("bot/chains/base.py",
"class ChainAdapter(ABC):",
'''class RateLimited(Exception):
    """A price/route API refused the request because we called it too often."""


class ChainAdapter(ABC):''', marker="class RateLimited")

# 2. solana.py: import
edit("bot/chains/solana.py",
"from .base import ChainAdapter\n",
"from .base import ChainAdapter, RateLimited\n")

# 3. solana.py: throttle class
edit("bot/chains/solana.py",
"class SolanaAdapter(ChainAdapter):",
'''class _Throttle:
    """Spaces out Jupiter calls and backs off after a rate-limit response."""

    def __init__(self, max_rps: float):
        self.interval = 1.0 / max_rps if max_rps > 0 else 0.0
        self.next_ok = 0.0
        self.cooldown_until = 0.0
        self.backoff = 2.0
        self.lock = asyncio.Lock()

    async def wait(self):
        async with self.lock:
            t = max(self.next_ok, self.cooldown_until)
            now = time.time()
            if t > now:
                await asyncio.sleep(t - now)
            self.next_ok = time.time() + self.interval

    def hit_limit(self) -> float:
        pause = self.backoff
        self.cooldown_until = time.time() + pause
        self.backoff = min(self.backoff * 2, 30.0)
        return pause

    def ok(self):
        self.backoff = 2.0


class SolanaAdapter(ChainAdapter):''', marker="class _Throttle")

# 4. solana.py: create throttle in __init__
edit("bot/chains/solana.py",
"        self._sol_usd_val, self._sol_usd_ts = 0.0, 0.0\n",
"        self._sol_usd_val, self._sol_usd_ts = 0.0, 0.0\n"
"        self.throttle = _Throttle(float(cfg.get(\"jupiter_max_rps\", 1.0)))\n",
marker="self.throttle = _Throttle")

# 5. solana.py: quote with throttle + rate-limit detection
edit("bot/chains/solana.py",
'''        async with self.session.get(f"{self.jup}/quote", params=params, headers=self._headers()) as r:
            q = await r.json(content_type=None)
        if r.status != 200 or "outAmount" not in q:
            raise RuntimeError(f"quote failed: {q}")
        return q''',
'''        await self.throttle.wait()
        async with self.session.get(f"{self.jup}/quote", params=params, headers=self._headers()) as r:
            text = await r.text()
        if r.status == 429 or "rate limit" in text.lower():
            pause = self.throttle.hit_limit()
            log.warning("Jupiter rate limit hit - pausing quotes for %.0fs", pause)
            raise RateLimited("jupiter rate limit")
        self.throttle.ok()
        try:
            q = json.loads(text)
        except ValueError:
            raise RuntimeError(f"quote failed: {text[:200]}")
        if r.status != 200 or "outAmount" not in q:
            raise RuntimeError(f"quote failed: {q}")
        return q''', marker="raise RateLimited(\"jupiter rate limit\")")

# 6. solana.py: throttle the swap call too
edit("bot/chains/solana.py",
'''        async with self.session.post(f"{self.jup}/swap", json=body, headers=self._headers()) as r:''',
'''        await self.throttle.wait()
        async with self.session.post(f"{self.jup}/swap", json=body, headers=self._headers()) as r:''',
marker='''        await self.throttle.wait()
        async with self.session.post(f"{self.jup}/swap"''')

# 7. solana.py: quote_exit passes rate limits up instead of reporting "no route"
edit("bot/chains/solana.py",
'''            q = await self._quote(pos.token, WSOL, pos.token_amount)
            return int(q["outAmount"]) / LAMPORTS
        except Exception as e:''',
'''            q = await self._quote(pos.token, WSOL, pos.token_amount)
            return int(q["outAmount"]) / LAMPORTS
        except RateLimited:
            raise
        except Exception as e:''')

# 8. positions.py: import
edit("bot/positions.py",
"from .store import Store\n",
"from .store import Store\nfrom .chains.base import RateLimited\n")

# 9. positions.py: a rate limit is not a failed quote
edit("bot/positions.py",
"        value = await self.adapters[p.chain].quote_exit(p)\n",
'''        try:
            value = await self.adapters[p.chain].quote_exit(p)
        except RateLimited:
            return          # try again next round; not a sign the token is dead
''', marker="        except RateLimited:\n            return")

# 10. positions.py: log "no exit route" once, then once a minute, not every second
edit("bot/positions.py",
"            if p.quote_failures >= self.max_quote_failures:\n",
"            if p.quote_failures == self.max_quote_failures or (\n"
"                    p.quote_failures > self.max_quote_failures and p.quote_failures % 60 == 0):\n")

# 11. positions.py: a rate-limited sell doesn't escalate slippage
edit("bot/positions.py",
"            p.sell_attempts += 1\n",
"            if \"rate limit\" not in (fill.error or \"\"):\n                p.sell_attempts += 1\n")

# 12. config.yaml: the new setting
s = open("config.yaml").read()
if "jupiter_max_rps" in s:
    print("config.yaml: already done")
else:
    s2 = re.sub(r"(\n(\s*)jupiter_base:[^\n]*\n)", r"\1\2jupiter_max_rps: 1.0\n", s, count=1)
    assert s2 != s, "config.yaml: jupiter_base line not found"
    open("config.yaml", "w").write(s2); print("config.yaml: ok")
print("DONE")
