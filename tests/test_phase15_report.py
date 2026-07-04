"""Phase 15 — wallet research report generator.

The report layer assembles and renders only. These tests build a fully-populated
golden fixture DB (the projection tables prior phases produce), then assert:
- every section renders and every numeric claim traces to a seeded value;
- an untrusted wallet gets a prominent data-quality banner;
- missing projections degrade to explicit "insufficient data" blocks;
- two differently-seeded wallets produce meaningfully different memos.
"""

from __future__ import annotations

import json

from sqlalchemy import text

from pmresearch.reports.render import render_wallet_profile
from pmresearch.reports.wallet_profile import build_wallet_profile

MM = "0x1111111111111111111111111111111111111111"
VB = "0x2222222222222222222222222222222222222222"


# --- seeding helpers --------------------------------------------------------


def _seed_pnl(session, wallet, scope, directional, bond_merge, reward, redemption, fees):
    session.execute(
        text(
            "INSERT INTO pnl_decomposition (wallet, scope, period, directional_pnl, "
            "bond_merge_pnl, reward_income, redemption_pnl, fees, computed_at, projection_version) "
            "VALUES (:w,:s,'all',:d,:b,:r,:red,:f,'2026-07-04T00:00:00+00:00',1)"
        ),
        {"w": wallet, "s": scope, "d": str(directional), "b": str(bond_merge),
         "r": str(reward), "red": str(redemption), "f": str(fees)},
    )


def _seed_episode(session, wallet, open_ts, close_ts, close_reason, realized, reward):
    session.execute(
        text(
            "INSERT INTO episodes (wallet, token_id, condition_id, open_ts, close_ts, "
            "close_reason, peak_qty, num_adds, num_partial_exits, wac_entry, realized_pnl, "
            "reward_income, fees_paid, events_consumed, projection_version) "
            "VALUES (:w,'tok','cond',:o,:c,:cr,'10',1,0,'0.5',:r,:rw,'0','[1]',1)"
        ),
        {"w": wallet, "o": open_ts, "c": close_ts, "cr": close_reason,
         "r": str(realized), "rw": str(reward)},
    )


def _seed_equity(session, wallet, date, value, drawdown, stale):
    session.execute(
        text(
            "INSERT INTO daily_equity (wallet, date, portfolio_value, realized_pnl_cum, "
            "unrealized_pnl, reward_income_cum, drawdown, stale_equity_share, projection_version, "
            "marked_pnl, drawdown_basis) "
            "VALUES (:w,:d,:v,'0','0','0',:dd,:st,2,:mp,'marked_pnl')"
        ),
        {"w": wallet, "d": date, "v": str(value), "dd": str(drawdown),
         "st": str(stale), "mp": str(value)},
    )


def _seed_fingerprint(session, wallet, scope, feature, value, value_type="scalar",
                      null_reason=None, window="all", version=1):
    session.execute(
        text(
            "INSERT INTO fingerprints (wallet, scope, feature, family, value, value_type, "
            "null_reason, window, computed_at, version) "
            "VALUES (:w,:s,:f,'fam',:v,:vt,:nr,:win,'2026-07-04T00:00:00+00:00',:ver)"
        ),
        {"w": wallet, "s": scope, "f": feature, "v": value, "vt": value_type,
         "nr": null_reason, "win": window, "ver": version},
    )


def _seed_label(session, wallet, scope, detector, score, confidence, blind_spots):
    session.execute(
        text(
            "INSERT INTO strategy_labels (wallet, scope, detector_name, detector_version, "
            "label, score, confidence, evidence_json, blind_spots, computed_at) "
            "VALUES (:w,:s,:dn,1,:dn,:sc,:cf,'{}',:bs,'2026-07-04T00:00:00+00:00')"
        ),
        {"w": wallet, "s": scope, "dn": detector, "sc": str(score),
         "cf": str(confidence), "bs": blind_spots},
    )


def _seed_trust(session, wallet, status, reason, ts=1000):
    session.execute(
        text(
            "INSERT INTO wallet_trust (wallet, status, since_ts, updated_ts, reason, "
            "last_reconciliation_ts) VALUES (:w,:st,:ts,:ts,:r,:ts)"
        ),
        {"w": wallet, "st": status, "ts": ts, "r": reason},
    )


