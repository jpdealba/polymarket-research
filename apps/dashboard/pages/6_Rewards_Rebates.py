"""Reward/rebate analysis (Phase 16, view 7). Imports only `pmresearch.api`."""

from __future__ import annotations

import streamlit as st
from plotly import graph_objects as go

from pmresearch import api

from _common import CHART_CONFIG, COLORS, _merge_layout, format_decimal, get_settings, wallet_selector

st.title("Rewards & Rebates")

settings = get_settings()
wallet = wallet_selector()

with st.spinner("Loading rewards data..."):
    with api.open_session(settings) as session:
        pnl_rows = api.fetch_pnl_decomposition(session, wallet, by_category=False)
        fingerprints = api.fetch_fingerprints(session, wallet, scope="all")
        fee_rows = api.fee_attribution_report(session, wallet=wallet)

# ── Reward income ────────────────────────────────────────────────────────────

st.subheader("Reward Income")
row_all = next((r for r in pnl_rows if r.scope == "all"), None) if pnl_rows else None
if row_all is None:
    st.info("No PnL decomposition computed yet. Run `pmr derive run` first.")
else:
    st.metric("Total Reward Income", format_decimal(row_all.reward_income))

# ── Reward/rebate fingerprints ───────────────────────────────────────────────

st.subheader("Reward/Rebate Fingerprint Features")
matches = [
    fp for fp in fingerprints if "reward" in fp.feature.lower() or "rebate" in fp.feature.lower()
]
if not matches:
    st.info("No reward/rebate fingerprint features computed yet. Run `pmr fingerprint run`.")
else:
    st.dataframe(
        [
            {
                "feature": fp.feature,
                "value": fp.value,
                "value_type": fp.value_type,
                "null_reason": fp.null_reason,
            }
            for fp in matches
        ],
        use_container_width=True,
    )

# ── Fee attribution: maker/taker split ───────────────────────────────────────

st.subheader("Fee Attribution: Maker/Taker Split")
if not fee_rows:
    st.info("No fee attribution computed yet. Run `pmr fees report` first.")
else:
    # Find the "all" row
    fee_all = next((r for r in fee_rows if r.period == "all" and r.category == "all"), None)

    if fee_all:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Maker Trades", fee_all.maker_trades)
        c2.metric("Taker Trades", fee_all.taker_trades)
        c3.metric("Maker Volume", format_decimal(fee_all.maker_volume))
        c4.metric("Taker Volume", format_decimal(fee_all.taker_volume))

        # Pie chart
        labels = ["Maker Volume", "Taker Volume"]
        values = [float(fee_all.maker_volume), float(fee_all.taker_volume)]
        if sum(values) > 0:
            fig = go.Figure(
                go.Pie(
                    labels=labels,
                    values=values,
                    marker=dict(colors=[COLORS["directional"], COLORS["negative"]]),
                    textinfo="label+percent",
                    hovertemplate="%{label}: $%{value:,.2f}<extra></extra>",
                )
            )
            fig.update_layout(
                **_merge_layout(dict(title="Volume Split: Maker vs Taker", showlegend=False))
            )
            st.plotly_chart(fig, use_container_width=True, config=CHART_CONFIG)

    # Detailed table
    with st.expander("Detailed fee attribution"):
        st.dataframe(
            [
                {
                    "period": r.period,
                    "category": r.category,
                    "maker_trades": r.maker_trades,
                    "taker_trades": r.taker_trades,
                    "maker_volume": format_decimal(r.maker_volume),
                    "taker_volume": format_decimal(r.taker_volume),
                    "maker_fee": format_decimal(r.maker_fee),
                    "taker_fee": format_decimal(r.taker_fee),
                }
                for r in fee_rows
            ],
            use_container_width=True,
        )
