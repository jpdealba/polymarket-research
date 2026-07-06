"""Focused tests for Phase 22 counterfactual simulation."""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal

import pytest
from click.testing import CliRunner
from sqlalchemy import text

from pmresearch.cli import main
from pmresearch.simulation.attribution import (
    fetch_attribution_summary,
    fetch_event_attribution,
    fetch_market_attribution,
)
from pmresearch.simulation.composite_search import (
    COMPONENT_CONTRIBUTION_FILENAME,
    COMPONENT_EFFECTIVENESS_FILENAME,
    COMPONENT_EFFECTIVENESS_REPORT_FILENAME,
    COMPOSITE_CANDIDATES_FILENAME,
    COMPOSITE_REPORT_FILENAME,
    COMPOSITE_TOP_FILENAME,
    EVENT_ROBUSTNESS_FILENAME,
    FORWARD_WATCH_FILENAME,
    PER_CANDIDATE_EVENT_PNL_FILENAME,
    SKIPPED_BY_COMPONENT_FILENAME,
    ComponentSpec,
    CompositeCandidate,
    CompositeMetric,
    _edge_bands,
    _final_status,
    _rank_candidates,
    _simulate_composite,
    run_progressive_composite_search,
    write_composite_outputs,
)
from pmresearch.simulation.engine import (
    DECISION_CONTEXT_FIELDS,
    GAP_WALLET,
    PROHIBITED_DECISION_FIELDS,
    RN1_WALLET,
    DecisionContext,
    run_simulation,
    run_strategy_simulation,
)
from pmresearch.simulation.holdout_failure import (
    BOOK_AGE_FILENAME,
    CONDITION_FILENAME,
    PRICE_BUCKET_FILENAME,
    REPORT_FILENAME,
    SIDE_FILENAME,
    TIME_BUCKET_FILENAME,
    generate_holdout_failure_diagnostics,
    write_holdout_failure_outputs,
)
from pmresearch.simulation.inventory_cycling import (
    InventoryCyclingConfig,
    InventoryLifecycleState,
    fetch_lifecycle_summary,
    merge_qty_for_condition,
    simulate_inventory_cycling,
    simulate_redeem,
)
from pmresearch.simulation.report import generate_compare_report, generate_sim_report
from pmresearch.simulation.risk import RiskLimits
from pmresearch.simulation.scenarios import CONSERVATIVE, MEDIUM, OPTIMISTIC
from pmresearch.simulation.search import (
    SearchCandidate,
    SearchMetric,
    candidate_test_passes,
    candidate_validation_passes,
    final_status,
    parameter_combinations,
    rank_candidates,
    run_strategy_search,
    selection_score,
    split_rows_by_time,
    top_candidates,
)


def _insert_wallet_event(
    session,
    wallet: str,
    event_id: int,
    ts: int,
    *,
    condition_id: str = "cond_a",
    token_id: str = "tok_a",
) -> int:
    session.execute(
        text(
            "INSERT INTO raw_fetches "
            "(source, endpoint, params_json, fetched_at, http_status, file_path, "
            " content_hash, row_count) "
            "VALUES (:source, :endpoint, :params_json, :fetched_at, :http_status, "
            " :file_path, :content_hash, :row_count)"
        ),
        {
            "source": "test",
            "endpoint": "test",
            "params_json": "{}",
            "fetched_at": "2026-07-01T00:00:00Z",
            "http_status": 200,
            "file_path": "/dev/null",
            "content_hash": f"hash_{wallet}_{event_id}",
            "row_count": 1,
        },
    )
    raw_id = session.execute(text("SELECT last_insert_rowid()")).scalar_one()
    session.execute(
        text(
            "INSERT INTO wallet_events "
            "(wallet, event_type, ts, tx_hash, condition_id, token_id, side, "
            " delta_shares, delta_usdc, price, usdc_size, source, is_derived, "
            " raw_ref, dedupe_key, ingested_at) "
            "VALUES (:wallet, :event_type, :ts, :tx_hash, :condition_id, :token_id, "
            " :side, :delta_shares, :delta_usdc, :price, :usdc_size, :source, "
            " :is_derived, :raw_ref, :dedupe_key, :ingested_at)"
        ),
        {
            "wallet": wallet.lower(),
            "event_type": "TRADE",
            "ts": ts,
            "tx_hash": f"0xtest_tx_{wallet}_{event_id}",
            "condition_id": condition_id,
            "token_id": token_id,
            "side": "BUY",
            "delta_shares": "100",
            "delta_usdc": "55",
            "price": "0.55",
            "usdc_size": "55",
            "source": "test",
            "is_derived": 0,
            "raw_ref": raw_id,
            "dedupe_key": f"dedup_{wallet}_{event_id}",
            "ingested_at": "2026-07-01T00:00:00Z",
        },
    )
    return session.execute(text("SELECT last_insert_rowid()")).scalar_one()


def _make_row(
    event_id: int,
    wallet: str,
    *,
    trade_ts: int = 1_000_000,
    best_bid_before: str = "0.40",
    best_ask_before: str = "0.55",
    mid_before: str = "0.50",
    spread_bps: str = "3000",
    context_status: str = "good",
    book_before_age_s: int = 5,
    qty_token_before: str = "0",
    bid_depth_top1: str = "80",
    ask_depth_top1: str = "80",
    condition_id: str = "cond_a",
    token_id: str = "tok_a",
) -> dict:
    return {
        "event_id": event_id,
        "wallet": wallet.lower(),
        "token_id": token_id,
        "condition_id": condition_id,
        "trade_ts": trade_ts,
        "trade_utc": "2026-07-01T12:00:00Z",
        "side": "BUY",
        "fill_price": "0.99",
        "fill_size": "999",
        "fill_shares": "999",
        "fill_notional_usdc": "989.01",
        "delta_usdc": "989.01",
        "role": "maker",
        "context_status": context_status,
        "book_before_age_s": book_before_age_s,
        "book_after_age_s": 1,
        "context_source": "all_fills",
        "best_bid_before": best_bid_before,
        "best_ask_before": best_ask_before,
        "mid_before": mid_before,
        "spread_before": str(Decimal(best_ask_before) - Decimal(best_bid_before)),
        "spread_bps": spread_bps,
        "bid_depth_top1": bid_depth_top1,
        "ask_depth_top1": ask_depth_top1,
        "bid_depth_top5": "200",
        "ask_depth_top5": "200",
        "book_imbalance_top1": "0",
        "book_imbalance_top5": "0",
        "distance_fill_to_mid": "0.49",
        "distance_fill_to_bid": "0.59",
        "distance_fill_to_ask": "0.44",
        "fill_inside_spread": 0,
        "fill_at_best_bid": 0,
        "fill_at_best_ask": 0,
        "trade_hour_utc": 12,
        "market_category": "sports",
        "time_to_event_start_s": 3600,
        "wallet_label": "Test",
        "qty_token_before": qty_token_before,
        "qty_complement_before": "0",
        "directional_before": qty_token_before,
        "bond_before": "0",
        "bond_ratio_before": "0",
        "qty_token_after": "999",
        "qty_complement_after": "0",
        "directional_after": "999",
        "bond_after": "0",
        "bond_ratio_after": "0",
        "bond_delta": "0",
        "directional_delta": "999",
        "event_exposure_before": "0",
        "event_exposure_after": "989.01",
        "event_exposure_delta": "989.01",
        "close_path": "RESOLUTION",
        "close_ts": 1_500_000,
        "hold_seconds": 500_000,
        "realized_pnl_wac": "9999",
        "realized_pnl_per_share": "9.99",
        "realized_pnl_bps_on_cost": "9999",
        "remaining_open_qty_after_24h": "0",
        "is_open_after_24h": 0,
        "closed_by_merge": 0,
        "closed_by_redeem": 0,
        "closed_by_sell": 0,
        "closed_by_resolution": 1,
        "closed_by_unresolved_open": 0,
        "markout_5m": "999",
        "markout_15m": "999",
        "markout_1h": "999",
        "markout_24h": "999",
        "pnl_episode": "9999",
        "pnl_at_resolution": "9999",
        "null_reasons_json": "{}",
        "dataset_version": 1,
        "watchlist": "test",
        "built_at": 1_000_000,
    }


