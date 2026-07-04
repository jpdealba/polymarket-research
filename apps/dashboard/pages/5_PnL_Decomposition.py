"""PnL decomposition (Phase 16, view 6): all-scope bar chart + per-category
table. Imports only `pmresearch.api`."""

from __future__ import annotations

import streamlit as st
from plotly import graph_objects as go

from pmresearch import api

from _common import CHART_CONFIG, COLORS, _merge_layout, format_decimal, get_settings, wallet_selector

st.title("PnL Decomposition")

settings = get_settings()
wallet = wallet_selector()

with st.spinner("Loading PnL decomposition..."):
    with api.open_session(settings) as session:
        all_rows = api.fetch_pnl_decomposition(session, wallet, by_category=False)
        cat_rows = api.fetch_pnl_decomposition(session, wallet, by_category=True)

# ── All scope chart ──────────────────────────────────────────────────────────

st.subheader("All Scope")
row_all = next((r for r in all_rows if r.scope == "all"), None) if all_rows else None
if row_all is None:
    st.info("No PnL decomposition computed yet for this wallet. Run `pmr derive run` first.")
else:
    cats = ["Directional", "Bond/Merge", "Reward", "Redemption", "Fees", "Total"]
    vals = [
        float(row_all.directional_pnl),
        float(row_all.bond_merge_pnl),
        float(row_all.reward_income),
        float(row_all.redemption_pnl),
        float(row_all.fees),
        float(row_all.total_pnl),
    ]
    bar_colors = [
        COLORS["directional"],
        COLORS["bond_merge"],
        COLORS["reward"],
        COLORS["redemption"],
        COLORS["fees"],
        COLORS["positive"] if vals[-1] >= 0 else COLORS["negative"],
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
                title="PnL Decomposition — All",
                xaxis_title="Source",
                yaxis_title="PnL ($)",
                showlegend=False,
            )
        )
    )
    st.plotly_chart(fig, use_container_width=True, config=CHART_CONFIG)

# ── By category ──────────────────────────────────────────────────────────────

st.subheader("By Category")
if not cat_rows:
    st.info("No per-category PnL decomposition computed yet for this wallet.")
else:
    st.dataframe(
        [
            {
                "category": r.scope.removeprefix("category:"),
                "directional_pnl": format_decimal(r.directional_pnl),
                "bond_merge_pnl": format_decimal(r.bond_merge_pnl),
                "reward_income": format_decimal(r.reward_income),
                "redemption_pnl": format_decimal(r.redemption_pnl),
                "fees": format_decimal(r.fees),
                "total_pnl": format_decimal(r.total_pnl),
            }
            for r in cat_rows
        ],
        use_container_width=True,
    )

    # Stacked bar chart per category
    categories = [r.scope.removeprefix("category:") for r in cat_rows]
    fig = go.Figure()
    fig.add_trace(
        go.Bar(
            name="Directional",
            x=categories,
            y=[float(r.directional_pnl) for r in cat_rows],
            marker_color=COLORS["directional"],
        )
    )
    fig.add_trace(
        go.Bar(
            name="Bond/Merge",
            x=categories,
            y=[float(r.bond_merge_pnl) for r in cat_rows],
            marker_color=COLORS["bond_merge"],
        )
    )
    fig.add_trace(
        go.Bar(
            name="Reward",
            x=categories,
            y=[float(r.reward_income) for r in cat_rows],
            marker_color=COLORS["reward"],
        )
    )
    fig.add_trace(
        go.Bar(
            name="Redemption",
            x=categories,
            y=[float(r.redemption_pnl) for r in cat_rows],
            marker_color=COLORS["redemption"],
        )
    )
    fig.update_layout(
        **_merge_layout(
            dict(
                title="PnL by Category",
                barmode="stack",
                xaxis_title="Category",
                yaxis_title="PnL ($)",
            )
        )
    )
    st.plotly_chart(fig, use_container_width=True, config=CHART_CONFIG)
