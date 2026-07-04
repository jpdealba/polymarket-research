"""Episode explorer (Phase 16, view 3): episodes table + stats + on-demand
fine-grained replay for a selected episode. Imports only `pmresearch.api`."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import streamlit as st
from plotly import graph_objects as go

from pmresearch import api

from _common import (
    CHART_CONFIG,
    COLORS,
    _merge_layout,
    format_decimal,
    format_ts,
    get_settings,
    wallet_selector,
)

st.title("Episode Explorer")

settings = get_settings()
wallet = wallet_selector()

token_id_filter = st.text_input("Filter by token_id (optional)")
open_only = st.checkbox("Open episodes only")

with st.spinner("Loading episodes..."):
    with api.open_session(settings) as session:
        episodes = api.fetch_episodes(
            session, wallet, token_id=token_id_filter or None, open_only=open_only
        )
        stats = api.episode_stats(session, wallet)

# ── Episode stats ────────────────────────────────────────────────────────────

st.subheader("Episode Stats")
if stats is None:
    st.info("No episodes computed yet. Run `pmr replay episodes` first.")
else:
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Episodes", stats.count)
    c2.metric("Open", stats.open_count)
    c3.metric("Closed (Flat)", stats.flat_closed_count)
    c4.metric("Closed (Resolution)", stats.resolution_closed_count)

    c5, c6, c7, c8 = st.columns(4)
    dur_p50 = f"{stats.duration_p50 / 3600:.1f}h" if stats.duration_p50 else "N/A"
    dur_p90 = f"{stats.duration_p90 / 3600:.1f}h" if stats.duration_p90 else "N/A"
    c5.metric("Median Duration", dur_p50)
    c6.metric("P90 Duration", dur_p90)
    c7.metric("Micro Episodes", stats.micro_episode_count)
    c8.metric("Micro Episode Share", f"{float(stats.micro_episode_share) * 100:.1f}%")


# ── Duration histogram ──────────────────────────────────────────────────────

if episodes and stats and stats.duration_p50 is not None:
    st.subheader("Episode Duration Distribution")
    durations_h = []
    for ep in episodes:
        if ep.close_ts and ep.open_ts:
            dur = (ep.close_ts - ep.open_ts) / 3600
            durations_h.append(dur)

    if durations_h:
        fig = go.Figure(
            go.Histogram(
                x=durations_h,
                nbinsx=30,
                marker_color=COLORS["directional"],
                hovertemplate="Duration: %{x:.1f}h<br>Count: %{y}<extra></extra>",
            )
        )
        fig.update_layout(
            **_merge_layout(
                dict(
                    title="Episode Duration Distribution",
                    xaxis_title="Duration (hours)",
                    yaxis_title="Count",
                    showlegend=False,
                )
            )
        )
        st.plotly_chart(fig, use_container_width=True, config=CHART_CONFIG)

# ── Episodes table ───────────────────────────────────────────────────────────

st.subheader("Episodes")
if not episodes:
    st.info("No episodes computed yet for this wallet. Run `pmr replay episodes` first.")
else:
    st.dataframe([dict(r._mapping) for r in episodes], use_container_width=True)

    # Drill-down
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
