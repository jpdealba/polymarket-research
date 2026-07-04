"""Phase 13 — behavioral fingerprints.

Golden-fixture unit tests (hand-computed expected values) for every feature,
NULL-with-reason semantics, plus an end-to-end compute over a tiny hand-built
projection set exercising category scoping, determinism and version bumps.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from sqlalchemy import text

from pmresearch.fingerprints.compute import (
    FINGERPRINT_VERSION,
    compute_fingerprints,
    fetch_fingerprints,
    fingerprint_scopes,
)
from pmresearch.fingerprints.features import (
    calibration,
    execution,
    income,
    inventory,
    quality,
)
from pmresearch.fingerprints.features.inputs import (
    EpisodeRec,
    ExposureDayAgg,
    PnlRec,
    ScopeInput,
    price_bucket,
)

D = Decimal


def _ep(**kw) -> EpisodeRec:
    defaults = dict(
        token_id="t",
        condition_id="c",
        category="Sports",
        open_ts=0,
        close_ts=None,
        close_reason="open",
        peak_qty=D("0"),
        wac_entry=D("0"),
        num_adds=0,
        num_partial_exits=0,
        realized_pnl=D("0"),
        reward_income=D("0"),
        start_date_ts=None,
        resolution_price=None,
    )
    defaults.update(kw)
    return EpisodeRec(**defaults)


@pytest.fixture
def golden_all_scope() -> ScopeInput:
    """A hand-built all-scope bundle with independently computable outputs."""
    ep1 = _ep(
        token_id="t1", open_ts=1000, close_ts=1030, close_reason="resolution",
        peak_qty=D("100"), wac_entry=D("0.40"), num_adds=2, num_partial_exits=1,
        realized_pnl=D("5"), reward_income=D("1"), start_date_ts=900,
        resolution_price=D("1"),
    )
    ep2 = _ep(
        token_id="t2", open_ts=2000, close_ts=2120, close_reason="resolution",
        peak_qty=D("50"), wac_entry=D("0.60"), num_adds=0, num_partial_exits=0,
        realized_pnl=D("-3"), reward_income=D("0"), start_date_ts=2100,
        resolution_price=D("0"),
    )
    ep3 = _ep(
        token_id="t3", open_ts=3000, close_ts=None, close_reason="open",
        peak_qty=D("10"), wac_entry=D("0.90"), num_adds=1, num_partial_exits=0,
        realized_pnl=D("0"), reward_income=D("2"),
    )
    return ScopeInput(
        wallet="0xabc",
        scope="all",
        window="all",
        episodes=[ep1, ep2, ep3],
        exposure_days=[
            ExposureDayAgg("2026-01-01", D("30"), D("10")),
            ExposureDayAgg("2026-01-02", D("0"), D("20")),
            ExposureDayAgg("2026-01-03", D("10"), D("0")),
        ],
        trade_total=20,
        trade_maker=8,
        trade_taker=2,
        trade_enriched=10,
        merge_count=6,
        redeem_count=9,
        active_days=3,
        pnl=PnlRec(
            directional=D("10"), bond_merge=D("20"), reward_income=D("3"),
            redemption=D("5"), fees=D("2"),
        ),
        latest_unrealized=D("12.5"),
        stale_equity_shares=[D("0.1"), D("0.3"), D("0.2")],
        category_episode_counts={"Sports": 3, "Crypto": 1},
    )


# --- golden per-feature values ----------------------------------------------


def test_execution_golden(golden_all_scope):
    assert execution.maker_fill_share(golden_all_scope).value == D("8") / D("10")
    assert execution.taker_fill_share(golden_all_scope).value == D("2") / D("10")


def test_quality_golden(golden_all_scope):
    assert quality.enrichment_coverage(golden_all_scope).value == D("10") / D("20")
    # mean([0.1, 0.3, 0.2]) = 0.2
    assert quality.stale_mark_share(golden_all_scope).value == D("0.2")


def test_inventory_golden(golden_all_scope):
    inp = golden_all_scope
    assert inventory.episode_count(inp).value == D("3")
    # durations [30, 120] -> median 75, nearest-rank p90 idx int(1*0.9)=0 -> 30
    assert inventory.episode_duration_p50(inp).value == D("75")
    assert inventory.episode_duration_p90(inp).value == D("30")
    # closed=[30,120]; <=60 -> just the 30 -> 1/2
    assert inventory.micro_episode_share(inp).value == D("0.5")
    assert inventory.adds_per_episode(inp).value == D("3") / D("3")
    assert inventory.partial_exit_frequency(inp).value == D("1") / D("3")
    # sizes: 100*.40=40, 50*.60=30, 10*.90=9 -> mean 79/3, median 30
    assert inventory.avg_position_size(inp).value == D("79") / D("3")
    assert inventory.median_position_size(inp).value == D("30")
    assert inventory.merge_frequency(inp).value == D("6") / D("3")
    assert inventory.redeem_frequency(inp).value == D("9") / D("3")
    # daily bond ratios: 30/40=.75, 0/20=0, 10/10=1 -> mean 1.75/3
    assert inventory.bond_inventory_ratio(inp).value == (D("0.75") + D("0") + D("1")) / D("3")
    # HHI: (3/4)^2 + (1/4)^2 = 0.625
    assert inventory.market_category_concentration(inp).value == D("0.625")


def test_income_golden(golden_all_scope):
    inp = golden_all_scope
    # realized = dir + bond + redeem - fees = 10 + 20 + 5 - 2 = 33
    assert income.realized_pnl(inp).value == D("33")
    # gross positive = reward 3 + 10 + 20 + 5 = 38 -> share 3/38
    assert income.reward_income_share(inp).value == D("3") / D("38")
    assert income.unrealized_pnl(inp).value == D("12.5")


def test_calibration_golden(golden_all_scope):
    inp = golden_all_scope
    # deltas: 1000-900=100, 2000-2100=-100 -> median 0
    assert calibration.time_to_event_start_at_entry(inp).value == D("0")
    dist = calibration.entry_price_distribution(inp).value
    assert dist == {
        "[0.4,0.5)": str(D("1") / D("3")),
        "[0.6,0.7)": str(D("1") / D("3")),
        "[0.9,1.0]": str(D("1") / D("3")),
    }
    calib = calibration.resolution_outcome_calibration(inp).value
    assert calib == {
        "[0.4,0.5)": {"n": 1, "actual_win_rate": "1", "implied": "0.45"},
        "[0.6,0.7)": {"n": 1, "actual_win_rate": "0", "implied": "0.65"},
    }


def test_price_bucket_edges():
    assert price_bucket(D("0")) == "[0.0,0.1)"
    assert price_bucket(D("0.1")) == "[0.1,0.2)"
    assert price_bucket(D("0.99")) == "[0.9,1.0]"
    assert price_bucket(D("1")) == "[0.9,1.0]"
    assert price_bucket(D("-0.1")) is None
    assert price_bucket(D("1.5")) is None


# --- NULL-with-reason semantics ---------------------------------------------


def test_empty_scope_nulls_with_reason():
    empty = ScopeInput(wallet="0x0", scope="category:Sports", window="all")
    # count is a real 0 (there genuinely are zero episodes), never NULL:
    assert inventory.episode_count(empty).value == D("0")
    # ratios/means over empty inputs are NULL-with-reason, never 0:
    for fn in (
        inventory.episode_duration_p50,
        inventory.episode_duration_p90,
        inventory.micro_episode_share,
        inventory.adds_per_episode,
        inventory.partial_exit_frequency,
        inventory.avg_position_size,
        inventory.median_position_size,
        inventory.bond_inventory_ratio,
        income.realized_pnl,
        income.reward_income_share,
        calibration.time_to_event_start_at_entry,
        calibration.entry_price_distribution,
        calibration.resolution_outcome_calibration,
    ):
        res = fn(empty)
        assert res.is_null and res.null_reason, fn.__name__


def test_maker_share_null_without_enrichment():
    inp = ScopeInput(wallet="0x0", scope="all", window="all", trade_total=100, trade_enriched=0)
    assert execution.maker_fill_share(inp).is_null
    assert execution.taker_fill_share(inp).is_null
    # coverage is a real 0/100 = 0, not NULL (there are trades to measure):
    assert quality.enrichment_coverage(inp).value == D("0")


def test_active_days_zero_nulls_frequencies():
    inp = ScopeInput(wallet="0x0", scope="all", window="all", merge_count=0, redeem_count=0)
    assert inventory.merge_frequency(inp).is_null
    assert inventory.redeem_frequency(inp).is_null


def test_all_scope_only_features_null_in_category_scope(golden_all_scope):
    cat = ScopeInput(
        wallet="0xabc", scope="category:Sports", window="all",
        episodes=golden_all_scope.episodes,
        stale_equity_shares=[D("0.2")],
        latest_unrealized=D("5"),
    )
    assert inventory.market_category_concentration(cat).is_null
    assert quality.stale_mark_share(cat).is_null
    assert income.unrealized_pnl(cat).is_null


def test_unrealized_null_in_90d_window(golden_all_scope):
    windowed = ScopeInput(
        wallet="0xabc", scope="all", window="90d", latest_unrealized=D("5"),
    )
    res = income.unrealized_pnl(windowed)
    assert res.is_null and "full history" in res.null_reason


# --- end-to-end compute over hand-built projections -------------------------


def _seed(session):
    now = "2026-07-04T00:00:00+00:00"
    session.execute(
        text(
            "INSERT INTO raw_fetches (id, source, endpoint, params_json, fetched_at, "
            "http_status, file_path, content_hash, row_count) VALUES "
            "(1,'test','a','{}',:t,200,'/x','h',0)"
        ),
        {"t": now},
    )
    # Two markets in two categories.
    for cond, cat, tok_yes, tok_no, res in (
        ("cond_s", "Sports", "s_yes", "s_no", {"s_yes": "1", "s_no": "0"}),
        ("cond_c", "Crypto", "c_yes", "c_no", {"c_yes": "0", "c_no": "1"}),
    ):
        session.execute(
            text(
                "INSERT INTO markets (condition_id, category, outcomes_json, "
                "clob_token_ids_json, structure_type, resolution_prices_json, "
                "start_date, closed, updated_at) VALUES (:c,:cat,'[]','[]','binary',"
                ":res,'2026-06-01T00:00:00Z',1,:u)"
            ),
            {"c": cond, "cat": cat, "res": json.dumps(res), "u": now},
        )
        for idx, tok in enumerate((tok_yes, tok_no)):
            session.execute(
                text(
                    "INSERT INTO tokens (token_id, condition_id, outcome_index) "
                    "VALUES (:t,:c,:i)"
                ),
                {"t": tok, "c": cond, "i": idx},
            )
    # Episodes: 2 Sports (one resolved win), 1 Crypto.
    eps = [
        ("s_yes", "cond_s", 1_000_000, 1_000_030, "resolution", "100", "0.40", 1, 0, "5", "1"),
        ("s_yes", "cond_s", 1_000_100, 1_000_400, "resolution", "20", "0.55", 0, 1, "-2", "0"),
        ("c_yes", "cond_c", 1_000_200, None, "open", "10", "0.30", 3, 0, "0", "4"),
    ]
    for tok, cond, o, c, reason, peak, wac, adds, pex, rp, rw in eps:
        session.execute(
            text(
                "INSERT INTO episodes (wallet, token_id, condition_id, open_ts, close_ts, "
                "close_reason, peak_qty, num_adds, num_partial_exits, wac_entry, "
                "realized_pnl, reward_income, events_consumed, projection_version) "
                "VALUES (:w,:t,:c,:o,:cl,:r,:pk,:ad,:pe,:wac,:rp,:rw,'[]',2)"
            ),
            {"w": WALLET, "t": tok, "c": cond, "o": o, "cl": c, "r": reason,
             "pk": peak, "ad": adds, "pe": pex, "wac": wac, "rp": rp, "rw": rw},
        )
    # Exposures: one day each condition.
    for cond, bond, direc in (("cond_s", "20", "10"), ("cond_c", "0", "15")):
        session.execute(
            text(
                "INSERT INTO exposures_daily (wallet, condition_id, date, directional, "
                "bond, structure_type, projection_version) VALUES "
                "(:w,:c,'2026-06-15',:d,:b,'binary',1)"
            ),
            {"w": WALLET, "c": cond, "d": direc, "b": bond},
        )
    # pnl_decomposition: all + Sports scopes.
    for scope, dpnl, bond, reward, redeem, fees in (
        ("all", "5", "2", "1", "3", "1"),
        ("category:Sports", "4", "2", "0", "3", "1"),
    ):
        session.execute(
            text(
                "INSERT INTO pnl_decomposition (wallet, scope, period, directional_pnl, "
                "bond_merge_pnl, reward_income, redemption_pnl, fees, computed_at, "
                "projection_version) VALUES (:w,:s,'all',:d,:b,:r,:rd,:f,:c,1)"
            ),
            {"w": WALLET, "s": scope, "d": dpnl, "b": bond, "r": reward,
             "rd": redeem, "f": fees, "c": now},
        )
    # daily_equity: two days.
    for date, unreal, stale in (("2026-06-14", "3", "0.1"), ("2026-06-15", "4", "0.3")):
        session.execute(
            text(
                "INSERT INTO daily_equity (wallet, date, portfolio_value, realized_pnl_cum, "
                "unrealized_pnl, reward_income_cum, marked_pnl, drawdown, drawdown_basis, "
                "stale_equity_share, projection_version) VALUES "
                "(:w,:d,'0','0',:u,'0','0','0','marked_pnl',:s,2)"
            ),
            {"w": WALLET, "d": date, "u": unreal, "s": stale},
        )
    # Trades (TRADE events) with enrichment: Sports 2 maker + 1 taker, Crypto 1 maker.
    trades = [
        (10, "cond_s", "s_yes", "maker"),
        (11, "cond_s", "s_yes", "taker"),
        (12, "cond_s", "s_no", None),  # unenriched
        (13, "cond_c", "c_yes", "maker"),
    ]
    for eid, cond, tok, role in trades:
        session.execute(
            text(
                "INSERT INTO wallet_events (id, wallet, event_type, ts, tx_hash, "
                "condition_id, token_id, delta_shares, delta_usdc, price, usdc_size, "
                "source, raw_ref, dedupe_key, ingested_at) VALUES "
                "(:id,:w,'TRADE',:ts,'0xtx',:c,:t,'1','-0.5','0.5','0.5','test',1,:dk,:ia)"
            ),
            {"id": eid, "w": WALLET, "ts": 1_000_000 + eid, "c": cond, "t": tok,
             "dk": f"dk{eid}", "ia": now},
        )
        if role:
            session.execute(
                text(
                    "INSERT INTO fill_enrichment (event_id, role, order_hash, fee, source, "
                    "enriched_at) VALUES (:e,:r,'oh','0','subgraph',:a)"
                ),
                {"e": eid, "r": role, "a": now},
            )
    # A MERGE and a REDEEM in Sports.
    for eid, etype in ((20, "MERGE"), (21, "REDEEM")):
        session.execute(
            text(
                "INSERT INTO wallet_events (id, wallet, event_type, ts, tx_hash, "
                "condition_id, token_id, delta_shares, delta_usdc, price, usdc_size, "
                "source, raw_ref, dedupe_key, ingested_at) VALUES "
                "(:id,:w,:et,:ts,'0xtx','cond_s',NULL,'0','0','0','0','test',1,:dk,:ia)"
            ),
            {"id": eid, "w": WALLET, "et": etype, "ts": 1_000_000 + eid,
             "dk": f"dk{eid}", "ia": now},
        )
    session.commit()


WALLET = "0x1111111111111111111111111111111111111111"


def test_compute_end_to_end_scoping(session):
    _seed(session)
    stats = compute_fingerprints(session, WALLET)
    assert stats.values_written > 0

    scopes = fingerprint_scopes(session, WALLET, window="all")
    assert scopes == ["all", "category:Crypto", "category:Sports"]

    all_rows = {r.feature: r for r in fetch_fingerprints(session, WALLET, scope="all")}
    # 3 episodes total.
    assert all_rows["episode_count"].value == "3"
    # maker/taker: 2 maker (Sports+Crypto), 1 taker -> maker share 2/3.
    assert Decimal(all_rows["maker_fill_share"].value) == Decimal("2") / Decimal("3")
    # coverage: 3 enriched of 4 trades.
    assert Decimal(all_rows["enrichment_coverage"].value) == Decimal("3") / Decimal("4")
    # unrealized from latest daily_equity row = 4.
    assert all_rows["unrealized_pnl"].value == "4"
    # realized = dir + bond + redeem - fees = 5 + 2 + 3 - 1 = 9.
    assert all_rows["realized_pnl"].value == "9"
    # reward share = 1 / (1 + 5 + 2 + 3) = 1/11.
    assert Decimal(all_rows["reward_income_share"].value) == Decimal("1") / Decimal("11")

    sports = {r.feature: r for r in fetch_fingerprints(session, WALLET, scope="category:Sports")}
    assert sports["episode_count"].value == "2"
    # Sports realized = 4 + 2 + 3 - 1 = 8; reward share = 0 (real 0, not NULL).
    assert sports["realized_pnl"].value == "8"
    assert sports["reward_income_share"].value == "0"
    crypto = {r.feature: r for r in fetch_fingerprints(session, WALLET, scope="category:Crypto")}
    assert crypto["episode_count"].value == "1"

    # unrealized_pnl is NULL-with-reason in a category scope.
    assert sports["unrealized_pnl"].value is None
    assert sports["unrealized_pnl"].null_reason


def test_compute_deterministic(session):
    _seed(session)
    compute_fingerprints(session, WALLET)
    first = {(r.scope, r.feature): (r.value, r.null_reason)
             for r in fetch_fingerprints(session, WALLET, scope="all")}
    compute_fingerprints(session, WALLET)
    second = {(r.scope, r.feature): (r.value, r.null_reason)
              for r in fetch_fingerprints(session, WALLET, scope="all")}
    assert first == second


def test_version_bump_recomputes(session):
    _seed(session)
    compute_fingerprints(session, WALLET, version=FINGERPRINT_VERSION)
    compute_fingerprints(session, WALLET, version=FINGERPRINT_VERSION + 1)
    # Default fetch returns the highest version and old rows are gone.
    rows = fetch_fingerprints(session, WALLET, scope="all")
    assert rows and all(r.version == FINGERPRINT_VERSION + 1 for r in rows)
    total = session.execute(
        text("SELECT COUNT(DISTINCT version) FROM fingerprints WHERE wallet = :w"),
        {"w": WALLET},
    ).scalar()
    assert total == 1


def test_null_rows_persist_reason(session):
    _seed(session)
    compute_fingerprints(session, WALLET)
    null_rows = session.execute(
        text(
            "SELECT feature, null_reason FROM fingerprints WHERE wallet = :w "
            "AND value IS NULL"
        ),
        {"w": WALLET},
    ).fetchall()
    assert null_rows
    assert all(r.null_reason for r in null_rows)
