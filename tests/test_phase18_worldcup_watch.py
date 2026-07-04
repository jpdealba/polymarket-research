import time
from dataclasses import replace
from decimal import Decimal

from sqlalchemy import text


def _raw_ref(session):
    return session.execute(
        text(
            "INSERT INTO raw_fetches "
            "(source, endpoint, params_json, fetched_at, http_status, file_path, content_hash, row_count) "
            "VALUES ('test', 'activity', '{}', 'test', 200, 'none', :h, 0) RETURNING id"
        ),
        {"h": f"h-{time.time_ns()}"},
    ).scalar()


def _seed_market(session, *, condition_id="cond_wc", token_id="tok_wc", question="World Cup France vs Brazil", closed=0):
    session.execute(
        text(
            "INSERT OR REPLACE INTO markets "
            "(condition_id, question, slug, category, event_id, neg_risk, "
            "outcomes_json, clob_token_ids_json, start_date, end_date, closed, "
            "resolution_prices_json, closed_time, structure_type, updated_at) "
            "VALUES (:cid, :question, :slug, 'sports', NULL, 0, '[]', '[]', "
            "NULL, NULL, :closed, NULL, NULL, 'binary', 'test')"
        ),
        {"cid": condition_id, "question": question, "slug": question.lower().replace(" ", "-"), "closed": closed},
    )
    session.execute(
        text(
            "INSERT OR REPLACE INTO tokens (token_id, condition_id, outcome_index, outcome_label) "
            "VALUES (:tid, :cid, 0, 'Yes')"
        ),
        {"tid": token_id, "cid": condition_id},
    )
    session.commit()


def _seed_trade(session, *, wallet="0xabc", event_id=None, token_id="tok_wc", condition_id="cond_wc", ts=1000):
    raw_ref = _raw_ref(session)
    cols = "" if event_id is None else "id, "
    vals = "" if event_id is None else ":event_id, "
    params = {
        "event_id": event_id,
        "wallet": wallet,
        "token_id": token_id,
        "condition_id": condition_id,
        "ts": ts,
        "raw_ref": raw_ref,
        "dedupe": f"d-{wallet}-{token_id}-{ts}-{time.time_ns()}",
    }
    inserted_id = session.execute(
        text(
            f"INSERT INTO wallet_events "
            f"({cols}wallet, event_type, ts, tx_hash, condition_id, token_id, side, "
            "delta_shares, delta_usdc, price, usdc_size, source, is_derived, raw_ref, "
            "dedupe_key, ingested_at) "
            f"VALUES ({vals}:wallet, 'TRADE', :ts, 'tx', :condition_id, :token_id, "
            "'BUY', '10', '-5', '0.5', '5', 'test', 0, :raw_ref, :dedupe, 'test') "
            "RETURNING id"
        ),
        params,
    ).scalar()
    session.commit()
    return inserted_id


def _seed_holding(session, *, wallet="0xabc", token_id="tok_wc"):
    session.execute(
        text(
            "INSERT OR REPLACE INTO holdings "
            "(wallet, token_id, qty, wac_cost, as_of_ts, projection_version) "
            "VALUES (:wallet, :token_id, '1', '0.5', 1000, 1)"
        ),
        {"wallet": wallet, "token_id": token_id},
    )
    session.commit()


def _seed_snapshot(session, *, token_id="tok_wc", ts=995):
    session.execute(
        text(
            "INSERT INTO book_snapshots "
            "(token_id, ts, best_bid, best_ask, spread, mid, depth_top_json, raw_ref) "
            "VALUES (:token_id, :ts, '0.49', '0.51', '0.02', '0.50', NULL, NULL)"
        ),
        {"token_id": token_id, "ts": ts},
    )
    session.commit()


def test_worldcup_watchlist_detects_keyword_market(session):
    _seed_market(session, token_id="tok_wc")
    _seed_market(session, condition_id="cond_other", token_id="tok_other", question="Will BTC hit 100k?")

    from pmresearch.watchlists.world_cup import build_world_cup_watchlist, list_watchlist_tokens

    stats = build_world_cup_watchlist(session, "0xabc")
    rows = list_watchlist_tokens(session, name="world_cup_2026", active_only=True)

    assert stats.active_tokens == 1
    assert [r.token_id for r in rows] == ["tok_wc"]
    assert rows[0].priority == 30


