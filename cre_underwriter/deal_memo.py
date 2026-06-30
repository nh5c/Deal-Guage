"""Deal memo (Phase E).

Turn a completed underwrite into a one-page professional deal memo — shown on screen
and downloadable as a PDF. Three pieces:

    build_memo_data(deal, metrics, exit_result, value_add)
        Assemble a structured memo object from data the engine ALREADY produced. Pure
        assembly — it never recomputes a figure.

    generate_narrative(memo_data)
        One Anthropic API call (Haiku tier, the same model extraction.py uses) that
        writes a short plain-English summary FROM the numbers. If the key is missing or
        the call fails, it falls back to a template summary built from the same numbers.
        Never raises.

    render_pdf(memo_data, narrative_text)
        Render the memo to a clean one-page PDF (via fpdf2) and return the bytes.

Guiding principle (same as the rest of the app): the deterministic engine produces
every number AND the buy/pass verdict. The AI only DESCRIBES those results in prose —
it never computes a figure and never makes or changes the decision. Numbers in the
memo come straight from the engine's output, not from the model.

Try it directly (renders a sample PDF; uses AI narrative only if ANTHROPIC_API_KEY is set):
    python cre_underwriter/deal_memo.py
"""

import os

import requests

# engine (for labels) and extraction (for the shared model id / API constants). The
# try/except lets this run both inside the package and as a plain script.
try:
    from cre_underwriter import engine, extraction
except ModuleNotFoundError:
    import engine
    import extraction


# A short, focused narrative — a few paragraphs, not an essay.
NARRATIVE_MAX_TOKENS = 800


# -----------------------------------------------------------------------------
# Formatting helpers (presentation only — they never change a value)
# -----------------------------------------------------------------------------
def _money(amount):
    """Format a dollar amount like $69,600 or -$3,480 (None -> 'n/a')."""
    if amount is None:
        return "n/a"
    sign = "-" if amount < 0 else ""
    return f"{sign}${abs(amount):,.0f}"


def _pct(fraction, places=2):
    """Format a decimal fraction as a percent (0.069 -> '6.90%'); None -> 'n/a'."""
    if fraction is None:
        return "n/a"
    return f"{fraction * 100:.{places}f}%"


def _ratio(value):
    """Format a ratio like 1.25x; None -> 'n/a'."""
    if value is None:
        return "n/a"
    return f"{value:.2f}x"