def _insert_dataset_rows(session, wallet: str, rows: list[dict]) -> None:
    for row in rows:
        event_id = _insert_wallet_event(
            session,
            wallet,
            row["event_id"],
            row["trade_ts"],
            condition_id=row["condition_id"],
            token_id=row["token_id"],
        )
        row = dict(row)
        row["event_id"] = event_id
        session.execute(
            text(
                "INSERT INTO microstructure_lifecycle_dataset "
                "(event_id, wallet, token_id, condition_id, trade_ts, trade_utc, "
                " side, fill_price, fill_size, fill_shares, fill_notional_usdc, "
                " delta_usdc, role, context_status, book_before_age_s, book_after_age_s, "
                " context_source, best_bid_before, best_ask_before, mid_before, "
                " spread_before, spread_bps, bid_depth_top1, ask_depth_top1, "
                " bid_depth_top5, ask_depth_top5, book_imbalance_top1, book_imbalance_top5, "
                " distance_fill_to_mid, distance_fill_to_bid, distance_fill_to_ask, "
                " fill_inside_spread, fill_at_best_bid, fill_at_best_ask, "
                " trade_hour_utc, market_category, time_to_event_start_s, wallet_label, "
                " qty_token_before, qty_complement_before, directional_before, "
                " bond_before, bond_ratio_before, qty_token_after, qty_complement_after, "
                " directional_after, bond_after, bond_ratio_after, bond_delta, "
                " directional_delta, event_exposure_before, event_exposure_after, "
                " event_exposure_delta, close_path, close_ts, hold_seconds, "
                " realized_pnl_wac, realized_pnl_per_share, realized_pnl_bps_on_cost, "
                " remaining_open_qty_after_24h, is_open_after_24h, "
                " closed_by_merge, closed_by_redeem, closed_by_sell, "
                " closed_by_resolution, closed_by_unresolved_open, "
                " markout_5m, markout_15m, markout_1h, markout_24h, "
                " pnl_episode, pnl_at_resolution, "
                " null_reasons_json, dataset_version, watchlist, built_at) "
                "VALUES "
                "(:event_id, :wallet, :token_id, :condition_id, :trade_ts, :trade_utc, "
                " :side, :fill_price, :fill_size, :fill_shares, :fill_notional_usdc, "
                " :delta_usdc, :role, :context_status, :book_before_age_s, :book_after_age_s, "
                " :context_source, :best_bid_before, :best_ask_before, :mid_before, "
                " :spread_before, :spread_bps, :bid_depth_top1, :ask_depth_top1, "
                " :bid_depth_top5, :ask_depth_top5, :book_imbalance_top1, :book_imbalance_top5, "
                " :distance_fill_to_mid, :distance_fill_to_bid, :distance_fill_to_ask, "
                " :fill_inside_spread, :fill_at_best_bid, :fill_at_best_ask, "
                " :trade_hour_utc, :market_category, :time_to_event_start_s, :wallet_label, "
                " :qty_token_before, :qty_complement_before, :directional_before, "
                " :bond_before, :bond_ratio_before, :qty_token_after, :qty_complement_after, "
                " :directional_after, :bond_after, :bond_ratio_after, :bond_delta, "
                " :directional_delta, :event_exposure_before, :event_exposure_after, "
                " :event_exposure_delta, :close_path, :close_ts, :hold_seconds, "
                " :realized_pnl_wac, :realized_pnl_per_share, :realized_pnl_bps_on_cost, "
                " :remaining_open_qty_after_24h, :is_open_after_24h, "
                " :closed_by_merge, :closed_by_redeem, :closed_by_sell, "
                " :closed_by_resolution, :closed_by_unresolved_open, "
                " :markout_5m, :markout_15m, :markout_1h, :markout_24h, "
                " :pnl_episode, :pnl_at_resolution, "
                " :null_reasons_json, :dataset_version, :watchlist, :built_at)"
            ),
            row,
        )
    session.commit()


def _insert_market_metadata(
    session,
    *,
    condition_id: str,
    question: str,
    event_id: str,
    event_title: str,
) -> None:
    session.execute(
        text(
            "INSERT OR REPLACE INTO pm_events "
            "(event_id, title, slug, neg_risk, tags_json) "
            "VALUES (:event_id, :title, :slug, 0, '[]')"
        ),
        {"event_id": event_id, "title": event_title, "slug": f"event-{event_id}"},
    )
    session.execute(
        text(
            "INSERT OR REPLACE INTO markets "
            "(condition_id, question, slug, category, event_id, neg_risk, outcomes_json, "
            "clob_token_ids_json, closed, structure_type, updated_at) "
            "VALUES "
            "(:condition_id, :question, :slug, 'sports', :event_id, 0, '[]', '[]', 0, 'binary', 'now')"
        ),
        {
            "condition_id": condition_id,
            "question": question,
            "slug": f"market-{condition_id}",
            "event_id": event_id,
        },
    )
    session.commit()


def _orders_for_run(session, run_id: int) -> list[tuple[str, str]]:
    rows = session.execute(
        text(
            "SELECT order_price, order_size FROM simulation_orders "
            "WHERE run_id = :run_id ORDER BY id"
        ),
        {"run_id": run_id},
    ).fetchall()
    return [(row.order_price, row.order_size) for row in rows]


def _skipped_reasons_for_run(session, run_id: int) -> list[str]:
    rows = session.execute(
        text(
            "SELECT skipped_reason FROM simulation_skipped_orders "
            "WHERE run_id = :run_id ORDER BY id"
        ),
        {"run_id": run_id},
    ).fetchall()
    return [row.skipped_reason for row in rows]


def test_decision_context_is_allowlist_only():
    row = _make_row(1, GAP_WALLET)
    ctx = DecisionContext.from_row(row)

    assert set(ctx.values).issubset(DECISION_CONTEXT_FIELDS)
    assert not (set(ctx.values) & PROHIBITED_DECISION_FIELDS)
    with pytest.raises(KeyError):
        ctx.get("fill_price")


def test_scenario_ordering_assumptions_are_strict():
    assert CONSERVATIVE.fill_rate_multiplier < MEDIUM.fill_rate_multiplier < OPTIMISTIC.fill_rate_multiplier
    assert CONSERVATIVE.slippage_bps > MEDIUM.slippage_bps > OPTIMISTIC.slippage_bps
    assert CONSERVATIVE.risk_limit_multiplier < MEDIUM.risk_limit_multiplier < OPTIMISTIC.risk_limit_multiplier


def test_event_timing_rejected(session):
    with pytest.raises(ValueError, match="event_timing"):
        run_simulation(session, RN1_WALLET, "event_timing", "conservative")


def test_run_rn1_completion_set_edge_conservative(session):
    rows = [_make_row(i, RN1_WALLET, trade_ts=1_000_000 + i * 10) for i in range(8)]
    _insert_dataset_rows(session, RN1_WALLET, rows)

    result = run_simulation(session, RN1_WALLET, "completion_set_edge", "conservative")

    assert result.wallet == RN1_WALLET
    assert result.rule_name == "completion_set_edge"
    assert result.strategy_name == "completion_set_edge"
    assert result.scenario == "conservative"
    assert result.candidate_signals_count == 8
    assert result.accepted_orders_count == 8
    assert result.orders_count == 8
    assert result.simulated_fills_count <= result.orders_count
    if result.net_pnl <= Decimal("0") or result.risk_breaches:
        assert result.conservative_pass is False


def test_run_gap_spread_capture_conservative(session):
    rows = [_make_row(i, GAP_WALLET, trade_ts=1_000_000 + i * 10) for i in range(8)]
    _insert_dataset_rows(session, GAP_WALLET, rows)

    result = run_simulation(session, GAP_WALLET, "spread_capture", "conservative")

    assert result.wallet == GAP_WALLET
    assert result.rule_name == "spread_capture"
    assert result.strategy_name == "spread_capture"
    assert result.scenario == "conservative"
    assert result.candidate_signals_count == 8
    assert result.accepted_orders_count == 8
    assert result.orders_count == 8
    assert result.simulated_fills_count <= result.orders_count


def test_run_rn1_completion_set_edge_risk_v2_conservative(session):
    rows = [_make_row(i, RN1_WALLET, trade_ts=1_000_000 + i * 10) for i in range(12)]
    _insert_dataset_rows(session, RN1_WALLET, rows)

    result = run_strategy_simulation(
        session,
        RN1_WALLET,
        "rn1_completion_set_edge_risk_v2",
        "conservative",
    )

    assert result.strategy_name == "rn1_completion_set_edge_risk_v2"
    assert result.base_rule == "completion_set_edge"
    assert result.risk_breaches == 0
    assert result.simulated_fills_count > 0


def test_run_gap_spread_capture_risk_v2_conservative(session):
    rows = [_make_row(i, GAP_WALLET, trade_ts=1_000_000 + i * 10) for i in range(12)]
    _insert_dataset_rows(session, GAP_WALLET, rows)

    result = run_strategy_simulation(
        session,
        GAP_WALLET,
        "gap_spread_capture_risk_v2",
        "conservative",
    )

    assert result.strategy_name == "gap_spread_capture_risk_v2"
    assert result.base_rule == "spread_capture"
    assert result.risk_breaches == 0
    assert result.simulated_fills_count > 0


