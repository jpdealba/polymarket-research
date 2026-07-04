"""Phase 12 — book sampler tests.

Tests cover:
1. CLOB source adapter: book parsing, empty/one-sided books, spread math
2. Relevant tokens query: open positions, recent trades, closed-market exclusion
3. Sampler: snapshot persistence, rotation, dedup
4. Retention: raw file pruning, raw_ref nulling
"""

import json
import time
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text

from pmresearch.rawstore.store import RawFetchResult, RawStore
from pmresearch.sources.clob import BookSnapshot, ClobSource, _parse_book, _safe_decimal


# --- helpers ----------------------------------------------------------------

_RAW_SEQ = [0]


def _raw_ref(session, source="test", endpoint="activity"):
    _RAW_SEQ[0] += 1
    seq = _RAW_SEQ[0]
    return session.execute(
        text(
            "INSERT INTO raw_fetches (source, endpoint, params_json, fetched_at, "
            "http_status, file_path, content_hash, row_count) "
            "VALUES (:source, :endpoint, :p, 'test', 200, 'none', :h, 0) RETURNING id"
        ),
        {"source": source, "endpoint": endpoint, "p": f'{{"n":{seq}}}', "h": f"hash-{seq}"},
    ).scalar()


def _seed_market(session, condition_id="cond1", token_id="tok0", closed=0):
    # Seed the event first (FK constraint)
    session.execute(
        text(
            "INSERT OR IGNORE INTO pm_events (event_id, title, slug, neg_risk, tags_json) "
            "VALUES ('evt1', 'Test Event', 'test-event', 0, '[]')"
        ),
    )
    session.execute(
        text(
            "INSERT OR REPLACE INTO markets "
            "(condition_id, question, slug, category, event_id, neg_risk, "
            "outcomes_json, clob_token_ids_json, start_date, end_date, closed, "
            "resolution_prices_json, closed_time, structure_type, updated_at) "
            "VALUES (:cid, 'Q', 'q', 'crypto', 'evt1', 0, '[]', '[]', "
            "'2026-01-01', '2026-12-31', :closed, NULL, NULL, 'binary', 'test')"
        ),
        {"cid": condition_id, "closed": closed},
    )
    session.execute(
        text(
            "INSERT OR REPLACE INTO tokens (token_id, condition_id, outcome_index, outcome_label) "
            "VALUES (:tid, :cid, 0, 'Yes')"
        ),
        {"tid": token_id, "cid": condition_id},
    )
    session.commit()


def _seed_trade(session, wallet, token_id, ts=1000):
    raw_ref = _raw_ref(session)
    session.execute(
        text(
            "INSERT INTO wallet_events "
            "(wallet, event_type, ts, tx_hash, condition_id, token_id, side, "
            "delta_shares, delta_usdc, price, usdc_size, source, is_derived, raw_ref, "
            "dedupe_key, ingested_at) "
            "VALUES (:w, 'TRADE', :ts, :tx, 'cond1', :token, 'BUY', '10', '-5', '0.5', '5', "
            "'test', 0, :raw_ref, :key, 'test')"
        ),
        {"w": wallet.lower(), "ts": ts, "tx": f"tx-{ts}", "token": token_id,
         "raw_ref": raw_ref, "key": f"dedupe-{wallet}-{token_id}-{ts}"},
    )
    session.commit()


def _seed_holding(session, wallet, token_id, qty="10"):
    session.execute(
        text(
            "INSERT OR REPLACE INTO holdings "
            "(wallet, token_id, qty, wac_cost, as_of_ts, projection_version) "
            "VALUES (:w, :tid, :qty, '0.5', 1000, 1)"
        ),
        {"w": wallet.lower(), "tid": token_id, "qty": qty},
    )
    session.commit()


# --- CLOB source tests ------------------------------------------------------


