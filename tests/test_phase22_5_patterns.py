"""Tests for Phase 22.5 order timing + pattern mining dataset."""

from __future__ import annotations

import csv
import json
import time
from decimal import Decimal

from click.testing import CliRunner
from sqlalchemy import text

from pmresearch.cli import main
from pmresearch.patterns.dataset import (
    MERGE_TIMING_COLUMNS,
    ORDER_TIMING_COLUMNS,
    PAIR_COMPLETION_COLUMNS,
    TIMELINE_COLUMNS,
    UNPAIRED_DURATION_COLUMNS,
    build_pair_completion_report,
    build_patterns_dataset,
    complete_set_cost,
    edge_per_set,
    paired_qty,
    resolve_order_timing,
    unpaired_no,
    unpaired_yes,
)
from pmresearch.patterns.rule_candidates import RULE_CANDIDATE_COLUMNS, extract_rule_candidates

WALLET = "0xabc"

_seq = [0]


def _raw_ref(session) -> int:
    _seq[0] += 1
    return session.execute(
        text(
            "INSERT INTO raw_fetches "
            "(source, endpoint, params_json, fetched_at, http_status, file_path, content_hash, row_count) "
            "VALUES ('test', 'activity', '{}', 'test', 200, 'none', :h, 0) RETURNING id"
        ),
        {"h": f"phase225-{_seq[0]}-{time.time_ns()}"},
    ).scalar_one()


def _seed_wallet(session):
    session.execute(
        text(
            "INSERT OR REPLACE INTO wallets (address, first_seen_at, display_name) "
            "VALUES (:wallet, 'test', 'RN1')"
        ),
        {"wallet": WALLET},
    )
    session.commit()


def _seed_market(
    session,
    *,
    condition_id="cond1",
    token0="tok0",
    token1="tok1",
    labels=("Alpha", "Beta"),
    event_id="event1",
    category="sports",
):
    session.execute(
        text(
            "INSERT OR REPLACE INTO pm_events (event_id, title, slug, neg_risk, tags_json) "
            "VALUES (:event_id, 'Match', 'match', 0, '[]')"
        ),
        {"event_id": event_id},
    )
    session.execute(
        text(
            "INSERT OR REPLACE INTO markets "
            "(condition_id, question, slug, category, event_id, neg_risk, outcomes_json, "
            "clob_token_ids_json, start_date, end_date, closed, resolution_prices_json, "
            "closed_time, structure_type, updated_at) "
            "VALUES (:cid, :question, :slug, :category, :event_id, 0, '[]', '[]', "
            "'1970-01-01T00:30:00+00:00', '1970-01-01T02:00:00+00:00', 0, NULL, NULL, "
            "'binary', 'test')"
        ),
        {
            "cid": condition_id,
            "question": f"Question {condition_id}",
            "slug": condition_id,
            "category": category,
            "event_id": event_id,
        },
    )
    session.execute(
        text(
            "INSERT OR REPLACE INTO tokens (token_id, condition_id, outcome_index, outcome_label) "
            "VALUES (:tid, :cid, 0, :label)"
        ),
        {"tid": token0, "cid": condition_id, "label": labels[0]},
    )
    session.execute(
        text(
            "INSERT OR REPLACE INTO tokens (token_id, condition_id, outcome_index, outcome_label) "
            "VALUES (:tid, :cid, 1, :label)"
        ),
        {"tid": token1, "cid": condition_id, "label": labels[1]},
    )
    session.commit()


