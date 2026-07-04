"""Streamlit entrypoint — wallet overview (Phase 16, view 1). Imports only
`pmresearch.api`; see `apps/dashboard/_common.py`."""

from __future__ import annotations

import streamlit as st
from plotly import graph_objects as go

from pmresearch import api

from _common import (
    CHART_CONFIG,
    COLORS,
    DEFAULT_CADENCE_S,
    _merge_layout,
    empty_state,
    format_decimal,
    format_pct,
    get_settings,
    wallet_selector,
)

st.set_page_config(page_title="PMR Research Shell", layout="wide", page_icon="📊")
st.title("Wallet Overview")

settings = get_settings()
wallet = wallet_selector()

# ── Load all data with spinners ──────────────────────────────────────────────

with st.spinner("Loading equity data..."):
    with api.open_session(settings) as session:
        equity = api.latest_daily_equity(session, wallet)
        all_equity = api.fetch_daily_equity(session, wallet)

with st.spinner("Loading PnL decomposition..."):
    with api.open_session(settings) as session:
        pnl_rows = api.fetch_pnl_decomposition(session, wallet, by_category=False)

with st.spinner("Loading trust & sync status..."):
    with api.open_session(settings) as session:
        trust_rows = api.fetch_wallet_trust(session, wallet)
        sync_state = api.get_sync_state(session, wallet)
        stale = api.is_stale(session, wallet, cadence_s=DEFAULT_CADENCE_S)

# ── Trust badge ──────────────────────────────────────────────────────────────

trust = trust_rows[0] if trust_rows else None
if trust is not None:
    if trust.status == "trusted":
        st.success(f"**Trust:** Trusted — {trust.reason}")
    elif trust.status == "warn":
        st.warning(f"**Trust:** Warning — {trust.reason}")
    else:
        st.error(f"**Trust:** Untrusted — {trust.reason}")

# ── Key metrics row ──────────────────────────────────────────────────────────

cols = st.columns(5)
if equity is not None:
    cols[0].metric("Portfolio Value", format_decimal(equity.portfolio_value))
    marked = float(equity.marked_pnl)
    cols[1].metric(
        "Marked PnL",
        format_decimal(marked),
        delta=f"{format_decimal(marked)}",
        delta_color="normal",
    )
    cols[2].metric("Stale Equity", format_pct(equity.stale_equity_share))
else:
    cols[0].metric("Portfolio Value", "N/A")
    cols[1].metric("Marked PnL", "N/A")
    cols[2].metric("Stale Equity", "N/A")

pnl_all = next((r for r in pnl_rows if r.scope == "all"), None) if pnl_rows else None
if pnl_all is not None:
    total = float(pnl_all.total_pnl)
    cols[3].metric(
        "Total PnL",
        format_decimal(total),
        delta=f"{format_decimal(total)}",
        delta_color="normal",
    )
    cols[4].metric("Reward Income", format_decimal(pnl_all.reward_income))
else:
    cols[3].metric("Total PnL", "N/A")
    cols[4].metric("Reward Income", "N/A")

# ── Equity curve ─────────────────────────────────────────────────────────────

st.subheader("Equity Curve")
if not all_equity:
    empty_state("No daily equity computed yet. Run `pmr equity build` first.")
else:
    dates = [eq.date for eq in all_equity]
    values = [float(eq.portfolio_value) for eq in all_equity]
    stale_shares = [float(eq.stale_equity_share) for eq in all_equity]

    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=dates,
            y=values,
            mode="lines+markers",
            name="Portfolio Value",
            line=dict(color=COLORS["positive"], width=2),
            marker=dict(size=4),
            hovertemplate="Date: %{x}<br>Value: $%{y:,.2f}<extra></extra>",
        )
    )

    # Add stale region
    stale_dates = [d for d, s in zip(dates, stale_shares) if s > 0.1]
    stale_vals = [v for v, s in zip(values, stale_shares) if s > 0.1]
    if stale_dates:
        fig.add_trace(
            go.Scatter(
                x=stale_dates,
                y=stale_vals,
                mode="markers",
                name="High Stale (>10%)",
                marker=dict(color=COLORS["negative"], size=8, symbol="x"),
                hovertemplate="Date: %{x}<br>Value: $%{y:,.2f}<br>Stale: high<extra></extra>",
            )
        )

    fig.update_layout(
        **_merge_layout(
            dict(
                title="Portfolio Value Over Time",
                xaxis_title="Date",
                yaxis_title="Value ($)",
                showlegend=True,
            )
        )
    )
    st.plotly_chart(fig, use_container_width=True, config=CHART_CONFIG)

# ── PnL decomposition ───────────────────────────────────────────────────────

st.subheader("PnL Decomposition")
if pnl_all is None:
    empty_state("No PnL decomposition computed yet. Run `pmr derive run` first.")
else:
    cats = ["Directional", "Bond/Merge", "Reward", "Redemption", "Fees"]
    vals = [
        float(pnl_all.directional_pnl),
        float(pnl_all.bond_merge_pnl),
        float(pnl_all.reward_income),
        float(pnl_all.redemption_pnl),
        float(pnl_all.fees),
    ]
    bar_colors = [
        COLORS["directional"],
        COLORS["bond_merge"],
        COLORS["reward"],
        COLORS["redemption"],
        COLORS["fees"],
    ]

    fig = go.Figure(
        go.Bar(
            x=cats,
            y=vals,
            marker_color=bar_colors,
            text=[format_decimal(v) for v in vals],
            textposition="outside",
            hovertemplate="%{x}: $%{y:,.2f}<extra></extra>",
        )
    )
    fig.update_layout(
        **_merge_layout(
            dict(
                title="PnL by Source",
                xaxis_title="Source",
                yaxis_title="PnL ($)",
                showlegend=False,
            )
        )
    )
    st.plotly_chart(fig, use_container_width=True, config=CHART_CONFIG)

# ── Sync status ──────────────────────────────────────────────────────────────

st.subheader("Sync Status")
if sync_state is None:
    empty_state("No sync state recorded for this wallet yet.")
else:
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Status", sync_state.status)
    c2.metric("Last Success", sync_state.last_success_at or "never")
    c3.metric("Stale", "Yes" if stale else "No")
    c4.metric("Consecutive Failures", sync_state.consecutive_failures)
    if sync_state.last_error:
        st.error(f"Last error: {sync_state.last_error}")
