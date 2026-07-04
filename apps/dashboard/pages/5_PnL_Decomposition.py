"""PnL decomposition (Phase 16, view 6): all-scope bar chart + per-category
table. Imports only `pmresearch.api`."""

from __future__ import annotations

import streamlit as st

from pmresearch import api

from _common import get_settings, wallet_selector

st.title("PnL Decomposition")

settings = get_settings()
wallet = wallet_selector()

with api.open_session(settings) as session:
    all_rows = api.fetch_pnl_decomposition(session, wallet, by_category=False)
    cat_rows = api.fetch_pnl_decomposition(session, wallet, by_category=True)

st.subheader("All scope")
row_all = next((r for r in all_rows if r.scope == "all"), None)
if row_all is None:
    st.info("No PnL decomposition computed yet for this wallet. Run `pmr derive run` first.")
else:
    st.bar_chart(
        {
            "directional": [float(row_all.directional_pnl)],
            "bond_merge": [float(row_all.bond_merge_pnl)],
            "reward_income": [float(row_all.reward_income)],
            "redemption": [float(row_all.redemption_pnl)],
            "fees": [float(row_all.fees)],
        }
    )

st.subheader("By category")
if not cat_rows:
    st.info("No per-category PnL decomposition computed yet for this wallet.")
else:
    st.dataframe(
        [
            {
                "category": r.scope.removeprefix("category:"),
                "directional_pnl": str(r.directional_pnl),
                "bond_merge_pnl": str(r.bond_merge_pnl),
                "reward_income": str(r.reward_income),
                "redemption_pnl": str(r.redemption_pnl),
                "fees": str(r.fees),
                "total_pnl": str(r.total_pnl),
            }
            for r in cat_rows
        ],
        use_container_width=True,
    )
