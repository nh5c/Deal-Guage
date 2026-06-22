"""Streamlit dashboard for the CRE Underwriter (Phase 4).

This is the presentation layer only. It does not do any underwriting math or any
SQL itself — it calls the existing engine and database modules:

    - all metrics + the buy/pass decision come from engine.py
    - all storage goes through database.py (save_deal, load_deal, list_deals)

Keeping that boundary strict means the day we swap SQLite for a hosted database,
only database.py changes — this file never touches a connection.

Run it with:  streamlit run cre_underwriter/dashboard.py
"""

import re

import pandas as pd
import streamlit as st

# Import the engine and storage layers. The try/except lets this run both under
# `streamlit run cre_underwriter/dashboard.py` (script folder on the path) and as
# part of the package.
try:
    from cre_underwriter import engine, database, rentcast
except ModuleNotFoundError:
    import engine
    import database
    import rentcast


# st.set_page_config must be the first Streamlit call.
st.set_page_config(page_title="CRE Underwriter", page_icon="🏢", layout="centered")

# Make sure the table exists before we read or write. This is idempotent.
database.initialize_database()


# -----------------------------------------------------------------------------
# Small formatting + computation helpers (presentation only)
# -----------------------------------------------------------------------------
def _money(amount):
    """Format a dollar amount like $69,600 or -$3,480."""
    sign = "-" if amount < 0 else ""
    return f"{sign}${abs(amount):,.0f}"


def _format_metric(value, display):
    """Format a metric the way the engine labels it: 'percent' or 'ratio'."""
    if display == "percent":
        return f"{value * 100:.2f}%"
    return f"{value:.2f}x"


def _markdown_table(headers, rows):
    """Build a clean markdown table (no index column) from headers + row lists."""
    head = "| " + " | ".join(headers) + " |"
    separator = "| " + " | ".join("---" for _ in headers) + " |"
    body = "\n".join("| " + " | ".join(str(cell) for cell in row) + " |" for row in rows)
    return "\n".join([head, separator, body])


def _value_or(value, default):
    """Return value, or the default when value is None (keeps a legitimate 0)."""
    return default if value is None else value


def _heat_color(value, vmin, vmax):
    """CSS background for a heatmap cell: red (low) -> yellow -> green (high).

    Pure-Python interpolation so we don't pull in matplotlib for a colormap.
    Returns "" for NaN/empty cells so they stay uncolored.
    """
    if pd.isna(value) or vmax == vmin:
        return ""
    t = (value - vmin) / (vmax - vmin)            # 0.0 = lowest IRR, 1.0 = highest
    if t < 0.5:                                   # red -> yellow
        f = t / 0.5
        r, g, b = 220 + (245 - 220) * f, 70 + (215 - 70) * f, 70 + (90 - 70) * f
    else:                                         # yellow -> green
        f = (t - 0.5) / 0.5
        r, g, b = 245 + (75 - 245) * f, 215 + (170 - 215) * f, 90 + (95 - 90) * f
    return f"background-color: rgb({int(r)}, {int(g)}, {int(b)}); color: #111;"


def compute_metrics(deal):
    """Run a deal (in engine units: rates as fractions) through every engine
    function and return all the numbers the UI needs. No math lives here — this
    just orchestrates calls into engine.py."""
    gross_rental_income = engine.resolve_gross_rental_income(deal)
    vacancy_rate = deal["vacancy_rate"]
    purchase_price = deal["purchase_price"]
    annual_interest_rate = deal["annual_interest_rate"]
    amortization_years = deal["amortization_years"]

    # Resolve operating expenses via the engine (structured line items or a single
    # total). No expense math is done in the dashboard.
    effective_gross_income = gross_rental_income * (1 - vacancy_rate)
    expense_result = engine.build_operating_expenses(deal, effective_gross_income)
    operating_expenses = expense_result["total"]

    noi = engine.calculate_noi(gross_rental_income, vacancy_rate, operating_expenses)
    cap_rate = engine.calculate_cap_rate(noi, purchase_price)
    # Resolve financing (Phase B): manual loan/down, or sized from LTV/DSCR using NOI.
    financing = engine.resolve_financing(deal, noi)
    loan_amount = financing["loan_amount"]
    down_payment = financing["down_payment"]
    annual_debt_service = engine.calculate_annual_debt_service(
        loan_amount, annual_interest_rate, amortization_years
    )
    dscr = engine.calculate_dscr(noi, annual_debt_service)
    cash_on_cash = engine.calculate_cash_on_cash_return(
        noi, annual_debt_service, down_payment
    )
    evaluation = engine.evaluate_deal(cap_rate, dscr, cash_on_cash)

    return {
        "noi": noi,
        "cap_rate": cap_rate,
        "annual_debt_service": annual_debt_service,
        "dscr": dscr,
        "cash_on_cash": cash_on_cash,
        "evaluation": evaluation,
        "operating_expenses": operating_expenses,
        "expense_result": expense_result,
        "loan_amount": loan_amount,
        "down_payment": down_payment,
        "sizing": financing["sizing"],
        # Intermediates for the breakdown tables.
        "vacancy_loss": gross_rental_income * vacancy_rate,
        "effective_gross_income": effective_gross_income,
        "monthly_payment": annual_debt_service / 12,
        "annual_cash_flow": noi - annual_debt_service,
    }


def _assumption_defaults():
    """Hold & exit assumption fields, defaulted from the engine's sample deal.

    Used to fill these in for deals that don't carry them — e.g. one loaded from
    the database, whose schema (unchanged) stores only the year-one inputs.
    """
    deal = engine.sample_deal
    return {
        "hold_period_years": deal["hold_period_years"],
        "rent_growth": deal["rent_growth"],
        "expense_growth": deal["expense_growth"],
        "exit_cap_rate": deal["exit_cap_rate"],
        "selling_cost_pct": deal["selling_cost_pct"],
    }


# -----------------------------------------------------------------------------
# RentCast lookups (Phase 5)
#
# RentCast is called ONLY on the "Look up property" button press (in the sidebar),
# never on an ordinary rerun. We also wrap the client in st.cache_data keyed by
# address, so looking up the same address twice costs nothing. The free plan is 50
# calls a month and each lookup is two calls (value + rent), so this matters.
#
# Successful results are cached. Failures are re-raised inside the cached function
# so st.cache_data does NOT store them — that way a transient error (rate limit,
# network blip) can be retried instead of being stuck in the cache.
# -----------------------------------------------------------------------------
class _LookupFailed(Exception):
    """Carries a rentcast error-result dict so we can avoid caching failures."""

    def __init__(self, result):
        super().__init__(result.get("error", "lookup failed"))
        self.result = result


@st.cache_data(show_spinner=False, ttl=24 * 60 * 60)
def _cached_value_estimate(address):
    result = rentcast.get_value_estimate(address)
    if not result["ok"]:
        raise _LookupFailed(result)  # don't cache failures
    return result


@st.cache_data(show_spinner=False, ttl=24 * 60 * 60)
def _cached_rent_estimate(address):
    result = rentcast.get_rent_estimate(address)
    if not result["ok"]:
        raise _LookupFailed(result)  # don't cache failures
    return result


def value_estimate(address):
    """Cached value lookup that always returns a result dict (never raises).

    The API key is checked LIVE here, BEFORE the cache. With no key we return the
    missing-key result without ever touching st.cache_data — so a no-key state is
    never cached, and a key set later takes effect on the very next lookup.
    """
    if not rentcast.has_api_key():
        return rentcast.missing_key_error()
    try:
        return _cached_value_estimate(address)
    except _LookupFailed as failure:
        return failure.result


def rent_estimate(address):
    """Cached rent lookup that always returns a result dict (never raises).

    Same as value_estimate: the key is checked live, before the cache.
    """
    if not rentcast.has_api_key():
        return rentcast.missing_key_error()
    try:
        return _cached_rent_estimate(address)
    except _LookupFailed as failure:
        return failure.result


