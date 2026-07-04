"""World Cup forward microstructure watch. Imports only `pmresearch.api`."""

from __future__ import annotations

import json
import time

import streamlit as st
from plotly import graph_objects as go

from pmresearch import api

from _common import CHART_CONFIG, _merge_layout, format_pct, format_ts, get_settings

st.title("World Cup Microstructure Watch")

settings = get_settings()

with api.open_session(settings) as session:
    wallet_rows = api.list_wallets(session, active_only=False)
    tracked_wallets = api.worldcup_tracked_wallets(session, settings)
    status = api.worldcup_collector_status(session, settings)
    tokens = api.worldcup_watchlist_tokens(
        session, name=settings.worldcup_watchlist_name, active_only=True
    )
    maker_fills = []
    taker_fills = []

if not status.tables_exist:
    st.info("World Cup Watch tables are not available yet. Run `pmr db upgrade`.")
    st.stop()

st.header("Tracked Wallets")
with st.form("worldcup_wallet_control"):
    new_wallet = st.text_input("Add wallet")
    wallet_labels = {
        row.address: f"{row.display_name or row.address} ({row.address})"
        for row in wallet_rows
    }
    selected_wallets = st.multiselect(
        "Track up to 2 wallets",
        options=[row.address for row in wallet_rows],
        default=[w for w in tracked_wallets if w in wallet_labels],
        format_func=lambda value: wallet_labels.get(value, value),
    )
    submitted = st.form_submit_button("Save")

if submitted:
    if new_wallet.strip():
        with api.open_session(settings) as session:
            api.add_wallet(session, new_wallet.strip())
        st.success(f"Added {new_wallet.strip().lower()}")
        st.rerun()
    if len(selected_wallets) > 2:
        st.error("Select at most 2 wallets.")
    else:
        with api.open_session(settings) as session:
            saved = api.set_worldcup_tracked_wallets(session, selected_wallets)
        st.success(f"Tracking {len(saved)} wallet(s).")
        st.rerun()

if len(tracked_wallets) == 0:
    st.warning("No World Cup tracked wallets selected. The collector will wait until you save up to 2 wallets here.")
else:
    st.caption("Collector tracking: " + ", ".join(tracked_wallets))

wallet_view_options = tracked_wallets or [row.address for row in wallet_rows]
if wallet_view_options:
    with api.open_session(settings) as session:
        for w in wallet_view_options:
            for role, bucket in (("maker", maker_fills), ("taker", taker_fills)):
                wallet_fills = api.worldcup_recent_maker_fills(
                    session,
                    wallet=w,
                    watchlist=settings.worldcup_watchlist_name,
                    limit=100,
                    role=role,
                )
                for row in wallet_fills:
                    bucket.append((w, row))
    maker_fills.sort(key=lambda pair: (pair[1].trade_ts, pair[1].event_id), reverse=True)
    taker_fills.sort(key=lambda pair: (pair[1].trade_ts, pair[1].event_id), reverse=True)

st.header("Collector Status")
c1, c2, c3, c4 = st.columns(4)
c1.metric("Enabled", "Yes" if status.enabled else "No")
c2.metric("Watchlist", status.watchlist_name)
c3.metric("Tracked Wallets", len(status.tracked_wallets))
c4.metric("Book Interval", f"{status.book_interval_s}s")

c5, c6, c7, c8 = st.columns(4)
c5.metric("Last Sample", format_ts(status.last_sample_run_ts))
c6.metric("Latest Book", format_ts(status.latest_book_ts))
c7.metric("Latest Wallet Event", format_ts(status.latest_wallet_event_ts))
c8.metric("Last Context Build", format_ts(status.latest_context_ts))

if status.latest_book_age_s is None:
    st.warning("No World Cup book snapshot has been collected yet.")
elif status.latest_book_age_s > 300:
    st.error("Latest World Cup book snapshot is older than 5 minutes.")
elif status.latest_book_age_s > max(120, 2 * status.book_interval_s):
    st.warning("Latest World Cup book snapshot is stale for the configured interval.")

b1, b2 = st.columns(2)
if b1.button("Refresh markets + watchlist now"):
    with st.spinner("Syncing market metadata from Gamma..."):
        api.run_markets_refresh_cycle(settings, missing_only=False)
    rebuild_summary = []
    with api.open_session(settings) as session:
        for w in wallet_view_options:
            stats = api.build_world_cup_watchlist(
                session,
                w,
                name=settings.worldcup_watchlist_name,
                dust_epsilon=str(settings.dust_epsilon),
            )
            rebuild_summary.append(f"{w}: +{stats.tokens_upserted} ({stats.active_tokens} active)")
    st.success("Watchlist refreshed. " + "; ".join(rebuild_summary))
    st.rerun()

if b2.button("Fetch recent wallet activity now"):
    with st.spinner("Syncing wallet activity, ingest, and holdings..."):
        api.run_worldcup_sync_cycle(settings)
    st.success("Wallet activity refreshed.")
    st.rerun()

