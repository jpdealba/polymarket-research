"""Episode explorer (Phase 16, view 3): episodes table + stats + on-demand
fine-grained replay for a selected episode. Imports only `pmresearch.api`."""

from __future__ import annotations

import json

import streamlit as st

from pmresearch import api

from _common import get_settings, wallet_selector

st.title("Episode Explorer")

settings = get_settings()
wallet = wallet_selector()

token_id_filter = st.text_input("Filter by token_id (optional)")
open_only = st.checkbox("Open episodes only")

with api.open_session(settings) as session:
    episodes = api.fetch_episodes(
        session, wallet, token_id=token_id_filter or None, open_only=open_only
    )
    stats = api.episode_stats(session, wallet)

st.subheader("Episode stats")
st.json(
    {
        "count": stats.count,
        "open_count": stats.open_count,
        "flat_closed_count": stats.flat_closed_count,
        "resolution_closed_count": stats.resolution_closed_count,
        "duration_min": stats.duration_min,
        "duration_p50": stats.duration_p50,
        "duration_p90": stats.duration_p90,
        "duration_max": stats.duration_max,
        "micro_episode_count": stats.micro_episode_count,
        "micro_episode_share": str(stats.micro_episode_share),
        "realized_pnl": str(stats.realized_pnl),
        "reward_income": str(stats.reward_income),
    }
)

st.subheader("Episodes")
if not episodes:
    st.info("No episodes computed yet for this wallet. Run `pmr replay episodes` first.")
else:
    st.dataframe([dict(r._mapping) for r in episodes], use_container_width=True)

    episode_ids = [r.id for r in episodes]
    selected_id = st.selectbox("Replay episode id", episode_ids)
    selected = next(r for r in episodes if r.id == selected_id)
    event_ids = json.loads(selected.events_consumed or "[]")

    with api.open_session(settings) as session:
        events = api.fetch_events_by_ids(session, event_ids)

    st.subheader(f"Events consumed by episode {selected_id}")
    if not events:
        st.info("No events found for this episode.")
    else:
        st.dataframe([dict(r._mapping) for r in events], use_container_width=True)