def test_recent_trade_priority_beats_keyword_only(session):
    now = int(time.time())
    _seed_market(session, token_id="tok_trade")
    _seed_market(session, condition_id="cond_keyword", token_id="tok_keyword", question="World Cup Mexico vs Spain")
    _seed_trade(session, token_id="tok_trade", condition_id="cond_wc", ts=now - 60)

    from pmresearch.watchlists.world_cup import build_world_cup_watchlist, list_watchlist_tokens

    build_world_cup_watchlist(session, "0xabc")
    rows = {r.token_id: r for r in list_watchlist_tokens(session, name="world_cup_2026")}

    assert rows["tok_trade"].priority == 10
    assert rows["tok_trade"].source == "rn1_recent_trade"
    assert rows["tok_keyword"].priority == 30


def test_open_holding_priority(session):
    _seed_market(session, token_id="tok_wc")
    _seed_holding(session, token_id="tok_wc")

    from pmresearch.watchlists.world_cup import build_world_cup_watchlist, list_watchlist_tokens

    build_world_cup_watchlist(session, "0xabc")
    row = list_watchlist_tokens(session, name="world_cup_2026")[0]
    assert row.priority == 20
    assert row.source == "rn1_open_holding"


def test_manual_token_add_is_idempotent(session):
    _seed_market(session, token_id="tok_manual")
    from pmresearch.watchlists.world_cup import add_manual_token

    assert add_manual_token(session, name="world_cup_2026", token_id="tok_manual") is True
    assert add_manual_token(session, name="world_cup_2026", token_id="tok_manual") is False

    count = session.execute(text("SELECT COUNT(*) FROM watchlist_tokens")).scalar()
    assert count == 1


def test_book_sample_runs_group_snapshots(settings, session, monkeypatch):
    _seed_market(session, token_id="tok_wc")
    from pmresearch.watchlists.world_cup import add_manual_token

    add_manual_token(session, name="world_cup_2026", token_id="tok_wc")

    class FakeClobSource:
        def close(self):
            pass

        def fetch_book_batch(self, raw_store, token_ids, *, per_token_delay_s=0):
            from pmresearch.rawstore.store import RawFetchResult
            from pmresearch.sources.clob import BookSnapshot

            out = []
            for token_id in token_ids:
                path = settings.raw_dir / "fake" / f"{token_id}.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}")
                raw_id = raw_store.session.execute(
                    text(
                        "INSERT INTO raw_fetches "
                        "(source, endpoint, params_json, fetched_at, http_status, file_path, content_hash, row_count) "
                        "VALUES ('clob', 'book', :p, 'test', 200, :fp, :h, 1) RETURNING id"
                    ),
                    {"p": f'{{"token_id":"{token_id}"}}', "fp": str(path), "h": f"h-{token_id}-{time.time_ns()}"},
                ).scalar()
                raw_store.session.commit()
                out.append(
                    BookSnapshot(
                        token_id=token_id,
                        best_bid=Decimal("0.49"),
                        best_ask=Decimal("0.51"),
                        spread=Decimal("0.02"),
                        mid=Decimal("0.50"),
                        depth_top=None,
                        raw_fetch=RawFetchResult(raw_id, path, "h", 1, False),
                    )
                )
            return out

    monkeypatch.setattr("pmresearch.booksampler.watchlist.ClobSource", FakeClobSource)

    from pmresearch.booksampler.watchlist import sample_watchlist_once

    stats = sample_watchlist_once(settings, name="world_cup_2026", limit=10, per_token_delay_s=0)

    assert stats.run_id is not None
    snap = session.execute(text("SELECT sample_run_id, watchlist_id FROM book_snapshots")).fetchone()
    assert snap.sample_run_id == stats.run_id
    assert snap.watchlist_id == stats.watchlist_id


