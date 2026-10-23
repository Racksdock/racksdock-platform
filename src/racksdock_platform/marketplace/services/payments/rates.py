"""TAO/USD for crediting transfers: the median of public quotes, cached for a minute; a pinned rate for test networks."""
from __future__ import annotations

import logging
import statistics
import time
from decimal import Decimal

import httpx

log = logging.getLogger("racksdock.payments.rates")

# name -> (url, extract the USD price from the JSON)
SOURCES = {
    "coingecko": ("https://api.coingecko.com/api/v3/simple/price?ids=bittensor&vs_currencies=usd", lambda j: j["bittensor"]["usd"]),
    "kraken": ("https://api.kraken.com/0/public/Ticker?pair=TAOUSD", lambda j: next(iter(j["result"].values()))["c"][0]),
    "binance": ("https://api.binance.com/api/v3/ticker/price?symbol=TAOUSDT", lambda j: j["price"]),
}
MAX_SPREAD = Decimal("0.05")  # a quote more than 5% from the median is dropped


class Rates:
    def __init__(self, fixed: float | None = None, ttl_s: float = 60.0, fetch=None):
        self.fixed = Decimal(str(fixed)) if fixed is not None else None
        self.ttl_s = ttl_s
        self._fetch = fetch or self._http
        self._cached: tuple[float, Decimal | None] = (0.0, None)

    @staticmethod
    def _http(url: str):
        r = httpx.get(url, timeout=8, headers={"User-Agent": "racksdock-platform"})
        r.raise_for_status()
        return r.json()

    def quotes(self) -> dict[str, Decimal]:
        out = {}
        for name, (url, pick) in SOURCES.items():
            try:
                out[name] = Decimal(str(pick(self._fetch(url))))
            except Exception as e:  # one source down is normal
                log.info("rate source %s unavailable: %s", name, e)
        return out

    def tao_usd(self) -> Decimal | None:
        """USD per TAO, or None when no source answers (the deposit then waits for the next scan)."""
        if self.fixed is not None:
            return self.fixed
        at, value = self._cached
        if value is not None and time.time() - at < self.ttl_s:
            return value
        q = self.quotes()
        if not q:
            return value  # stale is better than nothing while every source is down
        med = statistics.median(q.values())
        close = [v for v in q.values() if abs(v - med) <= med * MAX_SPREAD]
        value = statistics.median(close) if close else med
        value = value.quantize(Decimal("0.01"))
        self._cached = (time.time(), value)
        return value
