"""Ledger explorer (Phase 16, view 2): event-type counts + paginated raw
event listing. Imports only `pmresearch.api`."""

from __future__ import annotations

import streamlit as st

from pmresearch import api

from _common import get_settings, wallet_selector

st.title("Ledger Explorer")

settings = get_settings()
wallet = wallet_selector()

with api.open_session(settings) as session:
    counts = api.ledger_event_counts(session, wallet)

st.subheader("Event type summary")
if not counts:
    st.info("No ledger events computed yet for this wallet. Run `pmr ingest run` first.")
else:
    st.dataframe([dict(r._mapping) for r in counts], use_container_width=True)

event_types = [r.event_type for r in counts]
event_type = st.selectbox("Filter by event type", ["(all)"] + event_types)
selected_type = None if event_type == "(all)" else event_type

if "ledger_explorer_offset" not in st.session_state:
    st.session_state.ledger_explorer_offset = 0

page_size = 50
offset = st.session_state.ledger_explorer_offset

col_prev, col_next = st.columns(2)
if col_prev.button("Prev page", disabled=offset == 0):
    st.session_state.ledger_explorer_offset = max(0, offset - page_size)
    st.rerun()
if col_next.button("Next page"):
    st.session_state.ledger_explorer_offset = offset + page_size
    st.rerun()

with api.open_session(settings) as session:
    events = api.list_wallet_events(
        session, wallet, limit=page_size, offset=st.session_state.ledger_explorer_offset,
        event_type=selected_type,
    )

st.subheader(f"Events (offset {st.session_state.ledger_explorer_offset})")
if not events:
    st.info("No events at this page/filter.")
else:
    st.dataframe([dict(r._mapping) for r in events], use_container_width=True)
