#!/usr/bin/env python3
"""
Tiger daily execution dashboard: one glance at ~3:45 PM New York,
place the 0-2 orders it lists in the Tiger app, done.

This public version contains NO personal data. Real positions, account
value, and option values are read from Streamlit Secrets (app settings
-> Secrets), which stay private:

    nav = 28794.0

    [positions]
    HIVE = 3800
    BITX = 635
    SOXS = -390
    # symbol = quantity; negative = short

    [option_values]
    crypto = 4458.0
    other = 279.0

Signals (all end-of-day, no intraday noise):
  - HIVE sleeve gate:    BTC vs 20-day MA (QQQ 50-day MA as quality filter)
  - Semi sleeve gate:    SMH vs 200-day MA
  - Short-inverse bands: band logic on short SOXS / SQQQ (x0.65 / x1.35)
  - VIX >= 40:           cover shorts, de-risk

Run as a dashboard:   streamlit run tiger_daily.py
Run as a plain check: python tiger_daily.py
"""

from __future__ import annotations

import math
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# symbol -> theme bucket. Add rows for new tickers; quantities come from
# Streamlit Secrets (or the sidebar editor).
THEME_MAP: dict[str, str] = {
    "HIVE": "crypto",
    "BITX": "crypto",
    "ABTC": "crypto",
    "CORZ": "crypto",
    "SOXL": "semi",
    "SOXS": "semi_short",
    "SQQQ": "index_short",
    "SPXU": "index_short",
    "SPYM": "core",
    "QQQ": "core",
    "TIGR": "other",
    "UVIX": "vol_short",
    "UVXY": "vol_short",
}

DEFAULT_NAV = 10_000.0

# Target structure and hard caps (fractions of NAV)
TARGETS = {
    "core": 0.45,
    "crypto_cap": 0.30,
    "semi_short_target": 0.075,   # short SOXS sleeve
    "index_short_target": 0.075,  # short SQQQ sleeve
    "vol_short_cap": 0.02,
    "cash_floor": 0.15,
}

# Band rebalancing (DRIFTNET rules): act only outside target * [low, high].
BAND_LOW_MULT = 0.65
BAND_HIGH_MULT = 1.35

VIX_DANGER = 40.0
SHORT_VOL_KILL = 1.75    # cover a short sleeve when its 20-day realized vol >= 175%
SMH_EXT_TRIG = 0.35      # melt-up hedge advisory when SMH >= 35% above its 200-day MA
MELTUP_HEDGE_BUDGET = 0.02
SIGNAL_SYMBOLS = ["BTC-USD", "QQQ", "SMH", "HIVE", "SOXS", "SQQQ", "^VIX"]


def rebalance_decision(target: float, current_frac: float) -> bool:
    """True when a sleeve should be reset to target (band breach)."""
    if target == 0.0:
        return current_frac > 0.0
    return not target * BAND_LOW_MULT <= current_frac <= target * BAND_HIGH_MULT


# ---------------------------------------------------------------------------
# Data + signals (pure pandas/yfinance; no streamlit here)
# ---------------------------------------------------------------------------

def fetch_closes(symbols: list[str], period: str = "2y") -> pd.DataFrame:
    frames = {}
    for sym in symbols:
        h = yf.Ticker(sym).history(period=period, auto_adjust=True)["Close"]
        h.index = pd.to_datetime(h.index).tz_localize(None).normalize()
        frames[sym] = h
    df = pd.DataFrame(frames).sort_index()
    # Anchor to US equity trading days: BTC trades weekends, and its rows
    # would otherwise inject ffilled equity prices that shorten every
    # moving-average lookback and dilute realized-vol calculations.
    anchor = df["QQQ"].notna() if "QQQ" in df.columns else df.notna().any(axis=1)
    return df.ffill()[anchor]


