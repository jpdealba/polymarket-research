from decimal import Decimal

from click.testing import CliRunner
from sqlalchemy import text

from pmresearch.cli import main
from pmresearch.fees.estimate import compute_fee_estimates
from pmresearch.fees.schedules import SPORTS_FEE_START_TS
from pmresearch.ingest.markets import upsert_market_payloads
from pmresearch.reports.fee_attribution import fee_attribution_coverage, fee_attribution_report

WALLET = "0xfee000000000000000000000000000000000001"
COND_SPORTS = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
COND_FINANCE = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


def _market(condition_id: str, *, category: str) -> dict:
    return {
        "conditionId": condition_id,
        "question": "Fixture market",
        "slug": f"fixture-{condition_id[-4:]}",
        "category": category,
        "events": [{"id": f"event-{condition_id[-4:]}", "title": "Fixture event"}],
        "outcomes": '["Yes","No"]',
        "clobTokenIds": '["101","102"]',
        "outcomePrices": '["0.5","0.5"]',
        "closed": False,
    }


def _raw_id(session) -> int:
    session.execute(
        text(
            "INSERT INTO raw_fetches "
            "(source, endpoint, params_json, fetched_at, http_status, file_path, content_hash, row_count, ingested_at) "
            "VALUES ('fixture', 'activity', '{}', 'now', 200, 'fixture', :hash, 1, 'now')"
        ),
        {"hash": f"hash-{session.execute(text('SELECT COUNT(*) FROM raw_fetches')).scalar()}"},
    )
    return int(session.execute(text("SELECT max(id) FROM raw_fetches")).scalar())


def _event(
    session,
    *,
    tx: str,
    ts: int,
    condition_id: str | None,
    event_type: str = "TRADE",
    side: str | None = "BUY",
    delta_shares: str = "100",
    delta_usdc: str = "-50",
    price: str = "0.5",
    usdc_size: str = "50",
) -> int:
    raw_id = _raw_id(session)
    session.execute(
        text(
            "INSERT INTO wallet_events "
            "(wallet, event_type, ts, tx_hash, condition_id, token_id, side, delta_shares, "
            "delta_usdc, price, usdc_size, source, is_derived, raw_ref, dedupe_key, ingested_at) "
            "VALUES (:wallet, :event_type, :ts, :tx, :condition_id, '101', :side, "
            ":delta_shares, :delta_usdc, :price, :usdc_size, 'fixture', 0, :raw_id, :dedupe, 'now')"
        ),
        {
            "wallet": WALLET,
            "event_type": event_type,
            "ts": ts,
            "tx": tx,
            "condition_id": condition_id,
            "side": side,
            "delta_shares": delta_shares,
            "delta_usdc": delta_usdc,
            "price": price,
            "usdc_size": usdc_size,
            "raw_id": raw_id,
            "dedupe": f"dedupe-{tx}",
        },
    )
    session.commit()
    return int(session.execute(text("SELECT max(id) FROM wallet_events")).scalar())


def test_sports_trade_before_2026_03_30_gets_zero_fee(session):
    upsert_market_payloads(session, ([_market(COND_SPORTS, category="Sports")],), [COND_SPORTS])
    event_id = _event(session, tx="0xbefore", ts=SPORTS_FEE_START_TS - 1, condition_id=COND_SPORTS)

    stats = compute_fee_estimates(session, wallet=WALLET)

    assert stats.total_trades == 1
    fee = session.execute(
        text("SELECT estimated_fee, worst_case_fee, rule_name FROM fee_estimates WHERE event_id = :id"),
        {"id": event_id},
    ).fetchone()
    assert Decimal(fee.estimated_fee) == Decimal("0")
    assert Decimal(fee.worst_case_fee) == Decimal("0")
    assert fee.rule_name == "no_fee"


def test_sports_trade_after_2026_03_30_gets_sports_fee_estimate(session):
    upsert_market_payloads(session, ([_market(COND_SPORTS, category="Sports")],), [COND_SPORTS])
    event_id = _event(session, tx="0xafter", ts=SPORTS_FEE_START_TS, condition_id=COND_SPORTS)

    compute_fee_estimates(session, wallet=WALLET)

    fee = session.execute(
        text(
            "SELECT estimated_fee, worst_case_fee, actual_fee, rule_name, confidence "
            "FROM fee_estimates WHERE event_id = :id"
        ),
        {"id": event_id},
    ).fetchone()
    assert Decimal(fee.estimated_fee) == Decimal("0.3750")
    assert Decimal(fee.worst_case_fee) == Decimal("0.3750")
    assert fee.actual_fee is None
    assert fee.rule_name == "polymarket_sports_taker_fee_v1"
    assert fee.confidence == "estimate_taker_assumption_no_maker_taker"


