"""Unit + CLI smoke tests for Phase 19 wallet cluster comparison (read-only)."""

import json

from click.testing import CliRunner
from sqlalchemy import text

from pmresearch.cli import main
from pmresearch.reports.cluster import analyze_cluster_candidates, analyze_cluster_compare

LEADER = "0xc0111111111111111111111111111111111111ab"

_seq = [0]


def _raw_id(session) -> int:
    _seq[0] += 1
    session.execute(
        text(
            "INSERT INTO raw_fetches "
            "(source, endpoint, params_json, fetched_at, http_status, file_path, "
            "content_hash, row_count, ingested_at) "
            "VALUES ('fx','activity','{}','now',200,'fx',:h,1,'now')"
        ),
        {"h": f"h{_seq[0]}"},
    )
    return int(session.execute(text("SELECT max(id) FROM raw_fetches")).scalar())


def _event(session, wallet, cid, ts, token_id, side, price, shares):
    rid = _raw_id(session)
    _seq[0] += 1
    session.execute(
        text(
            "INSERT INTO wallet_events "
            "(wallet, event_type, ts, tx_hash, condition_id, token_id, side, "
            "delta_shares, delta_usdc, price, usdc_size, source, is_derived, raw_ref, "
            "dedupe_key, ingested_at) "
            "VALUES (:w,'TRADE',:ts,:tx,:c,:tok,:side,:ds,:du,:p,'0','fx',0,:r,:dk,'now')"
        ),
        {
            "w": wallet,
            "ts": ts,
            "tx": f"tx{_seq[0]}",
            "c": cid,
            "tok": token_id,
            "side": side,
            "ds": shares,
            "du": str(-float(price) * float(shares)),
            "p": price,
            "r": rid,
            "dk": f"dk{_seq[0]}",
        },
    )
    session.commit()


def _market(session, cid, token_id, *, event_id=None, question=None):
    if event_id is not None:
        session.execute(
            text(
                "INSERT OR IGNORE INTO pm_events (event_id, title, slug, neg_risk, tags_json) "
                "VALUES (:e,:e,:e,0,'[]')"
            ),
            {"e": event_id},
        )
    session.execute(
        text(
            "INSERT INTO markets (condition_id, question, slug, category, event_id, closed, "
            "structure_type, outcomes_json, clob_token_ids_json, updated_at) "
            "VALUES (:c,:q,:s,'Sports',:e,0,'binary',:oj,:cj,'now')"
        ),
        {
            "c": cid,
            "q": question or f"Q {cid[-4:]}",
            "s": cid[-6:],
            "e": event_id,
            "oj": json.dumps(["A", "B"]),
            "cj": json.dumps([token_id, token_id + "b"]),
        },
    )
    session.execute(
        text(
            "INSERT INTO tokens (token_id, condition_id, outcome_index, outcome_label) "
            "VALUES (:t,:c,0,'A')"
        ),
        {"t": token_id, "c": cid},
    )
    session.commit()


def test_same_system_candidate_classification(session):
    candidate = "0xc0222222222222222222222222222222222222ab"
    cid = "0x" + "b1" * 32
    tok = "1001"
    _market(session, cid, tok)
    _event(session, LEADER, cid, 1000, tok, "BUY", "0.50", "100")
    _event(session, candidate, cid, 1003, tok, "BUY", "0.51", "100")

    result = analyze_cluster_compare(session, LEADER, candidate, window_s=300)

    assert result.summary["matched_trade_count"] == 1
    assert result.summary["total_candidate_trades"] == 1
    assert result.matches[0]["delay_s"] == 3
    assert result.classification == "same_system_candidate"


def test_delay_sign_candidate_after_leader(session):
    candidate = "0xc0333333333333333333333333333333333333ab"
    cid = "0x" + "b2" * 32
    tok = "1002"
    _market(session, cid, tok)
    _event(session, LEADER, cid, 5000, tok, "BUY", "0.40", "50")
    _event(session, candidate, cid, 5120, tok, "BUY", "0.40", "50")

    result = analyze_cluster_compare(session, LEADER, candidate, window_s=300)

    assert result.matches[0]["delay_s"] == 120
    assert result.summary["rn1_first_share"] == 1.0
    assert result.summary["candidate_first_share"] == 0.0


