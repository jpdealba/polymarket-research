"""Market exposure (Phase 16, view 4): directional+bond over time for a
selected condition. Imports only `pmresearch.api`."""

from __future__ import annotations

import streamlit as st

from pmresearch import api

from _common import get_settings, wallet_selector

st.title("Market Exposure")

settings = get_settings()
wallet = wallet_selector()

with api.open_session(settings) as session:
    all_rows = api.fetch_exposures(session, wallet)

if not all_rows:
    st.info("No exposures computed yet for this wallet. Run `pmr derive run` first.")
else:
    condition_ids = sorted({r.condition_id for r in all_rows})
    condition_id = st.selectbox("Condition", condition_ids)

    rows = [r for r in all_rows if r.condition_id == condition_id]
    st.dataframe(
        [
            {
                "date": r.date,
                "directional": float(r.directional) if r.directional is not None else None,
                "bond": float(r.bond) if r.bond is not None else None,
                "structure_type": r.structure_type,
                "event_id": r.event_id,
            }
            for r in rows
        ],
        use_container_width=True,
    )

    if rows:
        chart_data = {
            "directional": [float(r.directional) if r.directional is not None else 0.0 for r in rows],
            "bond": [float(r.bond) if r.bond is not None else 0.0 for r in rows],
        }
        st.line_chart(chart_data)
        st.caption(f"x-axis is row order, {rows[0].date} -> {rows[-1].date}")