def compute_signals(px: pd.DataFrame) -> dict:
    last = px.iloc[-1]
    btc_ma20 = px["BTC-USD"].rolling(20).mean().iloc[-1]
    qqq_ma50 = px["QQQ"].rolling(50).mean().iloc[-1]
    qqq_ma200 = px["QQQ"].rolling(200).mean().iloc[-1]
    smh_ma200 = px["SMH"].rolling(200).mean().iloc[-1]
    vix = float(last["^VIX"])

    btc_on = float(last["BTC-USD"]) >= btc_ma20
    qqq_on = float(last["QQQ"]) >= qqq_ma50
    qqq200_on = float(last["QQQ"]) >= qqq_ma200
    smh_on = float(last["SMH"]) >= smh_ma200
    smh_ext = float(last["SMH"]) / float(smh_ma200) - 1.0
    danger = vix >= VIX_DANGER
    short_vol = {
        sym: float(px[sym].pct_change().rolling(20).std().iloc[-1]) * math.sqrt(252)
        for sym in ("SOXS", "SQQQ")
    }

    if danger:
        hive_target = 0.0
        hive_note = f"VIX {vix:.1f} >= {VIX_DANGER}: exit HIVE sleeve, cover shorts."
    elif not btc_on:
        hive_target = 0.0
        hive_note = "BTC below its 20-day MA: HIVE sleeve target 0%."
    elif not qqq_on:
        hive_target = 0.10
        hive_note = "BTC trend on but QQQ below 50-day MA: half sleeve (10%)."
    else:
        hive_target = 0.20
        hive_note = "BTC and QQQ trends on: full HIVE sleeve (20%)."

    return {
        "asof": str(px.index[-1].date()),
        "vix": vix,
        "danger": danger,
        "btc_on": btc_on,
        "qqq_on": qqq_on,
        "qqq200_on": qqq200_on,
        "smh_on": smh_on,
        "btc": float(last["BTC-USD"]), "btc_ma20": float(btc_ma20),
        "qqq": float(last["QQQ"]), "qqq_ma50": float(qqq_ma50),
        "qqq_ma200": float(qqq_ma200),
        "smh": float(last["SMH"]), "smh_ma200": float(smh_ma200),
        "hive_target": hive_target,
        "hive_note": hive_note,
        "short_vol": short_vol,
        "smh_ext": smh_ext,
        "prices": {s: float(last[s]) for s in px.columns},
    }


