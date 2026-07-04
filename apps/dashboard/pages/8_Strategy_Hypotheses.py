"""Strategy hypotheses (Phase 16, view 9): scores + evidence + blind spots.
No thresholding into pass/fail — scores are shown as given. Imports only
`pmresearch.api`."""

from __future__ import annotations

import json

import streamlit as st
from plotly import graph_objects as go

from pmresearch import api

from _common import CHART_CONFIG, COLORS, _merge_layout, get_settings, wallet_selector

st.title("Strategy Hypotheses")

settings = get_settings()
wallet = wallet_selector()

with st.spinner("Loading strategy labels..."):
    with api.open_session(settings) as session:
        scopes = api.label_scopes(session, wallet)

if not scopes:
    st.info("No strategy labels computed yet for this wallet. Run `pmr detect run` first.")
    st.stop()

scope = st.selectbox("Scope", scopes)

with api.open_session(settings) as session:
    labels = api.fetch_labels(session, wallet, scope=scope)

if not labels:
    st.info("No labels found for this scope.")
    st.stop()

# ── Score overview chart ─────────────────────────────────────────────────────

st.subheader("Detector Scores")
detectors = [lbl.detector_name for lbl in labels]
scores = [float(lbl.score) for lbl in labels]
confidences = [float(lbl.confidence) for lbl in labels]

fig = go.Figure()
fig.add_trace(
    go.Bar(
        name="Score",
        x=detectors,
        y=scores,
        marker_color=COLORS["directional"],
        text=[f"{s:.2f}" for s in scores],
        textposition="outside",
        hovertemplate="%{x}<br>Score: %{y:.3f}<extra></extra>",
    )
)
fig.add_trace(
    go.Bar(
        name="Confidence",
        x=detectors,
        y=confidences,
        marker_color=COLORS["bond"],
        text=[f"{c:.2f}" for c in confidences],
        textposition="outside",
        hovertemplate="%{x}<br>Confidence: %{y:.3f}<extra></extra>",
    )
)
fig.update_layout(
    **_merge_layout(
        dict(
            title="Strategy Detector Scores",
            xaxis_title="Detector",
            yaxis_title="Score",
            barmode="group",
            yaxis=dict(range=[0, 1.1]),
        )
    )
)
st.plotly_chart(fig, use_container_width=True, config=CHART_CONFIG)

# ── Detailed labels table ────────────────────────────────────────────────────

st.subheader("All Labels")
st.dataframe(
    [
        {
            "detector": lbl.detector_name,
            "version": lbl.detector_version,
            "label": lbl.label,
            "score": f"{float(lbl.score):.3f}",
            "confidence": f"{float(lbl.confidence):.3f}",
        }
        for lbl in labels
    ],
    use_container_width=True,
)

# ── Evidence expanders ───────────────────────────────────────────────────────

st.subheader("Evidence & Blind Spots")
for lbl in labels:
    with st.expander(f"{lbl.detector_name} — Evidence & Blind Spots", expanded=False):
        evidence = json.loads(lbl.evidence_json)

        # Split evidence into features and missing
        features = {k: v for k, v in evidence.items() if k != "missing_features"}
        missing = evidence.get("missing_features", [])

        if features:
            st.write("**Evidence Features:**")
            feat_names = list(features.keys())
            feat_vals = []
            for v in features.values():
                try:
                    feat_vals.append(float(v))
                except (TypeError, ValueError):
                    feat_vals.append(0.0)

            fig = go.Figure(
                go.Bar(
                    x=feat_names,
                    y=feat_vals,
                    marker_color=COLORS["directional"],
                    text=[f"{v:.3f}" for v in feat_vals],
                    textposition="outside",
                )
            )
            fig.update_layout(
                **_merge_layout(
                    dict(
                        title=f"{lbl.detector_name} — Feature Values",
                        showlegend=False,
                        height=300,
                    )
                )
            )
            st.plotly_chart(fig, use_container_width=True, config=CHART_CONFIG)

        if missing:
            st.write("**Missing Features:**")
            for feat in missing:
                st.warning(f"  {feat}")

        st.write(f"**Blind Spots:** {lbl.blind_spots}")