@st.cache_data(show_spinner=False, ttl=24 * 60 * 60)
def _cached_market_trends(zip_code):
    result = rentcast.get_market_trends(zip_code)
    if not result["ok"]:
        raise _LookupFailed(result)  # don't cache failures
    return result


def market_trends(zip_code):
    """Cached market-trends lookup (1 API call per new zip). Never raises.

    The key is checked live before the cache, like the value/rent lookups, so a
    no-key state is never cached. Repeat views of the same zip cost nothing.
    """
    if not rentcast.has_api_key():
        return rentcast.missing_key_error()
    try:
        return _cached_market_trends(zip_code)
    except _LookupFailed as failure:
        return failure.result


def _zip_from_address(address):
    """Pull the last 5-digit group out of an address string, or '' if none."""
    matches = re.findall(r"\b(\d{5})\b", address or "")
    return matches[-1] if matches else ""


def _render_market_trends():
    """Show the most recent market-trends result: zip history vs the user's input."""
    trends = st.session_state.get("_trends")
    if trends is None:
        return
    if not trends.get("ok"):
        st.warning(trends["error"])
        return

    zip_code = st.session_state.get("_trends_zip", "")
    user_growth_pct = st.session_state["rent_growth_pct"]     # already a percent
    history_growth = trends.get("annualized_rent_growth")     # fraction or None

    span_months = trends.get("rent_history_months") or 0
    left, right = st.columns(2)
    left.metric("Your rent-growth assumption", f"{user_growth_pct:.1f}%/yr")
    if history_growth is not None:
        right.metric(f"Zip {zip_code} rent growth (history)", f"{history_growth * 100:.1f}%/yr")
        st.caption(f"Annualized over the **{span_months} months** of history RentCast "
                   f"returned for zip {zip_code}. Informational only — your assumption is "
                   "left unchanged; adjust it yourself if the history suggests you should.")
    else:
        right.metric(f"Zip {zip_code} rent growth (history)", "n/a")
        st.caption("RentCast returned too little rent history for this zip to derive a "
                   "trend. Your assumption is left unchanged.")

    rent_history = trends.get("rent_history") or []
    if len(rent_history) >= 2:
        st.line_chart(
            {"Month": [m for m, _ in rent_history], "Avg rent": [v for _, v in rent_history]},
            x="Month", y="Avg rent",
        )


def _comp_cell(value, kind="plain"):
    """Format one comparable's cell, showing an em dash for missing values."""
    if value is None:
        return "—"
    if kind == "money":
        return _money(value)
    if kind == "sqft":
        return f"{value:,.0f}"
    if kind == "miles":
        return f"{value:.2f}"
    if kind == "pct":
        return f"{value * 100:.0f}%"
    if kind == "num":
        return f"{value:g}"
    return str(value)


def _comps_rows(comps, price_label):
    """Turn comp dicts into rows for st.dataframe (all strings, pre-formatted)."""
    rows = []
    for comp in comps:
        rows.append({
            "Address": comp.get("address") or "—",
            price_label: _comp_cell(comp.get("price"), "money"),
            "Bd": _comp_cell(comp.get("bedrooms"), "num"),
            "Ba": _comp_cell(comp.get("bathrooms"), "num"),
            "SqFt": _comp_cell(comp.get("squareFootage"), "sqft"),
            "Dist mi": _comp_cell(comp.get("distance"), "miles"),
            "Days old": _comp_cell(comp.get("daysOld"), "num"),
            "Match": _comp_cell(comp.get("correlation"), "pct"),
        })
    return rows


def _render_comp_check(label, kind, check, per_unit):
    """Render one market-validation line (rent or price) from compare_to_comps."""
    if not check.get("ok"):
        st.caption(f"{label}: not enough comps to compare.")
        return
    gap = check["gap_pct"]
    count = check["comp_count"]
    basis = "per-unit " if per_unit else ""
    median_text = _money(check["comp_median"]) + ("/mo" if kind == "rent" else "")
    if check["materially_above"]:
        tail = "possibly optimistic" if kind == "rent" else "you may be paying above market"
        st.warning(f"⚠️ Your {basis}{label.lower()} is ~{gap * 100:.0f}% **above** the "
                   f"median of {count} comps ({median_text}) — {tail}.")
    elif check["materially_below"]:
        tail = "conservative vs comps" if kind == "rent" else "below comparable sales"
        st.info(f"Your {basis}{label.lower()} is ~{abs(gap) * 100:.0f}% **below** the "
                f"median of {count} comps ({median_text}) — {tail}.")
    else:
        st.success(f"Your {basis}{label.lower()} is in line with comps "
                   f"({gap * 100:+.0f}% vs the median of {count}, {median_text}).")


def render_rentcast_panel():
    """Show the most recent RentCast lookup: estimates, apply buttons, and comps."""
    value_result = st.session_state.get("_rc_value")
    rent_result = st.session_state.get("_rc_rent")
    if value_result is None and rent_result is None:
        return  # nothing looked up yet this session

    address_used = st.session_state.get("_rc_address_used", "")
    with st.container(border=True):
        st.markdown(f"**RentCast lookup — {address_used}**")
        st.caption("Starting estimates to validate your inputs — not final truth. Eyeball the comps.")

        value_col, rent_col = st.columns(2)

        with value_col:
            st.markdown("**Value estimate**")
            if value_result and value_result.get("ok") and value_result.get("estimate") is not None:
                estimate = value_result["estimate"]
                st.metric("Estimated value", _money(estimate), label_visibility="collapsed")
                low, high = value_result.get("range_low"), value_result.get("range_high")
                if low is not None and high is not None:
                    st.caption(f"Range {_money(low)} – {_money(high)}")
                if st.button("→ Use as purchase price", key="rc_apply_price", use_container_width=True):
                    st.session_state["_pending_prefill"] = {
                        "fields": {"purchase_price": float(round(estimate))},
                        "message": f"Set purchase price to {_money(estimate)} from the RentCast value estimate.",
                    }
                    st.rerun()
            elif value_result and not value_result.get("ok"):
                st.warning(value_result["error"])
            else:
                st.caption("No value estimate returned.")

        with rent_col:
            st.markdown("**Rent estimate**")
            if rent_result and rent_result.get("ok") and rent_result.get("estimate") is not None:
                monthly_rent = rent_result["estimate"]
                annual_rent = monthly_rent * 12
                st.metric("Estimated rent", f"{_money(monthly_rent)}/mo", label_visibility="collapsed")
                low, high = rent_result.get("range_low"), rent_result.get("range_high")
                if low is not None and high is not None:
                    st.caption(f"Range {_money(low)} – {_money(high)}/mo · ≈ {_money(annual_rent)}/yr")
                else:
                    st.caption(f"≈ {_money(annual_rent)}/yr")
                if st.button("→ Use as gross rent (×12)", key="rc_apply_rent", use_container_width=True):
                    st.session_state["_pending_prefill"] = {
                        "fields": {"gross_rental_income": float(round(annual_rent))},
                        "message": (f"Set gross rental income to {_money(annual_rent)}/yr "
                                    f"({_money(monthly_rent)}/mo × 12) from RentCast."),
                    }
                    st.rerun()
            elif rent_result and not rent_result.get("ok"):
                st.warning(rent_result["error"])
            else:
                st.caption("No rent estimate returned.")

        if value_result and value_result.get("ok") and value_result.get("comps"):
            st.markdown("**Nearby sales (value comps)**")
            st.dataframe(_comps_rows(value_result["comps"], "Sale price"),
                         hide_index=True, use_container_width=True)
        if rent_result and rent_result.get("ok") and rent_result.get("comps"):
            st.markdown("**Nearby rentals (rent comps)**")
            st.dataframe(_comps_rows(rent_result["comps"], "Rent/mo"),
                         hide_index=True, use_container_width=True)

        # ---- Market validation: the user's numbers vs the comps (NO new API call) ----
        units = max(int(st.session_state.get("number_of_units", 1) or 1), 1)
        rent_has_comps = rent_result and rent_result.get("ok") and rent_result.get("comps")
        value_has_comps = value_result and value_result.get("ok") and value_result.get("comps")
        if rent_has_comps or value_has_comps:
            st.markdown("**Market validation** — a directional sanity check")
            if rent_has_comps:
                user_rent_per_unit = st.session_state["gross_rental_income"] / units / 12
                rent_check = engine.compare_to_comps(
                    user_rent_per_unit, [c["price"] for c in rent_result["comps"]])
                _render_comp_check("Gross rent", "rent", rent_check, per_unit=units > 1)
            if value_has_comps:
                user_price_per_unit = st.session_state["purchase_price"] / units
                price_check = engine.compare_to_comps(
                    user_price_per_unit, [c["price"] for c in value_result["comps"]])
                _render_comp_check("Purchase price", "price", price_check, per_unit=units > 1)
            st.caption("Approximate: RentCast comps skew toward single units, so for "
                       "multi-unit properties read this as a rough, directional check only.")


