from datetime import date, datetime, time, timezone
from decimal import Decimal
import json

import httpx
from sqlalchemy import text

from pmresearch.marks.base import Mark
from pmresearch.marks.prices_history import PricesHistoryMarkSource
from pmresearch.marks.service import MarkService
from pmresearch.projections.daily_equity import (
    DAILY_EQUITY_DRAWDOWN_BASIS,
    fetch_daily_equity,
    rebuild_daily_equity,
)
from pmresearch.rawstore.store import RawStore
from pmresearch.reconcile.checks import value_check_fact

DUST = Decimal("0.000001")


def _ts(day: date) -> int:
    return int(datetime.combine(day, time(23, 59, 59, tzinfo=timezone.utc)).timestamp())


def _raw_ref(session, wallet):
    return session.execute(
        text(
            "INSERT INTO raw_fetches (source, endpoint, params_json, fetched_at, "
            "http_status, file_path, content_hash, row_count) "
            "VALUES ('test', 'activity', :params, 'test', 200, 'none', :hash, 0) "
            "RETURNING id"
        ),
        {"params": f'{{"wallet":"{wallet}"}}', "hash": f"phase9-{wallet}"},
    ).scalar()


def _seed_market(session, closed=0, prices=None):
    prices = prices or {"tok_a": "1", "tok_b": "0"}
    session.execute(
        text(
            "INSERT INTO markets "
            "(condition_id, question, category, outcomes_json, clob_token_ids_json, "
            "closed, resolution_prices_json, structure_type, updated_at) "
            "VALUES ('cond', 'Question', 'Sports', :outcomes, :tokens, "
            ":closed, :prices, 'binary', 'test')"
        ),
        {
            "outcomes": json.dumps(["A", "B"]),
            "tokens": json.dumps(["tok_a", "tok_b"]),
            "closed": closed,
            "prices": json.dumps(prices),
        },
    )
    for index, token_id in enumerate(("tok_a", "tok_b")):
        session.execute(
            text(
                "INSERT INTO tokens (token_id, condition_id, outcome_index, outcome_label) "
                "VALUES (:token_id, 'cond', :outcome_index, :outcome_label)"
            ),
            {"token_id": token_id, "outcome_index": index, "outcome_label": token_id},
        )
    session.commit()


def _seed_ledger(session, wallet, events):
    raw_ref = _raw_ref(session, wallet)
    for index, event in enumerate(events):
        session.execute(
            text(
                "INSERT INTO wallet_events "
                "(wallet, event_type, ts, tx_hash, condition_id, token_id, side, "
                "delta_shares, delta_usdc, price, usdc_size, source, is_derived, raw_ref, "
                "dedupe_key, ingested_at) "
                "VALUES (:wallet, :event_type, :ts, :tx_hash, 'cond', :token_id, "
                "NULL, :delta_shares, :delta_usdc, :price, :usdc_size, 'test', 0, "
                ":raw_ref, :dedupe_key, 'test')"
            ),
            {
                "wallet": wallet,
                "event_type": event["type"],
                "ts": event["ts"],
                "tx_hash": event.get("tx_hash", f"0x{index}"),
                "token_id": event.get("token_id"),
                "delta_shares": event.get("delta_shares", "0"),
                "delta_usdc": event.get("delta_usdc", "0"),
                "price": event.get("price", "0"),
                "usdc_size": event.get("usdc_size", event.get("delta_usdc", "0")),
                "raw_ref": raw_ref,
                "dedupe_key": f"phase9-{wallet}-{index}",
            },
        )
    session.commit()


class StaticMarkSource:
    name = "static"

    def __init__(self, marks):
        self.marks = marks

    def get_mark(self, session, token_id, ts):
        price, stale, age = self.marks[(token_id, ts)]
        return Mark(
            token_id=token_id,
            ts=ts,
            price=Decimal(price),
            source=self.name,
            mark_age_s=age,
            stale=stale,
            meta={"test": True},
        )


