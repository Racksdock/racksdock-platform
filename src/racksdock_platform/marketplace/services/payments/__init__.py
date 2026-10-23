"""Payments: how credits (USD cents on the account) get bought.

- tao.py          TAO sent straight to the operator's treasury coldkey; a watcher reads finalized blocks and credits transfers
                  from wallets users linked (wallets.py). No hot keys: the treasury only receives.
- nowpayments.py  every other coin through NOWPayments' hosted checkout; its webhook (re-checked against its API) credits the deposit.
- ledger.py       the Deposit rows, crediting, and the testnet "free credits" stub.
- rates.py        TAO/USD for converting a transfer into cents.
"""
from __future__ import annotations

import importlib.util
import logging

from .nowpayments import NowPayments
from .rates import Rates
from .tao import MemoryTao, SubtensorTao, TaoWatcher
from .wallets import SDK_HINT

log = logging.getLogger("racksdock.payments")


class Payments:
    """Built once per app: the rate feed, the TAO watcher (when a treasury is set) and the processor (when its keys are set)."""

    def __init__(self, settings, db):
        self.settings = settings
        self.rates = Rates(fixed=settings.tao_usd)
        self.processor = NowPayments(settings.nowpayments_api_key, settings.nowpayments_ipn_secret, settings.nowpayments_sandbox)
        self.tao: TaoWatcher | None = None
        if settings.tao_treasury and importlib.util.find_spec("bittensor") is None:
            log.error("RACKSDOCK_TAO_TREASURY is set but TAO payments stay off: %s", SDK_HINT)
        elif settings.tao_treasury:
            chain = MemoryTao() if settings.chain_kind == "mock" else SubtensorTao(settings.chain_network)
            self.tao = TaoWatcher(db, chain, settings.tao_treasury, self.rates, settings.tao_start_block)

    def methods(self) -> dict:
        """What the billing page offers."""
        st = self.settings
        rate = self.rates.tao_usd() if self.tao else None
        return {"min_cents": st.min_deposit_cents,
                "tao": {"enabled": self.tao is not None, "treasury": st.tao_treasury, "network": st.chain_network if self.tao else "",
                        "rate_usd": str(rate) if rate is not None else None},
                "crypto": {"enabled": self.processor.enabled, "provider": "NOWPayments"},
                "free": {"enabled": st.free_credits_enabled}}

    def start(self) -> None:
        if self.tao is not None and self.tao.chain.network != "mock":
            self.tao.start()

    def stop(self) -> None:
        if self.tao is not None:
            self.tao.stop()