# -----------------------------------------------------------------------------
# Form state: seed defaults from the engine's sample deal, and load saved deals.
#
# Streamlit reruns this whole script on every interaction. We keep each input in
# st.session_state so values survive reruns and so "Load" can drop a saved deal
# back into the form. Vacancy and interest are kept in the UI as PERCENT (5.0,
# 6.5); we convert to fractions (0.05, 0.065) before calling the engine/database.
# -----------------------------------------------------------------------------
RENT_ROLL_COLUMNS = ["label", "unit_type", "square_footage", "monthly_rent", "market_rent", "occupied"]


def _na(value):
    """None for NaN/None, else the value (cleans st.data_editor blanks)."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    return value


def _clean_rent_roll(edited_df):
    """Turn the edited rent-roll dataframe into a clean list of unit dicts."""
    roll = []
    for i, rec in enumerate(edited_df.to_dict("records")):
        label = _na(rec.get("label"))
        unit_type = _na(rec.get("unit_type"))
        square_footage = _na(rec.get("square_footage"))
        monthly_rent = _na(rec.get("monthly_rent"))
        market_rent = _na(rec.get("market_rent"))
        occupied = _na(rec.get("occupied"))
        roll.append({
            "label": str(label).strip() if label is not None else f"Unit {i + 1}",
            "unit_type": str(unit_type) if unit_type is not None else "",
            "square_footage": float(square_footage) if square_footage is not None else None,
            "monthly_rent": float(monthly_rent) if monthly_rent is not None else 0.0,
            "market_rent": float(market_rent) if market_rent is not None else None,
            "occupied": bool(occupied) if occupied is not None else False,
        })
    return roll


def _apply_expense_template(property_type):
    """Load a property type's expense-template defaults into the form (overwriting
    the current line-item values). Called when the user changes the property type."""
    template = engine.EXPENSE_TEMPLATES.get(property_type, engine.EXPENSE_TEMPLATES[engine.DEFAULT_PROPERTY_TYPE])
    st.session_state["property_tax_pct"] = template["property_tax_rate"] * 100.0
    st.session_state["insurance_annual"] = float(template["insurance_annual"])
    st.session_state["hoa_annual"] = float(template["hoa_annual"])
    st.session_state["management_pct_ui"] = template["management_pct"] * 100.0
    st.session_state["repairs_pct_ui"] = template["repairs_pct"] * 100.0
    st.session_state["utilities_annual"] = float(template["utilities_annual"])
    st.session_state["reserves_per_unit"] = float(template["reserves_per_unit"])


def _seed_form_defaults():
    """Put the sample deal into session_state once, as the starting form values."""
    deal = engine.sample_deal
    st.session_state.setdefault("name", deal["name"])
    st.session_state.setdefault("purchase_price", float(deal["purchase_price"]))
    st.session_state.setdefault("gross_rental_income", float(deal["gross_rental_income"]))
    st.session_state.setdefault("vacancy_pct", deal["vacancy_rate"] * 100.0)
    st.session_state.setdefault("operating_expenses", float(deal["operating_expenses"]))
    # Operating expenses (Phase 9/12): property type + mode toggle + line-item inputs.
    st.session_state.setdefault("property_type", deal.get("property_type", engine.DEFAULT_PROPERTY_TYPE))
    st.session_state.setdefault("_applied_property_type", st.session_state["property_type"])
    st.session_state.setdefault("number_of_units", int(deal["number_of_units"]))
    # Income method (Phase A): default by property type; seed the rent roll from the sample.
    st.session_state.setdefault(
        "income_mode_label",
        "Detailed (rent roll)" if engine.default_income_mode(st.session_state["property_type"]) == "detailed"
        else "Simple (single total)",
    )
    st.session_state.setdefault(
        "rent_roll",
        [dict(u) for u in (deal.get("rent_roll")
                           or engine.default_rent_roll(deal["number_of_units"], deal["gross_rental_income"]))],
    )
    st.session_state.setdefault(
        "expense_mode_label",
        "Detailed (line items)" if deal.get("expense_mode") == "detailed" else "Simple (single total)",
    )
    st.session_state.setdefault("property_tax_pct", deal["property_tax_rate"] * 100.0)
    st.session_state.setdefault("insurance_annual", float(deal["insurance_annual"]))
    st.session_state.setdefault("hoa_annual", float(deal.get("hoa_annual", 0.0)))
    st.session_state.setdefault("management_pct_ui", deal["management_pct"] * 100.0)
    st.session_state.setdefault("repairs_pct_ui", deal["repairs_pct"] * 100.0)
    st.session_state.setdefault("utilities_annual", float(deal["utilities_annual"]))
    st.session_state.setdefault("reserves_per_unit", float(deal["reserves_per_unit"]))
    # Value-add (Phase C): renovation inputs.
    st.session_state.setdefault("renovation_cost_per_unit_input", float(deal.get("renovation_cost_per_unit") or 0.0))
    st.session_state.setdefault("renovation_pace_input", float(deal.get("renovation_pace") or 0.0))
    st.session_state.setdefault("loan_amount", float(deal["loan_amount"]))
    st.session_state.setdefault("interest_pct", deal["annual_interest_rate"] * 100.0)
    st.session_state.setdefault("amortization_years", int(deal["amortization_years"]))
    st.session_state.setdefault("down_payment", float(deal["down_payment"]))
    # Financing (Phase B): manual vs sized, plus the sizing inputs.
    st.session_state.setdefault(
        "financing_mode_label",
        "Size the loan (commercial)" if deal.get("financing_mode") == "sized"
        else "Manual (enter loan / down payment)",
    )
    st.session_state.setdefault("ltv_max_pct", deal.get("ltv_max", engine.DEFAULT_LTV_MAX) * 100.0)
    st.session_state.setdefault("dscr_min_input", deal.get("dscr_min", engine.DEFAULT_DSCR_MIN))
    # Hold & exit assumptions (Phase 7-8); the % ones are shown as percent in the UI.
    st.session_state.setdefault("hold_period_years", int(deal["hold_period_years"]))
    st.session_state.setdefault("rent_growth_pct", deal["rent_growth"] * 100.0)
    st.session_state.setdefault("expense_growth_pct", deal["expense_growth"] * 100.0)
    st.session_state.setdefault("exit_cap_pct", deal["exit_cap_rate"] * 100.0)
    st.session_state.setdefault("sell_cost_pct", deal["selling_cost_pct"] * 100.0)


def _apply_deal_to_form(deal):
    """Copy a deal dict (engine units) into the form's session_state fields."""
    st.session_state["name"] = deal["name"]
    st.session_state["purchase_price"] = float(deal["purchase_price"])
    st.session_state["gross_rental_income"] = float(deal["gross_rental_income"])
    st.session_state["vacancy_pct"] = float(deal["vacancy_rate"]) * 100.0
    # Operating expenses (Phase 9/12). A loaded deal carries these columns (NULL when a
    # simple-mode deal omitted line items); fall back to the property type's template on
    # None. Set _applied_property_type so the load isn't overwritten by the template.
    sample = engine.sample_deal
    property_type = deal.get("property_type") or engine.DEFAULT_PROPERTY_TYPE
    template = engine.expense_template_for(deal)
    st.session_state["property_type"] = property_type
    st.session_state["_applied_property_type"] = property_type
    st.session_state["number_of_units"] = int(_value_or(deal.get("number_of_units"), sample["number_of_units"]))
    # Income method (Phase A): from the loaded deal, defaulting by type if absent. Reset
    # the rent roll to the loaded one and clear the editor's edits from the prior deal.
    loaded_income_mode = deal.get("income_mode") or engine.default_income_mode(property_type)
    st.session_state["income_mode_label"] = (
        "Detailed (rent roll)" if loaded_income_mode == "detailed" else "Simple (single total)"
    )
    loaded_roll = deal.get("rent_roll")
    st.session_state["rent_roll"] = (
        [dict(u) for u in loaded_roll] if loaded_roll
        else engine.default_rent_roll(
            _value_or(deal.get("number_of_units"), sample["number_of_units"]),
            _value_or(deal.get("gross_rental_income"), sample["gross_rental_income"]))
    )
    st.session_state.pop("rent_roll_editor", None)
    st.session_state["expense_mode_label"] = (
        "Detailed (line items)" if deal.get("expense_mode") == "detailed" else "Simple (single total)"
    )
    st.session_state["operating_expenses"] = float(_value_or(deal.get("operating_expenses"), sample["operating_expenses"]))
    st.session_state["property_tax_pct"] = float(_value_or(deal.get("property_tax_rate"), template["property_tax_rate"])) * 100.0
    st.session_state["insurance_annual"] = float(_value_or(deal.get("insurance_annual"), template["insurance_annual"]))
    st.session_state["hoa_annual"] = float(_value_or(deal.get("hoa_annual"), template["hoa_annual"]))
    st.session_state["management_pct_ui"] = float(_value_or(deal.get("management_pct"), template["management_pct"])) * 100.0
    st.session_state["repairs_pct_ui"] = float(_value_or(deal.get("repairs_pct"), template["repairs_pct"])) * 100.0
    st.session_state["utilities_annual"] = float(_value_or(deal.get("utilities_annual"), template["utilities_annual"]))
    st.session_state["reserves_per_unit"] = float(_value_or(deal.get("reserves_per_unit"), template["reserves_per_unit"]))
    # Value-add (Phase C): renovation inputs from the loaded deal.
    st.session_state["renovation_cost_per_unit_input"] = float(_value_or(deal.get("renovation_cost_per_unit"), 0.0))
    st.session_state["renovation_pace_input"] = float(_value_or(deal.get("renovation_pace"), 0.0))
    st.session_state["loan_amount"] = float(deal["loan_amount"])
    st.session_state["interest_pct"] = float(deal["annual_interest_rate"]) * 100.0
    st.session_state["amortization_years"] = int(deal["amortization_years"])
    st.session_state["down_payment"] = float(deal["down_payment"])
    # Financing (Phase B): from the loaded deal, defaulting if absent.
    st.session_state["financing_mode_label"] = (
        "Size the loan (commercial)" if deal.get("financing_mode") == "sized"
        else "Manual (enter loan / down payment)"
    )
    st.session_state["ltv_max_pct"] = float(_value_or(deal.get("ltv_max"), engine.DEFAULT_LTV_MAX)) * 100.0
    st.session_state["dscr_min_input"] = float(_value_or(deal.get("dscr_min"), engine.DEFAULT_DSCR_MIN))
    # Hold & exit assumptions. A deal loaded from the database won't have these
    # (schema unchanged), so fall back to the engine's sample-deal defaults.
    defaults = engine.sample_deal
    st.session_state["hold_period_years"] = int(deal.get("hold_period_years", defaults["hold_period_years"]))
    st.session_state["rent_growth_pct"] = float(deal.get("rent_growth", defaults["rent_growth"])) * 100.0
    st.session_state["expense_growth_pct"] = float(deal.get("expense_growth", defaults["expense_growth"])) * 100.0
    st.session_state["exit_cap_pct"] = float(deal.get("exit_cap_rate", defaults["exit_cap_rate"])) * 100.0
    st.session_state["sell_cost_pct"] = float(deal.get("selling_cost_pct", defaults["selling_cost_pct"])) * 100.0