def _assert_marked_drawdown_consistency(rows):
    peak = None
    differences = []
    for row in rows:
        marked = row.realized_pnl_cum + row.unrealized_pnl + row.reward_income_cum
        peak = marked if peak is None else max(peak, marked)
        assert row.marked_pnl == marked
        expected_drawdown = peak - marked
        if row.drawdown != expected_drawdown:
            differences.append((row.date, row.drawdown, expected_drawdown))
        assert row.drawdown_basis == DAILY_EQUITY_DRAWDOWN_BASIS
    assert differences == []


def test_resolution_mark_overrides_fresher_cached_point(session):
    _seed_market(session, closed=1, prices={"tok_a": "1", "tok_b": "0"})
    target_ts = _ts(date(2026, 1, 2))
    stale_source = StaticMarkSource({("tok_a", target_ts): ("0.25", False, 0)})
    service = MarkService([stale_source])

    mark = service.get_mark(session, "tok_a", target_ts)

    assert mark.source == "resolution"
    assert mark.price == Decimal("1")
    assert not mark.stale


def test_prices_history_computes_staleness_and_caches(settings, session):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"history": [{"t": 100, "p": "0.42"}]})

    client = httpx.Client(base_url="https://fake", transport=httpx.MockTransport(handler))
    source = PricesHistoryMarkSource(
        RawStore(settings, session),
        client=client,
        staleness_window_s=50,
        sleep_fn=lambda s: None,
    )

    mark = source.get_mark(session, "tok", 175)
    again = source.get_mark(session, "tok", 180)

    assert mark.price == Decimal("0.42")
    assert mark.mark_age_s == 75
    assert mark.stale
    assert again.mark_age_s == 80
    assert calls["n"] == 1


def test_daily_equity_golden_fixture_with_stale_share(session):
    wallet = "0xequity"
    _seed_market(session)
    day1 = date(2026, 1, 1)
    day2 = date(2026, 1, 2)
    _seed_ledger(
        session,
        wallet,
        [
            {"type": "TRADE", "ts": _ts(day1) - 100, "token_id": "tok_a", "delta_shares": "10", "delta_usdc": "-4"},
            {"type": "TRADE", "ts": _ts(day1) - 50, "token_id": "tok_b", "delta_shares": "5", "delta_usdc": "-1"},
            {"type": "TRADE", "ts": _ts(day2) - 100, "token_id": "tok_a", "delta_shares": "-4", "delta_usdc": "2.4"},
        ],
    )
    marks = {
        ("tok_a", _ts(day1)): ("0.5", False, 10),
        ("tok_b", _ts(day1)): ("0.2", True, 90_000),
        ("tok_a", _ts(day2)): ("0.4", False, 10),
        ("tok_b", _ts(day2)): ("0.1", True, 90_000),
    }
    service = MarkService([StaticMarkSource(marks)])
    progress = []

    stats = rebuild_daily_equity(
        session,
        wallet,
        mark_service=service,
        dust_epsilon=DUST,
        through_date=day2,
        progress_fn=progress.append,
        equity_batch_size=1,
        mark_batch_size=1,
        event_progress_interval=1,
    )
    rows = fetch_daily_equity(session, wallet)
    stages = [item.stage for item in progress]

    assert stats.rows_written == 2
    assert "events" in stages
    assert "marks_flush" in stages
    assert "equity_flush" in stages
    assert [row.date for row in rows] == ["2026-01-01", "2026-01-02"]
    assert rows[0].portfolio_value == Decimal("6.0")
    assert rows[0].unrealized_pnl == Decimal("1.0")
    assert rows[0].stale_equity_share == Decimal("0.1666666666666666666666666667")
    assert rows[1].portfolio_value == Decimal("2.9")
    assert rows[1].realized_pnl_cum == Decimal("0.8")
    assert rows[1].unrealized_pnl == Decimal("-0.5")
    assert rows[1].marked_pnl == Decimal("0.3")
    assert rows[1].account_equity == Decimal("3.7")
    assert rows[1].drawdown == Decimal("0.7")
    _assert_marked_drawdown_consistency(rows)