st.header("Watchlist")
source_filter = st.multiselect(
    "Source",
    sorted({row.source for row in tokens}),
    default=sorted({row.source for row in tokens}),
)
priority_max = st.slider("Priority max", 1, 100, 100)
keyword = st.text_input("Team / keyword", "")
watch_rows = []
for row in tokens:
    haystack = f"{row.question or ''} {row.outcome_label or ''}".lower()
    if row.source not in source_filter:
        continue
    if row.priority > priority_max:
        continue
    if keyword and keyword.lower() not in haystack:
        continue
    age = None if row.latest_book_ts is None else max(0, int(time.time()) - int(row.latest_book_ts))
    watch_rows.append(
        {
            "priority": row.priority,
            "token_id": row.token_id,
            "question": row.question,
            "outcome": row.outcome_label,
            "source": row.source,
            "reason": row.reason,
            "last_seen_ts": row.last_seen_ts,
            "best_bid": row.latest_best_bid,
            "best_ask": row.latest_best_ask,
            "spread": row.latest_spread,
            "mid": row.latest_mid,
            "book_age_s": age,
        }
    )
st.dataframe(watch_rows, use_container_width=True)

st.header("Live Books")
token_options = [row.token_id for row in tokens]
token_labels = {
    row.token_id: f"{row.question or row.token_id} - {row.outcome_label or ''}".strip(" -")
    for row in tokens
}
selected_token = (
    st.selectbox("Token", token_options, format_func=lambda t: token_labels.get(t, t))
    if token_options
    else None
)
if selected_token:
    with api.open_session(settings) as session:
        history = api.worldcup_book_history(session, token_id=selected_token, limit=200)
    latest = history[0] if history else None
    if latest is None:
        st.info("No snapshots for this token yet.")
    else:
        age = max(0, int(time.time()) - int(latest.ts))
        badge = "fresh" if age <= 15 else "ok" if age <= 60 else "stale"
        b1, b2, b3, b4, b5 = st.columns(5)
        b1.metric("Best Bid", latest.best_bid or "N/A")
        b2.metric("Best Ask", latest.best_ask or "N/A")
        b3.metric("Spread", latest.spread or "N/A")
        b4.metric("Mid", latest.mid or "N/A")
        b5.metric("Age", f"{age}s {badge}")

        ordered = list(reversed(history))
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=[r.ts for r in ordered], y=[float(r.mid) if r.mid else None for r in ordered], name="mid"))
        fig.add_trace(go.Scatter(x=[r.ts for r in ordered], y=[float(r.spread) if r.spread else None for r in ordered], name="spread"))
        fig.update_layout(**_merge_layout(dict(title="Snapshot History", xaxis_title="ts", yaxis_title="price")))
        st.plotly_chart(fig, use_container_width=True, config=CHART_CONFIG)

        if latest.depth_top_json:
            depth = json.loads(latest.depth_top_json)
            d1, d2 = st.columns(2)
            d1.dataframe(depth.get("bids", []), use_container_width=True)
            d2.dataframe(depth.get("asks", []), use_container_width=True)

def _fill_rows(pairs, wallet):
    return [
        {
            "trade_utc": row.trade_utc,
            "token_id": row.token_id,
            "question": row.question,
            "outcome": row.outcome_label,
            "role": row.role,
            "side": row.side,
            "fill_price": row.fill_price,
            "fill_size": row.fill_size,
            "bid_before": row.best_bid_before,
            "ask_before": row.best_ask_before,
            "spread_before": row.spread_before,
            "mid_before": row.mid_before,
            "book_before_age_s": row.book_before_age_s,
            "bid_after": row.best_bid_after,
            "ask_after": row.best_ask_after,
            "book_after_age_s": row.book_after_age_s,
            "context_status": row.context_status,
            "null_reason": row.null_reason,
        }
        for w, row in pairs
        if w == wallet
    ]

st.header("Per-Wallet Fills and Coverage")
wallet_columns = st.columns(len(wallet_view_options)) if wallet_view_options else []
for col, w in zip(wallet_columns, wallet_view_options):
    with col:
        st.subheader(wallet_labels.get(w, w))
        with api.open_session(settings) as session:
            w_coverage = api.worldcup_context_coverage(session, wallet=w, role="maker")
        q1, q2 = st.columns(2)
        q1.metric("Maker Fills", w_coverage.total)
        q2.metric("Stale/Missing", w_coverage.stale + w_coverage.missing)
        st.caption(
            f"Strict (excellent+good): {w_coverage.strict_count}/{w_coverage.total} = "
            f"{format_pct(w_coverage.strict_share)}. "
            f"Loose (+usable): {w_coverage.loose_count}/{w_coverage.total} = "
            f"{format_pct(w_coverage.loose_share)}."
        )
        if w_coverage.total == 0:
            st.info("No maker-fill context yet.")
        elif w_coverage.strict_share < 0.5:
            st.warning("Strict maker-context coverage is low; avoid strategy conclusions from this sample.")

        st.markdown("**Maker Fills**")
        st.dataframe(_fill_rows(maker_fills, w), use_container_width=True)
        st.markdown("**Taker Fills**")
        st.dataframe(_fill_rows(taker_fills, w), use_container_width=True)