def _seed_trade(session, *, token_id, condition_id="cond1", ts=1000, side="BUY", shares="10", price="0.40"):
    _seq[0] += 1
    raw_ref = _raw_ref(session)
    qty = Decimal(shares)
    px = Decimal(price)
    delta_shares = qty if side == "BUY" else -qty
    delta_usdc = -(qty * px) if side == "BUY" else qty * px
    event_id = session.execute(
        text(
            "INSERT INTO wallet_events "
            "(wallet, event_type, ts, tx_hash, condition_id, token_id, side, "
            "delta_shares, delta_usdc, price, usdc_size, source, is_derived, raw_ref, "
            "dedupe_key, ingested_at) "
            "VALUES (:wallet, 'TRADE', :ts, :tx, :cid, :tid, :side, :ds, :du, :price, "
            ":size, 'test', 0, :raw_ref, :dedupe, 'test') RETURNING id"
        ),
        {
            "wallet": WALLET,
            "ts": ts,
            "tx": f"tx{_seq[0]}",
            "cid": condition_id,
            "tid": token_id,
            "side": side,
            "ds": str(delta_shares),
            "du": str(delta_usdc),
            "price": str(px),
            "size": str(abs(delta_usdc)),
            "raw_ref": raw_ref,
            "dedupe": f"trade-{_seq[0]}-{time.time_ns()}",
        },
    ).scalar_one()
    session.commit()
    _seed_enrichment(session, event_id=event_id)
    _seed_all_context(session, event_id=event_id, token_id=token_id, condition_id=condition_id, ts=ts, side=side, shares=shares, price=price)
    return event_id


def _seed_merge(session, *, condition_id="cond1", ts=1100, qty="10"):
    _seq[0] += 1
    raw_ref = _raw_ref(session)
    event_id = session.execute(
        text(
            "INSERT INTO wallet_events "
            "(wallet, event_type, ts, tx_hash, condition_id, token_id, side, "
            "delta_shares, delta_usdc, price, usdc_size, source, is_derived, raw_ref, "
            "dedupe_key, ingested_at) "
            "VALUES (:wallet, 'MERGE', :ts, :tx, :cid, NULL, NULL, :ds, :du, '1', "
            ":size, 'test', 0, :raw_ref, :dedupe, 'test') RETURNING id"
        ),
        {
            "wallet": WALLET,
            "ts": ts,
            "tx": f"merge{_seq[0]}",
            "cid": condition_id,
            "ds": f"-{qty}",
            "du": qty,
            "size": qty,
            "raw_ref": raw_ref,
            "dedupe": f"merge-{_seq[0]}-{time.time_ns()}",
        },
    ).scalar_one()
    session.commit()
    return event_id


def _seed_enrichment(session, *, event_id, role="maker", order_hash=None, source="test"):
    session.execute(
        text(
            "INSERT OR REPLACE INTO fill_enrichment "
            "(event_id, role, order_hash, fee, counterparty, source, enriched_at) "
            "VALUES (:event_id, :role, :order_hash, '0', NULL, :source, 'test')"
        ),
        {"event_id": event_id, "role": role, "order_hash": order_hash or f"0xorder{event_id}", "source": source},
    )
    session.commit()


def _seed_all_context(session, *, event_id, token_id, condition_id, ts, side, shares, price, role="maker"):
    session.execute(
        text(
            "INSERT OR REPLACE INTO all_fill_context "
            "(event_id, wallet, token_id, condition_id, trade_ts, trade_utc, side, fill_price, "
            "fill_size, fill_shares, fill_notional_usdc, delta_usdc, role, book_before_ts, "
            "book_before_age_s, best_bid_before, best_ask_before, spread_before, mid_before, "
            "depth_top_before_json, book_after_ts, book_after_age_s, best_bid_after, best_ask_after, "
            "spread_after, mid_after, depth_top_after_json, context_status, null_reason, created_at, updated_at) "
            "VALUES (:event_id, :wallet, :tid, :cid, :ts, :utc, :side, :price, :notional, "
            ":shares, :notional, :du, :role, :before_ts, 1, :bid, :ask, '0.02', :mid, NULL, "
            ":after_ts, 1, :bid, :ask, '0.02', :mid, NULL, 'excellent', NULL, :ts, :ts)"
        ),
        {
            "event_id": event_id,
            "wallet": WALLET,
            "tid": token_id,
            "cid": condition_id,
            "ts": ts,
            "utc": f"1970-01-01T00:{ts // 60:02d}:00+00:00",
            "side": side,
            "price": price,
            "notional": str(Decimal(shares) * Decimal(price)),
            "shares": shares,
            "du": str(-(Decimal(shares) * Decimal(price)) if side == "BUY" else Decimal(shares) * Decimal(price)),
            "role": role,
            "before_ts": ts - 1,
            "after_ts": ts + 1,
            "bid": price,
            "ask": price,
            "mid": price,
        },
    )
    session.commit()


