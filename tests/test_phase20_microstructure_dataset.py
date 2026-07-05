"""Tests for Phase 20 microstructure + lifecycle dataset builder."""

import json
import time
from decimal import Decimal

from click.testing import CliRunner
from sqlalchemy import text

from pmresearch.cli import main
from pmresearch.context.maker_fills import build_maker_fill_context
from pmresearch.microstructure.dataset import (
    build_microstructure_dataset,
    dataset_stats,
    export_dataset,
)
from pmresearch.projections.episodes import rebuild_episodes
from pmresearch.watchlists.world_cup import add_manual_token

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
        {"h": f"h-{_seq[0]}-{time.time_ns()}"},
    ).scalar()


def _seed_market(
    session,
    *,
    condition_id="cond1",
    token0="tok0",
    token1="tok1",
    resolution_prices=None,
):
    resolution_json = json.dumps(resolution_prices) if resolution_prices else None
    session.execute(
        text(
            "INSERT OR REPLACE INTO markets "
            "(condition_id, question, slug, category, event_id, neg_risk, "
            "outcomes_json, clob_token_ids_json, start_date, end_date, closed, "
            "resolution_prices_json, closed_time, structure_type, updated_at) "
            "VALUES (:cid, 'Q?', 'q', 'sports', NULL, 0, '[]', '[]', NULL, NULL, 0, "
            ":rp, NULL, 'binary', 'test')"
        ),
        {"cid": condition_id, "rp": resolution_json},
    )
    session.execute(
        text(
            "INSERT OR REPLACE INTO tokens (token_id, condition_id, outcome_index, outcome_label) "
            "VALUES (:tid, :cid, 0, 'Yes')"
        ),
        {"tid": token0, "cid": condition_id},
    )
    session.execute(
        text(
            "INSERT OR REPLACE INTO tokens (token_id, condition_id, outcome_index, outcome_label) "
            "VALUES (:tid, :cid, 1, 'No')"
        ),
        {"tid": token1, "cid": condition_id},
    )
    session.commit()


def _seed_trade(session, *, wallet, token_id, condition_id, ts, side, shares, price):
    raw_ref = _raw_ref(session)
    _seq[0] += 1
    shares = Decimal(shares)
    price = Decimal(price)
    delta_shares = shares if side == "BUY" else -shares
    delta_usdc = -(shares * price) if side == "BUY" else (shares * price)
    event_id = session.execute(
        text(
            "INSERT INTO wallet_events "
            "(wallet, event_type, ts, tx_hash, condition_id, token_id, side, "
            "delta_shares, delta_usdc, price, usdc_size, source, is_derived, raw_ref, "
            "dedupe_key, ingested_at) "
            "VALUES (:wallet, 'TRADE', :ts, :tx, :cid, :tid, :side, "
            ":ds, :du, :price, :size, 'test', 0, :raw_ref, :dedupe, 'test') "
            "RETURNING id"
        ),
        {
            "wallet": wallet,
            "ts": ts,
            "tx": f"tx{_seq[0]}",
            "cid": condition_id,
            "tid": token_id,
            "side": side,
            "ds": str(delta_shares),
            "du": str(delta_usdc),
            "price": str(price),
            "size": str(shares * price),
            "raw_ref": raw_ref,
            "dedupe": f"d-{_seq[0]}",
        },
    ).scalar()
    session.commit()
    return event_id


def _seed_resolution_settlement(session, *, wallet, token_id, condition_id, ts, qty, price):
    raw_ref = _raw_ref(session)
    _seq[0] += 1
    qty = Decimal(qty)
    price = Decimal(price)
    proceeds = qty * price
    event_id = session.execute(
        text(
            "INSERT INTO wallet_events "
            "(wallet, event_type, ts, tx_hash, condition_id, token_id, side, "
            "delta_shares, delta_usdc, price, usdc_size, source, is_derived, raw_ref, "
            "dedupe_key, ingested_at) "
            "VALUES (:wallet, 'RESOLUTION_SETTLEMENT', :ts, :tx, :cid, :tid, NULL, "
            ":ds, :du, :price, '0', 'derived', 1, :raw_ref, :dedupe, 'test') "
            "RETURNING id"
        ),
        {
            "wallet": wallet,
            "ts": ts,
            "tx": f"tx{_seq[0]}",
            "cid": condition_id,
            "tid": token_id,
            "ds": str(-qty),
            "du": str(proceeds),
            "price": str(price),
            "raw_ref": raw_ref,
            "dedupe": f"d-{_seq[0]}",
        },
    ).scalar()
    session.commit()
    return event_id


def _seed_fill_enrichment(session, *, event_id, role="maker"):
    session.execute(
        text(
            "INSERT INTO fill_enrichment "
            "(event_id, role, order_hash, fee, counterparty, source, enriched_at) "
            "VALUES (:event_id, :role, 'order', '0', NULL, 'test', 'test')"
        ),
        {"event_id": event_id, "role": role},
    )
    session.commit()


def _seed_snapshot(session, *, token_id, ts, best_bid="0.39", best_ask="0.41", mid="0.40", spread="0.02", depth_top_json=None):
    session.execute(
        text(
            "INSERT INTO book_snapshots "
            "(token_id, ts, best_bid, best_ask, spread, mid, depth_top_json, raw_ref) "
            "VALUES (:token_id, :ts, :bid, :ask, :spread, :mid, :depth, NULL)"
        ),
        {
            "token_id": token_id,
            "ts": ts,
            "bid": best_bid,
            "ask": best_ask,
            "spread": spread,
            "mid": mid,
            "depth": depth_top_json,
        },
    )
    session.commit()


