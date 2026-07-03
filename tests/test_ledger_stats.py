from decimal import Decimal

from pmresearch.cli.ingest import (
    SPORTS_FEE_CUTOFF_TS,
    _ledger_totals,
    _period_rows,
    _roi_on_buy_volume,
)


class Row:
    def __init__(self, event_type, side, delta_usdc, usdc_size, ts=0):
        self.event_type = event_type
        self.side = side
        self.delta_usdc = delta_usdc
        self.usdc_size = usdc_size
        self.ts = ts


def test_ledger_totals_pnl_formula():
    rows = [
        Row("TRADE", "BUY", "-100", "100"),
        Row("TRADE", "SELL", "40", "40"),
        Row("REDEEM", None, "10", "10"),
        Row("MERGE", None, "5", "5"),
        Row("REWARD", None, "2", "2"),
        Row("TAKER_REBATE", None, "1", "1"),
        Row("MAKER_REBATE", None, "3", "3"),
        Row("SPLIT", None, "-8", "8"),
    ]

    totals = _ledger_totals(rows, open_value=Decimal("25"))

    assert totals["buy"] == Decimal("100")
    assert totals["sell"] == Decimal("40")
    assert totals["redeem"] == Decimal("10")
    assert totals["merge"] == Decimal("5")
    assert totals["reward"] == Decimal("2")
    assert totals["taker_rebate"] == Decimal("1")
    assert totals["maker_rebate"] == Decimal("3")
    assert totals["split"] == Decimal("8")
    assert totals["open_value"] == Decimal("25")
    assert totals["pnl"] == Decimal("-22")


def test_fee_period_rows_split_on_sports_fee_cutoff():
    rows = [
        Row("TRADE", "BUY", "-100", "100", SPORTS_FEE_CUTOFF_TS - 1),
        Row("TRADE", "SELL", "10", "10", SPORTS_FEE_CUTOFF_TS - 1),
        Row("TRADE", "BUY", "-200", "200", SPORTS_FEE_CUTOFF_TS),
        Row("REWARD", None, "30", "30", SPORTS_FEE_CUTOFF_TS),
    ]

    pre = _period_rows(rows, None, SPORTS_FEE_CUTOFF_TS)
    post = _period_rows(rows, SPORTS_FEE_CUTOFF_TS, None)

    pre_totals = _ledger_totals(pre, open_value=Decimal("0"))
    post_totals = _ledger_totals(post, open_value=Decimal("0"))

    assert pre_totals["buy"] == Decimal("100")
    assert pre_totals["sell"] == Decimal("10")
    assert pre_totals["pnl"] == Decimal("-90")
    assert _roi_on_buy_volume(pre_totals) == Decimal("-0.9")

    assert post_totals["buy"] == Decimal("200")
    assert post_totals["reward"] == Decimal("30")
    assert post_totals["pnl"] == Decimal("-170")
    assert _roi_on_buy_volume(post_totals) == Decimal("-0.85")


def test_roi_on_buy_volume_is_none_without_buy_volume():
    totals = _ledger_totals([Row("REWARD", None, "10", "10")], open_value=Decimal("0"))

    assert _roi_on_buy_volume(totals) is None
