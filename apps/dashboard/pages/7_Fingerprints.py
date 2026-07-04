"""Fingerprint view (Phase 16, view 8): feature table + percentile rank vs
the watchlist. Imports only `pmresearch.api`."""

from __future__ import annotations

import streamlit as st

from pmresearch import api

from _common import get_settings, wallet_selector

st.title("Fingerprints")

settings = get_settings()
wallet = wallet_selector()

with api.open_session(settings) as session:
    scopes = api.fingerprint_scopes(session, wallet)

if not scopes:
    st.info("No fingerprints computed yet for this wallet. Run `pmr fingerprint run` first.")
    st.stop()

scope = st.selectbox("Scope", scopes)

with api.open_session(settings) as session:
    rows = api.fetch_fingerprints(session, wallet, scope=scope)

st.subheader("Features")
st.dataframe(
    [
        {
            "feature": r.feature,
            "family": r.family,
            "value": r.value,
            "value_type": r.value_type,
            "null_reason": r.null_reason,
        }
        for r in rows
    ],
    use_container_width=True,
)

st.subheader("Percentile vs watchlist (numeric features only)")
feature_names = [r.feature for r in rows]
feature = st.selectbox("Feature", feature_names)

with api.open_session(settings) as session:
    wallets = api.list_wallets(session, active_only=False)
    values_by_wallet: dict[str, float] = {}
    for w in wallets:
        wf_rows = api.fetch_fingerprints(session, w.address, scope=scope)
        cell = next((r for r in wf_rows if r.feature == feature), None)
        if cell is None or cell.value is None or cell.value_type != "scalar":
            continue
        try:
            values_by_wallet[w.address] = float(cell.value)
        except ValueError:
            continue

if wallet not in values_by_wallet or len(values_by_wallet) < 2:
    st.info("N/A insufficient comparison set")
else:
    sorted_addrs = sorted(values_by_wallet, key=lambda a: values_by_wallet[a])
    rank = sorted_addrs.index(wallet)
    percentile = rank / (len(sorted_addrs) - 1) * 100
    st.metric(
        f"{feature} percentile among {len(sorted_addrs)} wallets",
        f"{percentile:.1f}%",
    )
    st.write(f"Value: {values_by_wallet[wallet]}")
