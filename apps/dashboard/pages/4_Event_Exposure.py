"""Event exposure (Phase 16, view 5): negRisk event-level exposure vectors.
Imports only `pmresearch.api`."""

from __future__ import annotations

import streamlit as st

from pmresearch import api

from _common import get_settings, wallet_selector

st.title("Event Exposure")

settings = get_settings()
wallet = wallet_selector()

with api.open_session(settings) as session:
    rows = api.fetch_event_exposures(session, wallet)

if not rows:
    st.info("No event exposures computed yet for this wallet. Run `pmr derive run` first.")
else:
    st.dataframe(
        [
            {
                "event_id": r.event_id,
                "date": r.date,
                "net_after_exclusivity": str(r.net_after_exclusivity),
            }
            for r in rows
        ],
        use_container_width=True,
    )

    st.subheader("Exposure vectors")
    for r in rows:
        st.write(f"**{r.event_id}** @ {r.date}")
        st.json(r.exposure_vector)
