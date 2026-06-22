# CRE Underwriter

A commercial real estate underwriting tool. Enter a property's financials and it
computes the standard underwriting metrics (NOI, cap rate, DSCR, cash-on-cash
return) and returns a buy or pass signal based on configurable thresholds.

The buy/pass decision is plain deterministic math — never an AI or ML model.

See [CLAUDE.md](CLAUDE.md) for the full plan and build phases.

## Project layout

```
cre_underwriter/      # the application package
  engine.py           # underwriting metrics + buy/pass math (Phase 1-2)
  database.py         # SQLite storage for deals (Phase 3)
  dashboard.py        # Streamlit dashboard (Phase 4)
tests/                # tests for the engine
data/                 # local SQLite database file lives here (git-ignored)
requirements.txt      # dependencies (none required yet)
```

## Setup

```bash
# Create the virtual environment (already done once):
python3 -m venv .venv

# Activate it:
source .venv/bin/activate

# Deactivate when done:
deactivate
```

Requires Python 3.11+ (developed on 3.12).