def test_v2_has_no_more_risk_breaches_than_v1_conservative(session):
    rows = [_make_row(i, RN1_WALLET, trade_ts=1_000_000 + i * 10) for i in range(12)]
    _insert_dataset_rows(session, RN1_WALLET, rows)
    limits = RiskLimits(max_position_per_token=Decimal("15"))

    v1 = run_simulation(
        session,
        RN1_WALLET,
        "completion_set_edge",
        "conservative",
        risk_limits=limits,
    )
    v2 = run_strategy_simulation(
        session,
        RN1_WALLET,
        "rn1_completion_set_edge_risk_v2",
        "conservative",
        risk_limits=limits,
    )

    assert v2.risk_breaches <= v1.risk_breaches
    assert v2.risk_breaches == 0
    assert v2.risk_prevented_count > 0


def test_pre_trade_guard_skips_max_position_breach(session):
    rows = [_make_row(i, RN1_WALLET, trade_ts=1_000_000 + i * 10) for i in range(3)]
    _insert_dataset_rows(session, RN1_WALLET, rows)

    result = run_strategy_simulation(
        session,
        RN1_WALLET,
        "rn1_completion_set_edge_risk_v2",
        "conservative",
        risk_limits=RiskLimits(max_position_per_token=Decimal("5")),
    )

    assert result.orders_count == 0
    assert result.accepted_orders_count == 0
    assert result.candidate_signals_count == 3
    assert result.risk_breaches == 0
    assert result.risk_prevented_count == 3
    assert result.skipped_by_reason == {"max_position_per_token": 3}
    assert _skipped_reasons_for_run(session, result.run_id) == ["max_position_per_token"] * 3


def test_pre_trade_guard_skips_max_event_exposure_breach(session):
    rows = [_make_row(i, RN1_WALLET, trade_ts=1_000_000 + i * 10) for i in range(3)]
    _insert_dataset_rows(session, RN1_WALLET, rows)

    result = run_strategy_simulation(
        session,
        RN1_WALLET,
        "rn1_completion_set_edge_risk_v2",
        "conservative",
        risk_limits=RiskLimits(max_event_exposure=Decimal("4")),
    )

    assert result.orders_count == 0
    assert result.accepted_orders_count == 0
    assert result.candidate_signals_count == 3
    assert result.risk_breaches == 0
    assert result.risk_prevented_count == 3
    assert result.skipped_by_reason == {"max_event_exposure": 3}
    assert _skipped_reasons_for_run(session, result.run_id) == ["max_event_exposure"] * 3


def test_gap_max_daily_loss_stops_new_orders_for_utc_day(session):
    ts = 1_000_000
    rows = [
        _make_row(1, GAP_WALLET, trade_ts=ts),
        _make_row(
            2,
            GAP_WALLET,
            trade_ts=ts + 10,
            best_bid_before="0.005",
            best_ask_before="0.020",
            mid_before="0.010",
            spread_bps="15000",
        ),
        _make_row(
            3,
            GAP_WALLET,
            trade_ts=ts + 20,
            best_bid_before="0.005",
            best_ask_before="0.020",
            mid_before="0.010",
            spread_bps="15000",
        ),
    ]
    _insert_dataset_rows(session, GAP_WALLET, rows)

    result = run_strategy_simulation(
        session,
        GAP_WALLET,
        "gap_spread_capture_risk_v2",
        "conservative",
        risk_limits=RiskLimits(max_daily_loss=Decimal("4")),
    )

    assert result.simulated_fills_count == 1
    assert result.risk_breaches == 0
    assert result.skipped_by_reason["max_daily_loss"] == 1
    assert result.skipped_by_reason["max_daily_loss_day_stopped"] == 1
    assert _skipped_reasons_for_run(session, result.run_id) == [
        "max_daily_loss",
        "max_daily_loss_day_stopped",
    ]


def test_no_leakage_observed_fill_values_not_used(session):
    rows = [_make_row(i, GAP_WALLET, trade_ts=1_000_000 + i * 10) for i in range(5)]
    _insert_dataset_rows(session, GAP_WALLET, rows)

    result = run_simulation(session, GAP_WALLET, "spread_capture", "optimistic")
    order_prices = session.execute(
        text("SELECT order_price, order_size FROM simulation_orders WHERE run_id = :run_id"),
        {"run_id": result.run_id},
    ).fetchall()

    assert order_prices
    assert all(row.order_price != "0.99" for row in order_prices)
    assert all(row.order_size != "999" for row in order_prices)


def test_changing_historical_fill_price_does_not_change_generated_orders(session):
    rows = [_make_row(i, GAP_WALLET, trade_ts=1_000_000 + i * 10) for i in range(5)]
    _insert_dataset_rows(session, GAP_WALLET, rows)

    before = run_strategy_simulation(session, GAP_WALLET, "gap_spread_capture_risk_v2", "optimistic")
    before_orders = _orders_for_run(session, before.run_id)
    session.execute(
        text(
            "UPDATE microstructure_lifecycle_dataset "
            "SET fill_price = '0.01', fill_size = '1', realized_pnl_wac = '-9999', markout_1h = '-999' "
            "WHERE wallet = :wallet"
        ),
        {"wallet": GAP_WALLET},
    )
    session.commit()

    after = run_strategy_simulation(session, GAP_WALLET, "gap_spread_capture_risk_v2", "optimistic")

    assert before_orders
    assert _orders_for_run(session, after.run_id) == before_orders


def test_changing_book_before_can_change_generated_orders(session):
    rows = [_make_row(i, GAP_WALLET, trade_ts=1_000_000 + i * 10) for i in range(5)]
    _insert_dataset_rows(session, GAP_WALLET, rows)

    before = run_strategy_simulation(session, GAP_WALLET, "gap_spread_capture_risk_v2", "optimistic")
    before_orders = _orders_for_run(session, before.run_id)
    session.execute(
        text(
            "UPDATE microstructure_lifecycle_dataset "
            "SET best_bid_before = '0.30', spread_before = '0.25', spread_bps = '5000' "
            "WHERE wallet = :wallet"
        ),
        {"wallet": GAP_WALLET},
    )
    session.commit()

    after = run_strategy_simulation(session, GAP_WALLET, "gap_spread_capture_risk_v2", "optimistic")

    assert before_orders
    assert _orders_for_run(session, after.run_id) != before_orders


def test_compare_produces_distinct_assumptions(session):
    rows = [_make_row(i, GAP_WALLET, trade_ts=1_000_000 + i * 10) for i in range(20)]
    _insert_dataset_rows(session, GAP_WALLET, rows)

    optimistic = run_simulation(session, GAP_WALLET, "spread_capture", "optimistic")
    medium = run_simulation(session, GAP_WALLET, "spread_capture", "medium")
    conservative = run_simulation(session, GAP_WALLET, "spread_capture", "conservative")
    report = generate_compare_report([optimistic, medium, conservative])

    assert optimistic.simulated_fills_count >= medium.simulated_fills_count >= conservative.simulated_fills_count
    assert "Optimistic" in report
    assert "Medium" in report
    assert "Conservative" in report
    assert "Candidate signals" in report
    assert "Accepted orders" in report
    assert "Skipped candidates" in report
    assert report.count("## Conservative Gate") == 1


def test_report_declares_conservative_pass_or_fail(session):
    rows = [_make_row(i, GAP_WALLET, trade_ts=1_000_000 + i * 10) for i in range(8)]
    _insert_dataset_rows(session, GAP_WALLET, rows)

    result = run_simulation(session, GAP_WALLET, "spread_capture", "conservative")
    report = generate_sim_report(result)

    assert "Conservative Gate" in report
    assert "Candidate signals" in report
    assert "Accepted orders" in report
    assert "Skipped candidates" in report
    assert ("PASS" in report) or ("FAIL" in report)
    if result.net_pnl <= Decimal("0") or result.risk_breaches:
        assert result.conservative_pass is False
        assert "NOT eligible for paper trading" in report