def _seed_recon_size(session, wallet, subject, expected, computed, status, ts=1000):
    notes = json.dumps({"remote_present": True, "local_present": True,
                        "local_qty": str(computed), "price_for_notional": "0.5"})
    session.execute(
        text(
            "INSERT INTO reconciliation_facts (wallet, ts, check_type, subject, expected, "
            "computed, abs_diff, pct_diff, tolerance, status, source, reason_code, notes) "
            "VALUES (:w,:ts,'positions_size',:sub,:e,:c,'0','0','0.0001',:st,'dataapi',"
            ":rc,:notes)"
        ),
        {"w": wallet, "ts": ts, "sub": subject, "e": str(expected), "c": str(computed),
         "st": status, "rc": "exact_match" if status == "pass" else "qty_mismatch",
         "notes": notes},
    )


def _seed_recon_value(session, wallet, oracle, local, status, ts=1000):
    notes = json.dumps({"equity_date": "2026-07-03", "stale_equity_share": "0.05"})
    session.execute(
        text(
            "INSERT INTO reconciliation_facts (wallet, ts, check_type, subject, expected, "
            "computed, abs_diff, pct_diff, tolerance, status, source, reason_code, notes) "
            "VALUES (:w,:ts,'portfolio_value','portfolio',:e,:c,'0','0.01','0.02',:st,"
            "'dataapi','value_within_band',:notes)"
        ),
        {"w": wallet, "ts": ts, "e": str(oracle), "c": str(local), "st": status, "notes": notes},
    )


_EXEC = {"maker_fill_share": "0.9", "taker_fill_share": "0.1", "enrichment_coverage": "0.8"}
_INCOME = {"reward_income_share": "0.6", "realized_pnl": "1500", "unrealized_pnl": "200"}
_BEHAVIOR = {
    "bond_inventory_ratio": "0.7", "merge_frequency": "0.2", "redeem_frequency": "0.1",
    "episode_count": "40", "episode_duration_p50": "30", "episode_duration_p90": "300",
    "micro_episode_share": "0.8", "adds_per_episode": "1.5",
    "partial_exit_frequency": "0.3", "market_category_concentration": "0.25",
}


def _seed_full_wallet(session, wallet, *, trust_status="trusted",
                      directional="500", reward="1000", detector_scores=None):
    _seed_pnl(session, wallet, "all", directional, "300", reward, "200", "50")
    _seed_pnl(session, wallet, "category:Sports", "200", "100", "600", "50", "20")
    _seed_pnl(session, wallet, "category:Crypto", "300", "200", "400", "150", "30")
    _seed_episode(session, wallet, 100, 130, "flat", "50", "0")
    _seed_episode(session, wallet, 200, None, "open", "0", "0")
    _seed_episode(session, wallet, 300, 900600, "resolution", "100", "0")
    _seed_equity(session, wallet, "2026-07-01", "1000000", "0", "0.02")
    _seed_equity(session, wallet, "2026-07-02", "1100000", "50000", "0.05")
    _seed_equity(session, wallet, "2026-07-03", "1200000", "20000", "0.03")
    for f, v in {**_EXEC, **_INCOME, **_BEHAVIOR}.items():
        _seed_fingerprint(session, wallet, "all", f, v)
    scores = detector_scores or {"market_making": ("0.8", "1.0"),
                                 "inventory_cycling": ("0.4", "1.0"),
                                 "value_betting": ("0.2", "1.0")}
    for det, (score, conf) in scores.items():
        _seed_label(session, wallet, "all", det, score, conf, f"{det} blind spot text")
        _seed_label(session, wallet, "category:Sports", det, score, conf, f"{det} blind spot text")
    _seed_trust(session, wallet, trust_status, f"{trust_status} reason")
    _seed_recon_size(session, wallet, "tokA", "10", "10", "pass")
    _seed_recon_size(session, wallet, "tokB", "5", "5", "pass")
    _seed_recon_value(session, wallet, "1200000", "1188000", "pass")
    session.commit()


# --- tests ------------------------------------------------------------------


