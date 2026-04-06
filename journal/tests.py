from datetime import datetime
from zoneinfo import ZoneInfo

from django.test import SimpleTestCase

from .trade_lifecycle import (
    NormalizedFill,
    parse_fill_qty,
    process_fills_for_symbol,
    rithmic_side_to_is_buy,
    rithmic_symbol_and_mult,
    tradovate_side_to_is_buy,
    tradovate_symbol_and_mult,
)

CHI = ZoneInfo("America/Chicago")


def _dt(h: int, m: int = 0, s: int = 0) -> datetime:
    return datetime(2025, 6, 15, h, m, s, tzinfo=CHI)


class PartialExitRoundTripTests(SimpleTestCase):
    """One open position with partial exits must yield exactly one CompletedRoundTrip when flat."""

    def test_long_partial_exits_single_trip_fifo_pnl(self):
        fills = [
            NormalizedFill(_dt(9, 30), True, 10, 100.0, "MNQ", 2.0, seq=0),
            NormalizedFill(_dt(9, 31), False, 3, 101.0, "MNQ", 2.0, seq=1),
            NormalizedFill(_dt(9, 32), False, 4, 102.0, "MNQ", 2.0, seq=2),
            NormalizedFill(_dt(9, 33), False, 3, 103.0, "MNQ", 2.0, seq=3),
        ]
        completed, tail = process_fills_for_symbol(fills)
        self.assertEqual(tail, 0)
        self.assertEqual(len(completed), 1)
        c = completed[0]
        self.assertEqual(c.symbol, "MNQ")
        self.assertEqual(c.side, "B")
        self.assertEqual(c.quantity, 10)
        self.assertAlmostEqual(c.entry_price, 100.0)
        self.assertAlmostEqual(c.exit_price, 102.0)
        self.assertEqual(c.exit_time, _dt(9, 33))
        # FIFO PnL: (101-100)*2*3 + (102-100)*2*4 + (103-100)*2*3
        self.assertAlmostEqual(c.pnl, 40.0)

    def test_short_partial_covers_single_trip_fifo_pnl(self):
        fills = [
            NormalizedFill(_dt(10, 0), False, 10, 100.0, "MES", 5.0, seq=0),
            NormalizedFill(_dt(10, 1), True, 3, 99.0, "MES", 5.0, seq=1),
            NormalizedFill(_dt(10, 2), True, 4, 98.0, "MES", 5.0, seq=2),
            NormalizedFill(_dt(10, 3), True, 3, 97.0, "MES", 5.0, seq=3),
        ]
        completed, tail = process_fills_for_symbol(fills)
        self.assertEqual(tail, 0)
        self.assertEqual(len(completed), 1)
        c = completed[0]
        self.assertEqual(c.side, "S")
        self.assertEqual(c.quantity, 10)
        self.assertAlmostEqual(c.entry_price, 100.0)
        self.assertAlmostEqual(c.exit_price, 98.0)
        # Short FIFO: (100-99)*5*3 + (100-98)*5*4 + (100-97)*5*3 = 15+40+45
        self.assertAlmostEqual(c.pnl, 100.0)

    def test_same_timestamp_sorted_by_seq(self):
        t = _dt(11, 0)
        # Intentionally append sell before buy; seq restores buy-then-sell for a flat long.
        fills = [
            NormalizedFill(t, False, 5, 100.0, "MNQ", 2.0, seq=1),
            NormalizedFill(t, True, 5, 99.0, "MNQ", 2.0, seq=0),
        ]
        completed, tail = process_fills_for_symbol(fills)
        self.assertEqual(tail, 0)
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0].side, "B")


class SymbolNormalizationTests(SimpleTestCase):
    def test_tradovate_product_variants_bucket_with_root(self):
        self.assertEqual(tradovate_symbol_and_mult("MNQH5")[0], "MNQ")
        self.assertEqual(tradovate_symbol_and_mult("MESZ4")[0], "MES")
        self.assertEqual(tradovate_symbol_and_mult("  nq  ")[0], "NQ")

    def test_rithmic_symbol_whitespace_and_suffix(self):
        self.assertEqual(rithmic_symbol_and_mult("  MNQ 03-25 ")[0], "MNQ")
        self.assertEqual(rithmic_symbol_and_mult("Micro ESH5")[0], "ES")


class SideAndQtyParsingTests(SimpleTestCase):
    def test_rithmic_side(self):
        self.assertTrue(rithmic_side_to_is_buy("Buy"))
        self.assertTrue(rithmic_side_to_is_buy("B"))
        self.assertFalse(rithmic_side_to_is_buy("S"))
        self.assertFalse(rithmic_side_to_is_buy("Sell"))

    def test_tradovate_side_case_insensitive(self):
        self.assertTrue(tradovate_side_to_is_buy("Buy"))
        self.assertTrue(tradovate_side_to_is_buy("BUY"))
        self.assertFalse(tradovate_side_to_is_buy("Sell"))

    def test_parse_fill_qty_abs(self):
        self.assertEqual(parse_fill_qty(5), 5)
        self.assertEqual(parse_fill_qty(-5), 5)
        self.assertEqual(parse_fill_qty(0), 0)
        self.assertEqual(parse_fill_qty("bad"), 0)
