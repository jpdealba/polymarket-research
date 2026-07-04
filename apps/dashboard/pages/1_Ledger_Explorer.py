"""Ledger explorer (Phase 16, view 2): event-type counts + paginated raw
event listing. Imports only `pmresearch.api`."""

from __future__ import annotations

import streamlit as st
from plotly import graph_objects as go

from pmresearch import api

from _common import CHART_CONFIG, COLORS, _merge_layout, get_settings, wallet_selector

st.title("Ledger Explorer")

settings = get_settings()
wallet = wallet_selector()

with st.spinner("Loading ledger events..."):
    with api.open_session(settings) as session:
        counts = api.ledger_event_counts(session, wallet)

st.subheader("Event Type Summary")
if not counts:
    st.info("No ledger events computed yet for this wallet. Run `pmr ingest run` first.")
else:
    # Bar chart of event counts
    types = [r.event_type for r in counts]
    cnts = [r.cnt for r in counts]

    fig = go.Figure(
        go.Bar(
            x=types,
            y=cnts,
            marker_color=COLORS["directional"],
            text=cnts,
            textposition="outside",
            hovertemplate="%{x}: %{y}<extra></extra>",
        )
    )
    fig.update_layout(
        **_merge_layout(
            dict(
                title="Events by Type",
                xaxis_title="Event Type",
                yaxis_title="Count",
                showlegend=False,
            )
        )
    )
    st.plotly_chart(fig, use_container_width=True, config=CHART_CONFIG)

    # Data table
    with st.expander("Raw counts table"):
        st.dataframe([dict(r._mapping) for r in counts], use_container_width=True)

# ── Paginated events ─────────────────────────────────────────────────────────

event_types = [r.event_type for r in counts]
event_type = st.selectbox("Filter by event type", ["(all)"] + event_types)

if "ledger_explorer_offset" not in st.session_state:
    st.session_state.ledger_explorer_offset = 0

page_size = 50
offset = st.session_state.ledger_explorer_offset

col_prev, col_page, col_next = st.columns([1, 2, 1])
if col_prev.button("← Prev", disabled=offset == 0):
    st.session_state.ledger_explorer_offset = max(0, offset - page_size)
    st.rerun()
col_page.write(f"Page {offset // page_size + 1}")
if col_next.button("Next →"):
    st.session_state.ledger_explorer_offset = offset + page_size
    st.rerun()

selected_type = None if event_type == "(all)" else event_type

with api.open_session(settings) as session:
    events = api.list_wallet_events(
        session,
        wallet,
        limit=page_size,
        offset=st.session_state.ledger_explorer_offset,
        event_type=selected_type,
    )

st.subheader(f"Events (offset {offset})")
if not events:
    st.info("No events at this page/filter.")
else:
    st.dataframe([dict(r._mapping) for r in events], use_container_width=True)
