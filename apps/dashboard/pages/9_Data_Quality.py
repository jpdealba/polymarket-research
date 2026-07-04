"""Data quality (Phase 16, view 10): reconciliation, sync staleness, stale
marks, enrichment coverage, book sampler status, negative holdings. Trust and
reconciliation status are shown as a prominent banner. Imports only
`pmresearch.api`."""

from __future__ import annotations

import time

import streamlit as st
from plotly import graph_objects as go

from pmresearch import api

from _common import (
    CHART_CONFIG,
    COLORS,
    DEFAULT_CADENCE_S,
    _merge_layout,
    format_decimal,
    format_pct,
    format_ts,
    get_settings,
    wallet_selector,
)

st.title("Data Quality")

settings = get_settings()
wallet = wallet_selector()

with st.spinner("Loading data quality metrics..."):
    with api.open_session(settings) as session:
        recon_results = api.latest_reconciliation_result(session, wallet)
        sync_state = api.get_sync_state(session, wallet)
        stale = api.is_stale(session, wallet, cadence_s=DEFAULT_CADENCE_S)
        coverage = api.enrichment_coverage(session, wallet, now_ts=int(time.time()))
        equity = api.latest_daily_equity(session, wallet)
        book_status = api.book_sampler_status(session, settings)
        negative_rows, negative_summary = api.negative_holdings_report(session, wallet)
        # Phase 17 staleness alerts
        alerts = api.check_wallet_alerts(session, settings)
        wallet_alerts = [a for a in alerts if a.wallet == wallet.lower()]

# ── Staleness alert banner ──────────────────────────────────────────────────
if wallet_alerts:
    error_alerts = [a for a in wallet_alerts if a.severity == api.AlertSeverity.ERROR]
    warn_alerts = [a for a in wallet_alerts if a.severity == api.AlertSeverity.WARNING]
    if error_alerts:
        for a in error_alerts:
            st.error(f"ALERT: {a.message}")
    elif warn_alerts:
        for a in warn_alerts:
            st.warning(f"ALERT: {a.message}")

# ── Trust & reconciliation banner ────────────────────────────────────────────

match = next((pair for pair in recon_results if pair[0].wallet == wallet.lower()), None)

st.header("Trust & Reconciliation")
if match is None:
    st.warning("No reconciliation has run for this wallet. Run `pmr reconcile run`.")
else:
    result, trust = match
    if trust is None:
        st.warning("Reconciliation ran but no trust record exists.")
    elif trust.status == "trusted":
        st.success(f"**TRUSTED** — {trust.reason}")
    elif trust.status == "warn":
        st.warning(f"**WARNING** — {trust.reason}")
    else:
        st.error(f"**UNTRUSTED** — {trust.reason}")

    # Reconciliation summary as metrics
    summary = result.summary()
    if isinstance(summary, dict):
        st.subheader("Reconciliation Summary")
        cols = st.columns(min(len(summary), 6))
        for i, (key, val) in enumerate(summary.items()):
            cols[i % len(cols)].metric(key.replace("_", " ").title(), str(val))

    # Top discrepancies
    discrepancies = result.top_qty_discrepancies()
    if discrepancies:
        st.subheader("Top Quantity Discrepancies")
        st.dataframe(discrepancies, use_container_width=True)

# ── Sync staleness ──────────────────────────────────────────────────────────

st.header("Sync Status")
if sync_state is None:
    st.info("No sync state recorded for this wallet yet.")
else:
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Status", sync_state.status)
    c2.metric("Last Success", sync_state.last_success_at or "never")
    stale_color = "inverse" if stale else "normal"
    c3.metric("Stale", "Yes" if stale else "No")
    c4.metric("Failures", sync_state.consecutive_failures)

    if sync_state.last_error:
        st.error(f"Last error: {sync_state.last_error}")

# ── Stale marks ──────────────────────────────────────────────────────────────

st.header("Mark Quality")
if equity is None:
    st.info("No daily equity computed yet for this wallet.")
