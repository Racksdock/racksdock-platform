"""TAO deposits: transfers to the treasury coldkey, read from finalized blocks and credited to the linked sender.

The treasury is any coldkey the operator keeps cold; the platform never holds a key that can spend. A `TaoChain` gives the
finalized head and the Balances.Transfer events of a block (SubtensorTao over the bittensor SDK; MemoryTao for tests).
`TaoWatcher.scan_once` walks new blocks, records every transfer to the treasury and credits those whose sender a user
linked; a transfer from an unknown wallet is kept as `unmatched` and credited the moment that wallet is linked."""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from sqlalchemy import select

from ...models import Deposit, LinkedWallet, Setting, now_iso
from . import ledger
from .rates import Rates

log = logging.getLogger("racksdock.payments.tao")

ENDPOINT_NETWORKS = {"finney": "finney", "test": "test", "local": "local"}
LAST_BLOCK_KEY = "tao_last_block"
MAX_BLOCKS_PER_SCAN = 300


@dataclass(frozen=True)
class Transfer:
    block: int
    index: int  # position of the event in the block
    sender: str
    recipient: str
    rao: int

    @property
    def ref(self) -> str:
        return f"{self.block}:{self.index}"


class TaoChain(Protocol):
    network: str

    def finalized_block(self) -> int: ...

    def transfers(self, block: int) -> list[Transfer]: ...

    def reset(self) -> None: ...


class SubtensorTao:
    """Reads over the bittensor SDK; the connection is opened lazily and dropped on error so the next scan reconnects."""

    def __init__(self, network: str):
        self.network = network
        self._st = None

    def _client(self):
        if self._st is None:
            import bittensor as bt

            self._st = bt.Subtensor(network=self.network)
        return self._st

    def finalized_block(self) -> int:
        return int(self._client().finalized_block)

    def transfers(self, block: int) -> list[Transfer]:
        events = self._client().query(("System", "Events"), block=block)
        out = []
        for i, e in enumerate(events):
            if e.get("module_id") == "Balances" and e.get("event_id") == "Transfer":
                a = e.get("attributes") or {}
                out.append(Transfer(block, i, str(a.get("from", "")), str(a.get("to", "")), int(a.get("amount", 0))))
        return out

    def reset(self) -> None:
        st, self._st = self._st, None
        if st is not None:
            try:
                st.close()
            except Exception:
                pass


class MemoryTao:
    """A chain for tests: transfers are added by hand and the head is the highest block seen."""

    network = "mock"

    def __init__(self):
        self.head = 0
        self._blocks: dict[int, list[Transfer]] = {}

    def add_transfer(self, block: int, sender: str, recipient: str, rao: int) -> Transfer:
        t = Transfer(block, len(self._blocks.setdefault(block, [])), sender, recipient, rao)
        self._blocks[block].append(t)
        self.head = max(self.head, block)
        return t

    def finalized_block(self) -> int:
        return self.head

    def transfers(self, block: int) -> list[Transfer]:
        return list(self._blocks.get(block, []))

    def reset(self) -> None:
        pass


class TaoWatcher:
    def __init__(self, db, chain: TaoChain, treasury: str, rates: Rates, start_block: int | None = None, interval_s: float = 12.0):
        self.db, self.chain, self.treasury, self.rates, self.start_block, self.interval_s = db, chain, treasury, rates, start_block, interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- one pass ---------------------------------------------------------------------------------------------------------
    def scan_once(self) -> int:
        """Record and credit transfers in the finalized blocks not seen yet. Returns the number of blocks scanned."""
        head = self.chain.finalized_block()
        with self.db.session() as s:
            row = s.get(Setting, LAST_BLOCK_KEY)
            last = int(row.value) if row else (self.start_block if self.start_block else head) - 1  # a fresh database starts at the current head
        scanned = 0
        for block in range(last + 1, min(head, last + MAX_BLOCKS_PER_SCAN) + 1):
            transfers = [t for t in self.chain.transfers(block) if t.recipient == self.treasury]
            with self.db.session() as s:
                for t in transfers:
                    self.record(s, t)
                row = s.get(Setting, LAST_BLOCK_KEY)
                if row is None:
                    s.add(Setting(key=LAST_BLOCK_KEY, value=str(block)))
                else:
                    row.value = str(block)
            scanned += 1
        with self.db.session() as s:
            self.settle_waiting(s)
            ledger.expire_intents(s)
        return scanned

    def record(self, s, t: Transfer) -> Deposit | None:
        if s.scalar(select(Deposit).where(Deposit.method == "tao", Deposit.external_ref == t.ref)) is not None:
            return None  # seen already (a rescan after a restart)
        wallet = s.get(LinkedWallet, t.sender)
        dep = None
        if wallet is not None:  # the user's oldest open intent, if any, becomes this transfer
            dep = s.scalar(select(Deposit).where(Deposit.user_id == wallet.user_id, Deposit.method == "tao", Deposit.status == "pending")
                           .order_by(Deposit.created_at))
        if dep is None:
            dep = Deposit(user_id=wallet.user_id if wallet else None, method="tao")
            s.add(dep)
        dep.external_ref, dep.from_address, dep.asset, dep.amount = t.ref, t.sender, "TAO", ledger.tao_str(t.rao)
        dep.status = "confirming" if wallet else "unmatched"
        dep.note = f"{dep.amount} τ received in block {t.block}" + ("" if wallet else " from a wallet that is not linked to any account")
        dep.updated_at = now_iso()
        s.flush()
        if wallet is not None:
            self.settle(s, dep)
        log.info("tao transfer %s: %s τ from %s -> %s", t.ref, dep.amount, t.sender, dep.status)
        return dep

    def settle(self, s, dep: Deposit) -> bool:
        """Credit a received transfer at today's rate; False (and `confirming`) while no price source answers."""
        rate = self.rates.tao_usd()
        if rate is None or dep.user_id is None:
            return False
        rao = int(Decimal(dep.amount) * ledger.RAO)
        dep.rate = str(rate)
        ledger.credit(s, dep, ledger.cents_for_rao(rao, rate), f"{dep.amount} τ at ${rate} / τ (block {dep.external_ref.split(':')[0]})")
        return True

    def settle_waiting(self, s) -> int:
        n = 0
        for dep in s.scalars(select(Deposit).where(Deposit.method == "tao", Deposit.status == "confirming", Deposit.user_id.is_not(None))).all():
            n += self.settle(s, dep)
        return n

    def claim(self, s, user_id: str, address: str) -> list[Deposit]:
        """Transfers from `address` that arrived before it was linked now belong to `user_id`."""
        out = []
        for dep in s.scalars(select(Deposit).where(Deposit.method == "tao", Deposit.status == "unmatched", Deposit.from_address == address)).all():
            dep.user_id, dep.status, dep.updated_at = user_id, "confirming", now_iso()
            dep.note = f"{dep.amount} τ received in block {dep.external_ref.split(':')[0]}"
            self.settle(s, dep)
            out.append(dep)
        return out

    # ---- background thread ------------------------------------------------------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="tao-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        log.info("tao watcher: treasury %s on %s", self.treasury, self.chain.network)
        while not self._stop.is_set():
            try:
                self.scan_once()
            except Exception as e:
                log.warning("tao watcher: %s (reconnecting)", e)
                self.chain.reset()
            self._stop.wait(self.interval_s)