def test_full_report_renders_all_sections(session):
    _seed_full_wallet(session, MM)
    profile = build_wallet_profile(session, MM)
    md = render_wallet_profile(profile)

    for heading in [
        "# Why is", "## Executive summary", "## PnL decomposition",
        "## Category breakdown", "## Episode behavior", "## Equity & drawdown",
        "## Maker/taker & execution evidence", "## Strategy hypotheses",
        "## Reconciliation & trust", "## Limitations & data-quality notes",
    ]:
        assert heading in md, f"missing section: {heading}"


def test_numeric_claims_trace_to_seeded_values(session):
    _seed_full_wallet(session, MM, directional="777", reward="4242")
    md = render_wallet_profile(build_wallet_profile(session, MM))
    # PnL components and total appear verbatim.
    assert "777" in md            # directional
    assert "4242" in md           # reward income
    assert "1200000" in md        # latest portfolio value
    # total = 777 + 300 + 4242 + 200 - 50 = 5469
    assert "5469" in md


def test_output_changes_when_inputs_change(session):
    _seed_full_wallet(session, MM, reward="1000")
    a = render_wallet_profile(build_wallet_profile(session, MM))
    session.execute(text("DELETE FROM pnl_decomposition WHERE wallet = :w"), {"w": MM})
    _seed_pnl(session, MM, "all", "500", "300", "9999", "200", "50")
    session.commit()
    b = render_wallet_profile(build_wallet_profile(session, MM))
    assert a != b
    assert "9999" in b and "9999" not in a


def test_dominant_income_source_reported(session):
    # reward income dwarfs everything -> it is the dominant source.
    _seed_full_wallet(session, MM, directional="10", reward="100000")
    md = render_wallet_profile(build_wallet_profile(session, MM))
    assert "Dominant income source:** `reward_income`" in md


def test_untrusted_wallet_gets_prominent_banner(session):
    _seed_full_wallet(session, MM, trust_status="untrusted")
    md = render_wallet_profile(build_wallet_profile(session, MM))
    assert "WALLET UNTRUSTED" in md
    assert "do not treat any conclusion as reliable" in md.lower()


def test_missing_projections_degrade_to_insufficient_data(session):
    # Seed nothing for this wallet.
    empty = "0x3333333333333333333333333333333333333333"
    md = render_wallet_profile(build_wallet_profile(session, empty))
    assert "Insufficient data" in md
    # PnL, episodes, equity, hypotheses, reconciliation all degrade.
    assert md.count("Insufficient data") >= 5
    # trust unknown banner present
    assert "trust status unknown" in md


def test_null_fingerprint_feature_rendered_with_reason(session):
    _seed_full_wallet(session, MM)
    session.execute(
        text("UPDATE fingerprints SET value = NULL, value_type = NULL, "
             "null_reason = 'zero enrichment coverage' WHERE wallet = :w AND feature = :f"),
        {"w": MM, "f": "maker_fill_share"},
    )
    session.commit()
    md = render_wallet_profile(build_wallet_profile(session, MM))
    assert "null — zero enrichment coverage" in md


def test_contrast_wallets_differ(session):
    _seed_full_wallet(session, MM, directional="500", reward="5000",
                      detector_scores={"market_making": ("0.85", "1.0"),
                                       "inventory_cycling": ("0.3", "1.0"),
                                       "value_betting": ("0.15", "1.0")})
    _seed_full_wallet(session, VB, directional="8000", reward="100",
                      detector_scores={"market_making": ("0.1", "1.0"),
                                       "inventory_cycling": ("0.2", "1.0"),
                                       "value_betting": ("0.9", "1.0")})
    mm_md = render_wallet_profile(build_wallet_profile(session, MM))
    vb_md = render_wallet_profile(build_wallet_profile(session, VB))
    assert mm_md != vb_md
    # Top hypothesis differs.
    assert "Leading strategy hypothesis:** `market_making`" in mm_md
    assert "Leading strategy hypothesis:** `value_betting`" in vb_md


def test_report_is_wallet_generic(session):
    # Same builder, arbitrary address, no RN1-specific literals in output header.
    other = "0x4444444444444444444444444444444444444444"
    _seed_full_wallet(session, other)
    md = render_wallet_profile(build_wallet_profile(session, other))
    assert other in md
    assert "2005d16a" not in md  # no hardcoded RN1 assumption
