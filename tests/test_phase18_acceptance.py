"""Unit tests for the Phase 18 acceptance audit (read-only)."""

import time

from sqlalchemy import text

from pmresearch.config import get_settings
from pmresearch.evidence.phase18_acceptance import analyze_phase18

WALLET = "0xabc0000000000000000000000000000000000001"
TOKEN = "12345"
NAME = "world_cup_2026"


def _seed(session, *, before_ts, trade_ts, after_ts, status="excellent"):
    now = int(time.time())
    session.execute(
        text("INSERT INTO wallets (address, first_seen_at, display_name) "
             "VALUES (:a, 'now', 'Test')"),
        {"a": WALLET},
    )
    session.execute(
        text(
            "INSERT INTO worldcup_tracked_wallets "
            "(wallet, display_name, priority, source, selected_at, is_active) "
            "VALUES (:w, 'Test', 1, 'test', :n, 1)"
        ),
        {"w": WALLET, "n": now},
    )
    wid = session.execute(
        text(
            "INSERT INTO watchlists (name, created_at, updated_at, is_active) "
            "VALUES (:n, :t, :t, 1) RETURNING id"
        ),
        {"n": NAME, "t": now},
    ).scalar()
    session.execute(
        text(
            "INSERT INTO watchlist_tokens "
            "(watchlist_id, token_id, source, priority, first_seen_ts, last_seen_ts, is_active) "
            "VALUES (:wid, :tok, 'test', 10, :t, :t, 1)"
        ),
        {"wid": wid, "tok": TOKEN, "t": now},
    )
    session.execute(
        text(
            "INSERT INTO raw_fetches (source, endpoint, params_json, fetched_at, "
            "http_status, file_path, content_hash, row_count, ingested_at) "
            "VALUES ('fx','activity','{}','now',200,'fx','h1',1,'now')"
        )
    )
    rid = int(session.execute(text("SELECT max(id) FROM raw_fetches")).scalar())
    ev_ids = []
    for _ in range(2):
        eid = session.execute(
            text(
                "INSERT INTO wallet_events "
                "(wallet, event_type, ts, tx_hash, token_id, delta_shares, delta_usdc, "
                "price, usdc_size, source, is_derived, raw_ref, dedupe_key, ingested_at) "
                "VALUES (:w, 'TRADE', :t, :tx, :tok, '0','0','0.5','0','fx',0,:rid,:dk,'now') "
                "RETURNING id"
            ),
            {"w": WALLET, "t": trade_ts, "tx": f"0xtx{len(ev_ids)}", "tok": TOKEN,
             "rid": rid, "dk": f"dk{len(ev_ids)}"},
        ).scalar()
        ev_ids.append(int(eid))
    run_id = session.execute(
        text(
            "INSERT INTO book_sample_runs "
            "(watchlist_id, started_at, finished_at, status) "
            "VALUES (:wid, :t, :t, 'finished') RETURNING id"
        ),
        {"wid": wid, "t": now},
    ).scalar()
    for bts in (before_ts, after_ts):
        session.execute(
            text(
                "INSERT INTO book_snapshots "
                "(token_id, ts, best_bid, best_ask, mid, spread, sample_run_id, watchlist_id) "
                "VALUES (:tok, :ts, '0.4', '0.6', '0.5', '0.2', :run, :wid)"
            ),
            {"tok": TOKEN, "ts": bts, "run": run_id, "wid": wid},
        )
    session.execute(
        text(
            "INSERT INTO maker_fill_context "
            "(event_id, wallet, token_id, trade_ts, trade_utc, role, "
            "book_before_ts, book_before_age_s, book_after_ts, book_after_age_s, "
            "context_status, created_at, updated_at) "
            "VALUES (:eid, :w, :tok, :tt, 'x', 'maker', :bts, :bage, :ats, :aage, "
            ":st, :n, :n)"
        ),
        {"eid": ev_ids[0], "w": WALLET, "tok": TOKEN, "tt": trade_ts, "bts": before_ts,
         "bage": trade_ts - before_ts, "ats": after_ts, "aage": after_ts - trade_ts,
         "st": status, "n": now},
    )
    # a second, stale row so freshness classification spans >= 2 buckets
    session.execute(
        text(
            "INSERT INTO maker_fill_context "
            "(event_id, wallet, token_id, trade_ts, trade_utc, role, "
            "context_status, null_reason, created_at, updated_at) "
            "VALUES (:eid, :w, :tok, :tt, 'x', 'taker', 'missing', "
            "'no_book_before_fill', :n, :n)"
        ),
        {"eid": ev_ids[1], "w": WALLET, "tok": TOKEN, "tt": trade_ts, "n": now},
    )
    session.commit()


def test_phase18_acceptance_passes_on_clean_state(session, settings):
    now = int(time.time())
    _seed(session, before_ts=now - 100, trade_ts=now - 98, after_ts=now - 95)
    acc = analyze_phase18(session, get_settings())
    by_key = {c.key: c for c in acc.checks}

    assert by_key["watchlist_tokens"].status == "pass"
    assert by_key["books_linked"].status == "pass"
    assert by_key["context_freshness"].status == "pass"
    assert by_key["no_historical_claims"].status == "pass"
    assert by_key["dashboard"].status == "pass"
    assert acc.all_pass

    cov = acc.coverage[0]
    assert cov.total == 2
    assert cov.excellent == 1


def test_no_historical_claims_catches_after_labelled_before(session, settings):
    now = int(time.time())
    # before_ts is AFTER the trade — a post-fill book masquerading as pre-fill
    _seed(session, before_ts=now - 90, trade_ts=now - 100, after_ts=now - 80)
    acc = analyze_phase18(session, get_settings())
    check = {c.key: c for c in acc.checks}["no_historical_claims"]
    assert check.status == "fail"
