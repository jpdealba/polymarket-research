"""Fingerprint view (Phase 16, view 8): feature table + percentile rank vs
the watchlist. Imports only `pmresearch.api`."""

from __future__ import annotations

import streamlit as st
from plotly import graph_objects as go

from pmresearch import api

from _common import CHART_CONFIG, COLORS, _merge_layout, get_settings, wallet_selector

st.title("Fingerprints")

settings = get_settings()
wallet = wallet_selector()

with st.spinner("Loading fingerprints..."):
    with api.open_session(settings) as session:
        scopes = api.fingerprint_scopes(session, wallet)

if not scopes:
    st.info("No fingerprints computed yet for this wallet. Run `pmr fingerprint run` first.")
    st.stop()

scope = st.selectbox("Scope", scopes)

with api.open_session(settings) as session:
    rows = api.fetch_fingerprints(session, wallet, scope=scope)

# ── Features table ───────────────────────────────────────────────────────────

st.subheader("Features")
scalar_rows = [r for r in rows if r.value_type == "scalar"]
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

# ── Feature bar chart ────────────────────────────────────────────────────────

if scalar_rows:
    st.subheader("Feature Values (Scalar)")
    features = [r.feature for r in scalar_rows]
    values = []
    for r in scalar_rows:
        try:
            values.append(float(r.value))
        except (TypeError, ValueError):
            values.append(0.0)

    fig = go.Figure(
        go.Bar(
            x=features,
            y=values,
            marker_color=COLORS["directional"],
            text=[f"{v:.4f}" for v in values],
            textposition="outside",
            hovertemplate="%{x}: %{y:.4f}<extra></extra>",
        )
    )
    fig.update_layout(
        **_merge_layout(
            dict(
                title="Scalar Feature Values",
                xaxis_title="Feature",
                yaxis_title="Value",
                showlegend=False,
            )
        )
    )
    st.plotly_chart(fig, use_container_width=True, config=CHART_CONFIG)

# ── Percentile vs watchlist ──────────────────────────────────────────────────

st.subheader("Percentile vs Watchlist (Numeric Features Only)")
feature_names = [r.feature for r in rows if r.value_type == "scalar"]
if not feature_names:
    st.info("No scalar features available for comparison.")
else:
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
        st.info("N/A — insufficient comparison set (need at least 2 wallets with this feature)")
    else:
        sorted_addrs = sorted(values_by_wallet, key=lambda a: values_by_wallet[a])
        rank = sorted_addrs.index(wallet)
        percentile = rank / (len(sorted_addrs) - 1) * 100

        c1, c2, c3 = st.columns(3)
        c1.metric(
            f"{feature} Percentile",
            f"{percentile:.1f}%",
        )
        c2.metric("Your Value", f"{values_by_wallet[wallet]:.4f}")
        c3.metric("Wallets Compared", len(sorted_addrs))

        # Bar chart showing all wallets
        fig = go.Figure(
            go.Bar(
                x=list(values_by_wallet.keys()),
                y=list(values_by_wallet.values()),
                marker_color=[
                    COLORS["positive"] if a == wallet else COLORS["neutral"]
                    for a in values_by_wallet
                ],
                hovertemplate="%{x}: %{y:.4f}<extra></extra>",
            )
        )
        fig.update_layout(
            **_merge_layout(
                dict(
                    title=f"{feature} — All Wallets",
                    xaxis_title="Wallet",
                    yaxis_title="Value",
                    showlegend=False,
                )
            )
        )
        st.plotly_chart(fig, use_container_width=True, config=CHART_CONFIG)
