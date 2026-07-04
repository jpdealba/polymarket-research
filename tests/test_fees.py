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


def _enrichment(session, event_id: int, *, fee: str | None, role: str = "maker", source: str = "subgraph") -> None:
    session.execute(
        text(
            "INSERT INTO fill_enrichment "
            "(event_id, role, order_hash, fee, counterparty, source, enriched_at) "
            "VALUES (:event_id, :role, :order_hash, :fee, '0xother', :source, 'now')"
        ),
        {
            "event_id": event_id,
            "role": role,
            "order_hash": f"0xorder{event_id}",
            "fee": fee,
            "source": source,
        },
    )
    session.commit()


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


def test_enriched_trade_uses_fill_enrichment_fee_as_actual(session):
    upsert_market_payloads(session, ([_market(COND_SPORTS, category="Sports")],), [COND_SPORTS])
    event_id = _event(session, tx="0xactualfee", ts=SPORTS_FEE_START_TS, condition_id=COND_SPORTS)
    _enrichment(session, event_id, fee="0.125", role="maker", source="subgraph")

    stats = compute_fee_estimates(session, wallet=WALLET)
    fee = session.execute(
        text(
            "SELECT estimated_fee, actual_fee, fee_source "
            "FROM fee_estimates WHERE event_id = :id"
        ),
        {"id": event_id},
    ).fetchone()
    report = fee_attribution_report(session, wallet=WALLET)[0]

    assert Decimal(fee.estimated_fee) == Decimal("0.3750")
    assert Decimal(fee.actual_fee) == Decimal("0.125")
    assert fee.fee_source == "actual_subgraph"
    assert stats.actual_enriched_trades == 1
    assert stats.actual_fee_total == Decimal("0.125")
    assert stats.estimated_fee_fallback_total == Decimal("0")
    assert stats.blended_fee_total == Decimal("0.125")
    assert report.actual_fee == Decimal("0.125")
    assert report.blended_fee == Decimal("0.125")
    assert report.blended_net_pnl == Decimal("-50.125")


def test_enriched_trade_with_null_fee_falls_back_to_estimate(session):
    upsert_market_payloads(session, ([_market(COND_SPORTS, category="Sports")],), [COND_SPORTS])
    event_id = _event(session, tx="0xnullfee", ts=SPORTS_FEE_START_TS, condition_id=COND_SPORTS)
    _enrichment(session, event_id, fee=None, role="maker", source="rpc")

    compute_fee_estimates(session, wallet=WALLET)
    fee = session.execute(
        text("SELECT estimated_fee, actual_fee, fee_source FROM fee_estimates WHERE event_id = :id"),
        {"id": event_id},
    ).fetchone()
    row = fee_attribution_report(session, wallet=WALLET)[0]

    assert Decimal(fee.estimated_fee) == Decimal("0.3750")
    assert fee.actual_fee is None
    assert fee.fee_source == "estimated_schedule"
    assert row.actual_fee is None
    assert row.estimated_fee_fallback == Decimal("0.3750")
    assert row.blended_fee == Decimal("0.3750")


def test_non_enriched_trade_uses_estimated_schedule_fallback(session):
    upsert_market_payloads(session, ([_market(COND_SPORTS, category="Sports")],), [COND_SPORTS])
    event_id = _event(session, tx="0xnoenrich", ts=SPORTS_FEE_START_TS, condition_id=COND_SPORTS)

    stats = compute_fee_estimates(session, wallet=WALLET)
    fee = session.execute(
        text("SELECT actual_fee, fee_source FROM fee_estimates WHERE event_id = :id"),
        {"id": event_id},
    ).fetchone()

    assert fee.actual_fee is None
    assert fee.fee_source == "estimated_schedule"
    assert stats.actual_enriched_trades == 0
    assert stats.estimated_fee_fallback_total == Decimal("0.3750")
    assert stats.blended_fee_total == Decimal("0.3750")


def test_actual_fee_is_not_double_counted_with_estimate(session):
    upsert_market_payloads(session, ([_market(COND_SPORTS, category="Sports")],), [COND_SPORTS])
    actual_id = _event(session, tx="0xactualonly", ts=SPORTS_FEE_START_TS, condition_id=COND_SPORTS)
    _event(session, tx="0xfallbackonly", ts=SPORTS_FEE_START_TS + 1, condition_id=COND_SPORTS)
    _enrichment(session, actual_id, fee="0.1000", role="maker", source="rpc")

    stats = compute_fee_estimates(session, wallet=WALLET)
    coverage = fee_attribution_coverage(session, wallet=WALLET)
    row = fee_attribution_report(session, wallet=WALLET)[0]

    assert stats.estimated_fee_total == Decimal("0.7500")
    assert stats.actual_fee_total == Decimal("0.1000")
    assert stats.estimated_fee_fallback_total == Decimal("0.3750")
    assert stats.blended_fee_total == Decimal("0.4750")
    assert coverage.blended_fee_total == Decimal("0.475")
    assert row.actual_fee == Decimal("0.1000")
    assert row.estimated_fee_fallback == Decimal("0.3750")
    assert row.blended_fee == Decimal("0.4750")


def test_maker_taker_fee_breakdown_groups_by_enrichment_role(session):
    upsert_market_payloads(session, ([_market(COND_SPORTS, category="Sports")],), [COND_SPORTS])
    maker_id = _event(session, tx="0xmakerfee", ts=SPORTS_FEE_START_TS, condition_id=COND_SPORTS)
    taker_id = _event(session, tx="0xtakerfee", ts=SPORTS_FEE_START_TS + 1, condition_id=COND_SPORTS)
    _enrichment(session, maker_id, fee="0.01", role="maker", source="subgraph")
    _enrichment(session, taker_id, fee="0.02", role="taker", source="rpc")

    compute_fee_estimates(session, wallet=WALLET)
    row = fee_attribution_report(session, wallet=WALLET)[0]

    assert row.maker_trades == 1
    assert row.taker_trades == 1
    assert row.maker_volume == Decimal("50")
    assert row.taker_volume == Decimal("50")
    assert row.maker_fee == Decimal("0.01")
    assert row.taker_fee == Decimal("0.02")
    assert row.fee_source_counts == (("actual_rpc", 1), ("actual_subgraph", 1))


def test_fee_report_cli_outputs_blended_totals(settings, session, monkeypatch):
    monkeypatch.setenv("PMR_DATA_DIR", str(settings.data_dir))
    upsert_market_payloads(session, ([_market(COND_SPORTS, category="Sports")],), [COND_SPORTS])
    actual_id = _event(session, tx="0xcliactual", ts=SPORTS_FEE_START_TS, condition_id=COND_SPORTS)
    _event(session, tx="0xclifallback", ts=SPORTS_FEE_START_TS + 1, condition_id=COND_SPORTS)
    _enrichment(session, actual_id, fee="0.1", role="maker", source="subgraph")

    result = CliRunner().invoke(
        main,
        ["fees", "report", "--wallet", WALLET, "--by-category", "--pre-post-sports-fee"],
    )

    assert result.exit_code == 0, result.output
    assert "total_trades=2" in result.output
    assert "actual_enriched_trades=1" in result.output
    assert "actual_fee_coverage_pct=50.00" in result.output
    assert "actual_fee_total=0.1" in result.output
    assert "estimated_fee_fallback_total=0.375" in result.output
    assert "blended_fee_total=0.475" in result.output
    assert "net_pnl_after_blended_fees=-100.4750" in result.output
    assert "maker_trades=1" in result.output
    assert "fee_sources=actual_subgraph:1,estimated_schedule:1" in result.output


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