def test_attribution_sums_to_run_net_pnl_and_groups_metadata(session):
    for condition_id, question, event_id, event_title in [
        ("cond_a", "Market A", "event_1", "Event One"),
        ("cond_b", "Market B", "event_1", "Event One"),
        ("cond_c", "Market C", "event_2", "Event Two"),
    ]:
        _insert_market_metadata(
            session,
            condition_id=condition_id,
            question=question,
            event_id=event_id,
            event_title=event_title,
        )
    rows = [
        _make_row(
            i,
            GAP_WALLET,
            trade_ts=1_000_000 + i * 10,
            condition_id=["cond_a", "cond_b", "cond_c"][i % 3],
            token_id=["tok_a", "tok_b", "tok_c"][i % 3],
        )
        for i in range(15)
    ]
    _insert_dataset_rows(session, GAP_WALLET, rows)

    result = run_simulation(session, GAP_WALLET, "spread_capture", "conservative")
    markets = fetch_market_attribution(session, result.run_id)
    events = fetch_event_attribution(session, result.run_id)
    summary = fetch_attribution_summary(session, result.run_id)

    assert {row.condition_id for row in markets} == {"cond_a", "cond_b", "cond_c"}
    assert {row.question for row in markets} == {"Market A", "Market B", "Market C"}
    assert {row.event_id for row in events} == {"event_1", "event_2"}
    assert sum((row.total_pnl for row in markets), Decimal("0")) == result.net_pnl
    assert summary.residual == Decimal("0")
    assert Decimal("0") <= summary.top_1_event_pnl_share <= Decimal("1")
    assert Decimal("0") <= summary.top_3_event_pnl_share <= Decimal("1")
    assert Decimal("0") <= summary.top_5_market_pnl_share <= Decimal("1")


def test_attribution_cli_and_report_include_top_tables(settings, monkeypatch, session, tmp_path):
    monkeypatch.setenv("PMR_DATA_DIR", str(settings.data_dir))
    for condition_id, question, event_id, event_title in [
        ("cond_a", "Market A", "event_1", "Event One"),
        ("cond_b", "Market B", "event_1", "Event One"),
        ("cond_c", "Market C", "event_2", "Event Two"),
    ]:
        _insert_market_metadata(
            session,
            condition_id=condition_id,
            question=question,
            event_id=event_id,
            event_title=event_title,
        )
    rows = [
        _make_row(
            i,
            GAP_WALLET,
            trade_ts=1_000_000 + i * 10,
            condition_id=["cond_a", "cond_b", "cond_c"][i % 3],
            token_id=["tok_a", "tok_b", "tok_c"][i % 3],
        )
        for i in range(15)
    ]
    _insert_dataset_rows(session, GAP_WALLET, rows)
    result = run_simulation(session, GAP_WALLET, "spread_capture", "conservative")
    runner = CliRunner()

    market_result = runner.invoke(main, ["sim", "attribution", "--run-id", str(result.run_id), "--by", "market"])
    assert market_result.exit_code == 0, market_result.output
    assert "condition_id" in market_result.output
    assert "total_pnl" in market_result.output
    assert "Market A" in market_result.output

    event_result = runner.invoke(main, ["sim", "attribution", "--run-id", str(result.run_id), "--by", "event"])
    assert event_result.exit_code == 0, event_result.output
    assert "event_title" in event_result.output
    assert "max_event_exposure" in event_result.output
    assert "Event One" in event_result.output

    report_out = tmp_path / "sim_report.md"
    report_result = runner.invoke(
        main,
        ["sim", "report", "--wallet", GAP_WALLET, "--rule", "spread_capture", "--out", str(report_out)],
    )
    assert report_result.exit_code == 0, report_result.output
    report = report_out.read_text(encoding="utf-8")
    assert "Top 10 Markets By PnL" in report
    assert "Top 10 Events By PnL" in report
    assert "top_1_event_pnl_share" in report
    assert "top_3_event_pnl_share" in report
    assert "top_5_market_pnl_share" in report


def test_cli_run_compare_and_report(settings, monkeypatch, session, tmp_path):
    monkeypatch.setenv("PMR_DATA_DIR", str(settings.data_dir))
    rows = [_make_row(i, GAP_WALLET, trade_ts=1_000_000 + i * 10) for i in range(10)]
    _insert_dataset_rows(session, GAP_WALLET, rows)
    runner = CliRunner()

    run_result = runner.invoke(
        main,
        [
            "sim",
            "run",
            "--wallet",
            GAP_WALLET,
            "--rule",
            "spread_capture",
            "--scenario",
            "conservative",
        ],
    )
    assert run_result.exit_code == 0, run_result.output
    assert "conservative_gate=" in run_result.output
    assert "candidate_signals=" in run_result.output
    assert "accepted_orders=" in run_result.output

    strategy_result = runner.invoke(
        main,
        [
            "sim",
            "run",
            "--wallet",
            GAP_WALLET,
            "--strategy",
            "gap_spread_capture_risk_v2",
            "--scenario",
            "conservative",
        ],
    )
    assert strategy_result.exit_code == 0, strategy_result.output
    assert "strategy=gap_spread_capture_risk_v2" in strategy_result.output
    assert "accepted_orders=" in strategy_result.output

    compare_result = runner.invoke(
        main,
        ["sim", "compare", "--wallet", GAP_WALLET, "--rule", "spread_capture"],
    )
    assert compare_result.exit_code == 0, compare_result.output
    assert "Scenario Comparison" in compare_result.output
    assert "Candidate signals" in compare_result.output
    assert compare_result.output.count("## Conservative Gate") == 1

    out = tmp_path / "sim_report.md"
    report_result = runner.invoke(
        main,
        ["sim", "report", "--wallet", GAP_WALLET, "--rule", "spread_capture", "--out", str(out)],
    )
    assert report_result.exit_code == 0, report_result.output
    assert out.exists()
    assert "Conservative Gate" in out.read_text(encoding="utf-8")


def test_cli_rejects_event_timing(settings, monkeypatch):
    monkeypatch.setenv("PMR_DATA_DIR", str(settings.data_dir))
    runner = CliRunner()

    result = runner.invoke(
        main,
        [
            "sim",
            "run",
            "--wallet",
            RN1_WALLET,
            "--rule",
            "event_timing",
            "--scenario",
            "conservative",
        ],
    )

    assert result.exit_code != 0
    assert "event_timing" in result.output


def test_search_sampling_is_deterministic_with_fixed_seed():
    first = parameter_combinations("completion_set_edge", max_combos=6, seed=2202)
    second = parameter_combinations("completion_set_edge", max_combos=6, seed=2202)
    different = parameter_combinations("completion_set_edge", max_combos=6, seed=2203)

    assert first == second
    assert first != different


def test_search_split_is_time_ordered():
    rows = [
        _make_row(1, RN1_WALLET, trade_ts=300),
        _make_row(2, RN1_WALLET, trade_ts=100),
        _make_row(3, RN1_WALLET, trade_ts=600),
        _make_row(4, RN1_WALLET, trade_ts=200),
        _make_row(5, RN1_WALLET, trade_ts=500),
        _make_row(6, RN1_WALLET, trade_ts=400),
        _make_row(7, RN1_WALLET, trade_ts=700),
        _make_row(8, RN1_WALLET, trade_ts=800),
        _make_row(9, RN1_WALLET, trade_ts=900),
        _make_row(10, RN1_WALLET, trade_ts=1000),
    ]

    splits = split_rows_by_time(rows)

    assert [r["trade_ts"] for r in splits["train"]] == [100, 200, 300, 400, 500, 600]
    assert [r["trade_ts"] for r in splits["validation"]] == [700, 800]
    assert [r["trade_ts"] for r in splits["test"]] == [900, 1000]


def test_search_works_for_rn1_and_persists_all_splits(session):
    rows = [
        _make_row(i, RN1_WALLET, trade_ts=1_000_000 + i * 10, best_bid_before="0.30", best_ask_before="0.50")
        for i in range(15)
    ]
    _insert_dataset_rows(session, RN1_WALLET, rows)

    result = run_strategy_search(session, RN1_WALLET, "completion_set_edge", max_combos=3)

    assert result.rule_name == "completion_set_edge"
    assert result.evaluated_combos == 3
    persisted_splits = session.execute(
        text(
            "SELECT DISTINCT split_name FROM simulation_strategy_candidate_metrics "
            "WHERE candidate_id IN (SELECT id FROM simulation_strategy_candidates WHERE search_run_id = :run_id)"
        ),
        {"run_id": result.run_id},
    ).scalars().all()
    assert set(persisted_splits) == {"train", "validation", "test"}


def test_search_works_for_gap(session):
    rows = [_make_row(i, GAP_WALLET, trade_ts=1_000_000 + i * 10) for i in range(15)]
    _insert_dataset_rows(session, GAP_WALLET, rows)

    result = run_strategy_search(session, GAP_WALLET, "spread_capture", max_combos=3)

    assert result.rule_name == "spread_capture"
    assert result.evaluated_combos == 3
    assert all(set(candidate.metrics) == {"train", "validation", "test"} for candidate in result.candidates)