# Seed defaults first (only fills keys that don't exist yet).
_seed_form_defaults()

# Apply a pending load BEFORE any input widget is created this run. The "Load"
# button (below) just records an id and reruns; we do the actual load here so we
# never modify a widget's state after it has been instantiated.
_pending_load_id = st.session_state.pop("_pending_load_id", None)
if _pending_load_id is not None:
    loaded_deal = database.load_deal(_pending_load_id)
    if loaded_deal is not None:
        _apply_deal_to_form(loaded_deal)
        # Show the loaded deal's results immediately.
        st.session_state["_results_deal"] = loaded_deal
        st.session_state["_show_results"] = True
        st.session_state["_flash"] = f"Loaded “{loaded_deal['name']}” (deal #{_pending_load_id})."

# Apply a pending RentCast pre-fill (set by an "Apply" button) BEFORE the input
# widgets are created — same reason as the load above: you can't change a widget's
# value after it has been instantiated.
_pending_prefill = st.session_state.pop("_pending_prefill", None)
if _pending_prefill is not None:
    for field, value in _pending_prefill["fields"].items():
        st.session_state[field] = value
    st.session_state["_flash"] = _pending_prefill["message"]


# -----------------------------------------------------------------------------
# Header + one-time flash message (e.g. "Saved", "Loaded")
# -----------------------------------------------------------------------------
st.title("🏢 CRE Underwriter")
st.caption("Enter a small-multifamily deal, compute the metrics, and get a buy/pass read.")

_flash = st.session_state.pop("_flash", None)
if _flash:
    st.success(_flash)


# -----------------------------------------------------------------------------
# Sidebar: load a previously saved deal
# -----------------------------------------------------------------------------
with st.sidebar:
    st.header("Saved deals")
    saved_deals = database.list_deals()  # via database.py — no direct SQL here

    if saved_deals:
        # Map a readable label -> deal id for the dropdown.
        options = {
            f"#{deal['id']} · {deal['name']} · {deal['created_at'][:10]}": deal["id"]
            for deal in saved_deals
        }
        chosen_label = st.selectbox("Pick a deal", list(options.keys()), key="_deal_choice")
        if st.button("Load into form", key="load_btn", use_container_width=True):
            st.session_state["_pending_load_id"] = options[chosen_label]
            st.rerun()
    else:
        st.caption("No saved deals yet. Fill the form and click **Save deal**.")

    st.caption(f"{len(saved_deals)} saved")

    st.divider()
    st.header("RentCast lookup")
    # Live, non-secret diagnostic: does THIS running process see the key right now?
    # Reads os.environ at render time and never prints the key itself.
    if rentcast.has_api_key():
        st.caption("🔑 API key: **detected**")
    else:
        st.caption("🔑 API key: **not detected** — set RENTCAST_API_KEY, then restart Streamlit.")
    lookup_address = st.text_input(
        "Property address", key="_rc_address",
        placeholder="Street, City, State, Zip",
        help="Full address. One lookup = a value estimate + a rent estimate + comps.",
    )
    if st.button("Look up property", key="rc_lookup_btn", use_container_width=True):
        if not lookup_address.strip():
            st.warning("Enter an address before looking up.")
        else:
            # RentCast is called HERE ONLY (this button press). Results are cached
            # by address, so reruns and repeat lookups don't spend more calls.
            with st.spinner("Asking RentCast…"):
                st.session_state["_rc_value"] = value_estimate(lookup_address.strip())
                st.session_state["_rc_rent"] = rent_estimate(lookup_address.strip())
            st.session_state["_rc_address_used"] = lookup_address.strip()
    st.caption("Estimates validate your inputs — they aren't final truth.")

    with st.expander("Buy thresholds (from engine)"):
        thresholds = engine.DEFAULT_THRESHOLDS
        st.write(f"Cap rate ≥ {thresholds['min_cap_rate'] * 100:.2f}%")
        st.write(f"DSCR ≥ {thresholds['min_dscr']:.2f}x")
        st.write(f"Cash-on-cash ≥ {thresholds['min_cash_on_cash'] * 100:.2f}%")
        st.caption("Edit these in engine.py (DEFAULT_THRESHOLDS).")


