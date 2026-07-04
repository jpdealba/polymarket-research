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
    event_ids = sorted({r.event_id for r in rows})

    # Resolve event labels
    with api.open_session(settings) as session:
        event_labels = api.resolve_event_labels(session, event_ids)
        # Also resolve condition labels for the vectors
        all_conditions = set()
        for r in rows:
            all_conditions.update(r.exposure_vector.keys())
        condition_labels = api.resolve_market_labels(session, list(all_conditions))

    def _event_display(eid: str) -> str:
        lbl = event_labels.get(eid)
        if lbl:
            t = lbl.title[:50] + "..." if len(lbl.title) > 50 else lbl.title
            return f"{t} [{eid[:8]}]"
        return eid[:16] + "..."

    # Summary table
    st.subheader("Exposure Summary")
    st.dataframe(
        [
            {
                "event": (event_labels.get(r.event_id, None) or type("", (), {"title": r.event_id})()).title[:50],
                "date": r.date,
                "net_after_exclusivity": format_decimal(r.net_after_exclusivity),
            }
            for r in rows
        ],
        use_container_width=True,
    )

    # Visualization per event
    st.subheader("Exposure Vectors")
    selected_event = st.selectbox(
        "Select event",
        event_ids,
        format_func=_event_display,
    )

    event_rows = [r for r in rows if r.event_id == selected_event]
    if event_rows:
        latest = event_rows[-1]
        vector = latest.exposure_vector

        # Show event title
        evt_lbl = event_labels.get(selected_event)
        if evt_lbl:
            st.markdown(f"**{evt_lbl.title}**")

        if vector:
            conditions = list(vector.keys())
            values = [float(v) for v in vector.values()]

            # Use human-readable condition labels
            cond_display = []
            for c in conditions:
                clbl = condition_labels.get(c)
                if clbl:
                    q = clbl.question[:40] + "..." if len(clbl.question) > 40 else clbl.question
                    cond_display.append(q)
                else:
                    cond_display.append(c[:16] + "...")

            bar_colors = [COLORS["positive"] if v >= 0 else COLORS["negative"] for v in values]

            fig = go.Figure(
                go.Bar(
                    x=cond_display,
                    y=values,
                    marker_color=bar_colors,
                    text=[format_decimal(v) for v in values],
                    textposition="outside",
                    hovertemplate="%{x}<br>Net Exposure: %{y:.4f}<extra></extra>",
                )
            )
            fig.update_layout(
                **_merge_layout(
                    dict(
                        title=f"Exposure Vector — {evt_lbl.title[:40] if evt_lbl else selected_event}",
                        xaxis_title="Condition",
                        yaxis_title="Net Directional Exposure",
                        showlegend=False,
                    )
                )
            )
            st.plotly_chart(fig, use_container_width=True, config=CHART_CONFIG)

        c1, c2 = st.columns(2)
        c1.metric("Net After Exclusivity", format_decimal(latest.net_after_exclusivity))
        c2.metric("Date", latest.date)
