"""Minimal unit tests for the completion-set evidence audit (read-only)."""

from decimal import Decimal

from sqlalchemy import text

from pmresearch.evidence.completion_sets import analyze_completion_sets

WALLET = "0xc0119111111111111111111111111111111111ab"
T0 = "1000"  # outcome_index 0
T1 = "2000"  # outcome_index 1

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


def _market(session, cid, *, category="Sports", resolution=None, t0=T0, t1=T1):
    res_json = None
    if resolution is not None:
        import json
        res_json = json.dumps(resolution)
    import json as _json
    session.execute(
        text(
            "INSERT INTO markets (condition_id, question, slug, category, closed, "
            "resolution_prices_json, structure_type, outcomes_json, clob_token_ids_json, "
            "updated_at) "
            "VALUES (:c,:q,:s,:cat,:closed,:res,'binary',:oj,:cj,'now')"
        ),
        {"c": cid, "q": f"Q {cid[-4:]}", "s": cid[-6:], "cat": category,
         "closed": 1 if resolution else 0, "res": res_json,
         "oj": _json.dumps(["A", "B"]), "cj": _json.dumps([t0, t1])},
    )
    for tok, idx, lbl in ((t0, 0, "A"), (t1, 1, "B")):
        session.execute(
            text("INSERT INTO tokens (token_id, condition_id, outcome_index, outcome_label) "
                 "VALUES (:t,:c,:i,:l)"),
            {"t": tok, "c": cid, "i": idx, "l": lbl},
        )
    session.commit()


def _ev(session, *, cid, etype, ts, delta_shares, delta_usdc, token_id=None,
        price="0", side=None):
    rid = _raw_id(session)
    _seq[0] += 1
    session.execute(
        text(
            "INSERT INTO wallet_events "
            "(wallet, event_type, ts, tx_hash, condition_id, token_id, side, "
            "delta_shares, delta_usdc, price, usdc_size, source, is_derived, raw_ref, "
            "dedupe_key, ingested_at) "
            "VALUES (:w,:e,:ts,:tx,:c,:tok,:side,:ds,:du,:p,'0','fx',0,:r,:dk,'now')"
        ),
        {"w": WALLET, "e": etype, "ts": ts, "tx": f"tx{_seq[0]}", "c": cid,
         "tok": token_id, "side": side, "ds": delta_shares, "du": delta_usdc,
         "p": price, "r": rid, "dk": f"dk{_seq[0]}"},
    )
    session.commit()


def _row(rows, cid):
    return next(r for r in rows if r["condition_id"] == cid)


def test_complete_set_merge_edge(session):
    cid = "0x" + "a1" * 32
    _market(session, cid)
    _ev(session, cid=cid, etype="TRADE", ts=100, delta_shares="100",
        delta_usdc="-48", token_id=T0, price="0.48", side="BUY")
    _ev(session, cid=cid, etype="TRADE", ts=101, delta_shares="100",
        delta_usdc="-49", token_id=T1, price="0.49", side="BUY")
    _ev(session, cid=cid, etype="MERGE", ts=102, delta_shares="-100", delta_usdc="100")

    audit = analyze_completion_sets(session, WALLET)
    row = _row(audit.merge_edge, cid)
    assert Decimal(row["merge_sets"]) == Decimal("100")
    assert Decimal(row["realized_edge_usdc"]) == Decimal("3")
    assert Decimal(row["realized_edge_per_set"]) == Decimal("0.03")


def test_units_sets_vs_outcome_shares(session):
    cid = "0x" + "a2" * 32
    _market(session, cid)
    _ev(session, cid=cid, etype="TRADE", ts=1, delta_shares="100",
        delta_usdc="-40", token_id=T0, price="0.40", side="BUY")
    _ev(session, cid=cid, etype="TRADE", ts=2, delta_shares="100",
        delta_usdc="-40", token_id=T1, price="0.40", side="BUY")
    _ev(session, cid=cid, etype="MERGE", ts=3, delta_shares="-100", delta_usdc="100")

    audit = analyze_completion_sets(session, WALLET)
    row = _row(audit.pair_lifecycle, cid)
    assert Decimal(row["matched_pair_qty"]) == Decimal("100")
    assert Decimal(row["matched_outcome_shares_total"]) == Decimal("200")
    assert Decimal(row["merge_sets_total"]) == Decimal("100")
    assert Decimal(row["merge_usdc_actual"]) == Decimal("100")


