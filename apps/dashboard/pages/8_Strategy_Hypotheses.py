"""Strategy hypotheses (Phase 16, view 9): scores + evidence + blind spots.
No thresholding into pass/fail — scores are shown as given. Imports only
`pmresearch.api`."""

from __future__ import annotations

import json

import streamlit as st

from pmresearch import api

from _common import get_settings, wallet_selector

st.title("Strategy Hypotheses")

settings = get_settings()
wallet = wallet_selector()

with api.open_session(settings) as session:
    scopes = api.label_scopes(session, wallet)

if not scopes:
    st.info("No strategy labels computed yet for this wallet. Run `pmr detect run` first.")
    st.stop()

scope = st.selectbox("Scope", scopes)

with api.open_session(settings) as session:
    labels = api.fetch_labels(session, wallet, scope=scope)

st.dataframe(
    [
        {
            "detector_name": lbl.detector_name,
            "detector_version": lbl.detector_version,
            "label": lbl.label,
            "score": lbl.score,
            "confidence": lbl.confidence,
        }
        for lbl in labels
    ],
    use_container_width=True,
)

for lbl in labels:
    with st.expander(f"{lbl.detector_name} — evidence & blind spots"):
        st.json(json.loads(lbl.evidence_json))
        st.write(f"**Blind spots:** {lbl.blind_spots}")