def test_follower_classification(session):
    candidate = "0xc0444444444444444444444444444444444444ab"
    cid = "0x" + "b3" * 32
    tok = "1003"
    _market(session, cid, tok)
    for i in range(3):
        base = i * 1000
        _event(session, LEADER, cid, base, tok, "BUY", "0.40", "50")
        _event(session, candidate, cid, base + 120, tok, "BUY", "0.40", "50")

    result = analyze_cluster_compare(session, LEADER, candidate, window_s=300)

    assert result.summary["matched_trade_count"] == 3
    assert result.classification == "follower"


def test_independent_classification_no_overlap(session):
    candidate = "0xc0555555555555555555555555555555555555ab"
    cid_leader = "0x" + "b4" * 32
    cid_candidate = "0x" + "b5" * 32
    _market(session, cid_leader, "1004")
    _market(session, cid_candidate, "1005")
    _event(session, LEADER, cid_leader, 1000, "1004", "BUY", "0.40", "50")
    _event(session, candidate, cid_candidate, 999999, "1005", "SELL", "0.60", "10")

    result = analyze_cluster_compare(session, LEADER, candidate, window_s=300)

    assert result.summary["matched_trade_count"] == 0
    assert result.classification == "independent"


def test_shared_signal_classification_same_event(session):
    candidate = "0xc0666666666666666666666666666666666666ab"
    cid_leader = "0x" + "b6" * 32
    cid_candidate = "0x" + "b7" * 32
    _market(session, cid_leader, "1006", event_id="evt-1")
    _market(session, cid_candidate, "1007", event_id="evt-1")
    for i in range(3):
        base = i * 1000
        _event(session, LEADER, cid_leader, base, "1006", "BUY", "0.40", "50")
        _event(session, candidate, cid_candidate, base + 30, "1007", "SELL", "0.60", "10")

    result = analyze_cluster_compare(session, LEADER, candidate, window_s=300)

    assert result.summary["same_event_share"] == 1.0
    assert result.classification == "shared_signal"


def test_candidates_defaults_to_active_watchlist(session):
    candidate = "0xc0777777777777777777777777777777777777ab"
    cid = "0x" + "b8" * 32
    tok = "1008"
    _market(session, cid, tok)
    _event(session, LEADER, cid, 1000, tok, "BUY", "0.50", "100")
    _event(session, candidate, cid, 1002, tok, "BUY", "0.50", "100")

    now = "2026-01-01T00:00:00+00:00"
    for addr in (LEADER, candidate):
        session.execute(
            text("INSERT INTO wallets (address, first_seen_at) VALUES (:a,:t)"),
            {"a": addr, "t": now},
        )
        session.execute(
            text(
                "INSERT INTO watchlist (wallet, active, added_at) VALUES (:a,1,:t)"
            ),
            {"a": addr, "t": now},
        )
    session.commit()

    scores = analyze_cluster_candidates(session, LEADER)

    assert [s.wallet for s in scores] == [candidate]
    assert scores[0].classification == "same_system_candidate"


def test_cluster_compare_cli_smoke(settings, monkeypatch, tmp_path):
    monkeypatch.setenv("PMR_DATA_DIR", str(settings.data_dir))
    from pmresearch.db.engine import get_session_factory

    session = get_session_factory(settings)()
    candidate = "0xc0888888888888888888888888888888888888ab"
    cid = "0x" + "b9" * 32
    tok = "1009"
    _market(session, cid, tok)
    _event(session, LEADER, cid, 1000, tok, "BUY", "0.50", "100")
    _event(session, candidate, cid, 1002, tok, "BUY", "0.50", "100")
    session.close()

    out_dir = tmp_path / "cluster_out"
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "cluster",
            "compare",
            "--leader",
            LEADER,
            "--wallet",
            candidate,
            "--out",
            str(out_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Classification:" in result.output
    assert (out_dir / "CLUSTER_COMPARE.md").exists()
    assert (out_dir / "cluster_match_table.csv").exists()
