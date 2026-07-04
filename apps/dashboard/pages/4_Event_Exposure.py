"""Event exposure (Phase 16, view 5): negRisk event-level exposure vectors.
Imports only `pmresearch.api`."""

from __future__ import annotations

import streamlit as st
from plotly import graph_objects as go

from pmresearch import api

from _common import CHART_CONFIG, COLORS, _merge_layout, format_decimal, get_settings, wallet_selector

st.title("Event Exposure")

settings = get_settings()
wallet = wallet_selector()

with st.spinner("Loading event exposures..."):
    with api.open_session(settings) as session:
        rows = api.fetch_event_exposures(session, wallet)

if not rows:
    st.info("No event exposures computed yet for this wallet. Run `pmr derive run` first.")
else:
    # Summary table
    st.subheader("Exposure Summary")
    st.dataframe(
        [
            {
                "event_id": r.event_id,
                "date": r.date,
                "net_after_exclusivity": format_decimal(r.net_after_exclusivity),
            }
            for r in rows
        ],
        use_container_width=True,
    )

    # Visualization per event
    st.subheader("Exposure Vectors")
    event_ids = sorted({r.event_id for r in rows})
    selected_event = st.selectbox("Select event", event_ids)

    event_rows = [r for r in rows if r.event_id == selected_event]
    if event_rows:
        latest = event_rows[-1]
        vector = latest.exposure_vector

        if vector:
            conditions = list(vector.keys())
            values = [float(v) for v in vector.values()]
            bar_colors = [COLORS["positive"] if v >= 0 else COLORS["negative"] for v in values]

            fig = go.Figure(
                go.Bar(
                    x=[c[:16] + "..." if len(c) > 16 else c for c in conditions],
                    y=values,
                    marker_color=bar_colors,
                    text=[format_decimal(v) for v in values],
                    textposition="outside",
                    hovertemplate="Condition: %{x}<br>Net Exposure: %{y:.4f}<extra></extra>",
                )
            )
            fig.update_layout(
                **_merge_layout(
                    dict(
                        title=f"Exposure Vector — {selected_event}",
                        xaxis_title="Condition ID",
                        yaxis_title="Net Directional Exposure",
                        showlegend=False,
                    )
                )
            )
            st.plotly_chart(fig, use_container_width=True, config=CHART_CONFIG)

        c1, c2 = st.columns(2)
        c1.metric("Net After Exclusivity", format_decimal(latest.net_after_exclusivity))
        c2.metric("Date", latest.date)