# -----------------------------------------------------------------------------
# RentCast lookup results (rendered in the main area; populated by the sidebar)
# -----------------------------------------------------------------------------
render_rentcast_panel()


# -----------------------------------------------------------------------------
# The deal input form
# -----------------------------------------------------------------------------
st.subheader("Deal inputs")
st.text_input("Deal name", key="name")

# Property type sets the expense template (Phase 12). Changing it loads that type's
# expense defaults; it changes nothing else in the underwriting math.
property_type = st.selectbox(
    "Property type",
    options=list(engine.PROPERTY_TYPE_LABELS.keys()),
    format_func=lambda t: engine.PROPERTY_TYPE_LABELS[t],
    key="property_type",
    help="Residential only for now. Configures which expense line items show and their "
         "default values — the NOI / cap / DSCR / IRR math is identical for every type.",
)
# When the type changes, load its template defaults BEFORE the expense widgets are
# created (so they pick up the new values). Manual edits afterward still win.
if st.session_state.get("_applied_property_type") != property_type:
    _apply_expense_template(property_type)
    # Income method follows the new type's smart default (the user can re-toggle).
    st.session_state["income_mode_label"] = (
        "Detailed (rent roll)" if engine.default_income_mode(property_type) == "detailed"
        else "Simple (single total)"
    )
    st.session_state["_applied_property_type"] = property_type

property_col, financing_col = st.columns(2)

with property_col:
    st.markdown("**Property**")
    purchase_price = st.number_input(
        "Purchase price ($)", key="purchase_price",
        min_value=0.0, step=5000.0, format="%.0f",
        help="The price you pay for the property.",
    )
    vacancy_pct = st.number_input(
        "Vacancy rate (%)", key="vacancy_pct",
        min_value=0.0, max_value=100.0, step=0.5, format="%.2f",
        help="Expected ongoing vacancy assumption — separate from any units currently "
             "marked vacant in the rent roll.",
    )

with financing_col:
    st.markdown("**Loan terms**")
    interest_pct = st.number_input(
        "Interest rate (%)", key="interest_pct",
        min_value=0.0, max_value=30.0, step=0.125, format="%.3f",
        help="Annual fixed mortgage rate.",
    )
    amortization_years = st.number_input(
        "Amortization (years)", key="amortization_years",
        min_value=1, max_value=40, step=1,
        help="Years to fully pay the loan off.",
    )

# -----------------------------------------------------------------------------
# Income (Phase A): a single gross total, or a detailed per-unit rent roll.
# -----------------------------------------------------------------------------
st.markdown("**Income**")
income_mode_label = st.radio(
    "Income input mode", ["Detailed (rent roll)", "Simple (single total)"],
    key="income_mode_label", horizontal=True, label_visibility="collapsed",
    help="Rent roll sums per-unit rents (a vacant unit counts as $0); Simple takes one "
         "annual gross number. Defaults to rent roll for multifamily.",
)
income_detailed = income_mode_label.startswith("Detailed")

if income_detailed:
    st.caption("Add or remove unit rows. A vacant unit stays listed but contributes $0 "
               "to current income. Set a unit's **market rent** above its current rent to "
               "model renovation upside.")
    roll_df = pd.DataFrame(st.session_state["rent_roll"], columns=RENT_ROLL_COLUMNS)
    edited_roll = st.data_editor(
        roll_df, key="rent_roll_editor", num_rows="dynamic", hide_index=True,
        use_container_width=True,
        column_config={
            "label": st.column_config.TextColumn("Unit"),
            "unit_type": st.column_config.TextColumn("Type (bd/ba)"),
            "square_footage": st.column_config.NumberColumn("SqFt", min_value=0, step=10),
            "monthly_rent": st.column_config.NumberColumn("Rent/mo", min_value=0, step=25, format="$%d"),
            "market_rent": st.column_config.NumberColumn(
                "Market rent/mo", min_value=0, step=25, format="$%d",
                help="Achievable rent after renovation; leave blank if already at market."),
            "occupied": st.column_config.CheckboxColumn("Occupied"),
        },
    )
    rent_roll = _clean_rent_roll(edited_roll)
    roll_summary = engine.summarize_rent_roll(rent_roll)
    gross_rental_income = roll_summary["annual_gross_rental_income"]
    number_of_units = roll_summary["unit_count"]
    # Reconcile: in detailed mode the rent roll defines the unit count (used by per-unit
    # reserves), so derive number_of_units from it rather than a separate field.
    st.session_state["number_of_units"] = max(number_of_units, 1)

    # Split across two rows so the dollar figures get enough width to show in full
    # (a single 5-column row truncated "Annual gross" to "$102,...").
    count_totals = st.columns(3)
    count_totals[0].metric("Units", roll_summary["unit_count"])
    count_totals[1].metric("Occupied", f"{roll_summary['occupied_count']}/{roll_summary['unit_count']}")
    count_totals[2].metric("Occupancy", f"{roll_summary['physical_occupancy'] * 100:.0f}%")
    rent_totals = st.columns(2)
    rent_totals[0].metric("Monthly rent (occupied)", _money(roll_summary["occupied_monthly_rent"]))
    rent_totals[1].metric("Annual gross", _money(gross_rental_income))

    st.markdown("**Value-add** (renovate units toward market rent)")
    va_col1, va_col2 = st.columns(2)
    renovation_cost_per_unit = va_col1.number_input(
        "Renovation cost ($/unit)", key="renovation_cost_per_unit_input",
        min_value=0.0, step=1000.0, format="%.0f",
        help="Capex per unit to renovate it to market rent.",
    )
    renovation_pace = va_col2.number_input(
        "Renovation pace (units/yr)", key="renovation_pace_input",
        min_value=0.0, max_value=100.0, step=1.0, format="%.1f",
        help="Units renovated per year, e.g. as leases turn. 0 = no renovation plan.",
    )

    # Warn when an occupied unit's current rent is already at/above its market rent (a
    # market rent IS set, but there's no upside to capture) — otherwise the flat pro
    # forma looks like a bug. Blank/zero market rent = no upside modeled, so no warning.
    no_upside_units = [
        unit for unit in rent_roll
        if unit.get("occupied", True)
        and unit.get("market_rent")  # market rent set and non-zero
        and (unit.get("monthly_rent") or 0.0) >= unit["market_rent"]
    ]
    if no_upside_units:
        shown = ", ".join(str(unit.get("label") or "?") for unit in no_upside_units[:6])
        more = f" and {len(no_upside_units) - 6} more" if len(no_upside_units) > 6 else ""
        st.warning(
            f"⚠️ {len(no_upside_units)} occupied unit(s) ({shown}{more}) have a current rent "
            "**at or above** their market rent, so there's no renovation upside to capture — "
            "the value-add ramp won't do anything for them. Double-check that the current and "
            "market rents were entered correctly."
        )
else:
    income_col1, income_col2 = st.columns(2)
    gross_rental_income = income_col1.number_input(
        "Gross rental income ($/yr)", key="gross_rental_income",
        min_value=0.0, step=1000.0, format="%.0f",
        help="Total annual rent if every unit is occupied.",
    )
    number_of_units = income_col2.number_input(
        "Number of units", key="number_of_units",
        min_value=1, max_value=50, step=1,
        help="Unit count — used for per-unit replacement reserves.",
    )
    rent_roll = None
    # Value-add needs a rent roll; in simple mode carry the stored values (unused).
    renovation_cost_per_unit = st.session_state.get("renovation_cost_per_unit_input", 0.0)
    renovation_pace = st.session_state.get("renovation_pace_input", 0.0)

