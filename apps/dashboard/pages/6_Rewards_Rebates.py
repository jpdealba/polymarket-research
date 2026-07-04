"""Reward/rebate analysis (Phase 16, view 7). Imports only `pmresearch.api`."""

from __future__ import annotations

import streamlit as st

from pmresearch import api

from _common import get_settings, wallet_selector

st.title("Rewards & Rebates")

settings = get_settings()
wallet = wallet_selector()

with api.open_session(settings) as session:
    pnl_rows = api.fetch_pnl_decomposition(session, wallet, by_category=False)
    fingerprints = api.fetch_fingerprints(session, wallet, scope="all")
    fee_rows = api.fee_attribution_report(session, wallet=wallet)

st.subheader("Reward income (PnL decomposition, scope=all)")
row_all = next((r for r in pnl_rows if r.scope == "all"), None)
if row_all is None:
    st.info("No PnL decomposition computed yet for this wallet. Run `pmr derive run` first.")
else:
    st.metric("Reward income", f"{row_all.reward_income:.6f}")

st.subheader("Reward/rebate fingerprint features")
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

st.subheader("Fee attribution: maker/taker split")
if not fee_rows:
    st.info("No fee attribution computed yet for this wallet. Run `pmr fees report` first.")
else:
    st.dataframe(
        [
            {
                "period": r.period,
                "category": r.category,
                "maker_trades": r.maker_trades,
                "taker_trades": r.taker_trades,
                "maker_volume": str(r.maker_volume),
                "taker_volume": str(r.taker_volume),
                "maker_fee": str(r.maker_fee),
                "taker_fee": str(r.taker_fee),
            }
            for r in fee_rows
        ],
        use_container_width=True,
    )
    st.info(
        "`fee_attribution_report` has no dedicated reward/rebate field beyond "
        "maker/taker fee splits shown above."
    )