def test_context_status_boundaries():
    from pmresearch.context.maker_fills import context_status

    assert context_status(None) == ("missing", "no_book_before_fill")
    assert context_status(31) == ("stale", "book_before_too_old")
    assert context_status(11) == ("weak", None)
    assert context_status(6) == ("usable", None)
    assert context_status(3) == ("good", None)
    assert context_status(2) == ("excellent", None)


def test_book_after_is_never_entry_context(session):
    _seed_market(session, token_id="tok_wc")
    from pmresearch.watchlists.world_cup import add_manual_token

    add_manual_token(session, name="world_cup_2026", token_id="tok_wc")
    event_id = _seed_trade(session, event_id=77, token_id="tok_wc", ts=1000)
    session.execute(
        text(
            "INSERT INTO fill_enrichment "
            "(event_id, role, order_hash, fee, counterparty, source, enriched_at) "
            "VALUES (:event_id, 'maker', 'order', '0', NULL, 'test', 'test')"
        ),
        {"event_id": event_id},
    )
    _seed_snapshot(session, token_id="tok_wc", ts=1001)

    from pmresearch.context.maker_fills import build_maker_fill_context

    stats = build_maker_fill_context(session, wallet="0xabc", watchlist="world_cup_2026", max_age_s=60)
    row = session.execute(text("SELECT context_status, book_before_ts, book_after_ts FROM maker_fill_context")).fetchone()

    assert stats.missing == 1
    assert row.context_status == "missing"
    assert row.book_before_ts is None
    assert row.book_after_ts == 1001


def test_worldcup_scheduler_jobs_only_when_enabled(settings):
    from pmresearch.walletmanager.scheduler import build_scheduler

    off = build_scheduler(settings)
    assert not any(job.id.startswith("worldcup_") for job in off.get_jobs())

    enabled = replace(settings, worldcup_watch_enabled=True, worldcup_wallet="")
    on = build_scheduler(enabled)
    ids = {job.id for job in on.get_jobs()}
    assert {
        "worldcup_sync",
        "worldcup_watchlist_rebuild",
        "worldcup_fast_book_sample",
        "worldcup_book_sample",
        "worldcup_context",
    }.issubset(ids)


def test_worldcup_tracked_wallets_max_two(session):
    from pmresearch.walletmanager.manager import add_wallet
    from pmresearch.worldcup.status import (
        set_worldcup_tracked_wallets,
        worldcup_tracked_wallets,
    )

    add_wallet(session, "0xaaa")
    add_wallet(session, "0xbbb")
    add_wallet(session, "0xccc")

    saved = set_worldcup_tracked_wallets(session, ["0xAAA", "0xbbb"])
    assert saved == ["0xaaa", "0xbbb"]
    assert worldcup_tracked_wallets(session) == ["0xaaa", "0xbbb"]

    try:
        set_worldcup_tracked_wallets(session, ["0xaaa", "0xbbb", "0xccc"])
    except ValueError as exc:
        assert "at most 2" in str(exc)
    else:
        raise AssertionError("expected max-2 validation")


def test_phase18_docker_env_documented():
    compose = open("docker-compose.yml", encoding="utf-8").read()
    env_example = open(".env.example", encoding="utf-8").read()

    assert "restart: unless-stopped" in compose
    assert "PMR_WORLDCUP_WATCH_ENABLED" in compose
    assert "PMR_WORLDCUP_WALLET" in compose
    assert "PMR_WORLDCUP_SAMPLE_LIMIT" in env_example


def test_report_degrades_when_worldcup_coverage_is_insufficient(session):
    from pmresearch.reports.render import render_wallet_profile
    from pmresearch.reports.wallet_profile import build_wallet_profile
    from pmresearch.watchlists.world_cup import ensure_watchlist

    ensure_watchlist(session, "world_cup_2026")
    profile = build_wallet_profile(session, "0xabc")
    markdown = render_wallet_profile(profile)

    assert "World Cup Forward Watch" in markdown
    assert "Insufficient forward book context" in markdown
