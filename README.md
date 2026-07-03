# SOXS Signal Dashboard

Public Streamlit dashboard for manual SOXS signal checks.

This app:

- Pulls public market data from Yahoo Finance.
- Shows a manual SOXS signal dashboard.
- Can generate an optional weekly AI/semiconductor market brief.
- Does not connect to a broker.
- Does not place orders.
- Does not contain trading API keys.

## Streamlit Cloud

Main file:

```text
streamlit_signal_app.py
```

## Secrets

Add this Streamlit secret to protect the app:

```toml
SIGNAL_APP_PASSWORD = "your-password"
```

Optional weekly Claude brief:

```toml
ANTHROPIC_API_KEY = "your-anthropic-api-key"
CLAUDE_MODEL = "claude-haiku-4-5"
```

The weekly brief is generated only when you press the button in the app.

## Notes

This public repo is intentionally dashboard-only. Trading bots, broker workflows,
and execution logic live in private repositories.