def test_imbalance_unmatched_leg(session):
    cid = "0x" + "a3" * 32
    _market(session, cid)
    _ev(session, cid=cid, etype="TRADE", ts=1, delta_shares="100",
        delta_usdc="-45", token_id=T0, price="0.45", side="BUY")
    _ev(session, cid=cid, etype="TRADE", ts=2, delta_shares="60",
        delta_usdc="-30", token_id=T1, price="0.50", side="BUY")
    _ev(session, cid=cid, etype="MERGE", ts=3, delta_shares="-60", delta_usdc="60")

    audit = analyze_completion_sets(session, WALLET)
    row = _row(audit.temporal, cid)
    assert Decimal(row["token0_qty"]) == Decimal("100")
    assert Decimal(row["token1_qty"]) == Decimal("60")
    assert Decimal(row["matched_pair_qty"]) == Decimal("60")
    assert Decimal(row["directional_imbalance_qty"]) == Decimal("40")


def test_redemption_winner_residual(session):
    cid = "0x" + "a4" * 32
    _market(session, cid, resolution={T0: "1", T1: "0"})
    # buys more of the eventual winner (T0) than the loser (T1)
    _ev(session, cid=cid, etype="TRADE", ts=1, delta_shares="100",
        delta_usdc="-60", token_id=T0, price="0.60", side="BUY")
    _ev(session, cid=cid, etype="TRADE", ts=2, delta_shares="40",
        delta_usdc="-16", token_id=T1, price="0.40", side="BUY")
    _ev(session, cid=cid, etype="REDEEM", ts=3, delta_shares="-100", delta_usdc="100")

    audit = analyze_completion_sets(session, WALLET)
    row = _row(audit.redeem_orphan, cid)
    assert row["winner_token"] == T0
    assert Decimal(row["qty0_at_resolution"]) == Decimal("100")
    assert Decimal(row["qty1_at_resolution"]) == Decimal("40")
    assert Decimal(row["unmatched_winner_qty_at_resolution"]) == Decimal("60")
    assert Decimal(row["unmatched_loser_qty_at_resolution"]) == Decimal("0")
    # 40 matched shares held to resolution => residual reading is ambiguous
    assert row["ambiguous_complete_set_vs_winner_residual"] == 1


def test_loser_residual_flagged(session):
    cid = "0x" + "a5" * 32
    _market(session, cid, resolution={T0: "1", T1: "0"})
    # buys more of the eventual loser (T1) than the winner (T0)
    _ev(session, cid=cid, etype="TRADE", ts=1, delta_shares="40",
        delta_usdc="-24", token_id=T0, price="0.60", side="BUY")
    _ev(session, cid=cid, etype="TRADE", ts=2, delta_shares="100",
        delta_usdc="-40", token_id=T1, price="0.40", side="BUY")
    _ev(session, cid=cid, etype="REDEEM", ts=3, delta_shares="-40", delta_usdc="40")

    audit = analyze_completion_sets(session, WALLET)
    row = _row(audit.redeem_orphan, cid)
    assert row["winner_token"] == T0
    assert Decimal(row["unmatched_loser_qty_at_resolution"]) == Decimal("60")
    assert Decimal(row["unmatched_winner_qty_at_resolution"]) == Decimal("0")


def test_coverage_counts_only_wallet_binary_markets(session):
    active_cid = "0x" + "a6" * 32
    inactive_cid = "0x" + "a7" * 32
    _market(session, active_cid)
    _market(session, inactive_cid, t0="3000", t1="4000")
    _ev(session, cid=active_cid, etype="TRADE", ts=1, delta_shares="10",
        delta_usdc="-4", token_id=T0, price="0.40", side="BUY")

    audit = analyze_completion_sets(session, WALLET)

    assert audit.coverage["ledger_condition_ids"] == 1
    assert audit.coverage["binary_condition_ids"] == 1
