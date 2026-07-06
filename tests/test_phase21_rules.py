"""Phase 21 — interpretable rule reconstruction.

Tests for candidate rules, temporal splits, evaluation metrics, and
the fit/evaluate pipeline.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest
from click.testing import CliRunner
from sqlalchemy import text

from pmresearch.cli import main
from pmresearch.rules.base import (
    FORBIDDEN_FEATURES,
    FutureFeatureAccessError,
    RuleDecision,
    SplitMetrics,
    apply_rule_no_future,
    compute_split_metrics,
    promotion_eligible,
    temporal_split,
)
from pmresearch.rules.candidate_rules import (
    ClosedCycleEventTrading,
    CompletionSetEdge,
    CorrelatedSiblingMarkets,
    DepthImbalance,
    EventTiming,
    InventoryBalancing,
    SpreadCapture,
    default_rule_instances,
)
from pmresearch.rules.evaluate import (
    evaluate_rule,
    explain_fill,
    fetch_candidates,
    fit_result_from_eval,
    store_fit_result,
)
from pmresearch.rules.fit import fit_rules
from pmresearch.rules.report import generate_rule_report

D = Decimal


# ── helpers ──────────────────────────────────────────────────────────────────


def _row(**kwargs) -> dict:
    """Build a minimal dataset row with defaults for required fields."""
    defaults = {
        "event_id": "1",
        "wallet": "0xtest",
        "token_id": "tok_a",
        "condition_id": "cond_1",
        "trade_ts": "1000000",
        "trade_utc": "2026-01-01T00:00:00+00:00",
        "side": "BUY",
        "fill_price": "0.50",
        "fill_shares": "100",
        "fill_notional_usdc": "50",
        "fill_size": "50",
        "delta_usdc": "-50",
        "role": "TAKER",
        "context_status": "good",
        "book_before_age_s": "5",
        "spread_bps": "100",
        "best_bid_before": "0.48",
        "best_ask_before": "0.52",
        "mid_before": "0.50",
        "bid_depth_top1": "1000",
        "ask_depth_top1": "800",
        "bid_depth_top5": "5000",
        "ask_depth_top5": "3000",
        "book_imbalance_top1": "0.11",
        "book_imbalance_top5": "0.25",
        "trade_hour_utc": "14",
        "market_category": "Sports",
        "time_to_event_start_s": "3600",
        "qty_token_before": "0",
        "qty_complement_before": "0",
        "directional_before": "0",
        "bond_before": "0",
        "bond_ratio_before": "0",
        "event_exposure_before": None,
        "structure_type": None,
    }
    defaults.update(kwargs)
    return defaults


# ── Spread Capture ───────────────────────────────────────────────────────────


class TestSpreadCapture:
    def test_fires_on_wide_spread_favorable_fill(self):
        rule = SpreadCapture(min_spread_bps=D("50"), min_fill_improvement_bps=D("10"))
        row = _row(spread_bps="100", mid_before="0.50", fill_price="0.49", side="BUY")
        dec = rule.applies(row)
        assert dec.applies is True
        assert "spread" in dec.explanation

    def test_does_not_fire_on_narrow_spread(self):
        rule = SpreadCapture(min_spread_bps=D("50"))
        row = _row(spread_bps="20", mid_before="0.50", fill_price="0.49", side="BUY")
        dec = rule.applies(row)
        assert dec.applies is False
        assert "spread_bps" in dec.explanation

    def test_does_not_fire_on_unfavorable_fill(self):
        rule = SpreadCapture(min_spread_bps=D("50"), min_fill_improvement_bps=D("10"))
        row = _row(spread_bps="100", mid_before="0.50", fill_price="0.51", side="BUY")
        dec = rule.applies(row)
        assert dec.applies is False

    def test_sell_favorable_fill(self):
        rule = SpreadCapture(min_spread_bps=D("50"), min_fill_improvement_bps=D("10"))
        row = _row(spread_bps="100", mid_before="0.50", fill_price="0.51", side="SELL")
        dec = rule.applies(row)
        assert dec.applies is True

    def test_missing_features(self):
        rule = SpreadCapture()
        row = _row(spread_bps=None, mid_before="0.50", fill_price="0.49", side="BUY")
        dec = rule.applies(row)
        assert dec.applies is False
        assert "missing" in dec.explanation


# ── Inventory Balancing ──────────────────────────────────────────────────────


class TestInventoryBalancing:
    def test_fires_when_directional_reduced(self):
        rule = InventoryBalancing(
            require_directional_reduction=True,
            require_bond_increase=False,
            min_abs_directional_before=D("1"),
        )
        # SELL into long position = directional reduction
        row = _row(
            directional_before="10",
            bond_before="0", bond_after="0",
            side="SELL",
        )
        dec = rule.applies(row)
        assert dec.applies is True
        assert "directional_before" in dec.features_used

    def test_fires_when_bond_increased(self):
        rule = InventoryBalancing(
            require_directional_reduction=False,
            require_bond_increase=True,
            min_abs_directional_before=D("1"),
        )
        # BUY with complement position = bond increase
        row = _row(
            directional_before="10",
            bond_before="5", bond_after="8",
            side="BUY", qty_complement_before="5",
        )
        dec = rule.applies(row)
        assert dec.applies is True

    def test_does_not_fire_when_neither(self):
        rule = InventoryBalancing(
            require_directional_reduction=True,
            require_bond_increase=True,
            min_abs_directional_before=D("1"),
        )
        # BUY into long (not reducing) + no complement (not bonding)
        row = _row(
            directional_before="10",
            bond_before="5", bond_after="3",
            side="BUY", qty_complement_before="0",
        )
        dec = rule.applies(row)
        assert dec.applies is False

    def test_below_min_directional_threshold(self):
        rule = InventoryBalancing(min_abs_directional_before=D("5"))
        row = _row(
            directional_before="2", directional_after="1",
            bond_before="0", bond_after="1",
            side="SELL",
        )
        dec = rule.applies(row)
        assert dec.applies is False
        assert "threshold" in dec.explanation

    def test_missing_data(self):
        rule = InventoryBalancing()
        row = _row(directional_before=None, bond_before=None)
        dec = rule.applies(row)
        assert dec.applies is False


# ── Completion Set Edge ──────────────────────────────────────────────────────


class TestCompletionSetEdge:
    def test_fires_when_cost_below_one(self):
        rule = CompletionSetEdge(max_bond_cost=D("0.98"))
        row = _row(
            fill_price="0.45", best_ask_before="0.50", side="BUY",
            qty_token_before="0", qty_complement_before="0",
        )
        dec = rule.applies(row)
        assert dec.applies is True
        assert "0.95" in dec.explanation

    def test_does_not_fire_when_cost_above_one(self):
        rule = CompletionSetEdge(max_bond_cost=D("0.98"))
        row = _row(
            fill_price="0.55", best_ask_before="0.50", side="BUY",
            qty_token_before="0", qty_complement_before="0",
        )
        dec = rule.applies(row)
        assert dec.applies is False

    def test_respects_max_bond_cost_threshold_below_one(self):
        rule = CompletionSetEdge(max_bond_cost=D("0.95"))
        row = _row(
            fill_price="0.47", best_ask_before="0.50", side="BUY",
            qty_token_before="0", qty_complement_before="0",
        )
        dec = rule.applies(row)
        assert dec.applies is False
        assert "0.95" in dec.explanation

    def test_sell_considers_complement_as_main(self):
        rule = CompletionSetEdge(max_bond_cost=D("0.98"))
        row = _row(
            fill_price="0.55", best_ask_before="0.40", side="SELL",
            qty_token_before="0", qty_complement_before="0",
        )
        dec = rule.applies(row)
        # SELL: cost_this = best_ask_before (0.40) + cost_complement = fill_price (0.55) = 0.95
        assert dec.applies is True


# ── Depth Imbalance ──────────────────────────────────────────────────────────


class TestDepthImbalance:
    def test_fires_on_strong_imbalance_favourable_side(self):
        rule = DepthImbalance(min_imbalance=D("0.3"), require_favourable_side=True)
        row = _row(book_imbalance_top5="0.5", side="BUY")
        dec = rule.applies(row)
        assert dec.applies is True

    def test_does_not_fire_on_weak_imbalance(self):
        rule = DepthImbalance(min_imbalance=D("0.3"))
        row = _row(book_imbalance_top5="0.1", side="BUY")
        dec = rule.applies(row)
        assert dec.applies is False

    def test_does_not_fire_on_unfavourable_side(self):
        rule = DepthImbalance(min_imbalance=D("0.3"), require_favourable_side=True)
        row = _row(book_imbalance_top5="-0.5", side="BUY")
        dec = rule.applies(row)
        assert dec.applies is False
        assert "unfavorable" in dec.explanation

    def test_fires_without_side_check(self):
        rule = DepthImbalance(min_imbalance=D("0.3"), require_favourable_side=False)
        row = _row(book_imbalance_top5="-0.5", side="BUY")
        dec = rule.applies(row)
        assert dec.applies is True


# ── Event Timing ─────────────────────────────────────────────────────────────


class TestEventTiming:
    def test_fires_in_allowed_hours(self):
        rule = EventTiming(allowed_hours_utc=(12, 13, 14, 15))
        row = _row(trade_hour_utc="14")
        dec = rule.applies(row)
        assert dec.applies is True

    def test_does_not_fire_outside_hours(self):
        rule = EventTiming(allowed_hours_utc=(12, 13, 14, 15))
        row = _row(trade_hour_utc="3")
        dec = rule.applies(row)
        assert dec.applies is False

    def test_fires_all_hours_when_empty_tuple(self):
        rule = EventTiming(allowed_hours_utc=())
        row = _row(trade_hour_utc="23")
        dec = rule.applies(row)
        assert dec.applies is True

    def test_max_time_to_event_start(self):
        rule = EventTiming(max_time_to_event_start_s=3600)
        row = _row(trade_hour_utc="14", time_to_event_start_s="7200")
        dec = rule.applies(row)
        assert dec.applies is False

    def test_min_time_to_event_start(self):
        rule = EventTiming(min_time_to_event_start_s=3600)
        row = _row(trade_hour_utc="14", time_to_event_start_s="1800")
        dec = rule.applies(row)
        assert dec.applies is False

    def test_missing_hour(self):
        rule = EventTiming()
        row = _row(trade_hour_utc=None)
        dec = rule.applies(row)
        assert dec.applies is False


# ── Correlated Sibling Markets ───────────────────────────────────────────────


class TestCorrelatedSiblingMarkets:
    def test_fires_when_exposure_reducing(self):
        rule = CorrelatedSiblingMarkets(min_abs_event_exposure=D("5"))
        # Net long exposure + SELL = improving
        row = _row(event_exposure_before="10", event_exposure_after="5", side="SELL")
        dec = rule.applies(row)
        assert dec.applies is True
        assert "improving" in dec.explanation

    def test_does_not_fire_when_exposure_not_reducing(self):
        rule = CorrelatedSiblingMarkets(min_abs_event_exposure=D("5"))
        # Net long exposure + BUY = not improving
        row = _row(event_exposure_before="10", event_exposure_after="12", side="BUY")
        dec = rule.applies(row)
        assert dec.applies is False

    def test_below_threshold(self):
        rule = CorrelatedSiblingMarkets(min_abs_event_exposure=D("5"))
        row = _row(event_exposure_before="3", event_exposure_after="1", side="SELL")
        dec = rule.applies(row)
        assert dec.applies is False

    def test_missing_exposure(self):
        rule = CorrelatedSiblingMarkets()
        row = _row(event_exposure_before=None, side="SELL")
        dec = rule.applies(row)
        assert dec.applies is False


# ── Closed-Cycle Event Trading ───────────────────────────────────────────────


class TestClosedCycleEventTrading:
    def test_fires_on_all_conditions_met(self):
        rule = ClosedCycleEventTrading(min_spread_bps=D("30"), min_abs_event_exposure=D("5"))
        row = _row(
            spread_bps="50", event_exposure_before="10",
            side="BUY", directional_before="-5",
        )
        dec = rule.applies(row)
        assert dec.applies is True

    def test_does_not_fire_on_narrow_spread(self):
        rule = ClosedCycleEventTrading(min_spread_bps=D("30"))
        row = _row(spread_bps="10", event_exposure_before="10")
        dec = rule.applies(row)
        assert dec.applies is False

    def test_does_not_fire_without_event_exposure(self):
        rule = ClosedCycleEventTrading(min_spread_bps=D("30"))
        row = _row(spread_bps="50", event_exposure_before=None)
        dec = rule.applies(row)
        assert dec.applies is False

    def test_does_not_fire_on_low_event_exposure(self):
        rule = ClosedCycleEventTrading(min_spread_bps=D("30"), min_abs_event_exposure=D("5"))
        row = _row(spread_bps="50", event_exposure_before="2")
        dec = rule.applies(row)
        assert dec.applies is False


# ── temporal split ───────────────────────────────────────────────────────────


class TestTemporalSplit:
    def test_basic_split(self):
        rows = [{"trade_ts": str(i)} for i in range(100)]
        split = temporal_split(rows, train_ratio=0.6, validation_ratio=0.2)
        assert len(split.train) == 60
        assert len(split.validation) == 20
        assert len(split.test) == 20

    def test_empty(self):
        split = temporal_split([], train_ratio=0.6, validation_ratio=0.2)
        assert len(split.train) == 0
        assert len(split.validation) == 0
        assert len(split.test) == 0

    def test_small_dataset(self):
        rows = [{"trade_ts": "1"}, {"trade_ts": "2"}, {"trade_ts": "3"}]
        split = temporal_split(rows, train_ratio=0.6, validation_ratio=0.2)
        assert len(split.train) + len(split.validation) + len(split.test) == 3

    def test_rejects_empty_test_window_ratio(self):
        rows = [{"trade_ts": str(i)} for i in range(10)]
        with pytest.raises(ValueError):
            temporal_split(rows, train_ratio=0.8, validation_ratio=0.2)

    def test_rejects_negative_ratio(self):
        rows = [{"trade_ts": str(i)} for i in range(10)]
        with pytest.raises(ValueError):
            temporal_split(rows, train_ratio=-0.1, validation_ratio=0.2)


# ── metrics ──────────────────────────────────────────────────────────────────


class TestMetrics:
    def test_all_explained(self):
        rows = [_row() for _ in range(10)]
        decisions = {i: RuleDecision(True, {}, "yes") for i in range(10)}
        m = compute_split_metrics(rows, decisions, label_mode=False)
        assert m.total_fills == 10
        assert m.explained_fills == 10
        assert m.fill_explained_rate == D("1")

    def test_none_explained(self):
        rows = [_row() for _ in range(10)]
        decisions = {i: RuleDecision(False, {}, "no") for i in range(10)}
        m = compute_split_metrics(rows, decisions, label_mode=False)
        assert m.explained_fills == 0
        assert m.fill_explained_rate == D("0")

    def test_label_mode_uses_markout(self):
        rows = [_row(markout_5m="0.01", pnl_episode="0.50") for _ in range(5)]
        decisions = {i: RuleDecision(True, {}, "yes") for i in range(5)}
        m = compute_split_metrics(rows, decisions, label_mode=True)
        assert m.avg_markout_5m == D("0.01")
        assert m.avg_pnl_episode == D("0.50")

    def test_label_mode_precision_counts_negative_labels_as_false_positive(self):
        rows = [
            _row(markout_5m="0.01", pnl_episode="0.50"),
            _row(markout_5m="-0.01", pnl_episode="-0.50"),
        ]
        decisions = {i: RuleDecision(True, {}, "yes") for i in range(2)}
        m = compute_split_metrics(rows, decisions, label_mode=True)
        assert m.precision == D("0.5")
        assert m.false_positives == 1
        assert m.false_positive_rate == D("0.5")

    def test_promotion_requires_positive_validation_and_test_signal(self):
        pos_rows = [_row(markout_5m="0.01", pnl_episode="0.50")]
        neg_rows = [_row(markout_5m="-0.01", pnl_episode="-0.50")]
        decs = {0: RuleDecision(True, {}, "yes")}
        val = compute_split_metrics(pos_rows, decs, label_mode=True)
        test = compute_split_metrics(neg_rows, decs, label_mode=True)
        assert promotion_eligible(val, test) is False


# ── forbidden features guard ─────────────────────────────────────────────────


class TestForbiddenFeatures:
    def test_forbidden_set_is_frozen(self):
        assert isinstance(FORBIDDEN_FEATURES, frozenset)

    def test_known_forbidden_features_present(self):
        for f in (
            "markout_5m", "markout_1h", "pnl_episode", "close_path",
            "realized_pnl_wac", "directional_after", "bond_after",
        ):
            assert f in FORBIDDEN_FEATURES

    def test_allowed_features_not_forbidden(self):
        for f in (
            "spread_bps", "mid_before", "fill_price", "side",
            "directional_before", "bond_before", "qty_token_before",
            "trade_hour_utc", "time_to_event_start_s", "book_imbalance_top5",
        ):
            assert f not in FORBIDDEN_FEATURES

    def test_guard_blocks_hidden_future_feature_access(self):
        class BadRule:
            name = "bad_rule"
            version = 1
            parameters = {}

            def applies(self, row):
                row.get("markout_5m")
                return RuleDecision(True, {}, "lied about features")

        with pytest.raises(FutureFeatureAccessError):
            apply_rule_no_future(BadRule(), _row(markout_5m="0.25"))

    def test_guard_rejects_reported_forbidden_features(self):
        class BadRule:
            name = "bad_rule"
            version = 1
            parameters = {}

            def applies(self, row):
                return RuleDecision(True, {"pnl_episode": "1.00"}, "reported leak")

        with pytest.raises(FutureFeatureAccessError):
            apply_rule_no_future(BadRule(), _row(pnl_episode="1.00"))


# ── default rule instances ──────────────────────────────────────────────────


class TestDefaultRules:
    def test_all_seven_rules_present(self):
        instances = default_rule_instances()
        assert len(instances) == 7

    def test_names_unique(self):
        names = [r.name for r in default_rule_instances()]
        assert len(names) == len(set(names))

    def test_all_implement_protocol(self):
        for r in default_rule_instances():
            assert hasattr(r, "name")
            assert hasattr(r, "version")
            assert hasattr(r, "applies")
            assert callable(r.applies)

    def test_all_rules_have_parameters(self):
        for r in default_rule_instances():
            params = r.parameters
            assert isinstance(params, dict)


# ── report generation ────────────────────────────────────────────────────────


class TestReport:
    def test_generate_report_smoke(self):
        from pmresearch.rules.base import FitResult

        m_empty = SplitMetrics(
            total_fills=0, explained_fills=0, fill_explained_rate=D("0"),
            false_positives=0, false_positive_rate=D("0"),
            precision=D("0"), coverage=D("0"),
            avg_markout_5m=None, avg_markout_1h=None,
            avg_pnl_episode=None, avg_bond_delta=None,
            avg_exposure_delta=None, max_inventory_required=None,
            out_of_sample_edge_bps=None, out_of_sample_pnl=None,
        )
        result = FitResult(
            rule_name="test_rule",
            rule_version=1,
            parameters={"threshold": "0.5"},
            features_used=["spread_bps", "mid_before"],
            train=m_empty,
            validation=m_empty,
            test=m_empty,
            explained_fills_pct=D("0"),
            expected_pnl_or_markout=None,
            inventory_impact=None,
            risk_requirements="none",
            blind_spots="test blind spot",
            promoted=False,
        )
        report = generate_rule_report(result)
        assert "test_rule" in report
        assert "test blind spot" in report


# ── end-to-end fit ───────────────────────────────────────────────────────────


WALLET = "0x3333333333333333333333333333333333333333"


def _seed_dataset_rows(session, wallet: str, n: int = 20) -> None:
    """Insert synthetic rows into microstructure_lifecycle_dataset.

    Seeds the required parent rows (raw_fetches, wallets, wallet_events) so FK
    constraints are satisfied.
    """
    wallet = wallet.lower()
    # Seed raw_fetches (parent of wallet_events.raw_ref)
    session.execute(
        text(
            "INSERT INTO raw_fetches "
            "(source, endpoint, params_json, fetched_at, http_status, file_path, "
            "content_hash, row_count) "
            "VALUES ('test', 'test_endpoint', '{}', '2026-01-01T00:00:00+00:00', "
            "200, '/tmp/test', 'hash0', 1)"
        ),
    )
    raw_ref_id = session.execute(text("SELECT last_insert_rowid()")).scalar()
    # Seed wallet if not present
    session.execute(
        text(
            "INSERT OR IGNORE INTO wallets (address, first_seen_at, display_name) "
            "VALUES (:a, :fs, :d)"
        ),
        {"a": wallet, "fs": "2026-01-01T00:00:00+00:00", "d": "test_wallet"},
    )
    # Seed wallet_events rows (the parent of microstructure_lifecycle_dataset.event_id)
    for i in range(n):
        ts = 1000000 + i * 60
        session.execute(
            text(
                "INSERT OR IGNORE INTO wallet_events "
                "(id, wallet, event_type, ts, tx_hash, condition_id, token_id, "
                "side, delta_shares, delta_usdc, price, usdc_size, source, "
                "is_derived, raw_ref, dedupe_key, ingested_at) "
                "VALUES (:id, :w, 'TRADE', :ts, :tx, 'cond_1', 'tok_a', "
                "'BUY', '100', '-50', '0.50', '50', 'test', 0, :rr, :dk, :ia)"
            ),
            {"id": i + 1, "w": wallet, "ts": ts, "tx": f"0xtx{i:04d}",
             "rr": raw_ref_id, "dk": f"dk{i:04d}", "ia": "2026-01-01T00:00:00+00:00"},
        )
    session.commit()
    COLS = [
        "event_id", "wallet", "token_id", "condition_id", "trade_ts", "trade_utc",
        "side", "fill_price", "fill_size",
        "delta_usdc", "role", "context_status", "book_before_age_s", "book_after_age_s",
        "best_bid_before", "best_ask_before", "mid_before", "spread_before",
        "spread_bps", "bid_depth_top1", "ask_depth_top1", "bid_depth_top5",
        "ask_depth_top5", "book_imbalance_top1", "book_imbalance_top5",
        "distance_fill_to_mid", "distance_fill_to_bid", "distance_fill_to_ask",
        "fill_inside_spread", "fill_at_best_bid", "fill_at_best_ask",
        "trade_hour_utc", "market_category", "time_to_event_start_s",
        "wallet_label", "qty_token_before", "qty_complement_before",
        "directional_before", "bond_before", "bond_ratio_before",
        "qty_token_after", "qty_complement_after",
        "directional_after", "bond_after", "bond_ratio_after",
        "bond_delta", "directional_delta",
        "event_exposure_before", "event_exposure_after", "event_exposure_delta",
        "close_path", "close_ts", "hold_seconds",
        "realized_pnl_wac", "realized_pnl_per_share", "realized_pnl_bps_on_cost",
        "remaining_open_qty_after_24h", "is_open_after_24h",
        "closed_by_merge", "closed_by_redeem", "closed_by_sell",
        "closed_by_resolution", "closed_by_unresolved_open",
        "markout_5m", "markout_15m", "markout_1h", "markout_24h",
        "pnl_episode", "pnl_at_resolution",
        "null_reasons_json", "dataset_version", "watchlist", "built_at",
    ]
    placeholders = ", ".join([f":p{i}" for i in range(len(COLS))])
    col_list = ", ".join(COLS)
    sql = f"INSERT INTO microstructure_lifecycle_dataset ({col_list}) VALUES ({placeholders})"

    for i in range(n):
        ts = 1000000 + i * 60
        fp = str(D("0.45") + D(str(i % 10)) * D("0.01"))
        mid = "0.50"
        spread_bps = str(50 + i * 10)
        imb5 = "0.4" if i % 3 == 0 else "-0.4"
        hour = str(14 if i < 10 else 3)
        tts = str(3600 if i < 10 else 86400)
        close = "SELL" if i % 5 == 0 else "MERGE"
        m5s = str(D("0.01") * D(str(i % 5 - 2)))
        m1hs = str(D("0.02") * D(str(i % 5 - 2)))
        pnls = str(D("0.50") * D(str(i % 5 - 2)))
        vals = [
            i + 1,                    # event_id
            wallet.lower(),           # wallet
            "tok_a",                  # token_id
            "cond_1",                 # condition_id
            ts,                       # trade_ts
            f"2026-01-01T00:{i:02d}:00+00:00",  # trade_utc
            "BUY",                    # side
            fp,                       # fill_price
            "50",                     # fill_size
            "-50",                    # delta_usdc
            "TAKER",                  # role
            "good",                   # context_status
            "5",                      # book_before_age_s
            None,                     # book_after_age_s
            "0.48",                   # best_bid_before
            "0.52",                   # best_ask_before
            mid,                      # mid_before
            "0.04",                   # spread_before
            spread_bps,               # spread_bps
            "1000",                   # bid_depth_top1
            "800",                    # ask_depth_top1
            "5000",                   # bid_depth_top5
            "3000",                   # ask_depth_top5
            "0.11",                   # book_imbalance_top1
            imb5,                     # book_imbalance_top5
            "0",                      # distance_fill_to_mid
            "-0.02",                  # distance_fill_to_bid
            "0.02",                   # distance_fill_to_ask
            "0",                      # fill_inside_spread
            "0",                      # fill_at_best_bid
            "1",                      # fill_at_best_ask
            hour,                     # trade_hour_utc
            "Sports",                 # market_category
            tts,                      # time_to_event_start_s
            "test_wallet",            # wallet_label
            "0",                      # qty_token_before
            "0",                      # qty_complement_before
            "0",                      # directional_before
            "0",                      # bond_before
            "0",                      # bond_ratio_before
            "0",                      # qty_token_after
            "0",                      # qty_complement_after
            "0",                      # directional_after
            "0",                      # bond_after
            "0",                      # bond_ratio_after
            "0",                      # bond_delta
            "0",                      # directional_delta
            None,                     # event_exposure_before
            None,                     # event_exposure_after
            None,                     # event_exposure_delta
            close,                    # close_path
            None,                     # close_ts
            None,                     # hold_seconds
            None,                     # realized_pnl_wac
            None,                     # realized_pnl_per_share
            None,                     # realized_pnl_bps_on_cost
            None,                     # remaining_open_qty_after_24h
            None,                     # is_open_after_24h
            "0",                      # closed_by_merge
            "0",                      # closed_by_redeem
            "0",                      # closed_by_sell
            "0",                      # closed_by_resolution
            "0",                      # closed_by_unresolved_open
            m5s,                      # markout_5m
            None,                     # markout_15m
            m1hs,                     # markout_1h
            None,                     # markout_24h
            pnls,                     # pnl_episode
            None,                     # pnl_at_resolution
            "{}",                     # null_reasons_json
            1,                        # dataset_version
            "world_cup_2026",         # watchlist
            1700000000,               # built_at
        ]
        assert len(vals) == len(COLS), f"vals={len(vals)} cols={len(COLS)}"
        params = {f"p{j}": v for j, v in enumerate(vals)}
        session.execute(text(sql), params)
    session.commit()


def test_fit_rules_end_to_end(session):
    _seed_dataset_rows(session, WALLET, n=30)
    stats = fit_rules(session, WALLET, rule_names=["spread_capture"])
    assert stats.total_fills == 30
    assert stats.candidates_evaluated == 1
    assert len(stats.results) == 1
    r = stats.results[0]
    assert r.rule_name == "spread_capture"
    assert r.train.total_fills + r.validation.total_fills + r.test.total_fills == 30


def test_fit_rules_all_candidates(session):
    _seed_dataset_rows(session, WALLET, n=50)
    stats = fit_rules(session, WALLET)
    assert stats.total_fills == 50
    assert stats.candidates_evaluated == 7


def test_evaluate_rule_end_to_end(session):
    _seed_dataset_rows(session, WALLET, n=20)
    rule = SpreadCapture(min_spread_bps=D("50"))
    result = evaluate_rule(session, WALLET, rule)
    assert result.wallet == WALLET.lower()
    assert result.rule_name == "spread_capture"
    assert len(result.fill_details) == 20


def test_evaluate_rule_does_not_promote_negative_oos_signal(session):
    class AlwaysRule:
        name = "always_rule"
        version = 1
        description = "always fires"
        parameters = {}

        def applies(self, row):
            return RuleDecision(True, {"spread_bps": row.get("spread_bps")}, "always")

    _seed_dataset_rows(session, WALLET, n=20)
    session.execute(
        text(
            "UPDATE microstructure_lifecycle_dataset "
            "SET markout_5m = CASE WHEN trade_ts < 1000000 + 12 * 60 THEN '0.01' ELSE '-0.01' END, "
            "pnl_episode = CASE WHEN trade_ts < 1000000 + 12 * 60 THEN '0.50' ELSE '-0.50' END "
            "WHERE wallet = :w"
        ),
        {"w": WALLET.lower()},
    )
    session.commit()

    result = evaluate_rule(session, WALLET, AlwaysRule())
    assert result.validation.avg_pnl_episode == D("-0.50")
    assert result.test.avg_pnl_episode == D("-0.50")
    assert result.promoted is False


def test_evaluate_rule_blocks_hidden_future_feature_access(session):
    class BadRule:
        name = "bad_rule"
        version = 1
        description = "leaks"
        parameters = {}

        def applies(self, row):
            row.get("markout_5m")
            return RuleDecision(True, {}, "lied")

    _seed_dataset_rows(session, WALLET, n=5)
    with pytest.raises(FutureFeatureAccessError):
        evaluate_rule(session, WALLET, BadRule())


def test_explain_fill_end_to_end(session):
    _seed_dataset_rows(session, WALLET, n=5)
    rule = SpreadCapture(min_spread_bps=D("50"))
    result = explain_fill(session, 1, rule)
    assert result is not None
    assert result.event_id == 1
    assert isinstance(result.applies, bool)


def test_explain_fill_missing(session):
    rule = SpreadCapture()
    result = explain_fill(session, 999999, rule)
    assert result is None


def test_store_and_fetch_candidates(session):
    _seed_dataset_rows(session, WALLET, n=10)
    rule = SpreadCapture(min_spread_bps=D("50"))
    result = evaluate_rule(session, WALLET, rule)
    fit_result = fit_result_from_eval(result)
    store_fit_result(session, WALLET, fit_result)
    store_fit_result(session, WALLET, fit_result)
    candidates = fetch_candidates(session, WALLET)
    assert len(candidates) == 1
    assert candidates[0].rule_name == "spread_capture"


def test_default_event_timing_rejected_as_no_active_predicate(session):
    _seed_dataset_rows(session, WALLET, n=20)
    session.execute(
        text(
            "UPDATE microstructure_lifecycle_dataset "
            "SET markout_5m = '0.01', pnl_episode = '0.50' "
            "WHERE wallet = :w"
        ),
        {"w": WALLET.lower()},
    )
    session.commit()

    result = evaluate_rule(session, WALLET, EventTiming())
    fit_result = fit_result_from_eval(result)
    assert fit_result.promoted is False
    assert fit_result.promotion_rejection_reason == "no_active_predicate"

    store_fit_result(session, WALLET, fit_result)
    reasons = {
        r[0]
        for r in session.execute(
            text(
                "SELECT DISTINCT promotion_rejection_reason "
                "FROM rule_evaluations "
                "WHERE wallet = :wallet AND rule_name = 'event_timing'"
            ),
            {"wallet": WALLET.lower()},
        ).fetchall()
    }
    assert reasons == {"no_active_predicate"}


def test_rules_report_all_uses_stored_promotion_status(settings, session, tmp_path, monkeypatch):
    _seed_dataset_rows(session, WALLET, n=30)
    spread = fit_result_from_eval(evaluate_rule(session, WALLET, SpreadCapture()))
    completion = fit_result_from_eval(evaluate_rule(session, WALLET, CompletionSetEdge()))
    store_fit_result(session, WALLET, replace(spread, promoted=False))
    store_fit_result(session, WALLET, replace(completion, promoted=True))

    monkeypatch.setenv("PMR_DATA_DIR", str(settings.data_dir))
    runner = CliRunner()

    show_result = runner.invoke(
        main,
        ["rules", "show", "--wallet", WALLET, "--promoted-only"],
    )
    assert show_result.exit_code == 0
    assert "completion_set_edge" in show_result.output
    assert "spread_capture" not in show_result.output

    report_path = tmp_path / "stored_rules.md"
    report_result = runner.invoke(
        main,
        ["rules", "report-all", "--wallet", WALLET, "--out", str(report_path)],
    )
    assert report_result.exit_code == 0
    assert "Recomputing rule evaluations" not in report_result.output

    report = report_path.read_text(encoding="utf-8")
    assert "- Rules promoted: 1" in report
    assert "| completion_set_edge | 1 | yes |" in report
    assert "| spread_capture | 1 | no |" in report

    fresh_report_path = tmp_path / "fresh_rules.md"
    fresh_result = runner.invoke(
        main,
        [
            "rules", "report-all", "--wallet", WALLET,
            "--out", str(fresh_report_path), "--fresh",
        ],
    )
    assert fresh_result.exit_code == 0
    assert "Recomputing rule evaluations; not using stored strategy_candidates." in fresh_result.output


def test_promoted_only_excludes_degenerate_event_timing(settings, session, tmp_path, monkeypatch):
    _seed_dataset_rows(session, WALLET, n=30)
    spread = fit_result_from_eval(evaluate_rule(session, WALLET, SpreadCapture()))
    event_timing = fit_result_from_eval(evaluate_rule(session, WALLET, EventTiming()))
    store_fit_result(session, WALLET, replace(spread, promoted=True, promotion_rejection_reason=None))
    store_fit_result(session, WALLET, event_timing)

    monkeypatch.setenv("PMR_DATA_DIR", str(settings.data_dir))
    runner = CliRunner()

    show_result = runner.invoke(
        main,
        ["rules", "show", "--wallet", WALLET, "--promoted-only"],
    )
    assert show_result.exit_code == 0
    assert "spread_capture" in show_result.output
    assert "event_timing" not in show_result.output

    report_path = tmp_path / "gap_rules.md"
    report_result = runner.invoke(
        main,
        ["rules", "report-all", "--wallet", WALLET, "--out", str(report_path)],
    )
    assert report_result.exit_code == 0
    report = report_path.read_text(encoding="utf-8")
    assert "- Rules promoted: 1" in report
    assert "| spread_capture | 1 | yes |" in report
    assert "| event_timing | 1 | no |" in report


def test_rules_cli_smoke(settings, session, tmp_path, monkeypatch):
    _seed_dataset_rows(session, WALLET, n=20)
    monkeypatch.setenv("PMR_DATA_DIR", str(settings.data_dir))
    runner = CliRunner()

    list_result = runner.invoke(main, ["rules", "list"])
    assert list_result.exit_code == 0
    assert "spread_capture" in list_result.output

    eval_result = runner.invoke(
        main,
        ["rules", "evaluate", "--wallet", WALLET, "--rule", "spread_capture"],
    )
    assert eval_result.exit_code == 0
    assert "rule=spread_capture" in eval_result.output

    report_path = tmp_path / "rule_report.md"
    report_result = runner.invoke(
        main,
        [
            "rules", "report", "--wallet", WALLET, "--rule", "spread_capture",
            "--out", str(report_path),
        ],
    )
    assert report_result.exit_code == 0
    assert report_path.exists()
    assert "Rule Report: spread_capture" in report_path.read_text(encoding="utf-8")

    all_report_path = tmp_path / "all_rules.md"
    all_report_result = runner.invoke(
        main,
        ["rules", "report-all", "--wallet", WALLET, "--out", str(all_report_path)],
    )
    assert all_report_result.exit_code == 0
    assert all_report_path.exists()

    export_path = tmp_path / "fills.csv"
    export_result = runner.invoke(
        main,
        [
            "rules", "export-explained", "--wallet", WALLET,
            "--rule", "spread_capture", "--out", str(export_path),
        ],
    )
    assert export_result.exit_code == 0
    assert export_path.exists()
