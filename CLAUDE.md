# CRE Underwriter

## What this is
A commercial real estate underwriting tool. You enter a property's financials and
it computes the standard underwriting metrics (NOI, cap rate, DSCR, cash-on-cash
return) and returns a buy or pass signal based on configurable thresholds.

Built by a data science grad who knows Python and SQL well but is new to real
estate underwriting. Explain finance concepts in plain English as you go.

## Current focus
Small multifamily properties to start, since that's where our data source is
strongest. Keep the design open to other asset classes later, but don't build
for them yet.

## Tech stack
- Python 3.11+
- SQLite for storage (single file, no server)
- Streamlit for the dashboard
- RentCast API for property and rent data (added in a later phase, not yet)
- Standard library and well-known packages only. Ask before adding any new dependency.

## How the underwriting works (important)
The buy/pass decision is plain deterministic math, never an AI or ML model.
Every number must trace back to a formula and its inputs. No black boxes. If AI
is used later, it is only for turning documents into numbers, never for the
decision itself.

## Build phases (do not jump ahead)
1. Underwriting engine: pure Python functions. No database, UI, or API. Hardcode
   one sample deal and print results.
2. Buy/pass logic with configurable thresholds.
3. SQLite storage for saving and loading deals.
4. Streamlit dashboard for input and output.
5. RentCast API integration to auto-fill data (keep manual entry working).
6. Deploy to Streamlit Community Cloud.

Build one phase at a time. After each phase, stop so I can run it and review
before moving on.

## Code conventions
- Keep it simple and readable. This is a learning project.
- Comment the finance logic in plain English: what each formula means and why it matters.
- Variable names should match the finance terms (noi, cap_rate, dscr, annual_debt_service).
- Favor obvious code over clever or short code.

## Communication
- Plain, direct language. No filler, no hype.