else:
    stale_pct = float(equity.stale_equity_share)
    c1, c2 = st.columns(2)
    c1.metric("Stale Equity Share", format_pct(stale_pct))

    # Progress bar for staleness
    if stale_pct > 0.5:
        c2.error("High staleness — equity figures may be unreliable")
    elif stale_pct > 0.1:
        c2.warning("Moderate staleness — some holdings marked with old prices")
    else:
        c2.success("Low staleness — equity figures are reliable")

    st.progress(min(stale_pct, 1.0))

# ── Enrichment coverage ──────────────────────────────────────────────────────

st.header("Enrichment Coverage")
if coverage:
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Total Fills", coverage.total)
    c2.metric("Enriched", coverage.enriched)
    c3.metric("Pending", coverage.pending)
    c4.metric("Ambiguous", coverage.ambiguous)
    c5.metric("Missing", coverage.missing)

    enriched_pct = float(coverage.enriched_share)
    st.progress(min(enriched_pct, 1.0))
    st.caption(f"Enriched: {format_pct(enriched_pct)}")

    # Coverage by time bucket
    if hasattr(coverage, "buckets") and coverage.buckets:
        st.subheader("Coverage by Time Bucket")
        bucket_data = []
        for b in coverage.buckets:
            bucket_data.append(
                {
                    "Period": b.label,
                    "Total": b.total,
                    "Enriched": b.enriched,
                    "Pending": b.pending,
                    "Enriched %": f"{b.enriched / b.total * 100:.1f}%" if b.total > 0 else "N/A",
                }
            )
        st.dataframe(bucket_data, use_container_width=True)

        # Bar chart
        labels = [b.label for b in coverage.buckets]
        enriched_vals = [b.enriched for b in coverage.buckets]
        pending_vals = [b.pending for b in coverage.buckets]

        fig = go.Figure()
        fig.add_trace(
            go.Bar(
                name="Enriched",
                x=labels,
                y=enriched_vals,
                marker_color=COLORS["positive"],
            )
        )
        fig.add_trace(
            go.Bar(
                name="Pending",
                x=labels,
                y=pending_vals,
                marker_color=COLORS["bond"],
            )
        )
        fig.update_layout(
            **_merge_layout(
                dict(
                    title="Enrichment by Time Period",
                    barmode="stack",
                    xaxis_title="Time Period",
                    yaxis_title="Fills",
                )
            )
        )
        st.plotly_chart(fig, use_container_width=True, config=CHART_CONFIG)

# ── Book sampler status ──────────────────────────────────────────────────────

st.header("Book Sampler")
if book_status:
    c1, c2, c3 = st.columns(3)
    c1.metric("Tokens Tracked", book_status.token_count)
    c2.metric("Snapshots", book_status.snapshot_count)
    c3.metric("Storage", f"{book_status.raw_storage_bytes / 1024 / 1024:.1f} MB")

    c4, c5, c6 = st.columns(3)
    c4.metric("Raw Fetches", book_status.raw_fetch_count)
    c5.metric("Oldest", format_ts(book_status.oldest_ts) if book_status.oldest_ts else "N/A")
    c6.metric("Newest", format_ts(book_status.newest_ts) if book_status.newest_ts else "N/A")

# ── Negative holdings ────────────────────────────────────────────────────────

st.header("Negative Holdings")
if not negative_rows:
    st.success("No negative holdings for this wallet.")
else:
    summary = negative_summary
    if hasattr(summary, "__dict__"):
        c1, c2, c3 = st.columns(3)
        c1.metric("Negative Tokens", summary.negative_token_count)
        c2.metric("Negative Conditions", summary.negative_condition_count)
        c3.metric("Total Negative Qty", format_decimal(summary.total_negative_qty))

    st.dataframe(
        [
            {
                "token_id": r.token_id,
                "qty": format_decimal(r.qty),
                "condition_id": r.condition_id,
                "question": r.question,
                "cause_event_type": r.cause_event_type,
            }
            for r in negative_rows
        ],
        use_container_width=True,
    )
