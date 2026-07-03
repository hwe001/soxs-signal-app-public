#!/usr/bin/env python3
"""
Streamlit SOXS Manual Signal Dashboard.

Public-safe version:
- No Alpaca integration
- No order execution
- No broker credentials
"""

from __future__ import annotations

import os
import json
import time
import urllib.error
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
import yfinance as yf


CORE_SYMBOL = "QQQ"
SHORT_SYMBOL = "SOXS"
VIX_SYMBOL = "^VIX"

NORMAL_SHORT_ALLOC = 0.50
CAUTION_SHORT_ALLOC = 0.25
DANGER_SHORT_ALLOC = 0.15
EMERGENCY_SHORT_ALLOC = 0.00

VIX_CAUTION = 35.0
VIX_DANGER = 45.0
VIX_EMERGENCY = 55.0

SOXS_HIGH_LOOKBACK = 20
QQQ_MA_LOOKBACK = 20
RSI_PERIOD = 14
CLAUDE_MODEL = "claude-haiku-4-5"
AI_NEWS_SYMBOLS = ["NVDA", "MSFT", "GOOGL", "META", "AMZN", "AVGO", "AMD", "TSM"]


st.set_page_config(page_title="SOXS Signal", page_icon="📈", layout="wide")
st.markdown(
    """
    <style>
    div.stButton > button[kind="primary"] {
        background-color: #1d4ed8;
        border: 1px solid #1d4ed8;
        color: white;
        font-weight: 700;
    }
    div.stButton > button[kind="primary"]:hover {
        background-color: #1e40af;
        border-color: #1e40af;
        color: white;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def get_secret(name: str, default: str = "") -> str:
    try:
        return st.secrets.get(name, default)
    except Exception:
        return os.getenv(name, default)


def password_gate() -> bool:
    password = get_secret("SIGNAL_APP_PASSWORD", "")
    if not password:
        return True

    st.sidebar.subheader("Access")
    entered = st.sidebar.text_input("Password", type="password")
    if entered == password:
        return True
    if entered:
        st.sidebar.error("Incorrect password")
    st.info("Enter the dashboard password in the sidebar.")
    return False


@st.cache_data(ttl=300)
def fetch_history(symbol: str, period: str = "6mo") -> pd.Series:
    last_error = None
    for attempt in range(3):
        try:
            hist = yf.Ticker(symbol).history(period=period, auto_adjust=True)
            if not hist.empty:
                return hist["Close"].rename(symbol.replace("^", ""))
        except Exception as exc:
            last_error = exc
        time.sleep(1 + attempt)
    raise RuntimeError(f"No data for {symbol}. Last error: {last_error}")


def rsi(series: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, 1e-10)
    return 100 - 100 / (1 + rs)


def build_data() -> pd.DataFrame:
    qqq = fetch_history(CORE_SYMBOL)
    soxs = fetch_history(SHORT_SYMBOL)
    vix = fetch_history(VIX_SYMBOL)
    df = pd.concat([qqq, soxs, vix], axis=1, sort=True).ffill().dropna()
    df.columns = ["QQQ", "SOXS", "VIX"]
    df["QQQ_MA20"] = df["QQQ"].rolling(QQQ_MA_LOOKBACK).mean()
    df["QQQ_RSI14"] = rsi(df["QQQ"])
    df["SOXS_MA5"] = df["SOXS"].rolling(5).mean()
    df["SOXS_MA20"] = df["SOXS"].rolling(20).mean()
    df["SOXS_HIGH20"] = df["SOXS"].rolling(SOXS_HIGH_LOOKBACK).max()
    df["SOXS_PULLBACK_FROM_HIGH20"] = df["SOXS"] / df["SOXS_HIGH20"] - 1.0
    return df.dropna()


def signal(regime: str, action: str, confidence: str, target_alloc: float, reason: str, row: pd.Series) -> dict:
    return {
        "timestamp_ny": datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S"),
        "regime": regime,
        "action": action,
        "confidence": confidence,
        "target_short_alloc": target_alloc,
        "reason": reason,
        "qqq": float(row["QQQ"]),
        "qqq_ma20": float(row["QQQ_MA20"]),
        "qqq_rsi14": float(row["QQQ_RSI14"]),
        "qqq_below_ma20": bool(float(row["QQQ"]) < float(row["QQQ_MA20"])),
        "soxs": float(row["SOXS"]),
        "soxs_ma5": float(row["SOXS_MA5"]),
        "soxs_ma20": float(row["SOXS_MA20"]),
        "soxs_high20": float(row["SOXS_HIGH20"]),
        "soxs_pullback_from_high20": float(row["SOXS_PULLBACK_FROM_HIGH20"]),
        "soxs_at_high20": bool(float(row["SOXS"]) >= float(row["SOXS_HIGH20"]) * 0.98),
        "vix": float(row["VIX"]),
    }


def classify_signal(row: pd.Series) -> dict:
    vix = float(row["VIX"])
    qqq = float(row["QQQ"])
    qqq_ma20 = float(row["QQQ_MA20"])
    qqq_rsi = float(row["QQQ_RSI14"])
    soxs = float(row["SOXS"])
    soxs_ma5 = float(row["SOXS_MA5"])
    soxs_high20 = float(row["SOXS_HIGH20"])
    soxs_pullback = float(row["SOXS_PULLBACK_FROM_HIGH20"])

    soxs_at_high = soxs >= soxs_high20 * 0.98
    qqq_below_ma = qqq < qqq_ma20
    soxs_breaking_down = soxs < soxs_ma5 and soxs_pullback <= -0.08

    if vix >= VIX_EMERGENCY:
        return signal("emergency", "COVER / FLATTEN SOXS SHORT", "high", EMERGENCY_SHORT_ALLOC, "VIX is in crisis territory; short SOXS tail risk dominates.", row)
    if vix >= VIX_DANGER:
        return signal("danger", "REDUCE SOXS SHORT / DO NOT ADD", "high", DANGER_SHORT_ALLOC, "VIX is very elevated; reduce exposure and avoid adding into panic.", row)
    if vix >= VIX_CAUTION:
        return signal("caution", "HOLD SMALL SHORT OR REDUCE; DO NOT ADD", "medium", CAUTION_SHORT_ALLOC, "VIX is elevated; wait for clearer panic fade.", row)
    if soxs_at_high:
        if soxs_breaking_down and not qqq_below_ma:
            return signal("soxs_spike", "START / ADD SMALL SOXS SHORT", "medium", CAUTION_SHORT_ALLOC, "SOXS has spiked but is beginning to fade while QQQ is healthier.", row)
        return signal("soxs_spike", "WAIT; DO NOT SHORT VERTICAL SPIKE", "high", CAUTION_SHORT_ALLOC, "SOXS is near its 20-day high; wait for pullback confirmation.", row)
    if qqq_below_ma:
        return signal("qqq_soft", "HOLD OR REDUCE; DO NOT ADD", "medium", CAUTION_SHORT_ALLOC, "QQQ is below its 20-day moving average; avoid increasing short SOXS.", row)

    action = "SHORT / ADD SOXS TOWARD TARGET"
    reason = "VIX is calm, QQQ is above MA20, and SOXS is not at a panic high."
    if qqq_rsi > 75:
        action = "HOLD / ADD SLOWLY"
        reason += " QQQ is short-term overbought, so scale rather than chase."
    return signal("normal", action, "medium", NORMAL_SHORT_ALLOC, reason, row)


def pct(x: float) -> str:
    return f"{x:.1%}"


def build_weekly_brief_prompt(df: pd.DataFrame, sig: dict) -> str:
    recent = df.tail(6)
    qqq_week = recent["QQQ"].iloc[-1] / recent["QQQ"].iloc[0] - 1.0
    soxs_week = recent["SOXS"].iloc[-1] / recent["SOXS"].iloc[0] - 1.0
    vix_change = recent["VIX"].iloc[-1] - recent["VIX"].iloc[0]

    return f"""
You are writing a concise weekly market brief for a private manual trading dashboard.
The strategy uses QQQ as the core asset and SOXS short exposure as a risk-controlled overlay.

Do not give personalized financial advice. Do not tell the user to trade immediately.
Explain the signal and the risk posture in plain English.

Current signal:
- Action: {sig["action"]}
- Regime: {sig["regime"]}
- Target SOXS short allocation: {pct(sig["target_short_alloc"])}
- Reason: {sig["reason"]}
- VIX: {sig["vix"]:.2f}
- QQQ: {sig["qqq"]:.2f}
- QQQ MA20: {sig["qqq_ma20"]:.2f}
- QQQ RSI14: {sig["qqq_rsi14"]:.1f}
- SOXS: {sig["soxs"]:.2f}
- SOXS 20-day high: {sig["soxs_high20"]:.2f}
- SOXS pullback from 20-day high: {pct(sig["soxs_pullback_from_high20"])}

Last roughly one trading week:
- QQQ return: {pct(qqq_week)}
- SOXS return: {pct(soxs_week)}
- VIX change: {vix_change:+.2f}

Write exactly four short sections:
1. Weekly read
2. Current signal
3. Risk watch
4. What would change the signal
""".strip()


@st.cache_data(ttl=3600)
def fetch_ai_news() -> list[dict]:
    items = []
    seen = set()
    for symbol in AI_NEWS_SYMBOLS:
        try:
            for item in yf.Ticker(symbol).news[:5]:
                content = item.get("content", {}) if isinstance(item.get("content", {}), dict) else {}
                title = item.get("title") or content.get("title") or ""
                title = title.strip()
                if not title or title in seen:
                    continue
                seen.add(title)
                published = item.get("providerPublishTime") or content.get("pubDate")
                published_text = ""
                if isinstance(published, (int, float)):
                    published_text = datetime.fromtimestamp(published, ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
                elif isinstance(published, str):
                    published_text = published[:10]
                items.append(
                    {
                        "symbol": symbol,
                        "title": title,
                        "publisher": item.get("publisher") or content.get("provider", {}).get("displayName", ""),
                        "published": published_text,
                    }
                )
        except Exception:
            continue
    return items[:12]


def format_news_for_prompt(news_items: list[dict]) -> str:
    if not news_items:
        return "No recent AI/semiconductor headlines were available from the public data source."
    lines = []
    for item in news_items:
        date = f"{item['published']} | " if item.get("published") else ""
        publisher = f" | {item['publisher']}" if item.get("publisher") else ""
        lines.append(f"- {item['symbol']}: {date}{item['title']}{publisher}")
    return "\n".join(lines)


def build_weekly_brief_prompt_with_news(df: pd.DataFrame, sig: dict, news_items: list[dict]) -> str:
    base_prompt = build_weekly_brief_prompt(df, sig)
    return f"""
{base_prompt}

Recent AI / semiconductor / mega-cap headlines that may affect QQQ:
{format_news_for_prompt(news_items)}

Use the headlines only as context. Do not invent facts beyond these headlines.
If the headlines are not clearly relevant, say that news context is limited.
In section 3, mention whether AI-sector news appears supportive, neutral, or risky for QQQ trend.
""".strip()


def generate_weekly_brief(df: pd.DataFrame, sig: dict, news_items: list[dict]) -> str:
    api_key = get_secret("ANTHROPIC_API_KEY", "")
    if not api_key:
        return "Add ANTHROPIC_API_KEY in Streamlit secrets to enable the weekly AI brief."

    model = get_secret("CLAUDE_MODEL", CLAUDE_MODEL)
    payload = {
        "model": model,
        "max_tokens": 900,
        "messages": [{"role": "user", "content": build_weekly_brief_prompt_with_news(df, sig, news_items)}],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Claude API error {exc.code}: {detail}") from exc

    content = data.get("content", [])
    if not content:
        raise RuntimeError("Claude API returned no text.")
    return content[0].get("text", "").strip()


def render_dashboard() -> None:
    st.title("SOXS Manual Signal")
    st.caption("Manual guide only. No broker connection. No order execution.")

    if st.button("Refresh market data"):
        st.cache_data.clear()
        st.rerun()

    df = build_data()
    sig = classify_signal(df.iloc[-1])

    st.subheader(sig["action"])
    st.write(sig["reason"])
    st.caption(f"New York time: {sig['timestamp_ny']}")

    cols = st.columns(4)
    cols[0].metric("Regime", sig["regime"])
    cols[1].metric("Target SOXS Short", pct(sig["target_short_alloc"]))
    cols[2].metric("VIX", f"{sig['vix']:.2f}")
    cols[3].metric("Confidence", sig["confidence"])

    cols = st.columns(4)
    cols[0].metric("QQQ", f"${sig['qqq']:.2f}")
    cols[1].metric("QQQ MA20", f"${sig['qqq_ma20']:.2f}")
    cols[2].metric("QQQ RSI14", f"{sig['qqq_rsi14']:.1f}")
    cols[3].metric("QQQ Below MA20", "Yes" if sig["qqq_below_ma20"] else "No")

    cols = st.columns(4)
    cols[0].metric("SOXS", f"${sig['soxs']:.2f}")
    cols[1].metric("SOXS MA5", f"${sig['soxs_ma5']:.2f}")
    cols[2].metric("SOXS 20D High", f"${sig['soxs_high20']:.2f}")
    cols[3].metric("SOXS Pullback", pct(sig["soxs_pullback_from_high20"]))

    st.subheader("Weekly AI Brief")
    st.caption("Optional. Calls Claude only when you press the button. Includes recent public AI-sector headlines.")
    if st.button("Generate weekly AI brief", type="primary"):
        with st.spinner("Generating weekly brief..."):
            news_items = fetch_ai_news()
            st.markdown(generate_weekly_brief(df, sig, news_items))
            if news_items:
                with st.expander("Headlines used for this brief"):
                    for item in news_items:
                        date = f"{item['published']} | " if item.get("published") else ""
                        st.write(f"{item['symbol']}: {date}{item['title']}")

    st.divider()
    st.line_chart(df[["QQQ", "QQQ_MA20"]].tail(80))
    st.line_chart(df[["SOXS", "SOXS_MA5", "SOXS_MA20"]].tail(80))
    st.line_chart(df[["VIX"]].tail(80))

    st.subheader("Recent Data")
    st.dataframe(df.tail(20).round(2), use_container_width=True)

    st.download_button(
        "Download JSON signal",
        data=pd.Series(sig).to_json(indent=2),
        file_name="soxs_manual_signal.json",
        mime="application/json",
    )


if password_gate():
    try:
        render_dashboard()
    except Exception as exc:
        st.error(f"Could not generate signal: {exc}")
