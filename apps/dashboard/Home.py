"""Streamlit entrypoint — wallet overview (Phase 16, view 1). Imports only
`pmresearch.api`; see `apps/dashboard/_common.py`."""

from __future__ import annotations

import streamlit as st

from pmresearch import api

from _common import DEFAULT_CADENCE_S, get_settings, wallet_selector

st.set_page_config(page_title="PMR Research Shell", layout="wide")
st.title("Wallet overview")

settings = get_settings()
wallet = wallet_selector()

with api.open_session(settings) as session:
    equity = api.latest_daily_equity(session, wallet)
    pnl_rows = api.fetch_pnl_decomposition(session, wallet, by_category=False)
    trust_rows = api.fetch_wallet_trust(session, wallet)
    sync_state = api.get_sync_state(session, wallet)
    stale = api.is_stale(session, wallet, cadence_s=DEFAULT_CADENCE_S)

st.subheader("Trust")
trust = trust_rows[0] if trust_rows else None
if trust is None:
    st.warning("No reconciliation has run for this wallet yet. Run `pmr reconcile run`.")
elif trust.status == "trusted":
    st.success(f"trusted — {trust.reason}")
elif trust.status == "warn":
    st.warning(f"warn — {trust.reason}")
else:
    st.error(f"{trust.status} — {trust.reason}")

st.subheader("Latest daily equity")
if equity is None:
    st.info("No daily equity computed yet for this wallet. Run `pmr equity build` first.")
else:
    cols = st.columns(4)
    cols[0].metric("Portfolio value", f"{equity.portfolio_value:.2f}")
    cols[1].metric("Marked PnL", f"{equity.marked_pnl:.2f}")
    cols[2].metric("Stale equity share", f"{equity.stale_equity_share:.2%}")
    cols[3].metric("As of", equity.date)

st.subheader("PnL decomposition (all)")
pnl_all = next((r for r in pnl_rows if r.scope == "all"), None)
if pnl_all is None:
    st.info("No PnL decomposition computed yet for this wallet. Run `pmr derive run` first.")
else:
    cols = st.columns(5)
    cols[0].metric("Directional", f"{pnl_all.directional_pnl:.2f}")
    cols[1].metric("Bond/merge", f"{pnl_all.bond_merge_pnl:.2f}")
    cols[2].metric("Reward income", f"{pnl_all.reward_income:.2f}")
    cols[3].metric("Redemption", f"{pnl_all.redemption_pnl:.2f}")
    cols[4].metric("Total", f"{pnl_all.total_pnl:.2f}")

st.subheader("Sync status")
if sync_state is None:
    st.info("No sync state recorded for this wallet yet.")
else:
    cols = st.columns(3)
    cols[0].metric("Status", sync_state.status)
    cols[1].metric("Last success", sync_state.last_success_at or "never")
    cols[2].metric("Stale?", "yes" if stale else "no")
    if sync_state.last_error:
        st.error(f"Last error: {sync_state.last_error}")