def test_search_no_leakage_from_historical_fill_columns(session):
    rows = [_make_row(i, GAP_WALLET, trade_ts=1_000_000 + i * 10) for i in range(15)]
    _insert_dataset_rows(session, GAP_WALLET, rows)

    before = run_strategy_search(session, GAP_WALLET, "spread_capture", max_combos=2)
    before_metrics = [
        (
            c.parameters,
            c.metrics["validation"].candidate_signals_count,
            c.metrics["validation"].accepted_orders_count,
            c.metrics["validation"].simulated_fills_count,
            c.metrics["validation"].net_pnl,
        )
        for c in before.candidates
    ]
    session.execute(
        text(
            "UPDATE microstructure_lifecycle_dataset "
            "SET fill_price = '0.01', fill_size = '1', realized_pnl_wac = '-9999', "
            "markout_5m = '-999', markout_1h = '-999', pnl_at_resolution = '-9999', "
            "close_path = 'LEAK_SENTINEL' "
            "WHERE wallet = :wallet"
        ),
        {"wallet": GAP_WALLET},
    )
    session.commit()

    after = run_strategy_search(session, GAP_WALLET, "spread_capture", max_combos=2)
    after_metrics = [
        (
            c.parameters,
            c.metrics["validation"].candidate_signals_count,
            c.metrics["validation"].accepted_orders_count,
            c.metrics["validation"].simulated_fills_count,
            c.metrics["validation"].net_pnl,
        )
        for c in after.candidates
    ]

    assert after_metrics == before_metrics


def test_search_ranking_excludes_risk_breaches_ordering_and_nonpositive_pnl():
    good = _search_candidate(1, net_pnl="10", risk_breaches=0, ordering=False, fills=30)
    risky = _search_candidate(2, net_pnl="100", risk_breaches=1, ordering=False, fills=5)
    ordering = _search_candidate(3, net_pnl="100", risk_breaches=0, ordering=True, fills=5)
    flat = _search_candidate(4, net_pnl="0", risk_breaches=0, ordering=False, fills=5)

    ranked = rank_candidates([risky, ordering, flat, good])

    assert ranked == [good]


def test_search_selection_score_never_uses_test_split():
    good_test = _search_candidate(
        1,
        train_net_pnl="20",
        validation_net_pnl="15",
        test_net_pnl="1000",
        fills=40,
    )
    bad_test = _search_candidate(
        2,
        train_net_pnl="20",
        validation_net_pnl="15",
        test_net_pnl="-1000",
        test_fills=0,
        fills=40,
    )

    assert selection_score(good_test) == selection_score(bad_test)
    assert rank_candidates([bad_test, good_test]) == [good_test, bad_test]


def test_search_positive_test_weak_validation_is_not_selected():
    candidate = _search_candidate(
        1,
        train_net_pnl="20",
        validation_net_pnl="-1",
        test_net_pnl="1000",
        fills=40,
        validation_fills=40,
        test_fills=40,
    )

    assert rank_candidates([candidate]) == []
    assert final_status(candidate) == "NOT_SELECTED"


def test_search_strong_validation_failed_test_is_test_fail():
    candidate = _search_candidate(
        1,
        train_net_pnl="20",
        validation_net_pnl="15",
        test_net_pnl="-1",
        fills=40,
        test_fills=40,
    )

    assert rank_candidates([candidate]) == [candidate]
    assert candidate_validation_passes(candidate) is True
    assert candidate_test_passes(candidate) is False
    assert final_status(candidate) == "TEST_FAIL"


def test_top_eligible_only_excludes_test_fail(session):
    test_fail = _search_candidate(
        1,
        train_net_pnl="20",
        validation_net_pnl="15",
        test_net_pnl="-1",
        fills=40,
        test_fills=40,
    )
    test_pass = _search_candidate(
        2,
        train_net_pnl="18",
        validation_net_pnl="14",
        test_net_pnl="5",
        fills=40,
        test_fills=40,
    )
    _persist_search_candidates(session, GAP_WALLET, "spread_capture", [test_fail, test_pass])

    all_top = top_candidates(session, GAP_WALLET, "spread_capture", limit=10)
    eligible_top = top_candidates(session, GAP_WALLET, "spread_capture", limit=10, eligible_only=True)

    assert [c.candidate_index for c in all_top] == [1, 2]
    assert [c.candidate_index for c in eligible_top] == [2]


def test_top_eligible_only_excludes_test_fills_below_min(session):
    low_test_fills = _search_candidate(
        1,
        train_net_pnl="20",
        validation_net_pnl="15",
        test_net_pnl="5",
        fills=40,
        test_fills=29,
    )
    enough_test_fills = _search_candidate(
        2,
        train_net_pnl="18",
        validation_net_pnl="14",
        test_net_pnl="5",
        fills=40,
        test_fills=30,
    )
    _persist_search_candidates(session, GAP_WALLET, "spread_capture", [low_test_fills, enough_test_fills])

    eligible_top = top_candidates(session, GAP_WALLET, "spread_capture", limit=10, eligible_only=True)

    assert [c.candidate_index for c in eligible_top] == [2]


def test_search_cli_commands(settings, monkeypatch, session, tmp_path):
    monkeypatch.setenv("PMR_DATA_DIR", str(settings.data_dir))
    rows = [_make_row(i, GAP_WALLET, trade_ts=1_000_000 + i * 10) for i in range(15)]
    _insert_dataset_rows(session, GAP_WALLET, rows)
    runner = CliRunner()
    out = tmp_path / "search.md"

    search_result = runner.invoke(
        main,
        [
            "sim",
            "search",
            "--wallet",
            GAP_WALLET,
            "--rule",
            "spread_capture",
            "--max-combos",
            "2",
            "--out",
            str(out),
        ],
    )
    assert search_result.exit_code == 0, search_result.output
    assert out.exists()
    assert "search_run_id=" in search_result.output

    top_result = runner.invoke(
        main,
        ["sim", "top", "--wallet", GAP_WALLET, "--rule", "spread_capture", "--limit", "2"],
    )
    assert top_result.exit_code == 0, top_result.output
    assert ("No eligible candidates found." in top_result.output) or ("rank candidate score" in top_result.output)

    report_out = tmp_path / "report_search.md"
    report_result = runner.invoke(
        main,
        [
            "sim",
            "report-search",
            "--wallet",
            GAP_WALLET,
            "--rule",
            "spread_capture",
            "--out",
            str(report_out),
        ],
    )
    assert report_result.exit_code == 0, report_result.output
    assert "Strategy Search: spread_capture" in report_out.read_text(encoding="utf-8")


def test_holdout_failure_outputs_required_files(settings, monkeypatch, session, tmp_path):
    monkeypatch.setenv("PMR_DATA_DIR", str(settings.data_dir))
    rows = []
    for i in range(15):
        is_test = i >= 12
        rows.append(
            _make_row(
                i,
                RN1_WALLET,
                trade_ts=1_000_000 + i * 10,
                best_bid_before="0.30",
                best_ask_before="0.50",
                mid_before="0.20" if is_test else "0.50",
                context_status="weak" if i == 13 else "good",
                condition_id="cond_holdout",
                token_id="tok_holdout",
            )
        )
    _insert_dataset_rows(session, RN1_WALLET, rows)
    _insert_market_metadata(
        session,
        condition_id="cond_holdout",
        question="Holdout Market",
        event_id="event-holdout",
        event_title="Holdout Event",
    )
    candidate = _search_candidate(
        1,
        train_net_pnl="20",
        validation_net_pnl="10",
        test_net_pnl="-1",
        fills=40,
        test_fills=40,
    )
    candidate.parameters = {
        "max_bond_cost": "0.99",
        "book_age_s_max": 30,
        "max_order_size": "10",
        "max_position_per_token": "1000",
        "max_event_exposure": "1000",
        "max_capital_deployed": "10000",
        "min_depth": "0",
    }
    _persist_search_candidates(session, RN1_WALLET, "completion_set_edge", [candidate])

    diagnostics = write_holdout_failure_outputs(
        session,
        RN1_WALLET,
        "completion_set_edge",
        tmp_path,
    )

    assert diagnostics.candidate.candidate_id is not None
    for filename in (
        REPORT_FILENAME,
        CONDITION_FILENAME,
        PRICE_BUCKET_FILENAME,
        BOOK_AGE_FILENAME,
        SIDE_FILENAME,
        TIME_BUCKET_FILENAME,
    ):
        assert (tmp_path / filename).exists()
    report = (tmp_path / REPORT_FILENAME).read_text(encoding="utf-8")
    assert "Diagnostic only" in report
    assert "Holdout Event" in report
    assert "Simulated Fill vs Skipped" in report
    assert "Do not choose final thresholds from this test report" in report
    condition_csv = (tmp_path / CONDITION_FILENAME).read_text(encoding="utf-8")
    assert "event_id,event_title,condition_id,question" in condition_csv
    assert "event-holdout,Holdout Event,cond_holdout,Holdout Market" in condition_csv

    cli_out = tmp_path / "cli"
    cli_result = CliRunner().invoke(
        main,
        [
            "sim",
            "holdout-failure",
            "--wallet",
            RN1_WALLET,
            "--rule",
            "completion_set_edge",
            "--out-dir",
            str(cli_out),
        ],
    )
    assert cli_result.exit_code == 0, cli_result.output
    assert "holdout_failure search_run_id=" in cli_result.output
    assert (cli_out / REPORT_FILENAME).exists()