def _seed_snapshot(session, *, token_id="tok0", ts=990, bid="0.40", ask="0.42"):
    session.execute(
        text(
            "INSERT OR REPLACE INTO book_snapshots "
            "(token_id, ts, best_bid, best_ask, spread, mid, depth_top_json, raw_ref) "
            "VALUES (:tid, :ts, :bid, :ask, '0.02', '0.41', :depth, NULL)"
        ),
        {
            "tid": token_id,
            "ts": ts,
            "bid": bid,
            "ask": ask,
            "depth": json.dumps({"bids": [{"price": bid, "size": "100"}], "asks": [{"price": ask, "size": "50"}]}),
        },
    )
    session.commit()


def _read_csv(path):
    with path.open("r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _write_csv(path, columns, rows):
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def test_inventory_before_after_and_pair_math(session, tmp_path):
    _seed_wallet(session)
    _seed_market(session, labels=("Home", "Away"))
    _seed_snapshot(session, token_id="tok0", ts=990, bid="0.40")
    _seed_snapshot(session, token_id="tok1", ts=1005, bid="0.57")
    first = _seed_trade(session, token_id="tok0", ts=1000, shares="10", price="0.40")
    second = _seed_trade(session, token_id="tok1", ts=1010, shares="4", price="0.57")

    build_patterns_dataset(session, wallet=WALLET, out_dir=tmp_path)
    rows = {int(r["fill_event_id"]): r for r in _read_csv(tmp_path / "order_timing_dataset.csv")}

    assert rows[first]["qty_yes_before"] == "0"
    assert rows[first]["qty_no_before"] == "0"
    assert rows[first]["qty_yes_after"] == "10"
    assert rows[first]["qty_no_after"] == "0"
    assert rows[second]["qty_yes_before"] == "10"
    assert rows[second]["qty_no_before"] == "0"
    assert rows[second]["qty_yes_after"] == "10"
    assert rows[second]["qty_no_after"] == "4"
    assert paired_qty(Decimal("10"), Decimal("4")) == Decimal("4")
    assert unpaired_yes(Decimal("10"), Decimal("4")) == Decimal("6")
    assert unpaired_no(Decimal("10"), Decimal("4")) == Decimal("0")
    assert Decimal(rows[second]["bond_delta"]) == Decimal("4")
    assert Decimal(rows[first]["unpaired_delta"]) == Decimal("10")


def test_pair_completion_cost_edge_and_label_independent_complement(session, tmp_path):
    _seed_wallet(session)
    _seed_market(session, token0="alpha", token1="beta", labels=("NotYes", "DefinitelyNotNo"))
    _seed_snapshot(session, token_id="alpha", ts=990, bid="0.41")
    _seed_snapshot(session, token_id="beta", ts=1020, bid="0.56")
    _seed_trade(session, token_id="alpha", ts=1000, shares="10", price="0.41")
    _seed_trade(session, token_id="beta", ts=1030, shares="6", price="0.56")

    build_patterns_dataset(session, wallet=WALLET, out_dir=tmp_path)
    pair_rows = _read_csv(tmp_path / "pair_completion_report.csv")
    completed = [r for r in pair_rows if r["completion_confidence"] != "not_completed"][0]

    assert completed["time_to_complement_s"] == "30"
    assert completed["complement_token_id"] == "beta"
    assert Decimal(completed["complete_set_cost"]) == Decimal("0.97")
    assert Decimal(completed["edge_per_set"]) == Decimal("0.03")
    assert complete_set_cost(Decimal("0.41"), Decimal("0.56")) == Decimal("0.97")
    assert edge_per_set(Decimal("0.97")) == Decimal("0.03")


def test_merge_timing_and_batches(session, tmp_path):
    _seed_wallet(session)
    _seed_market(session)
    _seed_snapshot(session, token_id="tok0", ts=990, bid="0.40")
    _seed_snapshot(session, token_id="tok1", ts=1005, bid="0.58")
    _seed_trade(session, token_id="tok0", ts=1000, shares="10", price="0.40")
    _seed_trade(session, token_id="tok1", ts=1010, shares="10", price="0.58")
    _seed_merge(session, ts=1020, qty="5")
    _seed_merge(session, ts=1024, qty="5")

    build_patterns_dataset(session, wallet=WALLET, out_dir=tmp_path)
    merges = _read_csv(tmp_path / "merge_timing_report.csv")

    assert len(merges) == 2
    assert merges[0]["time_from_last_complement_fill_s"] == "10"
    assert merges[1]["time_from_last_complement_fill_s"] == "14"
    assert merges[0]["merge_batch_id"] == merges[1]["merge_batch_id"]
    assert merges[0]["merge_batch_id"]


def test_order_timing_exact_priority_and_unknown(session):
    _seed_market(session)
    _seed_snapshot(session, token_id="tok0", ts=900, bid="0.40")
    session.execute(
        text(
            "CREATE TABLE orders (order_hash TEXT PRIMARY KEY, created_ts INTEGER, cancelled_ts INTEGER, source TEXT)"
        )
    )
    session.execute(
        text("INSERT INTO orders (order_hash, created_ts, cancelled_ts, source) VALUES ('0xabc', 800, NULL, 'rpc')")
    )
    session.commit()

    timing = resolve_order_timing(
        session,
        order_hash="0xabc",
        token_id="tok0",
        side="BUY",
        role="maker",
        fill_ts=1000,
        fill_price=Decimal("0.40"),
        lookback_s=7200,
        tolerance_bps=5,
    )
    assert timing["order_time_confidence"] == "exact_onchain"
    assert timing["order_created_ts"] == 800
    assert timing["order_first_seen_book_ts"] is None

    unknown = resolve_order_timing(
        session,
        order_hash=None,
        token_id="tok0",
        side="BUY",
        role="taker",
        fill_ts=1000,
        fill_price=Decimal("0.40"),
        lookback_s=7200,
        tolerance_bps=5,
    )
    assert unknown["order_time_confidence"] == "fill_only_unknown"


def test_feature_availability_does_not_mark_post_fill_as_pre_fill(session, tmp_path):
    _seed_wallet(session)
    _seed_market(session)
    _seed_snapshot(session, token_id="tok0", ts=990, bid="0.40")
    _seed_trade(session, token_id="tok0", ts=1000, shares="10", price="0.40")

    build_patterns_dataset(session, wallet=WALLET, out_dir=tmp_path)
    rows = _read_csv(tmp_path / "order_timing_dataset.csv")

    assert rows[0]["feature_availability"] == "post_fill_diagnostic"
    assert rows[0]["qty_yes_after"] != ""


def test_cli_patterns_build_report_export(settings, session, tmp_path):
    _seed_wallet(session)
    _seed_market(session)
    _seed_snapshot(session, token_id="tok0", ts=990, bid="0.40")
    _seed_trade(session, token_id="tok0", ts=1000, shares="10", price="0.40")

    runner = CliRunner()
    env = {"PMR_DATA_DIR": str(settings.data_dir)}
    out_dir = tmp_path / "patterns"
    result = runner.invoke(
        main,
        ["patterns", "build", "--wallet", WALLET, "--out-dir", str(out_dir)],
        env=env,
    )
    assert result.exit_code == 0, result.output
    assert (out_dir / "order_timing_dataset.csv").exists()
    assert (out_dir / "pattern_mining_summary.md").exists()

    result = runner.invoke(
        main,
        ["patterns", "report", "--wallet", WALLET, "--out-dir", str(out_dir)],
        env=env,
    )
    assert result.exit_code == 0, result.output
    assert "wrote" in result.output

    export_path = tmp_path / "export.csv"
    result = runner.invoke(
        main,
        ["patterns", "export", "--wallet", WALLET, "--out", str(export_path)],
        env=env,
    )
    assert result.exit_code == 0, result.output
    assert export_path.exists()


def test_pair_completion_pure_function_detects_next_complement():
    rows = [
        {
            "wallet": WALLET,
            "side": "BUY",
            "event_id": "event1",
            "condition_id": "cond1",
            "question": "Q",
            "token_id": "tok0",
            "yes_token_id": "tok0",
            "no_token_id": "tok1",
            "outcome_label": "A",
            "fill_ts": 100,
            "fill_event_id": 1,
            "fill_price": "0.42",
            "fill_size": "5",
        },
        {
            "wallet": WALLET,
            "side": "BUY",
            "event_id": "event1",
            "condition_id": "cond1",
            "question": "Q",
            "token_id": "tok1",
            "yes_token_id": "tok0",
            "no_token_id": "tok1",
            "outcome_label": "B",
            "fill_ts": 145,
            "fill_event_id": 2,
            "fill_price": "0.55",
            "fill_size": "3",
        },
    ]

    report = build_pair_completion_report(rows)

    assert report[0]["time_to_complement_s"] == 45
    assert report[0]["completed_pair_qty"] == "3"
    assert report[0]["complete_set_cost"] == "0.97"


def test_phase22_5b_rule_extraction_outputs_and_censoring(tmp_path):
    out_dir = tmp_path / "patterns"
    out_dir.mkdir()
    wallet = WALLET
    base = {
        "wallet": wallet,
        "wallet_label": "RN1",
        "event_title": "Fixture Event",
        "side": "BUY",
        "role": "maker",
        "order_time_confidence": "estimated_book_seen",
        "feature_availability": "post_fill_diagnostic",
        "market_family": "unknown",
        "fill_size": "10",
        "fill_notional_usdc": "4",
        "event_market_count_active_before": "2",
        "event_unpaired_inventory_before": "10",
        "event_bond_qty_before": "0",
        "event_market_count_active_after": "2",
        "event_unpaired_inventory_after": "5",
        "event_bond_qty_after": "5",
    }
    order_rows = [
        {
            **base,
            "fill_event_id": "1",
            "event_id": "closed1",
            "condition_id": "cond1",
            "question": "Closed condition",
            "token_id": "yes1",
            "fill_token_side": "YES",
            "fill_ts": "100",
            "fill_utc": "1970-01-01T00:01:40+00:00",
            "fill_price": "0.40",
            "qty_yes_before": "0",
            "qty_no_before": "0",
            "qty_yes_after": "10",
            "qty_no_after": "0",
            "paired_qty_before": "0",
            "paired_qty_after": "0",
            "unpaired_yes_before": "0",
            "unpaired_no_before": "0",
            "unpaired_yes_after": "10",
            "unpaired_no_after": "0",
            "bond_delta": "0",
            "unpaired_delta": "10",
            "event_phase": "post_event",
        },
        {
            **base,
            "fill_event_id": "2",
            "event_id": "closed1",
            "condition_id": "cond1",
            "question": "Closed condition",
            "token_id": "no1",
            "fill_token_side": "NO",
            "fill_ts": "145",
            "fill_utc": "1970-01-01T00:02:25+00:00",
            "fill_price": "0.57",
            "qty_yes_before": "10",
            "qty_no_before": "0",
            "qty_yes_after": "10",
            "qty_no_after": "5",
            "paired_qty_before": "0",
            "paired_qty_after": "5",
            "unpaired_yes_before": "10",
            "unpaired_no_before": "0",
            "unpaired_yes_after": "5",
            "unpaired_no_after": "0",
            "bond_delta": "5",
            "unpaired_delta": "-5",
            "event_phase": "post_event",
        },
        {
            **base,
            "fill_event_id": "3",
            "event_id": "live1",
            "condition_id": "cond2",
            "question": "Live condition",
            "token_id": "yes2",
            "fill_token_side": "YES",
            "fill_ts": "200",
            "fill_utc": "1970-01-01T00:03:20+00:00",
            "fill_price": "0.44",
            "qty_yes_before": "0",
            "qty_no_before": "0",
            "qty_yes_after": "10",
            "qty_no_after": "0",
            "paired_qty_before": "0",
            "paired_qty_after": "0",
            "unpaired_yes_before": "0",
            "unpaired_no_before": "0",
            "unpaired_yes_after": "10",
            "unpaired_no_after": "0",
            "bond_delta": "0",
            "unpaired_delta": "10",
            "event_phase": "early_live",
        },
    ]
    _write_csv(out_dir / "order_timing_dataset.csv", ORDER_TIMING_COLUMNS, order_rows)
    _write_csv(out_dir / "condition_inventory_timeline.csv", TIMELINE_COLUMNS, order_rows)
    _write_csv(
        out_dir / "pair_completion_report.csv",
        PAIR_COMPLETION_COLUMNS,
        [
            {
                "wallet": wallet,
                "event_id": "closed1",
                "condition_id": "cond1",
                "question": "Closed condition",
                "first_leg_token_id": "yes1",
                "first_leg_price": "0.40",
                "first_leg_qty": "10",
                "complement_token_id": "no1",
                "complement_fill_ts": "145",
                "complement_fill_price": "0.57",
                "complement_fill_qty": "5",
                "time_to_complement_s": "45",
                "completed_pair_qty": "5",
                "complete_set_cost": "0.97",
                "completion_confidence": "observed_later_complement_fill",
            },
            {
                "wallet": wallet,
                "event_id": "live1",
                "condition_id": "cond2",
                "question": "Live condition",
                "first_leg_token_id": "yes2",
                "first_leg_price": "0.44",
                "first_leg_qty": "10",
                "completion_confidence": "not_completed",
            },
        ],
    )
    _write_csv(
        out_dir / "merge_timing_report.csv",
        MERGE_TIMING_COLUMNS,
        [
            {
                "wallet": wallet,
                "event_id": "closed1",
                "condition_id": "cond1",
                "question": "Closed condition",
                "merge_ts": "180",
                "merge_qty": "5",
                "time_from_last_complement_fill_s": "35",
                "capital_released": "5",
                "merge_batch_id": "merge_batch_1",
            }
        ],
    )
    _write_csv(
        out_dir / "unpaired_inventory_duration_report.csv",
        UNPAIRED_DURATION_COLUMNS,
        [
            {
                "wallet": wallet,
                "event_id": "closed1",
                "condition_id": "cond1",
                "question": "Closed condition",
                "duration_s": "80",
                "resolved_by": "redeem",
            },
            {
                "wallet": wallet,
                "event_id": "live1",
                "condition_id": "cond2",
                "question": "Live condition",
                "duration_s": "120",
                "resolved_by": "still_open",
            },
        ],
    )
    (out_dir / "pattern_mining_summary.md").write_text("# summary\n", encoding="utf-8")

    stats = extract_rule_candidates(out_dir)

    assert stats.rules == 7
    candidates = _read_csv(out_dir / "rule_candidates.csv")
    assert set(RULE_CANDIDATE_COLUMNS).issubset(candidates[0].keys())
    rule_a = next(row for row in candidates if row["rule_id"] == "A")
    assert rule_a["lifecycle_sample_scope"].startswith("closed_complete_only_view")
    assert rule_a["closed_complete_events_supported"] == "1"
    assert rule_a["lifecycle_metrics_reliable"] == "0"
    quality = (out_dir / "pattern_quality_report.md").read_text(encoding="utf-8")
    assert "live_or_in_progress" in quality

    runner = CliRunner()
    result = runner.invoke(main, ["patterns", "extract-rules", "--in-dir", str(out_dir)])
    assert result.exit_code == 0, result.output
    assert "rule_candidate_extraction_report.md" in result.output
