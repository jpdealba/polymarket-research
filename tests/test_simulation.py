"""Focused tests for Phase 22 counterfactual simulation."""

from __future__ import annotations

from decimal import Decimal

import pytest
from click.testing import CliRunner
from sqlalchemy import text

from pmresearch.cli import main
from pmresearch.simulation.engine import (
    DECISION_CONTEXT_FIELDS,
    GAP_WALLET,
    PROHIBITED_DECISION_FIELDS,
    RN1_WALLET,
    DecisionContext,
    run_simulation,
    run_strategy_simulation,
)
from pmresearch.simulation.report import generate_compare_report, generate_sim_report
from pmresearch.simulation.risk import RiskLimits
from pmresearch.simulation.scenarios import CONSERVATIVE, MEDIUM, OPTIMISTIC


def _insert_wallet_event(session, wallet: str, event_id: int, ts: int) -> int:
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
            "condition_id": "cond_a",
            "token_id": "tok_a",
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
) -> dict:
    return {
        "event_id": event_id,
        "wallet": wallet.lower(),
        "token_id": "tok_a",
        "condition_id": "cond_a",
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
        event_id = _insert_wallet_event(session, wallet, row["event_id"], row["trade_ts"])
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