st.markdown("**Operating expenses**")
expense_mode_label = st.radio(
    "Expense input mode", ["Detailed (line items)", "Simple (single total)"],
    key="expense_mode_label", horizontal=True, label_visibility="collapsed",
    help="Detailed builds the total from standard line items; Simple takes one number "
         "so a quick look doesn't need six fields.",
)
expense_detailed = expense_mode_label.startswith("Detailed")

if expense_detailed:
    ecol1, ecol2, ecol3 = st.columns(3)
    ecol1.number_input(
        "Property taxes (% of price)", key="property_tax_pct",
        min_value=0.0, max_value=10.0, step=0.05, format="%.2f",
    )
    ecol2.number_input(
        "Management (% of income)", key="management_pct_ui",
        min_value=0.0, max_value=30.0, step=0.5, format="%.1f",
    )
    ecol3.number_input(
        "Repairs (% of income)", key="repairs_pct_ui",
        min_value=0.0, max_value=30.0, step=0.5, format="%.1f",
    )
    ecol4, ecol5, ecol6 = st.columns(3)
    ecol4.number_input(
        "Insurance ($/yr)", key="insurance_annual",
        min_value=0.0, step=250.0, format="%.0f",
    )
    ecol5.number_input(
        "Utilities, owner-paid ($/yr)", key="utilities_annual",
        min_value=0.0, step=250.0, format="%.0f",
    )
    ecol6.number_input(
        "Reserves ($/unit/yr)", key="reserves_per_unit",
        min_value=0.0, step=50.0, format="%.0f",
        help="Multiplied by the number of units above.",
    )
    if property_type == "condo_townhome":
        st.number_input(
            "HOA fees ($/yr)", key="hoa_annual",
            min_value=0.0, step=300.0, format="%.0f",
            help="Condo/townhome HOA dues — the major line item for this type.",
        )
        st.caption("Condo defaults assume the HOA covers the structure, so owner-paid "
                   "**insurance and maintenance are intentionally low** to avoid double-counting "
                   "(the HOA fee already includes master insurance and exterior upkeep).")
    st.caption("The total operating expenses and the expense ratio appear in the results below.")
else:
    st.number_input(
        "Operating expenses, total ($/yr)", key="operating_expenses",
        min_value=0.0, step=1000.0, format="%.0f",
        help="A single all-in operating-expense number for a quick look.",
    )

# Assemble the deal so far (income + expenses + loan terms). Financing and the hold &
# exit assumptions are added below. NOI must be known before the loan can be sized.
current_deal = {
    "name": st.session_state["name"],
    "property_type": property_type,
    "purchase_price": purchase_price,
    "number_of_units": int(number_of_units),
    "income_mode": "detailed" if income_detailed else "simple",
    "gross_rental_income": gross_rental_income,
    "rent_roll": rent_roll,
    "vacancy_rate": vacancy_pct / 100.0,
    "annual_interest_rate": interest_pct / 100.0,
    "amortization_years": int(amortization_years),
    "expense_mode": "detailed" if expense_detailed else "simple",
    "operating_expenses": st.session_state["operating_expenses"],
    "property_tax_rate": st.session_state["property_tax_pct"] / 100.0,
    "insurance_annual": st.session_state["insurance_annual"],
    "hoa_annual": st.session_state["hoa_annual"],
    "management_pct": st.session_state["management_pct_ui"] / 100.0,
    "repairs_pct": st.session_state["repairs_pct_ui"] / 100.0,
    "utilities_annual": st.session_state["utilities_annual"],
    "reserves_per_unit": st.session_state["reserves_per_unit"],
    # Value-add (Phase C): renovate units toward market rent (market rents live in the
    # rent roll). No upside / no pace -> behaves like the flat-growth pro forma.
    "renovation_cost_per_unit": renovation_cost_per_unit,
    "renovation_pace": renovation_pace,
}
# NOI now, so the loan can be sized against it (Phase B). All math via the engine.
_gross_now = engine.resolve_gross_rental_income(current_deal)
noi_now = engine.calculate_noi(
    _gross_now, current_deal["vacancy_rate"],
    engine.build_operating_expenses(
        current_deal, _gross_now * (1 - current_deal["vacancy_rate"]))["total"],
)

# -----------------------------------------------------------------------------
# Financing (Phase B): manual loan/down, or size the loan from lender limits.
# -----------------------------------------------------------------------------
st.markdown("**Financing**")
financing_mode_label = st.radio(
    "Financing mode",
    ["Manual (enter loan / down payment)", "Size the loan (commercial)"],
    key="financing_mode_label", horizontal=True, label_visibility="collapsed",
    help="Manual: type the loan and down payment. Sized: the loan is the smaller of "
         "the max-LTV and min-DSCR limits, using this deal's NOI.",
)
financing_sized = financing_mode_label.startswith("Size")

if financing_sized:
    fin_col1, fin_col2 = st.columns(2)
    ltv_max = fin_col1.number_input(
        "Max LTV (%)", key="ltv_max_pct",
        min_value=1.0, max_value=100.0, step=1.0, format="%.1f",
        help="Loan can't exceed this share of the purchase price.",
    ) / 100.0
    dscr_min = fin_col2.number_input(
        "Min DSCR (x)", key="dscr_min_input",
        min_value=1.0, max_value=2.0, step=0.05, format="%.2f",
        help="NOI must cover annual debt service at least this many times.",
    )
    sizing = engine.size_loan(
        purchase_price, noi_now, current_deal["annual_interest_rate"],
        current_deal["amortization_years"], ltv_max, dscr_min,
    )
    loan_amount = sizing["sized_loan"]
    down_payment = sizing["down_payment"]
    st.caption(f"NOI used for sizing: **{_money(noi_now)}**  ·  binding constraint: "
               f"**{sizing['binding_constraint']}** (the loan is the smaller of the two limits).")
    size_cols = st.columns(4)
    size_cols[0].metric("LTV-constrained loan", _money(sizing["ltv_loan"]))
    size_cols[1].metric("DSCR-constrained loan", _money(sizing["dscr_loan"]))
    size_cols[2].metric("Sized loan", _money(sizing["sized_loan"]))
    size_cols[3].metric("Down payment", _money(sizing["down_payment"]))
    result_cols = st.columns(2)
    result_cols[0].metric(
        "Resulting DSCR",
        f"{sizing['resulting_dscr']:.2f}x" if sizing["resulting_dscr"] is not None else "n/a",
    )
    result_cols[1].metric("Resulting LTV", f"{sizing['resulting_ltv'] * 100:.1f}%")
else:
    fin_col1, fin_col2 = st.columns(2)
    loan_amount = fin_col1.number_input(
        "Loan amount ($)", key="loan_amount",
        min_value=0.0, step=5000.0, format="%.0f",
        help="Amount borrowed (principal).",
    )
    down_payment = fin_col2.number_input(
        "Down payment ($)", key="down_payment",
        min_value=0.0, step=5000.0, format="%.0f",
        help="Your out-of-pocket cash going in.",
    )
    ltv_max = st.session_state.get("ltv_max_pct", 75.0) / 100.0
    dscr_min = st.session_state.get("dscr_min_input", 1.25)

current_deal["financing_mode"] = "sized" if financing_sized else "manual"
current_deal["loan_amount"] = loan_amount
current_deal["down_payment"] = down_payment
current_deal["ltv_max"] = ltv_max
current_deal["dscr_min"] = dscr_min

