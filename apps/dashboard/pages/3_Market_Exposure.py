"""Market exposure (Phase 16, view 4): directional+bond over time for a
selected condition. Imports only `pmresearch.api`."""

from __future__ import annotations

import streamlit as st
from plotly import graph_objects as go

from pmresearch import api

from _common import CHART_CONFIG, COLORS, _merge_layout, format_decimal, get_settings, wallet_selector

st.title("Market Exposure")

settings = get_settings()
wallet = wallet_selector()

with st.spinner("Loading exposures..."):
    with api.open_session(settings) as session:
        all_rows = api.fetch_exposures(session, wallet)

if not all_rows:
    st.info("No exposures computed yet for this wallet. Run `pmr derive run` first.")
else:
    condition_ids = sorted({r.condition_id for r in all_rows})

    # Resolve labels for all conditions
    with api.open_session(settings) as session:
        labels = api.resolve_market_labels(session, condition_ids)

    # Build display names: question (truncated) or raw ID
    def _display_name(cid: str) -> str:
        lbl = labels.get(cid)
        if lbl:
            q = lbl.question[:60] + "..." if len(lbl.question) > 60 else lbl.question
            return f"{q} [{cid[:8]}]"
        return cid[:16] + "..."

    condition_id = st.selectbox(
        "Condition",
        condition_ids,
        format_func=_display_name,
    )

    rows = [r for r in all_rows if r.condition_id == condition_id]

    if rows:
        dates = [r.date for r in rows]
        directionals = [float(r.directional) if r.directional is not None else 0.0 for r in rows]
        bonds = [float(r.bond) if r.bond is not None else 0.0 for r in rows]

        # Show market label
        lbl = labels.get(condition_id)
        if lbl:
            st.markdown(f"**{lbl.question}**")
            if lbl.category:
                st.caption(f"Category: {lbl.category} | Structure: {rows[0].structure_type}")

        # Summary metrics first
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Structure", rows[0].structure_type)
        c2.metric("Latest Directional", format_decimal(directionals[-1]))
        c3.metric("Latest Bond", format_decimal(bonds[-1]))
        c4.metric("Data Points", len(rows))

        # Table before chart (especially useful when only 1 data point)
        with st.expander("Exposure data table", expanded=len(rows) <= 3):
            st.dataframe(
                [
                    {
                        "date": r.date,
                        "directional": format_decimal(r.directional),
                        "bond": format_decimal(r.bond),
                        "structure_type": r.structure_type,
                        "event_id": r.event_id,
                    }
                    for r in rows
                ],
                use_container_width=True,
            )

        # Chart (skip if only 1 point — line chart is meaningless)
        if len(rows) > 1:
            fig = go.Figure()
            fig.add_trace(
                go.Scatter(
                    x=dates,
                    y=directionals,
                    mode="lines+markers",
                    name="Directional",
                    line=dict(color=COLORS["directional"], width=2),
                    fill="tozeroy",
                    fillcolor="rgba(0, 212, 170, 0.15)",
                    hovertemplate="Date: %{x}<br>Directional: %{y:.4f}<extra></extra>",
                )
            )
            fig.add_trace(
                go.Scatter(
                    x=dates,
                    y=bonds,
                    mode="lines+markers",
                    name="Bond",
                    line=dict(color=COLORS["bond"], width=2),
                    fill="tozeroy",
                    fillcolor="rgba(255, 217, 61, 0.15)",
                    hovertemplate="Date: %{x}<br>Bond: %{y:.4f}<extra></extra>",
                )
            )

            fig.update_layout(
                **_merge_layout(
                    dict(
                        title=f"Exposure — {lbl.question[:40] if lbl else condition_id[:16]}",
                        xaxis_title="Date",
                        yaxis_title="Shares",
                        showlegend=True,
                    )
                )
            )
            st.plotly_chart(fig, use_container_width=True, config=CHART_CONFIG)
        else:
            st.caption("Chart requires 2+ data points. Showing table only.")
