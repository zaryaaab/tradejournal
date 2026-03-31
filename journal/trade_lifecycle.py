"""
Trade import: full position lifecycle (FIFO), partial scale-in/out, P&L per round trip.

CSV column reference (inferred from parsers — no sample files in repo):
- Rithmic "Completed Orders" section: Status, Remarks, Update Time (CST), Buy/Sell,
  Qty To Fill, Symbol, Avg Fill Price, P&L or PnL. Optional position/order ids are
  not used; grouping is FIFO by (account, normalized symbol) only.
- Tradovate: Status, Fill Time, B/S, filledQty, Product, avgPrice, pnl.

Grouping policy: one open position per normalized symbol per account in import batch;
round trip closes when net quantity returns to zero. Display: weighted average entry/exit
price, first opening time, last closing time, peak contracts as quantity. P&L: sum of
broker per-fill P&L slices attributed to closing legs when present; else FIFO price
difference × multiplier × contracts (same as legacy fallback).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import date, datetime
from typing import List, Optional, Tuple

from .time_utils import ensure_chicago_datetime


# --- Symbol / multiplier (mirrors previous parser logic) -----------------


def rithmic_symbol_and_mult(symbol_raw: str) -> Tuple[str, float]:
    s = str(symbol_raw).strip()
    if "MNQ" in s:
        return "MNQ", 2.0
    if "MES" in s:
        return "MES", 5.0
    if "NQ" in s:
        return "NQ", 20.0
    if "ES" in s:
        return "ES", 50.0
    return s, 1.0


def tradovate_symbol_and_mult(product: str) -> Tuple[str, float]:
    s = str(product).strip()
    if s == "MNQ":
        return "MNQ", 2.0
    if s == "MES":
        return "MES", 5.0
    if s == "NQ":
        return "NQ", 20.0
    if s == "ES":
        return "ES", 50.0
    return s, 1.0


def parse_broker_pnl_cell(raw) -> Optional[float]:
    if raw is None:
        return None
    t = str(raw).strip()
    if not t or t.lower() == "nan":
        return None
    is_neg = t.startswith("(") and t.endswith(")")
    cleaned = t.replace("$", "").replace(",", "").replace("(", "").replace(")", "")
    try:
        v = float(cleaned)
        return -v if is_neg else v
    except ValueError:
        return None


# --- Lifecycle engine ------------------------------------------------------


@dataclass
class NormalizedFill:
    ts: datetime
    is_buy: bool
    qty: int
    price: float
    symbol: str
    multiplier: float
    broker_pnl: Optional[float] = None
    is_auto_liq: bool = False


@dataclass
class CompletedRoundTrip:
    symbol: str
    side: str
    quantity: int
    entry_price: float
    exit_price: float
    entry_time: datetime
    exit_time: datetime
    pnl: float
    journal_date: date
    is_auto_liq: bool


def _fifo_realized_pnl_long(
    lots: deque, qty_to_close: int, exit_px: float, mult: float
) -> float:
    rem = qty_to_close
    pnl = 0.0
    while rem > 0 and lots:
        lq, lp = lots[0]
        take = min(lq, rem)
        pnl += (exit_px - lp) * mult * take
        if take == lq:
            lots.popleft()
        else:
            lots[0] = (lq - take, lp)
        rem -= take
    return pnl


def _fifo_realized_pnl_short(
    lots: deque, qty_to_close: int, cover_px: float, mult: float
) -> float:
    rem = qty_to_close
    pnl = 0.0
    while rem > 0 and lots:
        lq, lp = lots[0]
        take = min(lq, rem)
        pnl += (lp - cover_px) * mult * take
        if take == lq:
            lots.popleft()
        else:
            lots[0] = (lq - take, lp)
        rem -= take
    return pnl


class PositionBook:
    """FIFO book for one symbol; emits CompletedRoundTrip when flat."""

    __slots__ = (
        "net",
        "lots",
        "trip_open",
        "trip_close",
        "trip_pnl",
        "trip_auto_liq",
        "peak",
        "trip_side",
    )

    def __init__(self) -> None:
        self.net = 0
        self.lots: deque = deque()
        self.trip_open: List[Tuple[int, float, datetime]] = []
        self.trip_close: List[Tuple[int, float, datetime]] = []
        self.trip_pnl = 0.0
        self.trip_auto_liq = False
        self.peak = 0
        self.trip_side = "B"

    def _reset_trip(self) -> None:
        self.trip_open.clear()
        self.trip_close.clear()
        self.trip_pnl = 0.0
        self.trip_auto_liq = False
        self.peak = 0
        self.lots.clear()
        self.net = 0

    def _open_initial(self, is_buy: bool, qty: int, price: float, ts, f: NormalizedFill) -> None:
        self.trip_side = "B" if is_buy else "S"
        self.trip_open = [(qty, price, ts)]
        self.trip_close = []
        self.trip_pnl = 0.0
        self.trip_auto_liq = bool(f.is_auto_liq)
        self.lots = deque([(qty, price)])
        self.net = qty if is_buy else -qty
        self.peak = abs(self.net)

    def _emit(self, symbol: str) -> CompletedRoundTrip:
        total_oq = sum(x[0] for x in self.trip_open)
        total_cq = sum(x[0] for x in self.trip_close)
        entry_price = sum(x[0] * x[1] for x in self.trip_open) / total_oq
        exit_price = sum(x[0] * x[1] for x in self.trip_close) / total_cq
        entry_time = ensure_chicago_datetime(self.trip_open[0][2])
        exit_time = ensure_chicago_datetime(self.trip_close[-1][2])
        return CompletedRoundTrip(
            symbol=symbol,
            side=self.trip_side,
            quantity=int(self.peak),
            entry_price=entry_price,
            exit_price=exit_price,
            entry_time=entry_time,
            exit_time=exit_time,
            pnl=round(self.trip_pnl, 2),
            journal_date=entry_time.date(),
            is_auto_liq=self.trip_auto_liq,
        )

    def apply_fill(self, f: NormalizedFill) -> List[CompletedRoundTrip]:
        completed: List[CompletedRoundTrip] = []
        if f.qty <= 0:
            return completed

        if self.net == 0:
            self._open_initial(f.is_buy, f.qty, f.price, f.ts, f)
            return completed

        if (self.net > 0 and f.is_buy) or (self.net < 0 and not f.is_buy):
            self.trip_open.append((f.qty, f.price, f.ts))
            self.lots.append((f.qty, f.price))
            self.net += f.qty if f.is_buy else -f.qty
            self.peak = max(self.peak, abs(self.net))
            return completed

        close_qty = min(abs(self.net), f.qty)
        leftover = f.qty - close_qty

        broker_slice: Optional[float] = None
        if f.broker_pnl is not None and f.qty > 0:
            broker_slice = f.broker_pnl * (close_qty / f.qty)

        if self.net > 0:
            fifo_pnl = _fifo_realized_pnl_long(
                self.lots, close_qty, f.price, f.multiplier
            )
        else:
            fifo_pnl = _fifo_realized_pnl_short(
                self.lots, close_qty, f.price, f.multiplier
            )

        self.trip_pnl += broker_slice if broker_slice is not None else fifo_pnl
        self.trip_close.append((close_qty, f.price, f.ts))
        if f.is_auto_liq:
            self.trip_auto_liq = True

        if self.net > 0:
            self.net -= close_qty
        else:
            self.net += close_qty

        if self.net == 0:
            completed.append(self._emit(f.symbol))
            self._reset_trip()

        if leftover > 0:
            self._open_initial(f.is_buy, leftover, f.price, f.ts, f)

        return completed


def process_fills_for_symbol(
    fills: List[NormalizedFill],
) -> Tuple[List[CompletedRoundTrip], int]:
    """Returns (completed round trips, ending net position; nonzero means open lots left)."""
    if not fills:
        return [], 0
    sym = fills[0].symbol
    book = PositionBook()
    out: List[CompletedRoundTrip] = []
    for f in sorted(fills, key=lambda x: x.ts):
        if f.symbol != sym:
            raise ValueError("Mixed symbols in process_fills_for_symbol")
        out.extend(book.apply_fill(f))
    return out, book.net


def persist_round_trip(account, c: CompletedRoundTrip):
    """Insert one Trade if not already present (same dedupe keys as legacy)."""
    from .models import Trade

    return Trade.objects.get_or_create(
        account=account,
        symbol=c.symbol,
        entry_time=c.entry_time,
        exit_time=c.exit_time,
        entry_price=c.entry_price,
        exit_price=c.exit_price,
        defaults={
            "side": c.side,
            "quantity": c.quantity,
            "pnl": c.pnl,
            "date": c.journal_date,
            "is_auto_liq": c.is_auto_liq,
        },
    )