st.markdown("**Hold & exit assumptions**")
hold_col, rentg_col, expg_col, exitcap_col, sellcost_col = st.columns(5)
hold_period_years = hold_col.number_input(
    "Hold (yrs)", key="hold_period_years",
    min_value=1, max_value=40, step=1,
    help="How many years you plan to own before selling.",
)
rent_growth_pct = rentg_col.number_input(
    "Rent growth (%/yr)", key="rent_growth_pct",
    min_value=-20.0, max_value=20.0, step=0.25, format="%.2f",
    help="Annual growth in gross rent.",
)
expense_growth_pct = expg_col.number_input(
    "Expense growth (%/yr)", key="expense_growth_pct",
    min_value=-20.0, max_value=20.0, step=0.25, format="%.2f",
    help="Annual growth in operating expenses.",
)
exit_cap_pct = exitcap_col.number_input(
    "Exit cap rate (%)", key="exit_cap_pct",
    min_value=0.10, max_value=30.0, step=0.10, format="%.2f",
    help="Cap rate a buyer pays at sale: sale price = final-year NOI / exit cap rate.",
)
sell_cost_pct = sellcost_col.number_input(
    "Selling costs (%)", key="sell_cost_pct",
    min_value=0.0, max_value=20.0, step=0.5, format="%.2f",
    help="Broker + closing costs at sale, as a % of the sale price.",
)

# Add the hold & exit assumptions to the deal (Phase 7-8): percent inputs -> fractions.
current_deal["hold_period_years"] = int(hold_period_years)
current_deal["rent_growth"] = rent_growth_pct / 100.0
current_deal["expense_growth"] = expense_growth_pct / 100.0
current_deal["exit_cap_rate"] = exit_cap_pct / 100.0
current_deal["selling_cost_pct"] = sell_cost_pct / 100.0

# Market trends (Phase 11): OPT-IN, a single extra API call, cached by zip. Placed
# next to the rent-growth input so real history sits beside the user's assumption.
with st.expander("📈 Market trends from RentCast (uses 1 API call)"):
    st.caption("Optional: compare your rent-growth assumption to the zip's recent "
               "history. Separate from the address lookup, cached per zip, never automatic.")
    st.session_state.setdefault("_trend_zip", _zip_from_address(st.session_state.get("_rc_address", "")))
    zip_col, btn_col = st.columns([2, 1])
    trend_zip = zip_col.text_input("Zip code", key="_trend_zip", max_chars=5,
                                   placeholder="5-digit zip")
    fetch_trends = btn_col.button("Get market trends", key="trends_btn", use_container_width=True)
    if fetch_trends:
        cleaned_zip = (trend_zip or "").strip()
        if not (cleaned_zip.isdigit() and len(cleaned_zip) == 5):
            st.warning("Enter a 5-digit zip code.")
        else:
            with st.spinner("Asking RentCast for market history…"):
                st.session_state["_trends"] = market_trends(cleaned_zip)  # 1 call, cached by zip
            st.session_state["_trends_zip"] = cleaned_zip
    _render_market_trends()


# -----------------------------------------------------------------------------
# Action buttons: run the underwriting, or save the deal
# -----------------------------------------------------------------------------
run_col, save_col = st.columns(2)
run_clicked = run_col.button(
    "Run underwriting", key="run_btn", type="primary", use_container_width=True
)
save_clicked = save_col.button("Save deal", key="save_btn", use_container_width=True)

if run_clicked:
    # Snapshot the inputs so the results stay put until the next run.
    st.session_state["_results_deal"] = current_deal
    st.session_state["_show_results"] = True

if save_clicked:
    if not current_deal["name"].strip():
        st.error("Give the deal a name before saving.")
    else:
        new_id = database.save_deal(current_deal)  # via database.py — no direct SQL
        st.session_state["_flash"] = f"Saved “{current_deal['name']}” as deal #{new_id}."
        st.rerun()


# -----------------------------------------------------------------------------
# Results
# -----------------------------------------------------------------------------
def _verdict_banner(text, color, subtitle):
    """Big, centered, color-coded BUY/PASS banner."""
    st.markdown(
        f"<div style='padding:0.85rem 1rem;border-radius:0.5rem;background:{color};"
        f"color:white;text-align:center;font-size:1.7rem;font-weight:700;"
        f"letter-spacing:0.06em'>{text}</div>",
        unsafe_allow_html=True,
    )
    st.caption(subtitle)