# -----------------------------------------------------------------------------
# 1. Assemble the memo data object from already-computed results
# -----------------------------------------------------------------------------
def build_memo_data(deal, metrics, exit_result=None, value_add=None):
    """Assemble the structured memo object from data the engine already produced.

    Inputs (nothing is recomputed here):
        deal         - the deal dict in engine units (property facts + assumptions)
        metrics      - the dashboard's compute_metrics() output (NOI, cap rate, DSCR,
                       cash-on-cash, financing, expense breakdown, the evaluation, ...)
        exit_result  - engine.calculate_exit() output, or None if the hold couldn't be
                       projected (supplies IRR + equity multiple)
        value_add    - engine.value_add_summary() output, or None

    Returns a dict with: property, metrics, assumptions, value_add (or None), verdict.
    """
    number_of_units = int(deal.get("number_of_units") or 1)
    purchase_price = float(deal["purchase_price"])
    evaluation = metrics["evaluation"]

    property_type_key = deal.get("property_type") or engine.DEFAULT_PROPERTY_TYPE
    property_type_label = engine.PROPERTY_TYPE_LABELS.get(property_type_key, property_type_key)

    has_exit = bool(exit_result and exit_result.get("irr_ok"))

    memo = {
        "property": {
            "name": (deal.get("name") or "").strip() or "Untitled deal",
            "property_type": property_type_label,
            "purchase_price": purchase_price,
            "number_of_units": number_of_units,
            "price_per_unit": (purchase_price / number_of_units) if number_of_units else None,
        },
        "metrics": {
            "noi": metrics["noi"],
            "cap_rate": metrics["cap_rate"],
            "dscr": metrics["dscr"],
            "cash_on_cash": metrics["cash_on_cash"],
            "effective_gross_income": metrics["effective_gross_income"],
            "operating_expenses": metrics["operating_expenses"],
            "expense_ratio": metrics["expense_result"]["expense_ratio"],
            "annual_debt_service": metrics["annual_debt_service"],
            "annual_cash_flow": metrics["annual_cash_flow"],
            "loan_amount": metrics["loan_amount"],
            "down_payment": metrics["down_payment"],
            # Multi-year returns — None when the hold couldn't be projected.
            "irr": exit_result["irr"] if has_exit else None,
            "equity_multiple": exit_result["equity_multiple"] if exit_result else None,
        },
        "assumptions": {
            "vacancy_rate": deal["vacancy_rate"],
            "interest_rate": deal["annual_interest_rate"],
            "amortization_years": int(deal["amortization_years"]),
            "hold_period_years": int(deal["hold_period_years"]),
            "rent_growth": deal["rent_growth"],
            "expense_growth": deal["expense_growth"],
            "exit_cap_rate": deal["exit_cap_rate"],
            "selling_cost_pct": deal["selling_cost_pct"],
            "financing_mode": deal.get("financing_mode", "manual"),
        },
        "verdict": {
            "verdict": evaluation["verdict"],
            "passed_all": evaluation["passed_all"],
            "checks": [
                {
                    "label": check["label"],
                    "value": check["value"],
                    "minimum": check["minimum"],
                    "display": check["display"],
                    "passed": check["passed"],
                }
                for check in evaluation["checks"]
            ],
        },
        "value_add": None,
    }

    if value_add and value_add.get("has_value_add"):
        memo["value_add"] = {
            "num_upside_units": value_add["num_upside_units"],
            "going_in_noi": value_add["going_in_noi"],
            "stabilized_noi": value_add["stabilized_noi"],
            "noi_lift": value_add["noi_lift"],
            "value_gain": value_add["value_gain"],
            "total_renovation_cost": value_add["total_renovation_cost"],
            "years_to_stabilize": value_add["years_to_stabilize"],
        }

    return memo


# -----------------------------------------------------------------------------
# 2. The AI narrative (with a deterministic template fallback)
# -----------------------------------------------------------------------------
# The model only DESCRIBES the engine's results. It must not compute, invent, or
# decide anything — the verdict is the engine's, and the prose reports it as such.
NARRATIVE_SYSTEM_PROMPT = (
    "You are an analyst writing a brief, professional investment memo for a commercial "
    "real estate deal. A separate deterministic underwriting model has ALREADY computed "
    "every number and made the BUY/PASS decision. Your job is ONLY to describe those "
    "results in clear, plain English.\n\n"
    "Strict rules:\n"
    "- Use ONLY the figures provided below. Never invent, infer, estimate, or change a number.\n"
    "- You do NOT make, change, or second-guess the recommendation. The verdict was decided "
    "by the model. Report it as the model's conclusion — e.g. 'the model returns a BUY' or "
    "'the deal screens as a PASS'. NEVER write 'I recommend', 'I would', 'you should', "
    "'in my opinion', or otherwise present the decision as your own judgment or advice.\n"
    "- Plain text only: a few short paragraphs. No markdown, no headings, no bullet lists.\n"
    "- Professional, concise, neutral. No hype, no filler, no salesmanship."
)