def _fetch_row(session, event_id):
    return session.execute(
        text("SELECT * FROM microstructure_lifecycle_dataset WHERE event_id = :e"),
        {"e": event_id},
    ).mappings().fetchone()


def _setup_buy(session, *, condition_id="cond1", token0="tok0", token1="tok1", resolution_prices=None):
    _seed_market(session, condition_id=condition_id, token0=token0, token1=token1, resolution_prices=resolution_prices)
    add_manual_token(session, name="world_cup_2026", token_id=token0)
    buy_id = _seed_trade(
        session, wallet=WALLET, token_id=token0, condition_id=condition_id,
        ts=1000, side="BUY", shares=10, price="0.40",
    )
    _seed_fill_enrichment(session, event_id=buy_id)
    _seed_snapshot(session, token_id=token0, ts=995)
    build_maker_fill_context(session, wallet=WALLET, watchlist="world_cup_2026", max_age_s=60)
    return buy_id


def test_sell_closed_episode(session):
    buy_id = _setup_buy(session)
    _seed_trade(
        session, wallet=WALLET, token_id="tok0", condition_id="cond1",
        ts=1100, side="SELL", shares=10, price="0.50",
    )
    rebuild_episodes(session, WALLET)

    stats = build_microstructure_dataset(session, wallet=WALLET, watchlist="world_cup_2026")
    assert stats.rows_written == 1

    row = _fetch_row(session, buy_id)
    assert row["close_path"] == "SELL"
    assert row["closed_by_sell"] == 1
    assert row["closed_by_merge"] == 0
    assert row["qty_token_before"] == "0"
    assert row["qty_token_after"] == "10"
    assert row["directional_after"] == "10"
    assert row["bond_after"] == "0"
    assert row["realized_pnl_wac"] == "1.00"


def test_resolution_closed_episode(session):
    buy_id = _setup_buy(session, resolution_prices={"tok0": "1.0"})
    _seed_resolution_settlement(
        session, wallet=WALLET, token_id="tok0", condition_id="cond1", ts=1200, qty=10, price="1.0",
    )
    rebuild_episodes(session, WALLET)

    build_microstructure_dataset(session, wallet=WALLET, watchlist="world_cup_2026")
    row = _fetch_row(session, buy_id)
    assert row["close_path"] == "RESOLUTION"
    assert row["closed_by_resolution"] == 1
    assert row["pnl_at_resolution"] == row["realized_pnl_wac"]


def test_open_episode(session):
    buy_id = _setup_buy(session)
    rebuild_episodes(session, WALLET)

    build_microstructure_dataset(session, wallet=WALLET, watchlist="world_cup_2026")
    row = _fetch_row(session, buy_id)
    assert row["close_path"] == "OPEN"
    assert row["closed_by_unresolved_open"] == 1
    assert row["realized_pnl_wac"] is None
    reasons = json.loads(row["null_reasons_json"])
    assert reasons["realized_pnl_wac"] == "position_still_open"


def test_missing_book_depth_is_flagged(session):
    buy_id = _setup_buy(session)
    rebuild_episodes(session, WALLET)

    build_microstructure_dataset(session, wallet=WALLET, watchlist="world_cup_2026")
    row = _fetch_row(session, buy_id)
    assert row["bid_depth_top1"] is None
    reasons = json.loads(row["null_reasons_json"])
    assert reasons["bid_depth_top1"] == "no_book_depth"


def test_dataset_stats(session):
    _setup_buy(session)
    rebuild_episodes(session, WALLET)
    build_microstructure_dataset(session, wallet=WALLET, watchlist="world_cup_2026")

    result = dataset_stats(session, WALLET)
    assert result["total_rows"] == 1
    assert result["by_close_path"] == {"OPEN": 1}


def test_export_csv_and_parquet(session, tmp_path):
    _setup_buy(session)
    rebuild_episodes(session, WALLET)
    build_microstructure_dataset(session, wallet=WALLET, watchlist="world_cup_2026")

    csv_path = tmp_path / "out.csv"
    n = export_dataset(session, WALLET, csv_path, fmt="csv")
    assert n == 1
    assert csv_path.exists()

    parquet_path = tmp_path / "out.parquet"
    n2 = export_dataset(session, WALLET, parquet_path, fmt="parquet")
    assert n2 == 1
    assert parquet_path.exists()


def test_cli_build_stats_export(settings, session, tmp_path):
    _setup_buy(session)
    rebuild_episodes(session, WALLET)

    runner = CliRunner()
    env = {"PMR_DATA_DIR": str(settings.data_dir)}

    result = runner.invoke(
        main, ["dataset", "microstructure", "build", "--wallet", WALLET], env=env
    )
    assert result.exit_code == 0, result.output
    assert "rows_written=1" in result.output

    result = runner.invoke(
        main, ["dataset", "microstructure", "stats", "--wallet", WALLET], env=env
    )
    assert result.exit_code == 0, result.output
    assert "total_rows=1" in result.output

    out_path = tmp_path / "cli_export.csv"
    result = runner.invoke(
        main,
        [
            "dataset", "microstructure", "export",
            "--wallet", WALLET, "--out", str(out_path), "--format", "csv",
        ],
        env=env,
    )
    assert result.exit_code == 0, result.output
    assert out_path.exists()