def render_results(deal):
    """Show the metrics, the buy/pass verdict, the checks, and the breakdowns."""
    # Fill in hold/exit assumptions if missing (e.g. a deal loaded from the database,
    # whose schema doesn't store them) so the multi-year engine calls always work.
    deal = {**_assumption_defaults(), **deal}

    try:
        results = compute_metrics(deal)
    except ZeroDivisionError:
        st.error(
            "Can't compute the ratios when purchase price, loan amount, or down "
            "payment is zero. Adjust those inputs and run again."
        )
        return

    evaluation = results["evaluation"]
    verdict = evaluation["verdict"]

    # Full-hold projection via the engine (never re-implemented here). Guarded so a
    # pathological input can't crash the page — the year-one results still render.
    try:
        pro_forma = engine.build_pro_forma(deal)
        exit_result = engine.calculate_exit(deal)
        multi_year_ok = True
    except (ZeroDivisionError, ValueError):
        pro_forma, exit_result, multi_year_ok = None, None, False

    st.divider()
    st.subheader(f"Results — {deal['name']}")

    # Overall verdict: the ONLY place the words BUY / PASS appear.
    if verdict == "BUY":
        _verdict_banner("BUY", "#1b5e20", "Clears every threshold below.")
    else:
        _verdict_banner("PASS", "#b71c1c", "Below one or more thresholds — see the checks below.")

    # Year-one headline metrics as labeled numbers.
    metric_cols = st.columns(4)
    metric_cols[0].metric("NOI (annual)", _money(results["noi"]))
    metric_cols[1].metric("Cap rate", f"{results['cap_rate'] * 100:.2f}%")
    metric_cols[2].metric("DSCR", f"{results['dscr']:.2f}x")
    metric_cols[3].metric("Cash-on-cash", f"{results['cash_on_cash'] * 100:.2f}%")

    # Full-hold return metrics, right below the year-one numbers (still up top).
    hold_years = int(deal["hold_period_years"])
    st.caption(f"Full-hold returns ({hold_years}-year)")
    return_cols = st.columns(2)
    if multi_year_ok and exit_result["irr_ok"]:
        return_cols[0].metric("IRR (annualized)", f"{exit_result['irr'] * 100:.1f}%")
    else:
        return_cols[0].metric("IRR (annualized)", "n/a")
    if multi_year_ok:
        return_cols[1].metric("Equity multiple", f"{exit_result['equity_multiple']:.2f}x")
    else:
        return_cols[1].metric("Equity multiple", "n/a")
    # Plain-English note in place of the IRR number when there's no real IRR.
    if multi_year_ok and not exit_result["irr_ok"]:
        st.caption(f"No real IRR — {exit_result['irr_reason']}")
    elif not multi_year_ok:
        st.caption("Couldn't project the hold from these inputs — check the hold/exit assumptions.")

    # Per-threshold checks. Each reads MEETS / BELOW — never "PASS", so the word
    # PASS only ever means "decline the deal" (the verdict above).
    st.markdown("**Threshold checks**")
    check_rows = []
    for check in evaluation["checks"]:
        value = _format_metric(check["value"], check["display"])
        minimum = _format_metric(check["minimum"], check["display"])
        result = "✅ MEETS" if check["passed"] else "❌ BELOW"
        check_rows.append([check["label"], value, f"min {minimum}", result])
    st.markdown(_markdown_table(["Check", "Value", "Threshold", "Result"], check_rows))

    # Income and financing breakdowns, side by side.
    income_col, financing_col = st.columns(2)

    with income_col:
        st.markdown("**Income**")
        gross = deal["gross_rental_income"]
        vacancy_pct_display = deal["vacancy_rate"] * 100
        st.markdown(_markdown_table(["Item", "Amount"], [
            ["Gross rental income (100% occ.)", _money(gross)],
            [f"Less vacancy ({vacancy_pct_display:.1f}%)", _money(-results["vacancy_loss"])],
            ["Effective gross income", _money(results["effective_gross_income"])],
            ["Less operating expenses", _money(-results["operating_expenses"])],
            ["Net operating income (NOI)", _money(results["noi"])],
        ]))

    with financing_col:
        st.markdown("**Financing**")
        loan_label = "Loan amount"
        if results.get("sizing"):
            loan_label = f"Loan amount (sized, {results['sizing']['binding_constraint']}-bound)"
        st.markdown(_markdown_table(["Item", "Amount"], [
            ["Purchase price", _money(deal["purchase_price"])],
            [loan_label, _money(results["loan_amount"])],
            ["Down payment (cash in)", _money(results["down_payment"])],
            ["Interest / amortization",
             f"{deal['annual_interest_rate'] * 100:.3f}% / {int(deal['amortization_years'])} yrs"],
            ["Monthly payment", _money(results["monthly_payment"])],
            ["Annual debt service", _money(results["annual_debt_service"])],
            ["Annual cash flow (NOI − debt)", _money(results["annual_cash_flow"])],
        ]))

    # Operating-expense breakdown + expense-ratio sanity flag (Phase 9).
    expense_result = results["expense_result"]
    st.markdown("**Operating expenses**")
    expense_rows = [[line["name"], line["basis"], _money(line["amount"])]
                    for line in expense_result["lines"]]
    expense_rows.append(["**Total**", "", f"**{_money(expense_result['total'])}**"])
    st.markdown(_markdown_table(["Line item", "Basis", "Amount"], expense_rows))

    ratio = expense_result["expense_ratio"]
    ratio_text = f"{ratio * 100:.1f}%"
    if ratio < engine.IMPLAUSIBLY_LOW_EXPENSE_RATIO:
        st.warning(
            f"⚠️ Expense ratio **{ratio_text}** looks implausibly low. Small multifamily "
            "usually runs ~35-50% of effective gross income — double-check the expense inputs."
        )
    elif ratio < engine.TYPICAL_EXPENSE_RATIO_LOW:
        st.caption(f"Expense ratio {ratio_text} — a touch below the typical 35-50% band.")
    else:
        st.caption(f"Expense ratio {ratio_text} — typical small multifamily runs ~35-50%.")

    # ----- Multi-year detail (Phase 7 pro forma + Phase 8 exit), below year-one -----
    if not multi_year_ok:
        return

    st.divider()
    st.markdown(f"### Full-hold detail ({hold_years}-year)")

    # Exit breakdown: sale price, selling costs, loan payoff, net proceeds.
    st.markdown("**Exit — sale at end of hold**")
    exit_cap_display = deal["exit_cap_rate"] * 100
    selling_cost_display = deal["selling_cost_pct"] * 100
    st.markdown(_markdown_table(["Item", "Amount"], [
        [f"Sale price (yr {hold_years} NOI / {exit_cap_display:.2f}% cap)",
         _money(exit_result["sale_price"])],
        [f"Less selling costs ({selling_cost_display:.1f}%)",
         _money(-exit_result["selling_costs"])],
        ["Less loan payoff", _money(-exit_result["ending_loan_balance"])],
        ["Net sale proceeds", _money(exit_result["net_sale_proceeds"])],
    ]))

    # Year-by-year pro forma table.
    st.markdown(f"**{hold_years}-year pro forma**")
    pro_forma_rows = []
    for row in pro_forma:
        pro_forma_rows.append({
            "Year": row["year"],
            "Gross rent": _money(row["gross_rental_income"]),
            "NOI": _money(row["noi"]),
            "Debt service": _money(row["annual_debt_service"]),
            "Cash flow": _money(row["annual_cash_flow"]),
            "End loan bal": _money(row["ending_loan_balance"]),
        })
    st.dataframe(pro_forma_rows, hide_index=True, use_container_width=True)

    # Annual cash flow chart — years 1..N, with the year-N sale spike included.
    st.markdown("**Annual cash flow by year**")
    stream = exit_result["cash_flow_stream"]  # [year0 = -down payment, cf1, ..., cfN + sale]
    chart_data = {
        "Year": list(range(1, len(stream))),
        "Cash flow": stream[1:],
    }
    st.bar_chart(chart_data, x="Year", y="Cash flow")
    st.caption(f"Year {hold_years} spikes because the net sale proceeds "
               f"({_money(exit_result['net_sale_proceeds'])}) land on top of that year's "
               "operating cash flow.")

    # ----- Value-add (Phase C): rent ramp + value creation, only if there's upside -----
    value_add = engine.value_add_summary(deal)
    if value_add["has_value_add"]:
        st.divider()
        st.markdown("### Value-add — forcing NOI up forces value up")
        va_cols = st.columns(4)
        va_cols[0].metric("Going-in NOI", _money(value_add["going_in_noi"]))
        va_cols[1].metric("Stabilized NOI", _money(value_add["stabilized_noi"]),
                          delta=_money(value_add["noi_lift"]))
        va_cols[2].metric("Implied value gain", _money(value_add["value_gain"]))
        va_cols[3].metric("Renovation cost", _money(value_add["total_renovation_cost"]))
        st.caption(
            f"{value_add['num_upside_units']} units with upside · stabilizes in year "
            f"{value_add['years_to_stabilize']} · value {_money(value_add['going_in_value'])} → "
            f"{_money(value_add['stabilized_value'])} at the {deal['exit_cap_rate'] * 100:.2f}% exit "
            "cap (stabilized NOI ÷ exit cap, in today's dollars)."
        )
        st.markdown("**Rent ramp**")
        ramp_rows = [{
            "Year": row["year"],
            "Renovated": f"{row['units_renovated_cumulative']}/{row['num_upside_units']}",
            "+ this yr": row["units_renovated_this_year"],
            "Gross rent": _money(row["gross_rental_income"]),
            "Reno capex": _money(row["renovation_capex"]),
            "NOI": _money(row["noi"]),
            "Cash flow": _money(row["annual_cash_flow"]),
        } for row in pro_forma]
        st.dataframe(ramp_rows, hide_index=True, use_container_width=True)

    # ----- Sensitivity: IRR across exit cap rate (cols) and rent growth (rows) -----
    st.divider()
    st.markdown("### Sensitivity — IRR")
    st.caption("Each cell is the IRR if those two assumptions held — exit cap rate "
               "across the columns, rent growth down the rows. Greener = higher IRR; "
               "a cell with no real IRR shows n/a.")

    sensitivity = engine.run_sensitivity(deal)  # default axes: exit cap vs rent growth
    col_labels = [f"{v * 100:.2f}%" for v in sensitivity["col_values"]]   # exit cap rate
    row_labels = [f"{v * 100:.1f}%" for v in sensitivity["row_values"]]   # rent growth
    # IRR as percent; None -> NaN so the gradient skips it and we render it as "n/a".
    numeric_grid = [[float("nan") if irr is None else irr * 100 for irr in row]
                    for row in sensitivity["irr_grid"]]
    grid_df = pd.DataFrame(numeric_grid, index=row_labels, columns=col_labels)
    grid_df.index.name = "rent growth ↓ / exit cap →"

    flat = [value for row in numeric_grid for value in row if not pd.isna(value)]
    vmin, vmax = (min(flat), max(flat)) if flat else (0.0, 0.0)
    styled = (grid_df.style
              .format(lambda v: "n/a" if pd.isna(v) else f"{v:.1f}%")
              .map(lambda v: _heat_color(v, vmin, vmax)))
    st.dataframe(styled, use_container_width=True)


if st.session_state.get("_show_results") and "_results_deal" in st.session_state:
    render_results(st.session_state["_results_deal"])
else:
    st.info("Fill in the deal and click **Run underwriting** to see the metrics and verdict.")