def _facts_block(memo):
    """Render the memo numbers as a clean labeled text block for the model to describe."""
    prop = memo["property"]
    met = memo["metrics"]
    asm = memo["assumptions"]
    ver = memo["verdict"]

    lines = [
        "PROPERTY",
        f"- Name: {prop['name']}",
        f"- Type: {prop['property_type']}",
        f"- Units: {prop['number_of_units']}",
        f"- Purchase price: {_money(prop['purchase_price'])}",
        f"- Price per unit: {_money(prop['price_per_unit'])}",
        "",
        "YEAR-ONE METRICS",
        f"- Net operating income (NOI): {_money(met['noi'])}",
        f"- Cap rate: {_pct(met['cap_rate'])}",
        f"- DSCR: {_ratio(met['dscr'])}",
        f"- Cash-on-cash return: {_pct(met['cash_on_cash'])}",
        f"- Effective gross income: {_money(met['effective_gross_income'])}",
        f"- Operating expenses: {_money(met['operating_expenses'])} "
        f"(expense ratio {_pct(met['expense_ratio'], 1)})",
        f"- Loan amount: {_money(met['loan_amount'])}",
        f"- Down payment (cash in): {_money(met['down_payment'])}",
        f"- Annual debt service: {_money(met['annual_debt_service'])}",
        f"- Annual cash flow after debt: {_money(met['annual_cash_flow'])}",
        "",
        "FULL-HOLD RETURNS",
        f"- Hold period: {asm['hold_period_years']} years",
        f"- IRR (annualized): {_pct(met['irr'], 1) if met['irr'] is not None else 'no real IRR'}",
        f"- Equity multiple: {_ratio(met['equity_multiple'])}",
        "",
        "KEY ASSUMPTIONS",
        f"- Vacancy: {_pct(asm['vacancy_rate'], 1)}",
        f"- Interest rate: {_pct(asm['interest_rate'], 3)} over {asm['amortization_years']}-year amortization",
        f"- Financing mode: {asm['financing_mode']}",
        f"- Rent growth: {_pct(asm['rent_growth'], 1)}/yr; expense growth: {_pct(asm['expense_growth'], 1)}/yr",
        f"- Exit cap rate: {_pct(asm['exit_cap_rate'])}; selling costs: {_pct(asm['selling_cost_pct'], 1)}",
    ]

    if memo["value_add"]:
        va = memo["value_add"]
        lines += [
            "",
            "VALUE-ADD PLAN",
            f"- Units with rent upside: {va['num_upside_units']}",
            f"- Going-in NOI: {_money(va['going_in_noi'])}; stabilized NOI: {_money(va['stabilized_noi'])}",
            f"- NOI lift: {_money(va['noi_lift'])}; implied value gain: {_money(va['value_gain'])}",
            f"- Total renovation cost: {_money(va['total_renovation_cost'])}; "
            f"years to stabilize: {va['years_to_stabilize']}",
        ]

    lines += [
        "",
        "MODEL VERDICT (decided by the engine — do not change it)",
        f"- Verdict: {ver['verdict']}",
    ]
    for check in ver["checks"]:
        actual = _pct(check["value"]) if check["display"] == "percent" else _ratio(check["value"])
        minimum = _pct(check["minimum"]) if check["display"] == "percent" else _ratio(check["minimum"])
        status = "meets" if check["passed"] else "BELOW"
        lines.append(f"  - {check['label']}: {actual} (minimum {minimum}) — {status} the threshold")

    return "\n".join(lines)


def _narrative_prompt(memo):
    """The user-turn instruction: cover these points, using only the figures given."""
    return (
        "Write a short professional memo (3 to 5 short paragraphs) for this deal. Cover, "
        "in order:\n"
        "1. What the property is (name, type, unit count, price, and price per unit).\n"
        "2. The key return and risk metrics in plain English — NOI, cap rate, DSCR, "
        "cash-on-cash, and the projected IRR and equity multiple over the hold.\n"
        "3. The value-add plan, if one is present (units with upside, NOI lift, "
        "renovation cost, time to stabilize). Skip this if there is no value-add plan.\n"
        "4. The model's verdict and brief reasoning — which thresholds it cleared or "
        "missed. Report the verdict as the model's, not your own.\n\n"
        "Use ONLY these figures:\n\n"
        f"{_facts_block(memo)}"
    )