class TestClobParseBook:
    def test_normal_two_sided_book(self):
        payload = {
            "market": "mkt",
            "asset_id": "tok1",
            "bids": [
                {"price": "0.50", "size": "100"},
                {"price": "0.48", "size": "200"},
            ],
            "asks": [
                {"price": "0.52", "size": "50"},
                {"price": "0.54", "size": "75"},
            ],
            "hash": "abc",
            "timestamp": "1234",
        }
        raw = RawFetchResult(
            raw_fetch_id=1, file_path=Path("fake"), content_hash="x",
            row_count=1, deduped=False,
        )
        snap = _parse_book("tok1", payload, raw)

        assert snap.token_id == "tok1"
        assert snap.best_bid == Decimal("0.50")
        assert snap.best_ask == Decimal("0.52")
        assert snap.spread == Decimal("0.02")
        assert snap.mid == Decimal("0.51")
        assert snap.has_book is True
        assert len(snap.depth_top["bids"]) == 2
        assert len(snap.depth_top["asks"]) == 2
        assert snap.depth_top["bids"][0]["price"] == "0.50"
        assert snap.depth_top["bids"][1]["price"] == "0.48"
        assert snap.depth_top["asks"][0]["price"] == "0.52"

    def test_one_sided_book_bids_only(self):
        payload = {
            "bids": [{"price": "0.50", "size": "100"}],
            "asks": [],
        }
        raw = RawFetchResult(
            raw_fetch_id=1, file_path=Path("fake"), content_hash="x",
            row_count=1, deduped=False,
        )
        snap = _parse_book("tok1", payload, raw)

        assert snap.best_bid == Decimal("0.50")
        assert snap.best_ask is None
        assert snap.spread is None
        assert snap.mid is None
        assert snap.has_book is True

    def test_empty_book(self):
        payload = {"bids": [], "asks": []}
        raw = RawFetchResult(
            raw_fetch_id=1, file_path=Path("fake"), content_hash="x",
            row_count=1, deduped=False,
        )
        snap = _parse_book("tok1", payload, raw)

        assert snap.best_bid is None
        assert snap.best_ask is None
        assert snap.has_book is False
        assert snap.depth_top is None

    def test_depth_top_10_limit(self):
        bids = [{"price": str(i / 100), "size": "1"} for i in range(15, 0, -1)]
        asks = [{"price": str(i / 100), "size": "1"} for i in range(16, 31)]
        payload = {"bids": bids, "asks": asks}
        raw = RawFetchResult(
            raw_fetch_id=1, file_path=Path("fake"), content_hash="x",
            row_count=1, deduped=False,
        )
        snap = _parse_book("tok1", payload, raw)

        assert len(snap.depth_top["bids"]) == 10
        assert len(snap.depth_top["asks"]) == 10
        assert snap.depth_top["bids"][0]["price"] == "0.15"
        assert snap.depth_top["asks"][0]["price"] == "0.16"

    def test_missing_bids_asks_keys(self):
        payload = {}
        raw = RawFetchResult(
            raw_fetch_id=1, file_path=Path("fake"), content_hash="x",
            row_count=1, deduped=False,
        )
        snap = _parse_book("tok1", payload, raw)
        assert snap.has_book is False


class TestSafeDecimal:
    def test_valid(self):
        assert _safe_decimal("0.50") == Decimal("0.50")

    def test_none(self):
        assert _safe_decimal(None) is None

    def test_invalid(self):
        assert _safe_decimal("abc") is None

    def test_float(self):
        assert _safe_decimal(0.5) == Decimal("0.5")


# --- Relevant tokens tests --------------------------------------------------


class TestRelevantTokens:
    def test_open_position_included(self, settings, session):
        _seed_market(session, "cond1", "tok0")
        _seed_holding(session, "0xabc", "tok0", "10")

        from pmresearch.booksampler.relevant import relevant_token_ids
        tokens = relevant_token_ids(session)
        assert "tok0" in tokens

    def test_recent_trade_included(self, settings, session):
        _seed_market(session, "cond1", "tok0")
        now_ts = int(time.time())
        _seed_trade(session, "0xabc", "tok0", ts=now_ts - 3600)

        from pmresearch.booksampler.relevant import relevant_token_ids
        tokens = relevant_token_ids(session, recent_hours=24)
        assert "tok0" in tokens

    def test_closed_market_excluded(self, settings, session):
        _seed_market(session, "cond1", "tok0", closed=1)
        _seed_holding(session, "0xabc", "tok0", "10")

        from pmresearch.booksampler.relevant import relevant_token_ids
        tokens = relevant_token_ids(session)
        assert "tok0" not in tokens

    def test_dust_holding_excluded(self, settings, session):
        _seed_market(session, "cond1", "tok0")
        _seed_holding(session, "0xabc", "tok0", "0.0000005")

        from pmresearch.booksampler.relevant import relevant_token_ids
        tokens = relevant_token_ids(session, dust_epsilon="0.000001")
        assert "tok0" not in tokens

    def test_stale_trade_excluded(self, settings, session):
        _seed_market(session, "cond1", "tok0")
        now_ts = int(time.time())
        _seed_trade(session, "0xabc", "tok0", ts=now_ts - 86400 * 2)

        from pmresearch.booksampler.relevant import relevant_token_ids
        tokens = relevant_token_ids(session, recent_hours=24)
        assert "tok0" not in tokens

    def test_deduplication(self, settings, session):
        _seed_market(session, "cond1", "tok0")
        _seed_holding(session, "0xabc", "tok0", "10")
        now_ts = int(time.time())
        _seed_trade(session, "0xabc", "tok0", ts=now_ts - 3600)

        from pmresearch.booksampler.relevant import relevant_token_ids
        tokens = relevant_token_ids(session)
        assert tokens.count("tok0") == 1

    def test_empty_when_no_data(self, settings, session):
        from pmresearch.booksampler.relevant import relevant_token_ids
        tokens = relevant_token_ids(session)
        assert tokens == []


# --- Sampler tests ----------------------------------------------------------