def build_orders(positions: list[dict], option_values: dict, nav: float,
                 sig: dict, prices: dict) -> tuple[list[str], pd.DataFrame]:
    """Return (orders, theme exposure table)."""
    rows = []
    for p in positions:
        price = prices.get(p["symbol"])
        value = p["qty"] * price if price and p["qty"] else 0.0
        rows.append({**p, "price": price, "value": value})
    df = pd.DataFrame(rows) if rows else pd.DataFrame(columns=["symbol", "qty", "theme", "price", "value"])

    theme_val = df.groupby("theme")["value"].sum().to_dict() if len(df) else {}
    for theme, ov in option_values.items():
        theme_val[theme] = theme_val.get(theme, 0.0) + float(ov)

    orders: list[str] = []

    # 0) Danger overrides everything.
    if sig["danger"]:
        orders.append(f"VIX {sig['vix']:.1f} DANGER: cover ALL shorts, exit HIVE sleeve, hold core + cash.")

    # 1) Crypto sleeve vs regime target and hard cap.
    crypto_val = theme_val.get("crypto", 0.0)
    crypto_frac = crypto_val / nav if nav else 0.0
    if crypto_frac > TARGETS["crypto_cap"] + 0.02:
        excess = crypto_val - TARGETS["crypto_cap"] * nav
        bitx_px = prices.get("BITX")
        have = int(df.loc[df["symbol"] == "BITX", "qty"].sum()) if len(df) else 0
        if bitx_px and have > 0:
            if sig["btc_on"]:
                contracts = max(have // 100, 1)
                orders.append(
                    f"BITX PAID EXIT (crypto {crypto_frac:.0%} > {TARGETS['crypto_cap']:.0%} cap; "
                    f"BTC trend ON, so exit patiently): keep {contracts} covered calls sold, "
                    f"ATM to 5% OTM, 30-45 DTE — take assignment, never roll up. "
                    f"HARD FLOOR: the day BTC closes below its 20-day MA, sell all {have} BITX at market.")
            else:
                qty = min(math.ceil(excess / bitx_px), have)
                orders.append(
                    f"SELL {qty} BITX (~${qty * bitx_px:,.0f}): crypto {crypto_frac:.0%} exceeds the "
                    f"{TARGETS['crypto_cap']:.0%} cap and BTC trend is OFF — the patient-exit "
                    f"condition failed. Exit at market; buy back covered calls with the sale.")
    if not sig["danger"] and sig["hive_target"] == 0.0 and crypto_frac > 0.05:
        hive_px = prices.get("HIVE")
        have = int(df.loc[df["symbol"] == "HIVE", "qty"].sum()) if len(df) else 0
        if hive_px and have > 0:
            orders.append(f"SELL {have} HIVE (~${have * hive_px:,.0f}): {sig['hive_note']}")

    # 2) Semi long sleeve (SOXL) on the SMH 200-day gate.
    soxl_qty = int(df.loc[df["symbol"] == "SOXL", "qty"].sum()) if len(df) else 0
    if not sig["smh_on"] and soxl_qty > 0:
        orders.append(f"SELL {soxl_qty} SOXL: SMH closed below its 200-day MA — semi trend OFF.")

    # 2b) Core sleeve (DRIFTNET v2): hold QQQ/SPYM only while QQQ >= its 200-day MA.
    core_val = theme_val.get("core", 0.0)
    if not sig["qqq200_on"] and core_val > 0.02 * nav:
        core_rows = df[(df["theme"] == "core") & (df["qty"] > 0)] if len(df) else df
        for _, r in core_rows.iterrows():
            orders.append(
                f"SELL {int(r['qty'])} {r['symbol']} (~${r['value']:,.0f}): QQQ closed below "
                f"its 200-day MA — core trend OFF, park in cash (DRIFTNET v2 rule).")
    elif sig["qqq200_on"] and core_val < (TARGETS["core"] - 0.10) * nav:
        orders.append(
            f"NOTE: core is {core_val / nav:.0%} of NAV vs {TARGETS['core']:.0%} target and the "
            f"QQQ 200-day trend is ON — add to QQQ/SPYM gradually with spare cash "
            f"(respect the {TARGETS['cash_floor']:.0%} cash floor).")

    # 3) Short-inverse sleeves with band logic.
    for sym, tgt_key in (("SOXS", "semi_short_target"), ("SQQQ", "index_short_target")):
        px_ = prices.get(sym)
        if not px_:
            continue
        qty = int(df.loc[df["symbol"] == sym, "qty"].sum()) if len(df) else 0
        frac = abs(min(qty, 0)) * px_ / nav if nav else 0.0
        vol_kill = sig["short_vol"].get(sym, 0.0) >= SHORT_VOL_KILL
        target = 0.0 if (sig["danger"] or vol_kill) else TARGETS[tgt_key]
        if rebalance_decision(target, frac):
            tgt_qty = -math.floor(target * nav / px_)
            delta = tgt_qty - qty
            side = "SELL SHORT" if delta < 0 else "BUY TO COVER"
            if vol_kill and not sig["danger"]:
                why = (f"VOL KILL-SWITCH: {sym} 20-day vol "
                       f"{sig['short_vol'][sym]:.0%} >= {SHORT_VOL_KILL:.0%}")
            else:
                why = (f"short is {frac:.1%} of NAV vs target {target:.1%} "
                       f"(band {target * BAND_LOW_MULT:.1%}-{target * BAND_HIGH_MULT:.1%})")
            orders.append(f"{side} {abs(delta)} {sym}: {why}.")

    # 4) Short-vol sleeve: cap, recommend defined-risk instead.
    vol_val = abs(theme_val.get("vol_short", 0.0))
    if vol_val > TARGETS["vol_short_cap"] * nav:
        orders.append(
            f"COVER short-vol positions (UVIX/UVXY, ~${vol_val:,.0f}): above the "
            f"{TARGETS['vol_short_cap']:.0%} cap. Unbounded tail risk; use puts on them instead.")

    # 4b) Melt-up companion (advisory): small SOXS call overlay while semis are
    # extremely extended. Monetizes within-melt-up corrections, not crashes —
    # 20y backtest: +1.2 CAGR pts vs none, all of it in extension regimes.
    if not sig["danger"] and sig["smh_ext"] >= SMH_EXT_TRIG:
        budget = MELTUP_HEDGE_BUDGET * nav
        orders.append(
            f"ADVISORY — MELT-UP HEDGE: SMH is {sig['smh_ext']:.0%} above its 200-day MA "
            f"(trigger {SMH_EXT_TRIG:.0%}). Hold ~{MELTUP_HEDGE_BUDGET:.0%} of NAV "
            f"(${budget:,.0f}) in SOXS calls, 30-45 DTE, 20-30% OTM. Lottery-ticket "
            f"sizing — expect full loss; it pays during violent corrections inside the "
            f"melt-up. Drop it when extension falls below {SMH_EXT_TRIG:.0%}.")

    # 5) Cash floor check (informational).
    invested = sum(v for v in theme_val.values() if v > 0)
    est_cash_frac = max(0.0, 1.0 - invested / nav) if nav else 0.0
    if est_cash_frac < TARGETS["cash_floor"]:
        orders.append(
            f"NOTE: estimated cash {est_cash_frac:.0%} of NAV is below the "
            f"{TARGETS['cash_floor']:.0%} floor — route sale proceeds to cash, not new positions.")

    exposure = pd.DataFrame(
        [{"theme": k, "value": v, "pct_of_nav": v / nav if nav else 0.0}
         for k, v in sorted(theme_val.items(), key=lambda kv: -abs(kv[1]))])
    return orders, exposure


def positions_from_secrets(secrets) -> tuple[list[dict], dict, float]:
    """Read positions/nav/option values from Streamlit secrets; empty-safe."""
    nav = float(secrets.get("nav", DEFAULT_NAV))
    raw = secrets.get("positions", {})
    positions = [
        {"symbol": sym, "qty": int(qty), "theme": THEME_MAP.get(sym, "other")}
        for sym, qty in dict(raw).items()
    ]
    if not positions:
        positions = [{"symbol": s, "qty": 0, "theme": t} for s, t in THEME_MAP.items()]
    option_values = {str(k): float(v) for k, v in dict(secrets.get("option_values", {})).items()}
    return positions, option_values, nav


# ---------------------------------------------------------------------------
# Streamlit UI
# ---------------------------------------------------------------------------

def render() -> None:
    import streamlit as st

    st.set_page_config(page_title="Tiger Daily", page_icon="🐯", layout="wide")
    st.title("Tiger Daily — one look, few orders")
    st.caption("Signals are end-of-day. Check once near 3:45 PM New York; if nothing is listed, do nothing.")

    try:
        secret_positions, secret_options, secret_nav = positions_from_secrets(st.secrets)
    except Exception:
        secret_positions, secret_options, secret_nav = positions_from_secrets({})

    with st.sidebar:
        st.header("Account")
        nav = st.number_input("Total account value (USD)", value=float(secret_nav), step=100.0)
        st.header("Positions (qty < 0 = short)")
        st.caption("Loaded from app Secrets; edit here for what-if checks.")
        pos_df = st.data_editor(pd.DataFrame(secret_positions), num_rows="dynamic",
                                width="stretch")
        st.header("Option value by theme (USD)")
        opt_crypto = st.number_input("crypto options net value",
                                     value=float(secret_options.get("crypto", 0.0)))
        opt_other = st.number_input("other options net value",
                                    value=float(secret_options.get("other", 0.0)))

    positions = pos_df.to_dict("records")
    symbols = tuple(sorted(set(SIGNAL_SYMBOLS) | {p["symbol"] for p in positions if p.get("symbol")}))

    @st.cache_data(ttl=900)
    def _data(syms: tuple) -> pd.DataFrame:
        return fetch_closes(list(syms))

    px = _data(symbols)
    sig = compute_signals(px)
    orders, exposure = build_orders(positions, {"crypto": opt_crypto, "other": opt_other},
                                    nav, sig, sig["prices"])

    now_ny = datetime.now(ZoneInfo("America/New_York"))
    st.write(f"Data as of close **{sig['asof']}** — New York time now: {now_ny:%Y-%m-%d %H:%M}")

    def _pct_vs_ma(value: float, ma: float) -> float:
        return (value / ma - 1.0) * 100 if ma else 0.0

    gates = [
        ("BTC vs 20-day MA", sig["btc_on"], _pct_vs_ma(sig["btc"], sig["btc_ma20"])),
        ("QQQ vs 50-day MA", sig["qqq_on"], _pct_vs_ma(sig["qqq"], sig["qqq_ma50"])),
        ("QQQ vs 200-day MA (core)", sig["qqq200_on"], _pct_vs_ma(sig["qqq"], sig["qqq_ma200"])),
        ("SMH vs 200-day MA", sig["smh_on"], _pct_vs_ma(sig["smh"], sig["smh_ma200"])),
    ]
    gate_cols = st.columns(2)
    for i, (label, on, pct) in enumerate(gates):
        box = gate_cols[i % 2].success if on else gate_cols[i % 2].error
        box(f"**{label}**  \n{'ON' if on else 'OFF'} ({pct:+.1f}% vs MA)")

    if sig["danger"]:
        st.error(f"**VIX** {sig['vix']:.1f} — DANGER (>= {VIX_DANGER:.0f})")
    else:
        st.info(f"**VIX** {sig['vix']:.1f} — calm ({VIX_DANGER - sig['vix']:.1f} pts of headroom before danger)")

    st.subheader("Today's orders")
    if orders:
        for i, order in enumerate(orders, 1):
            st.warning(f"{i}. {order}")
    else:
        st.success("No orders today. Close the app. 🎣")

    st.subheader("Theme exposure vs caps")
    if len(exposure):
        exposure["pct_of_nav"] = (exposure["pct_of_nav"] * 100).round(1)
        st.dataframe(exposure, width="stretch")
    st.caption(
        f"Caps: crypto <= {TARGETS['crypto_cap']:.0%} | short-vol <= {TARGETS['vol_short_cap']:.0%} "
        f"| cash floor {TARGETS['cash_floor']:.0%} | HIVE sleeve regime target: {sig['hive_note']} | "
        f"Short-sleeve 20d vol (kill >= {SHORT_VOL_KILL:.0%}): "
        f"SOXS {sig['short_vol']['SOXS']:.0%}, SQQQ {sig['short_vol']['SQQQ']:.0%} | "
        f"SMH extension {sig['smh_ext']:.0%} (melt-up hedge >= {SMH_EXT_TRIG:.0%})")

    # --- insider context (display only — weeks-horizon signal, never an
    # order trigger; kept visually apart from Today's orders) -------------
    st.subheader("Insider buys — S&P 500, last 10 days")

    @st.cache_data(ttl=900)
    def _insider() -> dict:
        from insider_feed import load_insider_feed

        return load_insider_feed()

    feed = _insider()
    if feed["error"] and not feed["records"]:
        st.warning(f"Insider feed unavailable: {feed['error']}")
    else:
        freshness = f"data through **{feed['latest_date'] or 'unknown'}** · source: {feed['source']}"
        if feed["stale"]:
            st.error(f"STALE insider data — {freshness}")
        else:
            st.caption(freshness)
        if not feed["records"]:
            st.info("No qualifying insider buys in the last 10 days.")
        for r in feed["records"]:
            tag = (f"CLUSTER ×{r['cluster_size']}" if r.get("cluster_id")
                   else ("CEO/CFO" if r.get("is_ceo_cfo") else "Officer"))
            semi = " 🔶 **SEMI — you are short this theme**" if r.get("is_semi") else ""
            line = (f"**[{tag}] {r['ticker']}** — {r['insider_name']} ({r['insider_role']}) "
                    f"bought ${r['dollar_value']:,.0f} · filed {str(r['filed_at'])[:10]} · "
                    f"[filing]({r['filing_url']}){semi}")
            (st.warning if r.get("is_semi") else st.markdown)(line)


def cli() -> None:
    positions = [{"symbol": s, "qty": 0, "theme": t} for s, t in THEME_MAP.items()]
    symbols = sorted(set(SIGNAL_SYMBOLS) | set(THEME_MAP))
    px = fetch_closes(symbols)
    sig = compute_signals(px)
    orders, exposure = build_orders(positions, {}, DEFAULT_NAV, sig, sig["prices"])
    print(f"As of close {sig['asof']}: BTC {'ON' if sig['btc_on'] else 'OFF'} | "
          f"QQQ50 {'ON' if sig['qqq_on'] else 'OFF'} | "
          f"QQQ200/core {'ON' if sig['qqq200_on'] else 'OFF'} | "
          f"SMH {'ON' if sig['smh_on'] else 'OFF'} | VIX {sig['vix']:.1f}")
    print(f"HIVE sleeve: {sig['hive_note']}\n")
    if orders:
        print("SIGNALS (with empty placeholder positions):")
        for i, order in enumerate(orders, 1):
            print(f"  {i}. {order}")
    else:
        print("No orders today.")


def _in_streamlit() -> bool:
    try:
        from streamlit import runtime
        return runtime.exists()
    except Exception:
        return False


if _in_streamlit():
    render()
elif __name__ == "__main__":
    cli()