def test_holdout_failure_does_not_change_selected_candidate(session):
    rows = [
        _make_row(
            i,
            RN1_WALLET,
            trade_ts=1_000_000 + i * 10,
            best_bid_before="0.30",
            best_ask_before="0.50",
            mid_before="0.50",
            condition_id="cond_no_leak",
            token_id="tok_no_leak",
        )
        for i in range(15)
    ]
    _insert_dataset_rows(session, RN1_WALLET, rows)
    candidate = _search_candidate(
        1,
        train_net_pnl="20",
        validation_net_pnl="10",
        test_net_pnl="-1",
        fills=40,
        test_fills=40,
    )
    candidate.parameters = {
        "max_bond_cost": "0.99",
        "book_age_s_max": 30,
        "max_order_size": "10",
        "max_position_per_token": "1000",
        "max_event_exposure": "1000",
        "max_capital_deployed": "10000",
        "min_depth": "0",
    }
    _persist_search_candidates(session, RN1_WALLET, "completion_set_edge", [candidate])
    before = session.execute(
        text("SELECT selected_candidate_id FROM simulation_strategy_search_runs")
    ).scalar_one()

    generate_holdout_failure_diagnostics(session, RN1_WALLET, "completion_set_edge")

    after = session.execute(
        text("SELECT selected_candidate_id FROM simulation_strategy_search_runs")
    ).scalar_one()
    assert after == before


def test_composite_search_writes_required_outputs(session, tmp_path):
    rows = [
        _make_row(
            i,
            RN1_WALLET,
            trade_ts=1_000_000 + i * 10,
            best_bid_before="0.30",
            best_ask_before="0.50",
            mid_before="0.50",
            condition_id=f"cond_{i // 3}",
            token_id=f"tok_{i}",
        )
        for i in range(30)
    ]
    _insert_dataset_rows(session, RN1_WALLET, rows)

    result = run_progressive_composite_search(
        session,
        max_components=2,
        max_candidates=8,
        seed=2204,
        capital_mode="small",
        max_capital=Decimal("100"),
        max_order_size=Decimal("5"),
        min_events=1,
        min_fills=1,
        wallet=RN1_WALLET,
    )
    write_composite_outputs(result, tmp_path)

    for filename in (
        COMPOSITE_REPORT_FILENAME,
        COMPOSITE_CANDIDATES_FILENAME,
        COMPOSITE_TOP_FILENAME,
        FORWARD_WATCH_FILENAME,
        COMPONENT_EFFECTIVENESS_REPORT_FILENAME,
        COMPONENT_EFFECTIVENESS_FILENAME,
        PER_CANDIDATE_EVENT_PNL_FILENAME,
        SKIPPED_BY_COMPONENT_FILENAME,
        COMPONENT_CONTRIBUTION_FILENAME,
        EVENT_ROBUSTNESS_FILENAME,
    ):
        assert (tmp_path / filename).exists()
    report = (tmp_path / COMPOSITE_REPORT_FILENAME).read_text(encoding="utf-8")
    assert "Progressive Composite Strategy Search" in report
    assert "RN1 similarity score" in report or "No candidate passed" in report
    candidates_csv = (tmp_path / COMPOSITE_CANDIDATES_FILENAME).read_text(encoding="utf-8")
    assert "selected_components" in candidates_csv
    assert "final_status" in candidates_csv


def test_composite_search_no_leakage_from_forbidden_columns(session):
    rows = [
        _make_row(
            i,
            RN1_WALLET,
            trade_ts=1_000_000 + i * 10,
            best_bid_before="0.30",
            best_ask_before="0.50",
            mid_before="0.50",
            condition_id=f"cond_{i // 3}",
            token_id=f"tok_{i}",
        )
        for i in range(30)
    ]
    _insert_dataset_rows(session, RN1_WALLET, rows)

    before = run_progressive_composite_search(
        session,
        max_components=1,
        max_candidates=5,
        seed=2204,
        capital_mode="small",
        max_capital=Decimal("100"),
        max_order_size=Decimal("5"),
        min_events=1,
        min_fills=1,
        wallet=RN1_WALLET,
    )
    before_metrics = [
        (
            candidate.component_labels,
            candidate.metrics["validation"].candidate_signals_count,
            candidate.metrics["validation"].accepted_orders_count,
            candidate.metrics["validation"].simulated_fills_count,
            candidate.metrics["validation"].net_pnl,
            candidate.final_status,
        )
        for candidate in before.candidates
    ]
    session.execute(
        text(
            "UPDATE microstructure_lifecycle_dataset "
            "SET fill_price = '0.01', fill_size = '1', realized_pnl_wac = '-9999', "
            "markout_5m = '-999', markout_1h = '-999', pnl_at_resolution = '-9999', "
            "close_path = 'LEAK_SENTINEL', book_after_age_s = 999 "
            "WHERE wallet = :wallet"
        ),
        {"wallet": RN1_WALLET},
    )
    session.commit()

    after = run_progressive_composite_search(
        session,
        max_components=1,
        max_candidates=5,
        seed=2204,
        capital_mode="small",
        max_capital=Decimal("100"),
        max_order_size=Decimal("5"),
        min_events=1,
        min_fills=1,
        wallet=RN1_WALLET,
    )
    after_metrics = [
        (
            candidate.component_labels,
            candidate.metrics["validation"].candidate_signals_count,
            candidate.metrics["validation"].accepted_orders_count,
            candidate.metrics["validation"].simulated_fills_count,
            candidate.metrics["validation"].net_pnl,
            candidate.final_status,
        )
        for candidate in after.candidates
    ]

    assert after_metrics == before_metrics


def test_composite_low_positive_test_is_forward_watch_not_hard_fail():
    candidate = _composite_candidate(test_net_pnl="6.24", test_roi="0.025", validation_net_pnl="109.01")

    status = _classify_composite(candidate, max_capital=Decimal("250"))

    assert status == "FORWARD_WATCH_CANDIDATE"


def test_composite_material_negative_test_is_hard_fail():
    candidate = _composite_candidate(test_net_pnl="-8.00", test_roi="-0.04", validation_net_pnl="40")

    status = _classify_composite(candidate, max_capital=Decimal("250"))

    assert status == "TEST_FAIL_HARD"


def test_composite_near_breakeven_test_is_weak_or_forward_watch():
    candidate = _composite_candidate(test_net_pnl="0.50", test_roi="0.002", validation_net_pnl="25")

    status = _classify_composite(candidate, max_capital=Decimal("250"))

    assert status in {"WEAK_RESEARCH_CANDIDATE", "FORWARD_WATCH_CANDIDATE"}
    assert status == "WEAK_RESEARCH_CANDIDATE"


def test_composite_test_metrics_do_not_change_rank_or_selected():
    first = _composite_candidate(1, validation_net_pnl="30", validation_roi="0.30", test_net_pnl="-50")
    second = _composite_candidate(2, validation_net_pnl="20", validation_roi="0.20", test_net_pnl="100")
    first.final_status = _classify_composite(first, max_capital=Decimal("250"))
    second.final_status = _classify_composite(second, max_capital=Decimal("250"))

    before = _rank_candidates([second, first], min_events=1, min_fills=1)
    first.metrics["test"] = replace(first.metrics["test"], net_pnl=Decimal("500"), roi_on_capital=Decimal("2"))
    second.metrics["test"] = replace(second.metrics["test"], net_pnl=Decimal("-500"), roi_on_capital=Decimal("-2"))
    after = _rank_candidates([second, first], min_events=1, min_fills=1)

    assert [c.candidate_id for c in before] == [1, 2]
    assert [c.candidate_id for c in after] == [1, 2]
    assert before[0].candidate_id == after[0].candidate_id


