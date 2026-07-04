"""Data quality (Phase 16, view 10): reconciliation, sync staleness, stale
marks, enrichment coverage, book sampler status, negative holdings. Trust and
reconciliation status are shown as a prominent banner. Imports only
`pmresearch.api`."""

from __future__ import annotations

import time

import streamlit as st

from pmresearch import api

from _common import DEFAULT_CADENCE_S, get_settings, wallet_selector

st.title("Data Quality")

settings = get_settings()
wallet = wallet_selector()

with api.open_session(settings) as session:
    recon_results = api.latest_reconciliation_result(session, wallet)
    sync_state = api.get_sync_state(session, wallet)
    stale = api.is_stale(session, wallet, cadence_s=DEFAULT_CADENCE_S)
    coverage = api.enrichment_coverage(session, wallet, now_ts=int(time.time()))
    equity = api.latest_daily_equity(session, wallet)
    book_status = api.book_sampler_status(session, settings)
    negative_rows, negative_summary = api.negative_holdings_report(session, wallet)

match = next((pair for pair in recon_results if pair[0].wallet == wallet.lower()), None)

st.header("Trust & reconciliation banner")
if match is None:
    st.warning("No reconciliation has run for this wallet. Run `pmr reconcile run`.")
else:
    result, trust = match
    if trust is None:
        st.warning("Reconciliation ran but no trust record exists.")
    elif trust.status == "trusted":
        st.success(f"TRUSTED — {trust.reason}")
    elif trust.status == "warn":
        st.warning(f"WARN — {trust.reason}")
    else:
        st.error(f"UNTRUSTED — {trust.reason}")

    st.subheader("Reconciliation summary")
    st.json(result.summary())

    st.subheader("Top quantity discrepancies")
    discrepancies = result.top_qty_discrepancies()
    if not discrepancies:
        st.info("No quantity discrepancies.")
    else:
        st.dataframe(discrepancies, use_container_width=True)

st.header("Sync staleness")
if sync_state is None:
    st.info("No sync state recorded for this wallet yet.")
else:
    cols = st.columns(3)
    cols[0].metric("Status", sync_state.status)
    cols[1].metric("Last success", sync_state.last_success_at or "never")
    cols[2].metric("Stale?", "yes" if stale else "no")

st.header("Stale marks")
if equity is None:
    st.info("No daily equity computed yet for this wallet.")
else:
    st.metric("Latest stale-equity share", f"{equity.stale_equity_share:.2%}")

st.header("Enrichment coverage")
st.json(
    {
        "total": coverage.total,
        "enriched": coverage.enriched,
        "pending": coverage.pending,
        "ambiguous": coverage.ambiguous,
        "missing": coverage.missing,
        "enriched_share": str(coverage.enriched_share),
    }
)

st.header("Book sampler status")
st.json(
    {
        "token_count": book_status.token_count,
        "snapshot_count": book_status.snapshot_count,
        "with_raw_ref": book_status.with_raw_ref,
        "raw_fetch_count": book_status.raw_fetch_count,
        "oldest_ts": book_status.oldest_ts,
        "newest_ts": book_status.newest_ts,
        "raw_storage_bytes": book_status.raw_storage_bytes,
    }
)

st.header("Negative holdings")
if not negative_rows:
    st.info("No negative holdings for this wallet.")
else:
    st.write(negative_summary.__dict__ if hasattr(negative_summary, "__dict__") else negative_summary)
    st.dataframe(
        [
            {
                "token_id": r.token_id,
                "qty": str(r.qty),
                "condition_id": r.condition_id,
                "question": r.question,
                "cause_event_type": r.cause_event_type,
            }
            for r in negative_rows
        ],
        use_container_width=True,
    )