def _call_claude(system_prompt, user_text):
    """One text-in/text-out Messages API call. Returns {'ok': True, 'text': ...} or
    {'ok': False, 'error': ...}. Reads the key live; never raises."""
    api_key = os.environ.get(extraction.API_KEY_ENV_VAR)
    if not api_key:
        return {"ok": False, "error": "no API key"}

    headers = {
        "x-api-key": api_key,
        "anthropic-version": extraction.ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    payload = {
        "model": extraction.MODEL,
        "max_tokens": NARRATIVE_MAX_TOKENS,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_text}],
    }
    try:
        response = requests.post(
            extraction.MESSAGES_URL, headers=headers, json=payload,
            timeout=extraction.REQUEST_TIMEOUT_SECONDS,
        )
    except requests.exceptions.RequestException as exc:
        return {"ok": False, "error": f"Couldn't reach the Anthropic API: {exc}"}

    if response.status_code != 200:
        return {"ok": False, "error": f"Anthropic API error (HTTP {response.status_code})."}

    try:
        body = response.json()
    except ValueError:
        return {"ok": False, "error": "Unreadable response from the Anthropic API."}

    parts = [
        block.get("text", "")
        for block in (body.get("content") or [])
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    text = "".join(parts).strip()
    if not text:
        return {"ok": False, "error": "The Anthropic API returned an empty narrative."}
    return {"ok": True, "text": text}


def _fallback_narrative(memo):
    """A deterministic, template-based narrative built straight from the numbers, used
    when there's no API key or the call fails. Plain prose, same facts, no AI."""
    prop, met, asm, ver = memo["property"], memo["metrics"], memo["assumptions"], memo["verdict"]

    para1 = (
        f"{prop['name']} is a {prop['number_of_units']}-unit {prop['property_type'].lower()} "
        f"priced at {_money(prop['purchase_price'])} ({_money(prop['price_per_unit'])} per unit). "
        f"At that price the property produces net operating income of {_money(met['noi'])}, "
        f"a {_pct(met['cap_rate'])} cap rate."
    )

    para2 = (
        f"With financing in place, debt service runs {_money(met['annual_debt_service'])} a year, "
        f"leaving {_money(met['annual_cash_flow'])} of annual cash flow on {_money(met['down_payment'])} "
        f"of cash invested — a {_pct(met['cash_on_cash'])} cash-on-cash return and a "
        f"{_ratio(met['dscr'])} debt-service coverage ratio."
    )
    if met["irr"] is not None:
        para2 += (
            f" Over a {asm['hold_period_years']}-year hold the projected IRR is "
            f"{_pct(met['irr'], 1)} with a {_ratio(met['equity_multiple'])} equity multiple."
        )
    else:
        para2 += f" Over a {asm['hold_period_years']}-year hold the projection shows no positive IRR."

    paragraphs = [para1, para2]

    if memo["value_add"]:
        va = memo["value_add"]
        paragraphs.append(
            f"The plan is value-add: {va['num_upside_units']} units carry rent upside. Renovating them "
            f"lifts NOI from {_money(va['going_in_noi'])} to {_money(va['stabilized_noi'])} "
            f"(+{_money(va['noi_lift'])}) for about {_money(va['total_renovation_cost'])} of work over "
            f"{va['years_to_stabilize']} years, an implied value gain of {_money(va['value_gain'])}."
        )

    met_checks = ", ".join(
        f"{c['label'].lower()} {'meets' if c['passed'] else 'is below'} its minimum"
        for c in ver["checks"]
    )
    if ver["verdict"] == "BUY":
        verdict_para = (
            f"The underwriting model returns a BUY: the deal clears every threshold ({met_checks})."
        )
    else:
        verdict_para = (
            f"The underwriting model returns a PASS: one or more thresholds are not met ({met_checks}). "
            "A single miss is enough for the model to decline the deal."
        )
    paragraphs.append(verdict_para)

    return "\n\n".join(paragraphs)


def generate_narrative(memo_data):
    """Produce the memo narrative. Returns {'text': str, 'source': 'ai'|'template', 'error'?}.

    Tries the AI narrative (one API call); on a missing key or any failure it returns the
    deterministic template narrative instead, so a memo is always produced and nothing crashes.
    """
    if not extraction.has_api_key():
        return {"text": _fallback_narrative(memo_data), "source": "template",
                "error": "No ANTHROPIC_API_KEY set — used a template summary."}

    result = _call_claude(NARRATIVE_SYSTEM_PROMPT, _narrative_prompt(memo_data))
    if not result["ok"]:
        return {"text": _fallback_narrative(memo_data), "source": "template",
                "error": result["error"] + " Used a template summary instead."}
    return {"text": result["text"], "source": "ai"}


# -----------------------------------------------------------------------------
# 3. Render the memo to a one-page PDF
# -----------------------------------------------------------------------------
# fpdf2's core fonts are Latin-1 only, so map the few non-Latin-1 characters the AI
# prose tends to use (smart quotes, dashes, ellipsis) down to ASCII before drawing.
_PDF_REPLACEMENTS = {
    "—": "-", "–": "-", "‘": "'", "’": "'",
    "“": '"', "”": '"', "…": "...", "•": "-", " ": " ",
}


def _pdf_safe(text):
    """Make text safe for fpdf2's Latin-1 core fonts (replace smart punctuation)."""
    for source, target in _PDF_REPLACEMENTS.items():
        text = text.replace(source, target)
    return text.encode("latin-1", "replace").decode("latin-1")


def render_pdf(memo_data, narrative_text):
    """Render the memo to a clean one-page PDF and return the bytes."""
    from fpdf import FPDF   # imported here so the module loads even before fpdf2 is installed

    prop, met, asm, ver = (
        memo_data["property"], memo_data["metrics"],
        memo_data["assumptions"], memo_data["verdict"],
    )

    pdf = FPDF(format="Letter", unit="pt")
    pdf.set_auto_page_break(auto=True, margin=48)
    pdf.set_margins(48, 48, 48)
    pdf.add_page()
    width = pdf.w - 96   # usable content width (page width minus left+right margins)

    # ---- Header: property name + a one-line fact summary ----
    pdf.set_font("Helvetica", "B", 18)
    pdf.cell(0, 22, _pdf_safe(prop["name"]), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(90, 90, 90)
    subtitle = (f"{prop['property_type']}  -  {prop['number_of_units']} units  -  "
                f"{_money(prop['purchase_price'])}  ({_money(prop['price_per_unit'])}/unit)")
    pdf.cell(0, 14, _pdf_safe(subtitle), new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)
    pdf.ln(6)

    # ---- Verdict badge ----
    is_buy = ver["verdict"] == "BUY"
    pdf.set_fill_color(*(27, 94, 32) if is_buy else (183, 28, 28))   # green / red
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 13)
    label = f"MODEL VERDICT:  {ver['verdict']}"
    sub = "clears every threshold" if is_buy else "below one or more thresholds"
    pdf.cell(0, 26, _pdf_safe(f"   {label}     ({sub})"), new_x="LMARGIN", new_y="NEXT", fill=True)
    pdf.set_text_color(0, 0, 0)
    pdf.ln(10)

    # ---- Two-column metric/assumption section ----
    def section_title(text):
        pdf.set_font("Helvetica", "B", 11)
        pdf.cell(0, 16, _pdf_safe(text), new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 10)

    def kv_rows(pairs):
        """Two label:value pairs per line, each pair in half the content width."""
        col = width / 2
        for i in range(0, len(pairs), 2):
            chunk = pairs[i:i + 2]
            for label, value in chunk:
                pdf.set_font("Helvetica", "", 10)
                pdf.set_text_color(90, 90, 90)
                pdf.cell(col * 0.55, 15, _pdf_safe(label))
                pdf.set_text_color(0, 0, 0)
                pdf.set_font("Helvetica", "B", 10)
                pdf.cell(col * 0.45, 15, _pdf_safe(value))
            pdf.ln(15)

    irr_text = _pct(met["irr"], 1) if met["irr"] is not None else "no real IRR"
    section_title("Key metrics")
    kv_rows([
        ("NOI (annual)", _money(met["noi"])),
        ("Cap rate", _pct(met["cap_rate"])),
        ("DSCR", _ratio(met["dscr"])),
        ("Cash-on-cash", _pct(met["cash_on_cash"])),
        (f"IRR ({asm['hold_period_years']}-yr)", irr_text),
        ("Equity multiple", _ratio(met["equity_multiple"])),
        ("Loan amount", _money(met["loan_amount"])),
        ("Down payment", _money(met["down_payment"])),
        ("Annual cash flow", _money(met["annual_cash_flow"])),
        ("Operating expenses", _money(met["operating_expenses"])),
    ])
    pdf.ln(6)

    section_title("Key assumptions")
    kv_rows([
        ("Vacancy", _pct(asm["vacancy_rate"], 1)),
        ("Interest rate", _pct(asm["interest_rate"], 3)),
        ("Amortization", f"{asm['amortization_years']} yrs"),
        ("Hold period", f"{asm['hold_period_years']} yrs"),
        ("Rent growth", _pct(asm["rent_growth"], 1)),
        ("Expense growth", _pct(asm["expense_growth"], 1)),
        ("Exit cap rate", _pct(asm["exit_cap_rate"])),
        ("Selling costs", _pct(asm["selling_cost_pct"], 1)),
    ])
    pdf.ln(6)

    # ---- Value-add section (only when there's upside) ----
    if memo_data["value_add"]:
        va = memo_data["value_add"]
        section_title("Value-add plan")
        kv_rows([
            ("Units with upside", str(va["num_upside_units"])),
            ("Years to stabilize", str(va["years_to_stabilize"])),
            ("Going-in NOI", _money(va["going_in_noi"])),
            ("Stabilized NOI", _money(va["stabilized_noi"])),
            ("NOI lift", _money(va["noi_lift"])),
            ("Renovation cost", _money(va["total_renovation_cost"])),
            ("Implied value gain", _money(va["value_gain"])),
        ])
        pdf.ln(6)

    # ---- Narrative ----
    section_title("Summary")
    pdf.set_font("Helvetica", "", 10)
    for paragraph in (narrative_text or "").split("\n\n"):
        paragraph = paragraph.strip()
        if paragraph:
            pdf.multi_cell(width, 14, _pdf_safe(paragraph))
            pdf.ln(4)

    # ---- Footer disclaimer ----
    pdf.ln(2)
    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(120, 120, 120)
    pdf.multi_cell(width, 11, _pdf_safe(
        "All figures are computed by the deterministic underwriting engine; the summary is "
        "written by AI from those figures. The buy/pass result is a fixed threshold test, "
        "not investment advice."
    ))
    pdf.set_text_color(0, 0, 0)

    output = pdf.output()           # fpdf2 returns a bytearray when no destination is given
    return bytes(output)


# -----------------------------------------------------------------------------
# Manual smoke test: build a sample memo, generate a narrative (AI if a key is set,
# else the template fallback), and write a PDF to the scratch dir.
#     python cre_underwriter/deal_memo.py
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    # A sample "computed results" set, shaped exactly like compute_metrics() output.
    sample_metrics = {
        "noi": 42449.4,
        "cap_rate": 0.0679,
        "dscr": 1.19,
        "cash_on_cash": 0.0441,
        "annual_debt_service": 35556.0,
        "annual_cash_flow": 6893.4,
        "loan_amount": 468750.0,
        "down_payment": 156250.0,
        "effective_gross_income": 66120.0,
        "operating_expenses": 23670.6,
        "expense_result": {"expense_ratio": 0.358},
        "evaluation": {
            "verdict": "PASS",
            "passed_all": False,
            "checks": [
                {"label": "Cap rate", "value": 0.0679, "minimum": 0.06, "display": "percent", "passed": True},
                {"label": "DSCR", "value": 1.19, "minimum": 1.25, "display": "ratio", "passed": False},
                {"label": "Cash-on-cash return", "value": 0.0441, "minimum": 0.06, "display": "percent", "passed": False},
            ],
        },
    }
    sample_deal = {
        "name": "Maple Street Fourplex", "property_type": "small_multifamily",
        "purchase_price": 625000.0, "number_of_units": 4,
        "vacancy_rate": 0.05, "annual_interest_rate": 0.065, "amortization_years": 30,
        "hold_period_years": 5, "rent_growth": 0.03, "expense_growth": 0.025,
        "exit_cap_rate": 0.065, "selling_cost_pct": 0.06, "financing_mode": "manual",
    }
    sample_exit = {"irr_ok": True, "irr": 0.1566, "equity_multiple": 1.74}

    memo = build_memo_data(sample_deal, sample_metrics, sample_exit, value_add=None)
    narrative = generate_narrative(memo)
    print(f"Narrative source: {narrative['source']}")
    if narrative.get("error"):
        print(f"  ({narrative['error']})")
    print("\n----- NARRATIVE -----")
    print(narrative["text"])

    pdf_bytes = render_pdf(memo, narrative["text"])
    scratch = os.environ.get("TMPDIR", "/tmp")
    out_path = os.path.join(scratch, "sample_deal_memo.pdf")
    with open(out_path, "wb") as handle:
        handle.write(pdf_bytes)
    print(f"\nWrote {len(pdf_bytes):,} bytes -> {out_path}")
