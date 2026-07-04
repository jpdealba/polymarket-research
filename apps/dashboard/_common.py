"""Shared bootstrap helpers for every dashboard page. Imports only
`pmresearch.api` and Streamlit — never any other `pmresearch` submodule (see
the module docstring on `pmresearch/api.py` and the import-boundary test)."""

from __future__ import annotations

from typing import Any

import streamlit as st
from plotly import graph_objects as go

from pmresearch import api

# Staleness cadence used only for the dashboard's own "is this wallet stale"
# display; presentation-layer choice, not a new metric.
DEFAULT_CADENCE_S = 3600

# ── Plotly theme ──────────────────────────────────────────────────────────────

COLORS = {
    "positive": "#00d4aa",
    "negative": "#ff6b6b",
    "neutral": "#6c7a89",
    "bond": "#ffd93d",
    "directional": "#00d4aa",
    "reward": "#a78bfa",
    "redemption": "#f97316",
    "fees": "#ef4444",
    "bond_merge": "#ffd93d",
    "bg": "#0e1117",
    "card": "#1a1f2e",
    "grid": "#2a3040",
    "text": "#fafafa",
}

PLOTLY_LAYOUT = dict(
    template="plotly_dark",
    paper_bgcolor=COLORS["bg"],
    plot_bgcolor=COLORS["bg"],
    font=dict(family="sans-serif", size=13, color=COLORS["text"]),
    margin=dict(l=40, r=20, t=50, b=40),
    xaxis=dict(gridcolor=COLORS["grid"], showgrid=True),
    yaxis=dict(gridcolor=COLORS["grid"], showgrid=True),
    hoverlabel=dict(bgcolor=COLORS["card"], font_size=13),
    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
)

CHART_CONFIG = dict(displayModeBar=True, responsive=True)


def _merge_layout(extra: dict | None = None) -> dict:
    merged = dict(PLOTLY_LAYOUT)
    if extra:
        merged.update(extra)
    return merged


# ── Helpers ───────────────────────────────────────────────────────────────────


@st.cache_resource
def get_settings():
    settings = api.get_settings()
    api.ensure_data_dirs(settings)
    return settings


def wallet_selector():
    settings = get_settings()
    with api.open_session(settings) as session:
        wallets = api.list_wallets(session, active_only=False)
    addresses = [w.address for w in wallets]
    if not addresses:
        st.warning("No wallets on the watchlist. Run `pmr wallet add <addr>` first.")
        st.stop()

    prev = st.session_state.get("selected_wallet")
    idx = addresses.index(prev) if prev in addresses else 0

    with st.sidebar:
        selected = st.selectbox(
            "Wallet",
            addresses,
            index=idx,
            format_func=lambda a: next(
                (w.display_name or a for w in wallets if w.address == a), a
            ),
        )
    st.session_state["selected_wallet"] = selected
    return selected


def metric_card(label: str, value: str, delta: str | None = None, fmt: str = "normal"):
    """Render a styled metric card."""
    st.metric(label=label, value=value, delta=delta)


def pnl_color(value: float) -> str:
    """Return green/red/gray based on PnL sign."""
    if value > 0:
        return COLORS["positive"]
    elif value < 0:
        return COLORS["negative"]
    return COLORS["neutral"]


def format_decimal(value: Any, decimals: int = 2) -> str:
    """Format a Decimal or numeric value with commas."""
    if value is None:
        return "N/A"
    try:
        return f"{float(value):,.{decimals}f}"
    except (TypeError, ValueError):
        return str(value)


def format_pct(value: Any, decimals: int = 1) -> str:
    """Format a Decimal as percentage."""
    if value is None:
        return "N/A"
    try:
        return f"{float(value) * 100:.{decimals}f}%"
    except (TypeError, ValueError):
        return str(value)


def format_ts(ts: int | None) -> str:
    """Format epoch timestamp to readable string."""
    if ts is None:
        return "never"
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def empty_state(message: str):
    """Show a styled empty state with instructions."""
    st.info(message)
    st.stop()


def section_header(title: str, description: str | None = None):
    """Render a section header with optional description."""
    st.subheader(title)
    if description:
        st.caption(description)
