"""Wallet comparison (Phase 16, view 11): side-by-side metrics across a
selected subset of the watchlist. Imports only `pmresearch.api`."""

from __future__ import annotations

import streamlit as st
from plotly import graph_objects as go

from pmresearch import api

from _common import CHART_CONFIG, COLORS, _merge_layout, format_decimal, get_settings

st.title("Wallet Comparison")

settings = get_settings()

with st.spinner("Loading wallet list..."):
    with api.open_session(settings) as session:
        all_wallets = api.list_wallets(session, active_only=False)

addresses = [w.address for w in all_wallets]
if not addresses:
    st.warning("No wallets on the watchlist. Run `pmr wallet add <addr>` first.")
    st.stop()

selected = st.multiselect(
    "Wallets to compare",
    addresses,
    default=addresses[: min(3, len(addresses))],
)

if not selected:
    st.info("Select at least one wallet to compare.")
    st.stop()

# ── Load data for all selected wallets ───────────────────────────────────────

rows = []
with st.spinner("Loading wallet metrics..."):
    with api.open_session(settings) as session:
        for address in selected:
            pnl_rows = api.fetch_pnl_decomposition(session, address, by_category=False)
            pnl_all = next((r for r in pnl_rows if r.scope == "all"), None)
            equity = api.latest_daily_equity(session, address)
            stats = api.episode_stats(session, address)
            labels = api.fetch_labels(session, address, scope="all")
            top_label = max(labels, key=lambda lbl: float(lbl.score), default=None)

            rows.append(
                {
                    "wallet": address[:16] + "..." if len(address) > 16 else address,
                    "address": address,
                    "total_pnl": float(pnl_all.total_pnl) if pnl_all is not None else 0.0,
                    "portfolio_value": (
                        float(equity.portfolio_value) if equity is not None else 0.0
                    ),
                    "episode_count": stats.count if stats else 0,
                    "top_hypothesis": top_label.detector_name if top_label is not None else None,
                    "top_score": float(top_label.score) if top_label is not None else 0.0,
                }
            )

# ── Comparison table ─────────────────────────────────────────────────────────

st.subheader("Metrics Comparison")
st.dataframe(
    [
        {
            "Wallet": r["wallet"],
            "Total PnL": format_decimal(r["total_pnl"]),
            "Portfolio Value": format_decimal(r["portfolio_value"]),
            "Episodes": r["episode_count"],
            "Top Hypothesis": r["top_hypothesis"] or "N/A",
            "Score": f"{r['top_score']:.2f}" if r["top_score"] else "N/A",
        }
        for r in rows
    ],
    use_container_width=True,
)

# ── Charts ───────────────────────────────────────────────────────────────────

wallet_labels = [r["wallet"] for r in rows]

# PnL comparison
st.subheader("PnL Comparison")
fig = go.Figure(
    go.Bar(
        x=wallet_labels,
        y=[r["total_pnl"] for r in rows],
        marker_color=[
            COLORS["positive"] if r["total_pnl"] >= 0 else COLORS["negative"] for r in rows
        ],
        text=[format_decimal(r["total_pnl"]) for r in rows],
        textposition="outside",
        hovertemplate="%{x}: $%{y:,.2f}<extra></extra>",
    )
)
fig.update_layout(
    **_merge_layout(
        dict(
            title="Total PnL by Wallet",
            xaxis_title="Wallet",
            yaxis_title="PnL ($)",
            showlegend=False,
        )
    )
)
st.plotly_chart(fig, use_container_width=True, config=CHART_CONFIG)

# Portfolio value comparison
st.subheader("Portfolio Value Comparison")
fig = go.Figure(
    go.Bar(
        x=wallet_labels,
        y=[r["portfolio_value"] for r in rows],
        marker_color=COLORS["directional"],
        text=[format_decimal(r["portfolio_value"]) for r in rows],
        textposition="outside",
        hovertemplate="%{x}: $%{y:,.2f}<extra></extra>",
    )
)
fig.update_layout(
    **_merge_layout(
        dict(
            title="Portfolio Value by Wallet",
            xaxis_title="Wallet",
            yaxis_title="Value ($)",
            showlegend=False,
        )
    )
)
st.plotly_chart(fig, use_container_width=True, config=CHART_CONFIG)

# Episode count comparison
st.subheader("Episode Count")
fig = go.Figure(
    go.Bar(
        x=wallet_labels,
        y=[r["episode_count"] for r in rows],
        marker_color=COLORS["reward"],
        text=[str(r["episode_count"]) for r in rows],
        textposition="outside",
        hovertemplate="%{x}: %{y}<extra></extra>",
    )
)
fig.update_layout(
    **_merge_layout(
        dict(
            title="Episode Count by Wallet",
            xaxis_title="Wallet",
            yaxis_title="Count",
            showlegend=False,
        )
    )
)
st.plotly_chart(fig, use_container_width=True, config=CHART_CONFIG)