def test_composite_edge_bands_are_based_on_max_capital():
    bands = _edge_bands(Decimal("250"))

    assert bands == {
        "1pct": Decimal("2.50"),
        "2pct": Decimal("5.00"),
        "3pct": Decimal("7.50"),
        "5pct": Decimal("12.50"),
    }


def test_inventory_cycling_merge_qty_is_min_of_binary_legs():
    state = InventoryLifecycleState()
    state.apply_buy("cond_merge", "tok0", Decimal("0.40"), Decimal("7"))
    state.apply_buy("cond_merge", "tok1", Decimal("0.50"), Decimal("3"))

    assert merge_qty_for_condition(state, "cond_merge") == Decimal("3")


def test_inventory_cycling_merge_releases_capital_and_reduces_unpaired():
    state = InventoryLifecycleState()
    state.apply_buy("cond_merge", "tok0", Decimal("0.40"), Decimal("7"))
    state.apply_buy("cond_merge", "tok1", Decimal("0.50"), Decimal("3"))
    locked_before = state.locked_capital
    unpaired_before = state.unpaired_inventory("cond_merge")

    proceeds, _merge_pnl, _before, after = state.merge("cond_merge", Decimal("3"))

    assert proceeds == Decimal("3")
    assert state.released_credit == Decimal("3")
    assert state.locked_capital < locked_before
    assert state.unpaired_inventory("cond_merge") < unpaired_before
    assert after == {"tok0": "4"}


def test_inventory_cycling_recycled_capital_allows_new_orders():
    state = InventoryLifecycleState()
    config = InventoryCyclingConfig(max_capital=Decimal("5"), recycle_capital_enabled=True)
    state.apply_buy("cond_a", "tok0", Decimal("0.50"), Decimal("5"))
    state.apply_buy("cond_a", "tok1", Decimal("0.50"), Decimal("5"))
    available_before_merge = state.available_capital(config)

    state.merge("cond_a", Decimal("5"))

    assert available_before_merge < Decimal("5")
    assert state.available_capital(config) >= Decimal("5")


def test_inventory_cycling_redeem_values_winner_and_loser():
    state = InventoryLifecycleState()
    state.apply_buy("cond_res", "winner", Decimal("0.20"), Decimal("2"))
    state.apply_buy("cond_res", "loser", Decimal("0.10"), Decimal("1"))

    events, metrics = simulate_redeem(
        state,
        {"cond_res": {"winner": Decimal("1"), "loser": Decimal("0")}},
        ts=123,
    )

    assert len(events) == 2
    assert metrics.redeem_count == 2
    assert metrics.redeem_pnl > Decimal("0")
    assert state.lot("cond_res", "winner").qty == Decimal("0")
    assert state.lot("cond_res", "loser").qty == Decimal("0")


def test_inventory_cycling_auto_merge_disabled_leaves_inventory_unmerged():
    rows = [
        _make_row(1, RN1_WALLET, trade_ts=1_000_000, condition_id="cond_no_merge", token_id="tok0"),
        _make_row(2, RN1_WALLET, trade_ts=1_000_010, condition_id="cond_no_merge", token_id="tok1"),
    ]

    result = simulate_inventory_cycling(
        rows,
        CONSERVATIVE,
        RiskLimits(max_capital_deployed=Decimal("100"), max_order_size=Decimal("10")),
        config=InventoryCyclingConfig(auto_merge_enabled=False, max_capital=Decimal("100")),
        resolution_prices={},
    )

    lifecycle = result.lifecycle_metrics
    assert lifecycle.merge_count == 0
    assert lifecycle.unresolved_inventory_value > Decimal("0")