class TestSampler:
    def test_book_snapshots_persisted(self, settings, session):
        from pmresearch.booksampler.sampler import _persist_snapshot

        raw_store = RawStore(settings, session)
        raw_ref = _raw_ref(session, "clob", "book")

        file_path = settings.raw_dir / "clob" / "book" / "tok0" / "test.json.gz"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.touch()

        snap = BookSnapshot(
            token_id="tok0",
            best_bid=Decimal("0.50"),
            best_ask=Decimal("0.52"),
            spread=Decimal("0.02"),
            mid=Decimal("0.51"),
            depth_top={"bids": [{"price": "0.50", "size": "100"}], "asks": [{"price": "0.52", "size": "50"}]},
            raw_fetch=RawFetchResult(
                raw_fetch_id=raw_ref,
                file_path=file_path,
                content_hash="abc123",
                row_count=1,
                deduped=False,
            ),
        )

        result = _persist_snapshot(session, raw_store, snap)
        assert result is True

        row = session.execute(
            text("SELECT token_id, best_bid, best_ask, spread, mid FROM book_snapshots WHERE token_id = 'tok0'")
        ).fetchone()
        assert row is not None
        assert row.token_id == "tok0"
        assert row.best_bid == "0.50"
        assert row.best_ask == "0.52"
        assert row.spread == "0.02"
        assert row.mid == "0.51"

    def test_book_snapshot_dedup(self, settings, session):
        from pmresearch.booksampler.sampler import _persist_snapshot

        raw_store = RawStore(settings, session)
        raw_ref = _raw_ref(session, "clob", "book")

        file_path = settings.raw_dir / "clob" / "book" / "tok0" / "test.json.gz"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.touch()

        snap = BookSnapshot(
            token_id="tok0",
            best_bid=Decimal("0.50"),
            best_ask=Decimal("0.52"),
            spread=Decimal("0.02"),
            mid=Decimal("0.51"),
            depth_top=None,
            raw_fetch=RawFetchResult(
                raw_fetch_id=raw_ref,
                file_path=file_path,
                content_hash="abc123",
                row_count=1,
                deduped=False,
            ),
        )

        result1 = _persist_snapshot(session, raw_store, snap)
        assert result1 is True

        result2 = _persist_snapshot(session, raw_store, snap)
        assert result2 is False

        count = session.execute(text("SELECT COUNT(*) FROM book_snapshots")).scalar()
        assert count == 1


# --- Retention tests --------------------------------------------------------


class TestRetention:
    def test_prune_old_raw_files(self, settings, session):
        from pmresearch.booksampler.retention import prune_raw_books

        old_file = settings.raw_dir / "old_file.json.gz"
        old_file.parent.mkdir(parents=True, exist_ok=True)
        old_file.write_text("old data")

        raw_id = session.execute(
            text(
                "INSERT INTO raw_fetches (source, endpoint, params_json, fetched_at, "
                "http_status, file_path, content_hash, row_count) "
                "VALUES ('clob', 'book', '{}', :ft, 200, :fp, 'hash', 1) RETURNING id"
            ),
            {"ft": "2026-01-01T00:00:00+00:00", "fp": str(old_file)},
        ).scalar()
        session.commit()

        session.execute(
            text(
                "INSERT INTO book_snapshots "
                "(token_id, ts, best_bid, best_ask, spread, mid, depth_top_json, raw_ref) "
                "VALUES ('tok0', 1000, '0.5', '0.52', '0.02', '0.51', NULL, :raw_ref)"
            ),
            {"raw_ref": raw_id},
        )
        session.commit()

        stats = prune_raw_books(session, settings, retention_days=7)

        assert stats.raw_files_deleted >= 1
        assert not old_file.exists()

        row = session.execute(
            text("SELECT raw_ref FROM book_snapshots WHERE token_id = 'tok0'")
        ).fetchone()
        assert row.raw_ref is None

    def test_recent_files_not_pruned(self, settings, session):
        from pmresearch.booksampler.retention import prune_raw_books

        recent_file = settings.raw_dir / "recent_file.json.gz"
        recent_file.parent.mkdir(parents=True, exist_ok=True)
        recent_file.write_text("recent data")

        raw_id = session.execute(
            text(
                "INSERT INTO raw_fetches (source, endpoint, params_json, fetched_at, "
                "http_status, file_path, content_hash, row_count) "
                "VALUES ('clob', 'book', '{}', :ft, 200, :fp, 'hash2', 1) RETURNING id"
            ),
            {"ft": "2026-07-04T00:00:00+00:00", "fp": str(recent_file)},
        ).scalar()
        session.commit()

        session.execute(
            text(
                "INSERT INTO book_snapshots "
                "(token_id, ts, best_bid, best_ask, spread, mid, depth_top_json, raw_ref) "
                "VALUES ('tok1', 2000, '0.5', '0.52', '0.02', '0.51', NULL, :raw_ref)"
            ),
            {"raw_ref": raw_id},
        )
        session.commit()

        stats = prune_raw_books(session, settings, retention_days=7)

        assert stats.raw_files_deleted == 0
        assert recent_file.exists()