def test_non_sports_category_does_not_receive_sports_fee(session):
    upsert_market_payloads(session, ([_market(COND_FINANCE, category="Finance")],), [COND_FINANCE])
    event_id = _event(session, tx="0xfinance", ts=SPORTS_FEE_START_TS, condition_id=COND_FINANCE)

    compute_fee_estimates(session, wallet=WALLET)

    fee = session.execute(
        text("SELECT estimated_fee, rule_name FROM fee_estimates WHERE event_id = :id"),
        {"id": event_id},
    ).fetchone()
    assert Decimal(fee.estimated_fee) == Decimal("0")
    assert fee.rule_name == "no_fee"


def test_fees_schedules_cli_seeds_and_lists_defaults(settings, monkeypatch):
    monkeypatch.setenv("PMR_DATA_DIR", str(settings.data_dir))
    runner = CliRunner()

    result = runner.invoke(main, ["fees", "schedules"])

    assert result.exit_code == 0
    assert "category=__default__" in result.output
    assert "category=sports" in result.output
    assert "from_utc=2026-03-30T00:00:00Z" in result.output
    assert "rule_name=polymarket_sports_taker_fee_v1" in result.output


def test_actual_fee_remains_unavailable_without_phase_11_enrichment(session):
    upsert_market_payloads(session, ([_market(COND_SPORTS, category="Sports")],), [COND_SPORTS])
    _event(session, tx="0xactual", ts=SPORTS_FEE_START_TS, condition_id=COND_SPORTS)

    stats = compute_fee_estimates(session, wallet=WALLET)
    report = fee_attribution_report(session, wallet=WALLET)
    coverage = fee_attribution_coverage(session, wallet=WALLET)

    row = report[0]
    assert stats.actual_enriched_trades == 0
    assert coverage.actual_enriched_trades == 0
    assert row.estimated_fee == Decimal("0.3750")
    assert row.worst_case_fee == Decimal("0.3750")
    assert row.actual_fee is None
    assert row.actual_net_pnl is None


def test_fee_report_shows_gross_and_estimated_net_without_mutating_ledger(session):
    upsert_market_payloads(session, ([_market(COND_SPORTS, category="Sports")],), [COND_SPORTS])
    _event(
        session,
        tx="0xbuy",
        ts=SPORTS_FEE_START_TS,
        condition_id=COND_SPORTS,
        delta_shares="100",
        delta_usdc="-60",
        price="0.6",
        usdc_size="60",
    )
    _event(
        session,
        tx="0xsell",
        ts=SPORTS_FEE_START_TS + 1,
        condition_id=COND_SPORTS,
        side="SELL",
        delta_shares="-50",
        delta_usdc="30",
        price="0.6",
        usdc_size="30",
    )
    _event(
        session,
        tx="0xreward",
        ts=SPORTS_FEE_START_TS + 2,
        condition_id=None,
        event_type="MAKER_REBATE",
        side=None,
        delta_shares="0",
        delta_usdc="2",
        price="0",
        usdc_size="2",
    )
    _event(
        session,
        tx="0xmerge",
        ts=SPORTS_FEE_START_TS + 3,
        condition_id=COND_SPORTS,
        event_type="MERGE",
        side=None,
        delta_shares="-5",
        delta_usdc="5",
        price="0",
        usdc_size="5",
    )

    compute_fee_estimates(session, wallet=WALLET)
    rows = fee_attribution_report(
        session,
        wallet=WALLET,
        by_category=True,
        pre_post_sports_fee=True,
    )

    assert len(rows) == 2
    by_category = {row.category: row for row in rows}
    sports = by_category["sports"]
    unclassified = by_category["unclassified"]

    assert sports.period == "post_sports_fee"
    assert sports.buy_volume == Decimal("60")
    assert sports.gross_pnl == Decimal("-25")
    assert sports.estimated_fee == Decimal("0.6480")
    assert sports.worst_case_fee == Decimal("0.6480")
    assert sports.estimated_net_pnl == Decimal("-25.6480")
    assert sports.gross_roi == Decimal("-25") / Decimal("60")
    assert sports.estimated_net_roi == Decimal("-25.6480") / Decimal("60")
    assert sports.actual_fee is None
    assert sports.actual_net_pnl is None

    assert unclassified.gross_pnl == Decimal("2")
    assert unclassified.estimated_fee == Decimal("0")


def test_fee_report_coverage_counts_classification_estimates_and_actuals(session):
    upsert_market_payloads(session, ([_market(COND_SPORTS, category="Sports")],), [COND_SPORTS])
    _event(session, tx="0xknown", ts=SPORTS_FEE_START_TS, condition_id=COND_SPORTS)
    _event(session, tx="0xunknown", ts=SPORTS_FEE_START_TS, condition_id="0xmissing")

    stats = compute_fee_estimates(session, wallet=WALLET)
    coverage = fee_attribution_coverage(session, wallet=WALLET)

    assert stats.total_trades == 2
    assert stats.category_classified_trades == 1
    assert stats.fee_estimated_trades == 1
    assert stats.unknown_category_trades == 1
    assert stats.actual_enriched_trades == 0
    assert coverage.total_trades == 2
    assert coverage.category_classified_trades == 1
    assert coverage.fee_estimated_trades == 1
    assert coverage.unknown_category_trades == 1
    assert coverage.actual_enriched_trades == 0