def test_event_inventory_cycling_strategy_run_creates_merge(settings, monkeypatch, session):
    monkeypatch.setenv("PMR_DATA_DIR", str(settings.data_dir))
    rows = [
        _make_row(i, RN1_WALLET, trade_ts=1_000_000 + i * 10, condition_id="cond_cycle", token_id=f"tok{i % 2}")
        for i in range(10)
    ]
    _insert_dataset_rows(session, RN1_WALLET, rows)

    result = CliRunner().invoke(
        main,
        [
            "sim",
            "run",
            "--wallet",
            RN1_WALLET,
            "--strategy",
            "event_inventory_cycling_v1",
            "--scenario",
            "conservative",
            "--max-capital",
            "100",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "strategy=event_inventory_cycling_v1" in result.output
    assert "lifecycle merge_count=" in result.output
    run_id = int(result.output.split("run_id=")[1].split()[0])
    summary = fetch_lifecycle_summary(session, run_id)
    assert summary is not None
    assert summary.merge_count > 0
    assert summary.released_capital_total > Decimal("0")


def test_event_inventory_cycling_resolution_changes_do_not_change_entry_decisions(session):
    rows = [
        _make_row(1, RN1_WALLET, trade_ts=1_000_000, condition_id="cond_leak_res", token_id="tok0"),
        _make_row(2, RN1_WALLET, trade_ts=1_000_010, condition_id="cond_leak_res", token_id="tok1"),
        _make_row(3, RN1_WALLET, trade_ts=1_000_020, condition_id="cond_leak_res", token_id="tok0"),
    ]
    _insert_dataset_rows(session, RN1_WALLET, rows)
    _insert_market_metadata(
        session,
        condition_id="cond_leak_res",
        question="Leakage Resolution Market",
        event_id="event-leak-res",
        event_title="Leakage Event",
    )
    session.execute(
        text(
            "UPDATE markets SET resolution_prices_json = :prices "
            "WHERE condition_id = 'cond_leak_res'"
        ),
        {"prices": json.dumps({"tok0": "1", "tok1": "0"})},
    )
    session.commit()

    before = run_strategy_simulation(session, RN1_WALLET, "event_inventory_cycling_v1", "conservative")
    before_orders = _orders_for_run(session, before.run_id)
    session.execute(
        text(
            "UPDATE markets SET resolution_prices_json = :prices "
            "WHERE condition_id = 'cond_leak_res'"
        ),
        {"prices": json.dumps({"tok0": "0", "tok1": "1"})},
    )
    session.commit()

    after = run_strategy_simulation(session, RN1_WALLET, "event_inventory_cycling_v1", "conservative")
    after_orders = _orders_for_run(session, after.run_id)

    assert after_orders == before_orders


def test_event_inventory_cycling_participates_in_composite_search(session):
    rows = [
        _make_row(i, RN1_WALLET, trade_ts=1_000_000 + i * 10, condition_id=f"cond_inv_{i // 2}", token_id=f"tok_{i % 2}")
        for i in range(12)
    ]
    _insert_dataset_rows(session, RN1_WALLET, rows)

    result = run_progressive_composite_search(
        session,
        max_components=1,
        max_candidates=3,
        seed=2204,
        capital_mode="small",
        max_capital=Decimal("100"),
        max_order_size=Decimal("5"),
        min_events=1,
        min_fills=1,
        wallet=RN1_WALLET,
        strategy_family="event_inventory_cycling",
    )

    assert result.candidates
    assert result.candidates[0].components[0].name == "event_inventory_cycling_v1"


def test_composite_impossible_filter_reduces_inventory_orders_and_fills_to_zero():
    rows = [
        _make_row(i, RN1_WALLET, trade_ts=1_000_000 + i * 10, condition_id="cond_guard", token_id=f"tok{i % 2}")
        for i in range(10)
    ]
    base = ComponentSpec(
        "event_inventory_cycling_v1",
        "event_inventory_cycling",
        "base",
        {
            "max_bond_cost": "0.98",
            "min_bond_delta": "0",
            "max_unpaired_inventory": "100",
            "min_merge_qty": "1",
            "auto_merge_enabled": True,
            "recycle_capital_enabled": True,
        },
    )
    impossible = ComponentSpec(
        "price_bucket_filters",
        "price_bucket_filters",
        "filter",
        {"min_mid": "2.00", "max_mid": "3.00"},
    )

    result = _simulate_composite(
        rows,
        (base, impossible),
        CONSERVATIVE,
        RiskLimits(max_capital_deployed=Decimal("100"), max_order_size=Decimal("10")),
        split_name="validation",
    )
    metric = result.metric

    assert metric.accepted_orders_count == 0
    assert metric.simulated_fills_count == 0
    assert metric.skipped_orders_count > 0
    assert metric.skipped_by_reason["component:price_bucket_filters"] > 0


def _classify_composite(candidate: CompositeCandidate, *, max_capital: Decimal) -> str:
    candidate.final_status = _final_status(
        candidate,
        ordering_violation=False,
        max_capital=max_capital,
        capital_mode="small",
        min_events=1,
        min_fills=1,
    )
    return candidate.final_status


def _composite_candidate(
    candidate_id: int = 1,
    *,
    train_net_pnl: str = "30",
    validation_net_pnl: str = "25",
    test_net_pnl: str = "5",
    train_roi: str = "0.30",
    validation_roi: str = "0.25",
    test_roi: str = "0.02",
) -> CompositeCandidate:
    metrics = {
        "train": _composite_metric("train", net_pnl=train_net_pnl, roi=train_roi),
        "validation": _composite_metric("validation", net_pnl=validation_net_pnl, roi=validation_roi),
        "test": _composite_metric("test", net_pnl=test_net_pnl, roi=test_roi),
    }
    candidate = CompositeCandidate(
        candidate_id=candidate_id,
        stage=1,
        components=(
            ComponentSpec(
                "completion_set_edge",
                "completion_set_edge",
                "base",
                {"max_bond_cost": "0.98"},
            ),
        ),
        metrics=metrics,
        validation_score=metrics["validation"].score,
        rn1_similarity_score=Decimal("0.50"),
        gap_similarity_score=Decimal("0"),
    )
    return candidate


def _composite_metric(
    split_name: str,
    *,
    net_pnl: str,
    roi: str,
    risk_breaches: int = 0,
    fills: int = 10,
    events: int = 3,
) -> CompositeMetric:
    pnl = Decimal(net_pnl)
    roi_d = Decimal(roi)
    return CompositeMetric(
        split_name=split_name,
        candidate_signals_count=fills,
        accepted_orders_count=fills,
        skipped_orders_count=0,
        simulated_fills_count=fills,
        events_count=events,
        fill_rate_on_candidates=Decimal("1"),
        net_pnl=pnl,
        roi_on_capital=roi_d,
        max_drawdown=Decimal("1"),
        max_event_loss=Decimal("1"),
        capital_required=Decimal("100"),
        turnover=Decimal("100"),
        capital_recycling=Decimal("1"),
        risk_breaches=risk_breaches,
        risk_prevented_count=0,
        concentration=Decimal("0.20"),
        score=roi_d,
        event_rows=(
            {
                "split_name": split_name,
                "event_id": "event_a",
                "total_pnl": str(pnl),
                "fills_count": fills,
                "turnover": "100",
                "max_event_exposure": "10",
            },
        ),
    )


def _search_candidate(
    candidate_index: int,
    *,
    net_pnl: str | None = None,
    train_net_pnl: str | None = None,
    validation_net_pnl: str | None = None,
    test_net_pnl: str | None = None,
    risk_breaches: int = 0,
    ordering: bool = False,
    fills: int = 30,
    train_fills: int | None = None,
    validation_fills: int | None = None,
    test_fills: int | None = None,
) -> SearchCandidate:
    train_net_pnl = train_net_pnl or net_pnl or "10"
    validation_net_pnl = validation_net_pnl or net_pnl or "10"
    test_net_pnl = test_net_pnl or net_pnl or "10"
    train_fills = fills if train_fills is None else train_fills
    validation_fills = fills if validation_fills is None else validation_fills
    test_fills = fills if test_fills is None else test_fills
    metrics = {
        "train": _search_metric(
            "train",
            net_pnl=train_net_pnl,
            risk_breaches=risk_breaches,
            ordering=ordering,
            fills=train_fills,
        ),
        "validation": _search_metric(
            "validation",
            net_pnl=validation_net_pnl,
            risk_breaches=risk_breaches,
            ordering=ordering,
            fills=validation_fills,
        ),
        "test": _search_metric(
            "test",
            net_pnl=test_net_pnl,
            risk_breaches=risk_breaches,
            ordering=ordering,
            fills=test_fills,
        ),
    }
    return SearchCandidate(
        candidate_id=candidate_index,
        candidate_index=candidate_index,
        strategy_name=f"candidate_{candidate_index}",
        parameters={},
        metrics=metrics,
    )


def _search_metric(
    split_name: str,
    *,
    net_pnl: str,
    risk_breaches: int,
    ordering: bool,
    fills: int,
) -> SearchMetric:
    return SearchMetric(
        split_name=split_name,
        candidate_signals_count=5,
        accepted_orders_count=fills,
        skipped_orders_count=0,
        skipped_by_reason={},
        simulated_fills_count=fills,
        fill_rate_on_candidates=Decimal("1"),
        net_pnl=Decimal(net_pnl),
        max_drawdown=Decimal("1"),
        max_inventory=Decimal("1"),
        capital_required=Decimal("10"),
        turnover=Decimal("10"),
        risk_breaches=risk_breaches,
        risk_prevented_count=0,
        ordering_violation=ordering,
        conservative_pass=(Decimal(net_pnl) > Decimal("0") and risk_breaches == 0 and not ordering and fills > 0),
        score=Decimal(net_pnl) / Decimal("10") if Decimal(net_pnl) > Decimal("0") else None,
    )


def _persist_search_candidates(
    session,
    wallet: str,
    rule_name: str,
    candidates: list[SearchCandidate],
) -> None:
    session.execute(
        text(
            "INSERT INTO simulation_strategy_search_runs "
            "(wallet, rule_name, strategy_family, seed, max_combos, total_combos, evaluated_combos, "
            "selected_candidate_id, run_ts, elapsed_ms, status, notes) "
            "VALUES (:wallet, :rule_name, 'test', 2202, :count, :count, :count, NULL, 1, 0, 'complete', '')"
        ),
        {"wallet": wallet, "rule_name": rule_name, "count": len(candidates)},
    )
    run_id = session.execute(text("SELECT last_insert_rowid()")).scalar_one()
    ranked = rank_candidates(candidates)
    selected_id = None
    for rank, candidate in enumerate(ranked, start=1):
        result = session.execute(
            text(
                "INSERT INTO simulation_strategy_candidates "
                "(search_run_id, candidate_index, strategy_name, parameter_json, rank_index, eligible, selected_for_test) "
                "VALUES (:run_id, :candidate_index, :strategy_name, :parameter_json, :rank, 1, :selected)"
            ),
            {
                "run_id": run_id,
                "candidate_index": candidate.candidate_index,
                "strategy_name": candidate.strategy_name,
                "parameter_json": json.dumps(candidate.parameters, sort_keys=True),
                "rank": rank,
                "selected": 1 if rank == 1 else 0,
            },
        )
        candidate_id = int(result.lastrowid)
        if rank == 1:
            selected_id = candidate_id
        _persist_candidate_metrics(session, candidate_id, candidate.metrics)
    if selected_id is not None:
        session.execute(
            text(
                "UPDATE simulation_strategy_search_runs "
                "SET selected_candidate_id = :selected_id WHERE id = :run_id"
            ),
            {"selected_id": selected_id, "run_id": run_id},
        )
    session.commit()


def _persist_candidate_metrics(
    session,
    candidate_id: int,
    metrics: dict[str, SearchMetric],
) -> None:
    for metric in metrics.values():
        session.execute(
            text(
                "INSERT INTO simulation_strategy_candidate_metrics "
                "(candidate_id, split_name, candidate_signals_count, accepted_orders_count, "
                "skipped_orders_count, skipped_by_reason_json, simulated_fills_count, fill_rate_on_candidates, "
                "net_pnl, max_drawdown, max_inventory, capital_required, turnover, risk_breaches, "
                "risk_prevented_count, ordering_violation, conservative_pass, score) "
                "VALUES (:candidate_id, :split_name, :candidate_signals_count, :accepted_orders_count, "
                ":skipped_orders_count, '{}', :simulated_fills_count, :fill_rate_on_candidates, "
                ":net_pnl, :max_drawdown, :max_inventory, :capital_required, :turnover, :risk_breaches, "
                ":risk_prevented_count, :ordering_violation, :conservative_pass, :score)"
            ),
            {
                "candidate_id": candidate_id,
                "split_name": metric.split_name,
                "candidate_signals_count": metric.candidate_signals_count,
                "accepted_orders_count": metric.accepted_orders_count,
                "skipped_orders_count": metric.skipped_orders_count,
                "simulated_fills_count": metric.simulated_fills_count,
                "fill_rate_on_candidates": str(metric.fill_rate_on_candidates),
                "net_pnl": str(metric.net_pnl),
                "max_drawdown": str(metric.max_drawdown),
                "max_inventory": str(metric.max_inventory),
                "capital_required": str(metric.capital_required),
                "turnover": str(metric.turnover),
                "risk_breaches": metric.risk_breaches,
                "risk_prevented_count": metric.risk_prevented_count,
                "ordering_violation": 1 if metric.ordering_violation else 0,
                "conservative_pass": 1 if metric.conservative_pass else 0,
                "score": str(metric.score) if metric.score is not None else None,
            },
        )