def test_daily_equity_drawdown_uses_marked_pnl_when_realized_and_unrealized_offset(session):
    wallet = "0xrn1drawdown"
    _seed_market(session)
    day1 = date(2026, 3, 7)
    day2 = date(2026, 3, 8)
    _seed_ledger(
        session,
        wallet,
        [
            {
                "type": "TRADE",
                "ts": _ts(day1) - 600,
                "token_id": "tok_b",
                "delta_shares": "1",
                "delta_usdc": "0",
            },
            {
                "type": "TRADE",
                "ts": _ts(day1) - 500,
                "token_id": "tok_b",
                "delta_shares": "-1",
                "delta_usdc": "10095757.067828942",
            },
            {
                "type": "TRADE",
                "ts": _ts(day1) - 400,
                "token_id": "tok_a",
                "delta_shares": "1",
                "delta_usdc": "-5898079.933995848",
            },
            {
                "type": "REWARD",
                "ts": _ts(day1) - 300,
                "delta_usdc": "43.3524",
            },
            {
                "type": "TRADE",
                "ts": _ts(day2) - 500,
                "token_id": "tok_a",
                "delta_shares": "-1",
                "delta_usdc": "61907.29408396",
            },
            {
                "type": "TRADE",
                "ts": _ts(day2) - 400,
                "token_id": "tok_b",
                "delta_shares": "1",
                "delta_usdc": "-6177.987991793",
            },
        ],
    )
    marks = {
        ("tok_a", _ts(day1)): ("69595.842857", False, 10),
        ("tok_b", _ts(day2)): ("6326.56866", False, 10),
    }
    service = MarkService([StaticMarkSource(marks)])

    rebuild_daily_equity(
        session,
        wallet,
        mark_service=service,
        dust_epsilon=DUST,
        through_date=day2,
    )
    rows = fetch_daily_equity(session, wallet)

    assert [row.date for row in rows] == ["2026-03-07", "2026-03-08"]
    assert rows[0].realized_pnl_cum == Decimal("10095757.067828942")
    assert rows[0].unrealized_pnl == Decimal("-5828484.091138848")
    assert rows[0].marked_pnl == Decimal("4267316.329090094")
    assert rows[1].realized_pnl_cum == Decimal("4259584.427917054")
    assert rows[1].unrealized_pnl == Decimal("148.580668207")
    assert rows[1].marked_pnl == Decimal("4259776.360985261")
    assert rows[1].drawdown < Decimal("10000")
    assert rows[1].account_equity == Decimal("4265954.348977054")
    assert rows[0].account_equity - rows[1].account_equity > Decimal("5800000")
    _assert_marked_drawdown_consistency(rows)


def test_daily_equity_rebuild_is_reproducible(session):
    wallet = "0xrepro"
    _seed_market(session)
    day = date(2026, 1, 1)
    _seed_ledger(
        session,
        wallet,
        [{"type": "TRADE", "ts": _ts(day) - 100, "token_id": "tok_a", "delta_shares": "10", "delta_usdc": "-4"}],
    )
    service = MarkService([StaticMarkSource({("tok_a", _ts(day)): ("0.5", False, 0)})])

    rebuild_daily_equity(session, wallet, mark_service=service, dust_epsilon=DUST, through_date=day)
    first = [row.portfolio_value for row in fetch_daily_equity(session, wallet)]
    rebuild_daily_equity(session, wallet, mark_service=service, dust_epsilon=DUST, through_date=day)
    second = [row.portfolio_value for row in fetch_daily_equity(session, wallet)]

    assert first == second == [Decimal("5.0")]


def test_value_within_band_passes():
    fact = value_check_fact(
        wallet="0xabc",
        run_ts=1,
        oracle_value=Decimal("100"),
        local_value=Decimal("98.5"),
        stale_equity_share=Decimal("0.25"),
        equity_date="2026-01-01",
    )

    assert fact.status == "pass"
    assert fact.reason_code == "within_value_band"
    assert fact.notes["stale_equity_share"] == "0.25"
