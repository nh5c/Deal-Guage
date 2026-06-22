"""Underwriting engine for small multifamily deals.

Phase 1: pure-Python functions that compute the core underwriting metrics.
Phase 2: a configurable buy/pass decision built on top of those metrics.
Phase 7: a multi-year pro forma that projects the deal over a hold period.
Phase 8: the exit (sale) plus IRR and equity multiple over that hold.
Everything runs against one hardcoded sample deal and prints to the console.
No database, UI, or API.

Every number here is plain, deterministic math — you can trace each metric back
to its formula and inputs. There is no AI or ML in the underwriting itself.

Run it with:  python cre_underwriter/engine.py
"""

import math


# -----------------------------------------------------------------------------
# Buy / pass thresholds  (EDIT THESE)
#
# This is the one place to tune the decision. Each entry is the MINIMUM a deal
# must hit for that metric. A deal earns a "buy" only if it clears every one;
# change a number here and the verdict changes — no other code to touch.
#
# Starting defaults for a conservative small-multifamily buyer, and the reasoning:
#
#   min_cap_rate = 0.06  (6.0%)
#       Cap rate is the all-cash yield. Below ~6% you are paying a rich price for
#       the income, leaving little margin if rents soften or expenses climb. 6% is
#       a reasonable floor in most small-multifamily markets. Raise it to insist on
#       cheaper entry prices; lower it for hotter, low-cap markets.
#
#   min_dscr = 1.25  (1.25x)
#       DSCR is the lender's safety margin. Banks typically require 1.20-1.25 on
#       multifamily; a conservative buyer sits at the top of that range, so NOI
#       covers the mortgage with a 25% cushion against vacancy and surprise repairs.
#
#   min_cash_on_cash = 0.06  (6.0%)
#       The year-one cash yield on your own money. A conservative buyer wants it to
#       beat safe alternatives (~4-5% on cash/Treasuries) plus a premium for the
#       risk and work of owning property. 6% is a modest floor; many hold out for 8%+.
# -----------------------------------------------------------------------------
DEFAULT_THRESHOLDS = {
    "min_cap_rate": 0.06,         # 6.0%  — minimum all-cash yield (cap rate)
    "min_dscr": 1.25,             # 1.25x — minimum NOI-to-mortgage coverage
    "min_cash_on_cash": 0.06,     # 6.0%  — minimum year-one cash yield on cash in
}


# -----------------------------------------------------------------------------
# Net Operating Income (NOI)
#
# What it means:  The property's annual operating profit — the income left over
#                 after you collect the rent (minus expected vacancy) and pay the
#                 normal operating bills. It stops BEFORE the mortgage payment, so
#                 it describes the building itself, regardless of how you finance
#                 it. Two investors looking at the same building get the same NOI
#                 even if one pays all cash and the other borrows heavily.
#
# Formula:        NOI = Effective Gross Income - Operating Expenses
#                 Effective Gross Income = Gross Rental Income x (1 - vacancy_rate)
#                 (the rent you actually expect to collect after empty units)
#
# Inputs:         gross_rental_income  - total annual rent if 100% occupied
#                 vacancy_rate         - fraction expected empty (0.05 = 5%)
#                 operating_expenses   - annual taxes, insurance, repairs, mgmt,
#                                        etc.  (NOT the loan payment)
#
# Why it matters: NOI is the foundation of the whole analysis. Cap rate, DSCR,
#                 and cash flow all build on top of it. Lenders and buyers value
#                 a property as a multiple of its NOI.
# -----------------------------------------------------------------------------
def calculate_noi(gross_rental_income, vacancy_rate, operating_expenses):
    """Return annual net operating income (NOI) in dollars."""
    # The rent we realistically expect to collect once some units sit empty.
    vacancy_loss = gross_rental_income * vacancy_rate
    effective_gross_income = gross_rental_income - vacancy_loss

    # Operating profit, before any loan payment.
    noi = effective_gross_income - operating_expenses
    return noi


# -----------------------------------------------------------------------------
# Cap Rate (Capitalization Rate)
#
# What it means:  The unleveraged yield of the property — the year-one return you
#                 would earn if you paid all cash and took on no debt. It puts very
#                 different buildings on equal footing and is the market's main
#                 pricing yardstick ("this neighborhood trades at a 6% cap").
#
# Formula:        Cap Rate = NOI / Purchase Price
#
# Inputs:         noi             - net operating income (from calculate_noi)
#                 purchase_price  - the price you pay for the property
#
# Why it matters: It ties income to price. A higher cap rate is more income per
#                 dollar of price (cheaper, often riskier); a lower cap rate means
#                 paying more per dollar of income (pricier, often safer or higher
#                 growth). Buyers judge a deal by comparing its cap rate to what
#                 similar properties trade at.
#
# Returned as a decimal fraction: 0.069 means 6.9%.
# -----------------------------------------------------------------------------
def calculate_cap_rate(noi, purchase_price):
    """Return the cap rate as a decimal fraction (0.069 = 6.9%)."""
    cap_rate = noi / purchase_price
    return cap_rate


# -----------------------------------------------------------------------------
# Annual Debt Service
#
# What it means:  The total of the twelve monthly mortgage payments in a year —
#                 the yearly cost of the loan (principal + interest combined). We
#                 model a standard fully-amortizing loan with a fixed monthly
#                 payment, the way most mortgages work.
#
# Formula:        First the monthly payment, using the standard amortization
#                 (annuity) formula:
#
#                     monthly_payment = P * r / (1 - (1 + r) ** -n)
#
#                   P = loan_amount
#                   r = monthly interest rate = annual_interest_rate / 12
#                   n = total number of payments = amortization_years * 12
#
#                 Then:  annual_debt_service = monthly_payment * 12
#
# Inputs:         loan_amount           - amount borrowed (principal)
#                 annual_interest_rate  - yearly rate as a fraction (0.065 = 6.5%)
#                 amortization_years    - years to fully pay off the loan (e.g. 30)
#
# Why it matters: This is the cash you owe the bank every year. It is the bridge
#                 from NOI (the building's profit) to your own cash flow, and it
#                 is the denominator of DSCR — the number lenders care about most.
# -----------------------------------------------------------------------------
def calculate_annual_debt_service(loan_amount, annual_interest_rate, amortization_years):
    """Return the total annual mortgage payment (principal + interest) in dollars."""
    monthly_interest_rate = annual_interest_rate / 12
    number_of_payments = amortization_years * 12

    # A 0% loan is just the principal spread evenly across every payment.
    # Handling it separately also avoids dividing by zero in the formula below.
    if monthly_interest_rate == 0:
        monthly_payment = loan_amount / number_of_payments
    else:
        monthly_payment = (
            loan_amount
            * monthly_interest_rate
            / (1 - (1 + monthly_interest_rate) ** -number_of_payments)
        )

    annual_debt_service = monthly_payment * 12
    return annual_debt_service


# -----------------------------------------------------------------------------
# Debt Service Coverage Ratio (DSCR)
#
# What it means:  How many times over the property's NOI can cover its annual loan
#                 payment. A DSCR of 1.25 means the building earns $1.25 of
#                 operating profit for every $1.00 of mortgage due — a 25% cushion.
#
# Formula:        DSCR = NOI / Annual Debt Service
#
# Inputs:         noi                  - net operating income (from calculate_noi)
#                 annual_debt_service  - yearly loan payment (from above)
#
# Why it matters: This is the single most important number to a lender. Below 1.0,
#                 the property does not earn enough to pay its own mortgage. Most
#                 lenders require roughly 1.20-1.25 minimum on multifamily, as a
#                 safety margin against vacancies and surprise expenses.
#
# Returned as a plain ratio: 1.21 means 1.21x coverage.
# -----------------------------------------------------------------------------
def calculate_dscr(noi, annual_debt_service):
    """Return the debt service coverage ratio (1.21 = 1.21x coverage)."""
    dscr = noi / annual_debt_service
    return dscr


# -----------------------------------------------------------------------------
# Cash-on-Cash Return
#
# What it means:  The first-year cash yield on the money YOU actually put in.
#                 Unlike cap rate (which ignores the loan), this reflects your
#                 financing: it is the cash left after the mortgage, divided by
#                 your out-of-pocket cash (here, the down payment).
#
# Formula:        annual_cash_flow = NOI - Annual Debt Service
#                 Cash-on-Cash = annual_cash_flow / cash_invested
#
# Inputs:         noi                  - net operating income (from calculate_noi)
#                 annual_debt_service  - yearly loan payment (from above)
#                 cash_invested        - your out-of-pocket cash (the down payment;
#                                        in real life also add closing costs and
#                                        any up-front repairs/reserves)
#
# Why it matters: Cap rate tells you the building's return; cash-on-cash tells you
#                 YOUR return given the leverage you used. Borrowing can push this
#                 above the cap rate (helpful leverage) or below it (the loan costs
#                 more than the building earns). Investors compare it against other
#                 places they could park the same cash.
#
# Returned as a decimal fraction: 0.048 means 4.8%.
# -----------------------------------------------------------------------------
def calculate_cash_on_cash_return(noi, annual_debt_service, cash_invested):
    """Return the year-one cash-on-cash return as a decimal fraction (0.048 = 4.8%)."""
    # The cash that actually lands in your pocket after paying the mortgage.
    annual_cash_flow = noi - annual_debt_service

    cash_on_cash_return = annual_cash_flow / cash_invested
    return cash_on_cash_return


def _build_check(label, value, minimum, display):
    """Build one threshold-check record. 'display' is 'percent' or 'ratio'."""
    return {
        "label": label,
        "value": value,
        "minimum": minimum,
        "display": display,
        "passed": value >= minimum,    # a check passes when the metric meets its minimum
    }


# -----------------------------------------------------------------------------
# Buy / Pass Decision
#
# What it does:   Takes the three return/risk metrics the engine already computed
#                 and checks each against its minimum in the thresholds config. The
#                 deal earns a "BUY" only if it clears EVERY threshold; if even one
#                 fails, the verdict is "PASS" — meaning pass ON the deal, walk away.
#
#                 This is deliberately plain, deterministic logic: three numeric
#                 comparisons, no scoring and no model. You can read off exactly why
#                 a deal passed or failed.
#
# Inputs:         cap_rate, dscr, cash_on_cash_return  - the computed metrics
#                 thresholds  - the minimums to test against (DEFAULT_THRESHOLDS)
#
# Returns:        a dict with:
#                   "checks"     - one record per metric (label, value, minimum,
#                                  whether it passed, and how to display it)
#                   "passed_all" - True only if every check passed
#                   "verdict"    - "BUY" if passed_all else "PASS"
#
# Heads-up on the word "pass": a single check "passes" when it MEETS its minimum.
# The overall "PASS" verdict means the opposite — decline the deal. Same word, two
# senses; the printout labels checks PASS/FAIL and the verdict BUY/PASS.
# -----------------------------------------------------------------------------
def evaluate_deal(cap_rate, dscr, cash_on_cash_return, thresholds=DEFAULT_THRESHOLDS):
    """Compare each metric to its minimum threshold and return the checks + verdict."""
    checks = [
        _build_check("Cap rate", cap_rate, thresholds["min_cap_rate"], "percent"),
        _build_check("DSCR", dscr, thresholds["min_dscr"], "ratio"),
        _build_check(
            "Cash-on-cash return",
            cash_on_cash_return,
            thresholds["min_cash_on_cash"],
            "percent",
        ),
    ]

    # A buy requires clearing every single threshold — one failure means pass.
    passed_all = all(check["passed"] for check in checks)
    verdict = "BUY" if passed_all else "PASS"

    return {"checks": checks, "passed_all": passed_all, "verdict": verdict}


# -----------------------------------------------------------------------------
# Structured Operating Expenses  (Phase 9)
#
# Builds total operating expenses from standard small-multifamily line items, each
# entered on whatever basis is natural for it. There is also a "simple" escape
# hatch: if a deal just carries a single operating_expenses total, use that and
# skip the line items.
#
# Line items and their bases (per-type defaults in EXPENSE_TEMPLATES below):
#     Property taxes         - % of purchase price
#     Insurance              - flat $/yr
#     HOA fees               - flat $/yr  (a real value only for condos; else 0)
#     Property management    - % of effective gross income
#     Repairs & maintenance  - % of effective gross income
#     Utilities (owner-paid) - flat $/yr
#     Replacement reserves   - $/unit/yr  (flat fallback if no unit count)
#
# Property type (Phase 12) configures the EXPENSE TEMPLATE only — which default a
# line starts at. Every line is ALWAYS present in the data model (lines a type
# doesn't use simply default to 0, e.g. HOA for non-condos), so the engine keeps
# summing all lines with zero type-specific branching, and the NOI / cap / DSCR /
# pro forma / exit / IRR math is completely unchanged across types.
#
# The expense ratio (total operating expenses / EGI) is only REPORTED. Small
# multifamily typically runs ~35-50%; an unrealistically low ratio is a red flag,
# but the engine does not block on it — that judgment is left to the caller/UI.
# -----------------------------------------------------------------------------
EXPENSE_TEMPLATES = {
    "single_family_rental": {
        "property_tax_rate": 0.011,     # 1.1% of purchase price
        "insurance_annual": 1500.0,
        "hoa_annual": 0.0,
        "management_pct": 0.08,
        "repairs_pct": 0.08,            # higher: owner owns the roof + grounds
        "utilities_annual": 0.0,        # tenant pays
        "reserves_per_unit": 1500.0,    # higher: owner reserves for the structure
    },
    "condo_townhome": {
        "property_tax_rate": 0.011,
        "insurance_annual": 500.0,      # LOW: interior HO-6 only; HOA master policy covers structure
        "hoa_annual": 4800.0,           # the major expense
        "management_pct": 0.08,
        "repairs_pct": 0.03,            # LOW: HOA covers exterior / common areas
        "utilities_annual": 0.0,        # often HOA-covered
        "reserves_per_unit": 300.0,     # LOW: in-unit only; HOA reserves for the structure
    },
    "small_multifamily": {
        "property_tax_rate": 0.011,
        "insurance_annual": 4000.0,
        "hoa_annual": 0.0,
        "management_pct": 0.08,
        "repairs_pct": 0.05,
        "utilities_annual": 3000.0,     # owner-paid common utilities
        "reserves_per_unit": 300.0,
    },
    "larger_multifamily": {             # Phase A: like small MF, sized for a bigger building
        "property_tax_rate": 0.011,
        "insurance_annual": 8000.0,     # bigger building
        "hoa_annual": 0.0,
        "management_pct": 0.08,
        "repairs_pct": 0.05,
        "utilities_annual": 6000.0,     # more common-area utilities
        "reserves_per_unit": 300.0,
    },
}
DEFAULT_PROPERTY_TYPE = "small_multifamily"   # used when a deal has no property_type
DEFAULT_RESERVES_FLAT = 1200.0                # used when a deal has no unit count

# Human-readable labels for the UI (residential types only, for now).
PROPERTY_TYPE_LABELS = {
    "single_family_rental": "Single-family rental",
    "condo_townhome": "Condo / townhome",
    "small_multifamily": "Small multifamily",
    "larger_multifamily": "Larger multifamily (5+)",
}

# Income method (Phase A) defaults BY property type. Property type and income method
# are separate; they connect only through this smart default. The user can override.
RENT_ROLL_DEFAULT_TYPES = {"small_multifamily", "larger_multifamily"}


def default_income_mode(property_type):
    """Income input default for a property type: 'detailed' (rent roll) for
    multifamily, 'simple' (single gross-income total) for single-family / condo."""
    return "detailed" if property_type in RENT_ROLL_DEFAULT_TYPES else "simple"

# Typical small-multifamily operating-expense ratio band, for sanity checks.
TYPICAL_EXPENSE_RATIO_LOW = 0.35
TYPICAL_EXPENSE_RATIO_HIGH = 0.50
IMPLAUSIBLY_LOW_EXPENSE_RATIO = 0.30


def expense_template_for(deal):
    """The expense-default template for a deal's property_type (falls back to the
    default type if it's missing or unrecognized)."""
    property_type = deal.get("property_type") or DEFAULT_PROPERTY_TYPE
    return EXPENSE_TEMPLATES.get(property_type, EXPENSE_TEMPLATES[DEFAULT_PROPERTY_TYPE])


def _expense_setting(deal, template, key):
    """A line-item setting from the deal, falling back to the template if absent/None."""
    value = deal.get(key)
    return template[key] if value is None else value


def build_operating_expenses(deal, effective_gross_income):
    """Return the operating-expense breakdown, total, and expense ratio for a deal.

    Two modes:
      - "simple":   the deal carries a single operating_expenses total; use it.
      - "detailed": build the total from the standard line items above.

    Returns:
        {
            "mode": "simple" | "detailed",
            "lines": [{"name", "basis", "amount"}, ...],
            "total": float,
            "expense_ratio": float,   # total / EGI (just reported, never blocking)
        }
    """
    mode = deal.get("expense_mode")
    if mode not in ("simple", "detailed"):
        # Infer when unspecified: an explicit total means simple, else build detailed.
        mode = "simple" if deal.get("operating_expenses") is not None else "detailed"

    if mode == "simple":
        total = float(deal["operating_expenses"])
        lines = [{"name": "Operating expenses", "basis": "single total", "amount": total}]
    else:
        # Pick the per-type template; the deal's own values override it field by field.
        template = expense_template_for(deal)
        property_tax_rate = _expense_setting(deal, template, "property_tax_rate")
        management_pct = _expense_setting(deal, template, "management_pct")
        repairs_pct = _expense_setting(deal, template, "repairs_pct")

        property_taxes = deal["purchase_price"] * property_tax_rate
        insurance = float(_expense_setting(deal, template, "insurance_annual"))
        hoa = float(_expense_setting(deal, template, "hoa_annual"))
        management = effective_gross_income * management_pct
        repairs = effective_gross_income * repairs_pct
        utilities = float(_expense_setting(deal, template, "utilities_annual"))

        # Reserves: per-unit when we know the unit count, else a flat fallback.
        reserves_per_unit = _expense_setting(deal, template, "reserves_per_unit")
        number_of_units = deal.get("number_of_units")
        if number_of_units:
            reserves = reserves_per_unit * number_of_units
            reserves_basis = f"${reserves_per_unit:,.0f}/unit x {int(number_of_units)} units"
        else:
            reserves = DEFAULT_RESERVES_FLAT
            reserves_basis = "flat (no unit count)"

        # Every line is always present (HOA is 0 for non-condos), so the total is a
        # plain sum with no type-specific branching.
        lines = [
            {"name": "Property taxes", "basis": f"{property_tax_rate * 100:.2f}% of price", "amount": property_taxes},
            {"name": "Insurance", "basis": "flat $/yr", "amount": insurance},
            {"name": "HOA fees", "basis": "flat $/yr", "amount": hoa},
            {"name": "Property management", "basis": f"{management_pct * 100:.1f}% of EGI", "amount": management},
            {"name": "Repairs & maintenance", "basis": f"{repairs_pct * 100:.1f}% of EGI", "amount": repairs},
            {"name": "Utilities (owner-paid)", "basis": "flat $/yr", "amount": utilities},
            {"name": "Replacement reserves", "basis": reserves_basis, "amount": reserves},
        ]
        total = sum(line["amount"] for line in lines)

    expense_ratio = total / effective_gross_income if effective_gross_income else 0.0
    return {"mode": mode, "lines": lines, "total": total, "expense_ratio": expense_ratio}


# -----------------------------------------------------------------------------
# Rent Roll -> Gross Rental Income  (Phase A)
#
# This changes how income is ENTERED, not the engine math. Gross rental income can
# come from a per-unit rent roll (the detailed option) or stay a single number (the
# simple escape hatch). Everything downstream — EGI, NOI, cap rate, DSCR, pro forma,
# exit, IRR — is unchanged; it's just fed the summed rent roll instead of one field.
#
# A rent roll is a list of unit dicts:
#     {"label", "unit_type", "square_footage", "monthly_rent", "occupied"}
# A VACANT unit contributes 0 to current income but stays in the roll, so physical
# vacancy can be shown separately from the vacancy-rate assumption.
# -----------------------------------------------------------------------------
def gross_rental_income_from_rent_roll(rent_roll):
    """Annual gross rental income from a rent roll: sum of CURRENT monthly rents
    (a vacant unit contributes 0) x 12."""
    monthly = sum(
        (unit.get("monthly_rent") or 0.0)
        for unit in rent_roll
        if unit.get("occupied", True)
    )
    return monthly * 12


def summarize_rent_roll(rent_roll):
    """Roll-up stats for a rent roll, for display (units, occupancy, rent totals)."""
    unit_count = len(rent_roll)
    occupied_count = sum(1 for unit in rent_roll if unit.get("occupied", True))
    annual_gross = gross_rental_income_from_rent_roll(rent_roll)
    return {
        "unit_count": unit_count,
        "occupied_count": occupied_count,
        "vacant_count": unit_count - occupied_count,
        "occupied_monthly_rent": annual_gross / 12,
        "annual_gross_rental_income": annual_gross,
        "physical_occupancy": (occupied_count / unit_count) if unit_count else 0.0,
    }


def resolve_gross_rental_income(deal):
    """Gross annual rental income for a deal: from its rent roll (detailed income
    mode) or its single gross_rental_income field (simple mode / escape hatch).

    The rent roll is the source of truth in detailed mode, so this never goes stale.
    """
    mode = deal.get("income_mode")
    if mode not in ("simple", "detailed"):
        # Infer: a rent roll present means detailed, otherwise simple.
        mode = "detailed" if deal.get("rent_roll") else "simple"
    if mode == "detailed":
        return gross_rental_income_from_rent_roll(deal.get("rent_roll") or [])
    return float(deal.get("gross_rental_income") or 0.0)


def default_rent_roll(number_of_units, gross_rental_income):
    """Build a placeholder rent roll: N units splitting the annual gross evenly, all
    occupied. A starting point the user edits in the UI."""
    units = max(int(number_of_units or 1), 1)
    monthly_each = round((gross_rental_income or 0.0) / units / 12, 2)
    return [
        {"label": f"Unit {i + 1}", "unit_type": "", "square_footage": None,
         "monthly_rent": monthly_each, "occupied": True}
        for i in range(units)
    ]


# -----------------------------------------------------------------------------
# Commercial Loan Sizing  (Phase B)
#
# Commercial debt is set by whichever of two lender limits is more constraining:
#   - Max LTV:  loan <= ltv_max * purchase_price
#   - Min DSCR: NOI must cover debt service at least dscr_min times, so the most debt
#               service the deal supports is NOI / dscr_min; we back the loan out of
#               that debt service by inverting the amortization payment formula.
# The sized loan is the SMALLER of the two. Down payment = price - sized loan.
#
# This is a new financing MODE, not a replacement: a deal can still give loan_amount
# and down_payment directly (manual mode). The engine math downstream is unchanged;
# it's just handed a loan/down that was sized instead of typed in.
# -----------------------------------------------------------------------------
DEFAULT_LTV_MAX = 0.75
DEFAULT_DSCR_MIN = 1.25


def loan_from_annual_debt_service(annual_debt_service, annual_interest_rate, amortization_years):
    """Invert the amortization formula: the loan principal whose fixed payments total
    `annual_debt_service` per year. The exact inverse of calculate_annual_debt_service."""
    monthly_payment = annual_debt_service / 12
    monthly_interest_rate = annual_interest_rate / 12
    number_of_payments = amortization_years * 12

    # A 0% loan is just the payments stacked up; otherwise invert the annuity formula.
    if monthly_interest_rate == 0:
        return monthly_payment * number_of_payments
    return (
        monthly_payment
        * (1 - (1 + monthly_interest_rate) ** -number_of_payments)
        / monthly_interest_rate
    )


def size_loan(purchase_price, noi, annual_interest_rate, amortization_years,
              ltv_max=DEFAULT_LTV_MAX, dscr_min=DEFAULT_DSCR_MIN):
    """Size a commercial loan from the LTV and DSCR limits. Returns both constrained
    loans, which one binds, the final sized loan, the implied down payment, and the
    resulting DSCR and LTV at that loan."""
    # LTV cap.
    ltv_loan = ltv_max * purchase_price

    # DSCR cap: the most debt service NOI supports, backed out to a loan amount.
    max_annual_debt_service = (noi / dscr_min) if dscr_min else 0.0
    dscr_loan = loan_from_annual_debt_service(
        max(max_annual_debt_service, 0.0), annual_interest_rate, amortization_years
    )

    # The binding constraint is whichever allows the smaller loan.
    binding_constraint = "DSCR" if dscr_loan <= ltv_loan else "LTV"
    sized_loan = max(min(ltv_loan, dscr_loan), 0.0)
    down_payment = purchase_price - sized_loan

    # Resulting metrics at the sized loan.
    if sized_loan > 0:
        sized_debt_service = calculate_annual_debt_service(
            sized_loan, annual_interest_rate, amortization_years
        )
        resulting_dscr = (noi / sized_debt_service) if sized_debt_service else None
    else:
        resulting_dscr = None  # no debt -> DSCR undefined
    resulting_ltv = (sized_loan / purchase_price) if purchase_price else 0.0

    return {
        "ltv_loan": ltv_loan,
        "dscr_loan": dscr_loan,
        "binding_constraint": binding_constraint,
        "sized_loan": sized_loan,
        "down_payment": down_payment,
        "resulting_dscr": resulting_dscr,
        "resulting_ltv": resulting_ltv,
        "max_annual_debt_service": max_annual_debt_service,
    }


def resolve_financing(deal, noi):
    """Loan amount + down payment for a deal given its NOI. 'sized' mode computes them
    from the LTV/DSCR limits; 'manual' mode (the default) takes the deal's loan_amount
    and down_payment directly. Returns {loan_amount, down_payment, sizing} where sizing
    is the size_loan breakdown (or None in manual mode)."""
    if deal.get("financing_mode") == "sized":
        sizing = size_loan(
            deal["purchase_price"], noi, deal["annual_interest_rate"],
            deal["amortization_years"],
            deal.get("ltv_max", DEFAULT_LTV_MAX),
            deal.get("dscr_min", DEFAULT_DSCR_MIN),
        )
        return {
            "loan_amount": sizing["sized_loan"],
            "down_payment": sizing["down_payment"],
            "sizing": sizing,
        }
    return {
        "loan_amount": float(deal["loan_amount"]),
        "down_payment": float(deal["down_payment"]),
        "sizing": None,
    }


# -----------------------------------------------------------------------------
# Remaining Loan Balance
#
# What it means:  How much principal you still owe after making a given number of
#                 monthly payments. With a fixed amortizing loan, early payments
#                 are mostly interest, so the balance falls slowly at first and
#                 faster later. The gap between the price and this balance is the
#                 equity you've built — which Phase 8's sale will turn into cash.
#
# How:            Walk the loan forward one month at a time. Each month interest
#                 accrues on the current balance, and whatever is left of the fixed
#                 payment pays down principal:
#                     interest       = balance * monthly_rate
#                     principal_paid = monthly_payment - interest
#                     balance        = balance - principal_paid
#                 The fixed monthly payment is reused from calculate_annual_debt_service,
#                 so this never re-derives the amortization math.
#
# Inputs:         loan_amount, annual_interest_rate, amortization_years - the loan
#                 months_elapsed  - how many monthly payments have been made
#
# Why it matters: Tracks equity build-up over the hold; Phase 8 needs the balance
#                 at sale to compute net proceeds. Returned in dollars.
# -----------------------------------------------------------------------------
def calculate_remaining_loan_balance(loan_amount, annual_interest_rate,
                                     amortization_years, months_elapsed):
    """Return the principal still owed after `months_elapsed` monthly payments."""
    monthly_interest_rate = annual_interest_rate / 12
    monthly_payment = calculate_annual_debt_service(
        loan_amount, annual_interest_rate, amortization_years
    ) / 12

    balance = loan_amount
    for _ in range(months_elapsed):
        interest = balance * monthly_interest_rate
        principal_paid = monthly_payment - interest
        balance = balance - principal_paid

    # Guard tiny float drift (and an over-long horizon) from showing a negative balance.
    return max(balance, 0.0)


# -----------------------------------------------------------------------------
# Value-Add Modeling  (Phase C)
#
# Each unit can carry a market_rent (achievable after renovation) alongside its
# current monthly_rent. The investor renovates `renovation_pace` units per year at
# `renovation_cost_per_unit`, moving each from current to market rent — so gross
# income RAMPS from in-place toward market over several years instead of every unit
# jumping at once. Once every upside unit is renovated, the deal is "stabilized."
#
# Backward compatible: a deal with no market rents (or no rent roll / no pace)
# behaves exactly like the flat-growth pro forma — the ramp reduces to base gross
# times the rent-growth factor, with zero renovation capex.
# -----------------------------------------------------------------------------
def going_in_noi(deal):
    """Year-zero, in-place NOI (current rents, before any renovation) — the basis a
    lender underwrites at acquisition, and what the loan is sized on."""
    base_gross = resolve_gross_rental_income(deal)
    vacancy_rate = deal["vacancy_rate"]
    base_operating_expenses = build_operating_expenses(
        deal, base_gross * (1 - vacancy_rate)
    )["total"]
    return calculate_noi(base_gross, vacancy_rate, base_operating_expenses)


def _occupied_unit_rents(deal):
    """For a detailed-income deal, the (current, market) monthly rent of each OCCUPIED
    unit, in roll order. market >= current always (no upside => market == current).
    Empty list for simple-income deals, so the model falls back to flat growth."""
    if deal.get("income_mode") != "detailed":
        return []
    rents = []
    for unit in deal.get("rent_roll") or []:
        if not unit.get("occupied", True):
            continue
        current = float(unit.get("monthly_rent") or 0.0)
        market = unit.get("market_rent")
        market = float(market) if (market is not None and market > current) else current
        rents.append((current, market))
    return rents


def renovation_plan(deal):
    """Per-year value-add schedule over the hold period. Renovates `pace` upside units
    per year (current -> market), applies normal rent growth on top of whichever rent
    is in effect, and charges that year's renovation capex. With no upside it reduces
    to flat rent growth and zero capex. Returns one dict per year."""
    hold_period_years = deal["hold_period_years"]
    rent_growth = deal["rent_growth"]
    pace = float(deal.get("renovation_pace") or 0)
    cost_per_unit = float(deal.get("renovation_cost_per_unit") or 0)

    occupied_rents = _occupied_unit_rents(deal)
    num_upside_units = sum(1 for current, market in occupied_rents if market > current)
    value_add = num_upside_units > 0 and pace > 0
    base_gross = resolve_gross_rental_income(deal)

    schedule = []
    for year in range(1, hold_period_years + 1):
        growth = (1 + rent_growth) ** (year - 1)
        if value_add:
            # Cumulative units renovated by the END of this year (pace per year, capped).
            renovated_cumulative = min(int(pace * year + 1e-9), num_upside_units)
            renovated_prev = min(int(pace * (year - 1) + 1e-9), num_upside_units)
            units_this_year = renovated_cumulative - renovated_prev

            # Sum each occupied unit's in-effect rent (market if renovated, else current),
            # grown by the normal rent-growth factor.
            monthly = 0.0
            upside_rank = 0
            for current, market in occupied_rents:
                if market > current:
                    rent = market if upside_rank < renovated_cumulative else current
                    upside_rank += 1
                else:
                    rent = current
                monthly += rent * growth
            gross = monthly * 12
            capex = units_this_year * cost_per_unit
        else:
            renovated_cumulative = 0
            units_this_year = 0
            gross = base_gross * growth
            capex = 0.0

        schedule.append({
            "year": year,
            "gross_rental_income": gross,
            "units_renovated_cumulative": renovated_cumulative,
            "units_renovated_this_year": units_this_year,
            "renovation_capex": capex,
            "num_upside_units": num_upside_units,
            "fully_stabilized": (not value_add) or renovated_cumulative >= num_upside_units,
        })
    return schedule


def value_add_summary(deal):
    """Going-in vs stabilized NOI and the implied value created by renovations.
    'Stabilized' = every upside unit at market rent in TODAY's dollars (no growth),
    so the value gain isolates the forced-NOI lift, not market drift."""
    vacancy_rate = deal["vacancy_rate"]
    exit_cap_rate = deal.get("exit_cap_rate")  # may be absent on a freshly-loaded deal
    occupied_rents = _occupied_unit_rents(deal)
    num_upside_units = sum(1 for current, market in occupied_rents if market > current)

    base_gross = resolve_gross_rental_income(deal)
    base_operating_expenses = build_operating_expenses(
        deal, base_gross * (1 - vacancy_rate)
    )["total"]
    going_in = calculate_noi(base_gross, vacancy_rate, base_operating_expenses)

    # Stabilized gross: every occupied unit at its market rent (today's dollars).
    if occupied_rents:
        stabilized_gross = sum(market for _current, market in occupied_rents) * 12
    else:
        stabilized_gross = base_gross
    stabilized = calculate_noi(stabilized_gross, vacancy_rate, base_operating_expenses)

    going_in_value = (going_in / exit_cap_rate) if exit_cap_rate else 0.0
    stabilized_value = (stabilized / exit_cap_rate) if exit_cap_rate else 0.0
    pace = float(deal.get("renovation_pace") or 0)
    cost_per_unit = float(deal.get("renovation_cost_per_unit") or 0)
    years_to_stabilize = math.ceil(num_upside_units / pace) if (num_upside_units > 0 and pace > 0) else 0

    return {
        "has_value_add": num_upside_units > 0,
        "num_upside_units": num_upside_units,
        "going_in_noi": going_in,
        "stabilized_noi": stabilized,
        "noi_lift": stabilized - going_in,
        "going_in_value": going_in_value,
        "stabilized_value": stabilized_value,
        "value_gain": stabilized_value - going_in_value,
        "total_renovation_cost": num_upside_units * cost_per_unit,
        "years_to_stabilize": years_to_stabilize,
    }


# -----------------------------------------------------------------------------
# Multi-Year Pro Forma  (Phase 7)
#
# What it does:   Projects the deal year by year over the hold period. Income and
#                 expenses grow — at their own separate rates — while the mortgage
#                 payment does NOT. It reuses the year-one engine functions, so the
#                 projection can never drift away from the single-year math.
#
# Per year t (1 .. hold_period_years):
#     rent_factor    = (1 + rent_growth) ** (t - 1)
#     expense_factor = (1 + expense_growth) ** (t - 1)
#     gross rent     = base gross rent * rent_factor
#     EGI            = gross rent * (1 - vacancy_rate)
#     operating exp. = base operating expenses * expense_factor
#     NOI            = EGI - operating expenses        (via calculate_noi)
#     cash flow      = NOI - annual_debt_service       (debt service is constant)
#     ending balance = principal still owed after t * 12 payments
#
# At t = 1 both growth factors are 1.0, so year 1 reproduces the standalone
# single-year engine output exactly — a built-in correctness check.
#
# Inputs:         deal  - a deal dict that also carries hold_period_years,
#                         rent_growth, and expense_growth
# Returns:        a list of per-year dicts, year 1 first.
# -----------------------------------------------------------------------------
def build_pro_forma(deal):
    """Project a deal year by year over its hold period; return a list of dicts."""
    base_gross_rental_income = resolve_gross_rental_income(deal)
    vacancy_rate = deal["vacancy_rate"]
    annual_interest_rate = deal["annual_interest_rate"]
    amortization_years = deal["amortization_years"]
    hold_period_years = deal["hold_period_years"]
    rent_growth = deal["rent_growth"]
    expense_growth = deal["expense_growth"]

    # Resolve year-1 (base) operating expenses from the structured line items or the
    # simple total (Phase 9). The %-of-income lines use the year-1 effective gross
    # income; later years grow this base total by expense_growth, just as before.
    base_effective_gross_income = base_gross_rental_income * (1 - vacancy_rate)
    base_operating_expenses = build_operating_expenses(
        deal, base_effective_gross_income
    )["total"]

    # Resolve the loan (Phase B): a manual amount, or sized from the LTV/DSCR limits
    # using year-1 NOI. Sized once at acquisition, then fixed for the hold like any
    # commercial loan.
    base_noi = calculate_noi(base_gross_rental_income, vacancy_rate, base_operating_expenses)
    loan_amount = resolve_financing(deal, base_noi)["loan_amount"]

    # The mortgage payment is fixed for the life of the loan — compute it once and
    # reuse the same number every year.
    annual_debt_service = calculate_annual_debt_service(
        loan_amount, annual_interest_rate, amortization_years
    )

    # Value-add ramp (Phase C): per-year gross income + renovation capex. With no
    # market rents / no pace this is just base gross * rent-growth and zero capex,
    # so the pro forma is identical to before.
    schedule = renovation_plan(deal)

    pro_forma = []
    for year in range(1, hold_period_years + 1):
        # Expenses still grow flatly; gross comes from the value-add ramp.
        expense_factor = (1 + expense_growth) ** (year - 1)
        ramp = schedule[year - 1]

        gross_rental_income = ramp["gross_rental_income"]
        operating_expenses = base_operating_expenses * expense_factor

        # Rent we expect to collect after vacancy.
        effective_gross_income = gross_rental_income * (1 - vacancy_rate)

        # Reuse the engine's NOI logic instead of re-deriving it here.
        noi = calculate_noi(gross_rental_income, vacancy_rate, operating_expenses)

        # Cash flow is NOI less the fixed debt service AND this year's renovation capex.
        renovation_capex = ramp["renovation_capex"]
        annual_cash_flow = noi - annual_debt_service - renovation_capex

        # Principal still owed at the end of this year (year * 12 payments made).
        ending_loan_balance = calculate_remaining_loan_balance(
            loan_amount, annual_interest_rate, amortization_years, year * 12
        )

        pro_forma.append({
            "year": year,
            "gross_rental_income": gross_rental_income,
            "effective_gross_income": effective_gross_income,
            "operating_expenses": operating_expenses,
            "noi": noi,
            "annual_debt_service": annual_debt_service,
            "renovation_capex": renovation_capex,
            "units_renovated_cumulative": ramp["units_renovated_cumulative"],
            "units_renovated_this_year": ramp["units_renovated_this_year"],
            "num_upside_units": ramp["num_upside_units"],
            "annual_cash_flow": annual_cash_flow,
            "ending_loan_balance": ending_loan_balance,
        })

    return pro_forma


# -----------------------------------------------------------------------------
# Net Present Value (NPV) — helper for the IRR search below
#
# What it means:  Today's value of a dated stream of cash flows, discounted at an
#                 annual rate. A dollar next year is worth less than a dollar today,
#                 so each future amount is divided by (1 + rate) ** year.
#
# Inputs:         rate         - the annual discount rate (0.10 = 10%)
#                 cash_flows   - a year-indexed list: cash_flows[0] is the time-zero
#                                amount (usually negative — your cash in), and
#                                cash_flows[t] is the amount at the end of year t
# -----------------------------------------------------------------------------
def net_present_value(rate, cash_flows):
    """Discount a year-indexed cash flow stream to today at the given annual rate."""
    npv = 0.0
    for year, cash_flow in enumerate(cash_flows):
        npv += cash_flow / (1 + rate) ** year
    return npv


# -----------------------------------------------------------------------------
# Internal Rate of Return (IRR) — by bisection
#
# What it means:  The single annual rate that makes the NPV of the whole cash flow
#                 stream exactly zero — i.e. the deal's compounding return, taking
#                 the TIMING of every dollar into account.
#
# Why bisection (and not Newton's method): bisection is guaranteed to converge as
# long as the starting bracket contains a sign change in NPV. It can't diverge or
# oscillate the way Newton's method can on lumpy cash flows, and the logic is short
# and easy to read — repeatedly halve the range, always keeping the half that still
# straddles zero. The cost (a few extra iterations) doesn't matter here. This keeps
# the return math a transparent, deterministic calculation.
#
# Inputs:         cash_flows  - the year-indexed stream (year 0 negative, etc.)
#                 low_rate, high_rate        - the bracket to search (-99% .. 100%)
#                 tolerance, max_iterations  - stop when the range is tiny OR after a
#                                fixed number of steps, so it can never spin forever
#
# Returns:        {"ok": True,  "irr": rate}                     on success, or
#                 {"ok": False, "irr": None, "reason": "..."}    when NPV never
#                 changes sign across the bracket (no real IRR — e.g. a deal that
#                 never returns more than was invested).
# -----------------------------------------------------------------------------
def calculate_irr(cash_flows, low_rate=-0.99, high_rate=1.0,
                  tolerance=1e-7, max_iterations=200):
    """Return the IRR of a cash flow stream via bisection (see comment above)."""
    npv_low = net_present_value(low_rate, cash_flows)
    npv_high = net_present_value(high_rate, cash_flows)

    # A clean hit right at a bracket end.
    if npv_low == 0.0:
        return {"ok": True, "irr": low_rate}
    if npv_high == 0.0:
        return {"ok": True, "irr": high_rate}

    # Bisection needs the root bracketed: NPV must have OPPOSITE signs at the ends.
    # If both ends share a sign, no rate in this range zeroes NPV — there is no IRR.
    if (npv_low > 0) == (npv_high > 0):
        return {
            "ok": False,
            "irr": None,
            "reason": (f"NPV doesn't change sign between {low_rate:.0%} and "
                       f"{high_rate:.0%}, so there is no real IRR in this range "
                       "(the deal likely never returns more than was invested)."),
        }

    # Repeatedly test the midpoint; keep whichever half still contains the sign change.
    for _ in range(max_iterations):
        mid_rate = (low_rate + high_rate) / 2
        npv_mid = net_present_value(mid_rate, cash_flows)

        # Done: NPV is essentially zero, or the bracket has shrunk to nothing.
        if abs(npv_mid) < tolerance or (high_rate - low_rate) / 2 < tolerance:
            return {"ok": True, "irr": mid_rate}

        if (npv_mid > 0) == (npv_low > 0):
            low_rate, npv_low = mid_rate, npv_mid     # the root is in the upper half
        else:
            high_rate, npv_high = mid_rate, npv_mid   # the root is in the lower half

    # Hit the iteration cap (it can never loop forever) — return the best estimate.
    return {"ok": True, "irr": (low_rate + high_rate) / 2}


# -----------------------------------------------------------------------------
# Exit & Return Metrics  (Phase 8)
#
# What it does:   Models selling the property at the end of the hold and rolls the
#                 whole deal up into two headline numbers: IRR and equity multiple.
#                 It reuses build_pro_forma (yearly cash flows + the final-year NOI)
#                 and calculate_remaining_loan_balance (the payoff at sale) —
#                 nothing here is re-derived.
#
# The exit:
#     sale price        = final-year NOI / exit_cap_rate   (cap the income at sale)
#     selling costs     = sale price * selling_cost_pct     (broker + closing)
#     loan payoff       = balance still owed after the full hold
#     net sale proceeds = sale price - selling costs - loan payoff
#
# The equity cash-flow stream (what the returns are measured on):
#     year 0        = -down payment (your cash going in)
#     years 1..N-1  = each year's operating cash flow
#     year N        = final year's operating cash flow PLUS net sale proceeds
#
# Returns:
#     equity multiple = total cash returned / cash invested, where total cash
#                       returned = sum of all yearly cash flows + net sale proceeds.
#                       Ignores timing.
#     IRR             = the annual rate that zeroes the stream's NPV. Accounts for
#                       timing (a dollar at sale is worth less than a dollar in yr 1).
# -----------------------------------------------------------------------------
def calculate_exit(deal):
    """Compute the sale, the equity cash-flow stream, IRR, and equity multiple."""
    pro_forma = build_pro_forma(deal)
    hold_period_years = deal["hold_period_years"]
    exit_cap_rate = deal["exit_cap_rate"]
    selling_cost_pct = deal["selling_cost_pct"]

    # Resolve financing from GOING-IN NOI (Phase B/C) so the down payment and loan
    # payoff match the loan the pro forma sized — value-add ramps year-1 NOI above
    # going-in, but the loan is underwritten on the in-place income.
    financing = resolve_financing(deal, going_in_noi(deal))
    loan_amount = financing["loan_amount"]
    down_payment = financing["down_payment"]

    # Value the building at sale by capping its final-year NOI at the exit cap rate.
    final_year_noi = pro_forma[-1]["noi"]
    sale_price = final_year_noi / exit_cap_rate

    # Cost to sell, and the mortgage still owed at the end of the hold.
    selling_costs = sale_price * selling_cost_pct
    ending_loan_balance = calculate_remaining_loan_balance(
        loan_amount, deal["annual_interest_rate"],
        deal["amortization_years"], hold_period_years * 12,
    )

    # What the sale actually puts in your pocket.
    net_sale_proceeds = sale_price - selling_costs - ending_loan_balance

    # Build the equity cash-flow stream: cash out at year 0, operating cash flow
    # each year, and the sale stacked on top of the final year's cash flow.
    cash_flow_stream = [-down_payment]
    for row in pro_forma:
        cash_flow_stream.append(row["annual_cash_flow"])
    cash_flow_stream[-1] += net_sale_proceeds

    # Returns.
    total_cash_returned = (
        sum(row["annual_cash_flow"] for row in pro_forma) + net_sale_proceeds
    )
    equity_multiple = total_cash_returned / down_payment
    irr_result = calculate_irr(cash_flow_stream)

    return {
        "sale_price": sale_price,
        "selling_costs": selling_costs,
        "ending_loan_balance": ending_loan_balance,
        "net_sale_proceeds": net_sale_proceeds,
        "final_year_noi": final_year_noi,
        "cash_flow_stream": cash_flow_stream,
        "total_cash_returned": total_cash_returned,
        "equity_multiple": equity_multiple,
        "irr": irr_result["irr"],
        "irr_ok": irr_result["ok"],
        "irr_reason": irr_result.get("reason"),
    }


# -----------------------------------------------------------------------------
# Sensitivity Analysis  (Phase 10)
#
# Runs a two-variable grid: for every combination of two varied inputs, it copies
# the deal, overrides those two inputs, and asks the EXISTING engine for the IRR
# (via calculate_exit, which runs build_pro_forma + the exit math). No pro forma,
# exit, or IRR math is re-implemented here.
#
# Defaults to exit_cap_rate across the columns and rent_growth down the rows, but
# either axis can be any numeric deal input (e.g. annual_interest_rate). If a cell
# has no real IRR — or the inputs make the exit math blow up — that cell is None,
# and the grid still completes.
#
# Returns:
#     {
#         "col_variable", "col_values",        # columns (default: exit cap rate)
#         "row_variable", "row_values",        # rows    (default: rent growth)
#         "irr_grid",                          # row-major: irr_grid[row][col] = IRR or None
#         "base_row_index", "base_col_index",  # cell nearest the deal's current values
#     }
# -----------------------------------------------------------------------------
DEFAULT_SENSITIVITY_STEPS = {
    "exit_cap_rate": 0.005,         # half a point per step
    "rent_growth": 0.01,            # one point per step
    "expense_growth": 0.01,
    "annual_interest_rate": 0.005,
    "vacancy_rate": 0.01,
}


def _sensitivity_axis_values(deal, variable, count=5):
    """Build `count` values centered on the deal's current value for `variable`."""
    center = deal[variable]
    step = DEFAULT_SENSITIVITY_STEPS.get(variable, abs(center) * 0.1 or 0.01)
    half = count // 2
    return [center + (i - half) * step for i in range(count)]


def run_sensitivity(deal, col_variable="exit_cap_rate", col_values=None,
                    row_variable="rent_growth", row_values=None):
    """Return a two-variable IRR sensitivity grid (see section comment above)."""
    if col_values is None:
        col_values = _sensitivity_axis_values(deal, col_variable)
    if row_values is None:
        row_values = _sensitivity_axis_values(deal, row_variable)

    irr_grid = []
    for row_value in row_values:
        row_cells = []
        for col_value in col_values:
            # Copy the deal and override only the two varied inputs.
            cell_deal = dict(deal)
            cell_deal[row_variable] = row_value
            cell_deal[col_variable] = col_value
            try:
                exit_result = calculate_exit(cell_deal)
                irr = exit_result["irr"] if exit_result["irr_ok"] else None
            except (ZeroDivisionError, ValueError):
                # e.g. a zero / negative exit cap rate; keep the grid intact.
                irr = None
            row_cells.append(irr)
        irr_grid.append(row_cells)

    # The cell nearest the deal's current values — the demo checks that it matches
    # the IRR the engine already produces for the base deal.
    base_col_index = min(range(len(col_values)),
                         key=lambda i: abs(col_values[i] - deal[col_variable]))
    base_row_index = min(range(len(row_values)),
                         key=lambda i: abs(row_values[i] - deal[row_variable]))

    return {
        "col_variable": col_variable,
        "col_values": col_values,
        "row_variable": row_variable,
        "row_values": row_values,
        "irr_grid": irr_grid,
        "base_row_index": base_row_index,
        "base_col_index": base_col_index,
    }


# -----------------------------------------------------------------------------
# Market validation against comps  (Phase 11, NO API calls)
#
# A directional sanity check: compare one of the user's own numbers to the central
# tendency of the comparable properties the RentCast lookup ALREADY returned. This
# adds no API calls — the caller passes in comp values it already has.
#
# It is unit-agnostic on purpose: the caller must put the user's number in the SAME
# units as the comps (RentCast rent comps are monthly per unit; sale comps are whole
# sale prices). For multi-unit deals the comps skew toward single units, so treat
# the result as approximate.
#
# Returns: comp count, average + median, the % gap (positive = the user's number is
# above the comps), a direction, and material above/below flags. With no usable
# comps it returns {"ok": False, "reason": ...}.
# -----------------------------------------------------------------------------
COMP_MATERIAL_GAP = 0.08   # gaps beyond ~8% from the comps are flagged as material


def _median(values):
    """Median of a non-empty list of numbers."""
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def compare_to_comps(user_value, comp_values, material_gap=COMP_MATERIAL_GAP):
    """Compare the user's figure to the median of comparable values (see above)."""
    usable = [v for v in comp_values if v is not None and v > 0]
    if not usable or not user_value or user_value <= 0:
        return {"ok": False, "reason": "no usable comps to compare against"}

    average = sum(usable) / len(usable)
    median = _median(usable)
    gap_pct = (user_value - median) / median   # compare to the (robust) median

    if gap_pct > material_gap:
        direction = "above"
    elif gap_pct < -material_gap:
        direction = "below"
    else:
        direction = "in-line"

    return {
        "ok": True,
        "user_value": user_value,
        "comp_count": len(usable),
        "comp_average": average,
        "comp_median": median,
        "gap_pct": gap_pct,
        "direction": direction,
        "materially_above": gap_pct > material_gap,
        "materially_below": gap_pct < -material_gap,
    }


# -----------------------------------------------------------------------------
# Sample deal: one realistic small-multifamily property, hardcoded.
#
# "Maple Street Fourplex" — a 4-unit building. The values below are the raw inputs
# an underwriter would gather; every metric is computed from them. Note that
# loan_amount + down_payment = purchase_price (25% down, no closing costs financed).
# -----------------------------------------------------------------------------
sample_deal = {
    "name": "Maple Street Fourplex",
    "purchase_price": 625_000,
    "number_of_units": 4,             # used for per-unit replacement reserves
    "gross_rental_income": 69_600,    # 4 units x $1,450/mo x 12 months
    # Income method (Phase A): "simple" uses the single gross above; "detailed" sums
    # the rent roll. Kept "simple" here so the engine reproduces the original metrics.
    "income_mode": "simple",
    "rent_roll": [
        {"label": "Unit 1", "unit_type": "2BR/1BA", "square_footage": 850, "monthly_rent": 1450, "occupied": True},
        {"label": "Unit 2", "unit_type": "2BR/1BA", "square_footage": 850, "monthly_rent": 1450, "occupied": True},
        {"label": "Unit 3", "unit_type": "2BR/1BA", "square_footage": 850, "monthly_rent": 1450, "occupied": True},
        {"label": "Unit 4", "unit_type": "2BR/1BA", "square_footage": 850, "monthly_rent": 1450, "occupied": True},
    ],
    "vacancy_rate": 0.05,             # expect 5% of unit-time empty over the year
    # Operating expenses (Phase 9). "detailed" builds the total from the line items
    # below; "simple" would instead use the single operating_expenses total.
    "property_type": "small_multifamily",  # Phase 12: configures the expense template
    "expense_mode": "detailed",
    "operating_expenses": 23_000,     # simple-mode fallback total (~35% of rent)
    "property_tax_rate": 0.011,       # 1.1% of purchase price
    "insurance_annual": 4_000,        # flat $/yr
    "hoa_annual": 0,                  # no HOA for multifamily
    "management_pct": 0.08,           # 8% of effective gross income
    "repairs_pct": 0.05,              # 5% of effective gross income
    "utilities_annual": 3_000,        # flat $/yr (owner-paid)
    "reserves_per_unit": 300,         # $/unit/yr
    "loan_amount": 468_750,           # borrowing 75% of the price
    "annual_interest_rate": 0.065,    # 6.5% fixed
    "amortization_years": 30,         # paid off over 30 years
    "down_payment": 156_250,          # 25% of the price — your cash in
    # Financing mode (Phase B): "manual" uses loan_amount/down_payment above; "sized"
    # computes them from the LTV/DSCR limits below.
    "financing_mode": "manual",
    "ltv_max": 0.75,                  # max loan-to-value if sized
    "dscr_min": 1.25,                 # min DSCR if sized
    # Value-add (Phase C): renovate units toward market rent. The sample has no market
    # rents and no plan, so it behaves exactly as before (no upside, no capex).
    "renovation_cost_per_unit": 0,
    "renovation_pace": 0,
    # Pro forma assumptions (Phase 7): how long we hold, and how fast rents and
    # operating costs grow each year.
    "hold_period_years": 5,           # plan to own for 5 years before selling
    "rent_growth": 0.03,              # rents rise ~3%/yr
    "expense_growth": 0.025,          # operating costs rise ~2.5%/yr
    # Exit assumptions (Phase 8): how we value the sale and what selling costs.
    "exit_cap_rate": 0.065,           # cap rate a buyer pays at sale (6.5%)
    "selling_cost_pct": 0.06,         # broker + closing costs at sale (6%)
}


def summarize_deal(deal):
    """Run every metric on a deal dict and print a clean labeled summary."""
    # Pull the raw inputs into plainly named locals.
    gross_rental_income = resolve_gross_rental_income(deal)
    vacancy_rate = deal["vacancy_rate"]
    purchase_price = deal["purchase_price"]
    annual_interest_rate = deal["annual_interest_rate"]
    amortization_years = deal["amortization_years"]

    # Resolve operating expenses from the structured line items or a simple total.
    effective_gross_income = gross_rental_income * (1 - vacancy_rate)
    operating_expenses = build_operating_expenses(deal, effective_gross_income)["total"]

    # Run the engine, chaining each metric onto the ones before it.
    noi = calculate_noi(gross_rental_income, vacancy_rate, operating_expenses)
    cap_rate = calculate_cap_rate(noi, purchase_price)
    # Resolve financing (Phase B): manual amount, or sized from LTV/DSCR using NOI.
    financing = resolve_financing(deal, noi)
    loan_amount = financing["loan_amount"]
    down_payment = financing["down_payment"]
    annual_debt_service = calculate_annual_debt_service(
        loan_amount, annual_interest_rate, amortization_years
    )
    dscr = calculate_dscr(noi, annual_debt_service)
    cash_on_cash_return = calculate_cash_on_cash_return(
        noi, annual_debt_service, down_payment
    )

    # Phase 2: run the buy/pass decision on the metrics we just computed.
    evaluation = evaluate_deal(cap_rate, dscr, cash_on_cash_return)

    # A few intermediate numbers, recomputed here only for the printout.
    vacancy_loss = gross_rental_income * vacancy_rate
    effective_gross_income = gross_rental_income - vacancy_loss
    monthly_payment = annual_debt_service / 12
    annual_cash_flow = noi - annual_debt_service

    # Small formatting helpers, scoped to this report.
    def money(amount):
        sign = "-" if amount < 0 else ""
        return f"{sign}${abs(amount):,.0f}"

    def line(label, value):
        print(f"    {label:<32}{value:>14}")

    def metric_text(value, display):
        # Cap rate and cash-on-cash read as percentages; DSCR reads as a ratio.
        if display == "percent":
            return f"{value * 100:.2f}%"
        return f"{value:.2f}x"

    print()
    print("=" * 52)
    print(f"  UNDERWRITING SUMMARY  -  {deal['name']}")
    print("=" * 52)

    print("\n  Income")
    line("Gross rental income (100% occ.)", money(gross_rental_income))
    line(f"Less vacancy ({vacancy_rate * 100:.0f}%)", money(-vacancy_loss))
    line("Effective gross income", money(effective_gross_income))
    line("Less operating expenses", money(-operating_expenses))
    line("Net operating income (NOI)", money(noi))

    print("\n  Financing")
    line("Purchase price", money(purchase_price))
    line("Loan amount", money(loan_amount))
    line("Down payment (cash invested)", money(down_payment))
    line(
        "Interest rate / amortization",
        f"{annual_interest_rate * 100:.2f}% / {amortization_years} yrs",
    )
    line("Monthly payment", money(monthly_payment))
    line("Annual debt service", money(annual_debt_service))
    line("Annual cash flow (NOI - debt)", money(annual_cash_flow))

    print("\n  Key metrics")
    line("Cap rate", f"{cap_rate * 100:.2f}%")
    line("DSCR", f"{dscr:.2f}x")
    line("Cash-on-cash return", f"{cash_on_cash_return * 100:.2f}%")

    print("\n  Buy / pass decision  (a buy must clear every threshold)")
    for check in evaluation["checks"]:
        actual = metric_text(check["value"], check["display"])
        minimum = metric_text(check["minimum"], check["display"])
        status = "PASS" if check["passed"] else "FAIL"
        print(f"    {check['label']:<20} {actual:>7}  min {minimum:>7}   {status}")

    if evaluation["passed_all"]:
        reason = "clears every threshold"
    else:
        reason = "one or more thresholds failed"
    print(f"\n  VERDICT:  {evaluation['verdict']}   ({reason})")
    print("=" * 52)


def print_pro_forma(deal):
    """Run the multi-year pro forma on a deal and print it as a labeled table."""
    pro_forma = build_pro_forma(deal)
    hold_period_years = deal["hold_period_years"]

    def money(amount):
        sign = "-" if amount < 0 else ""
        return f"{sign}${abs(amount):,.0f}"

    header = (
        f"  {'Yr':>2}  {'Gross rent':>12}  {'EGI':>12}  {'Op ex':>11}  "
        f"{'NOI':>11}  {'Debt svc':>11}  {'Cash flow':>11}  {'End loan bal':>13}"
    )
    width = len(header)

    print()
    print("=" * width)
    print(f"  {hold_period_years}-YEAR PRO FORMA  -  {deal['name']}")
    print(f"  rent +{deal['rent_growth'] * 100:.1f}%/yr | "
          f"expenses +{deal['expense_growth'] * 100:.1f}%/yr | "
          f"vacancy {deal['vacancy_rate'] * 100:.0f}% | debt service fixed")
    print("=" * width)
    print(header)
    print("  " + "-" * (width - 2))
    for row in pro_forma:
        print(
            f"  {row['year']:>2}  "
            f"{money(row['gross_rental_income']):>12}  "
            f"{money(row['effective_gross_income']):>12}  "
            f"{money(row['operating_expenses']):>11}  "
            f"{money(row['noi']):>11}  "
            f"{money(row['annual_debt_service']):>11}  "
            f"{money(row['annual_cash_flow']):>11}  "
            f"{money(row['ending_loan_balance']):>13}"
        )
    print("=" * width)

    # Correctness check: year 1 must equal the standalone single-year engine,
    # because both growth factors are 1.0 when t = 1.
    year_one = pro_forma[0]
    engine_noi = calculate_noi(
        deal["gross_rental_income"], deal["vacancy_rate"], deal["operating_expenses"]
    )
    engine_debt_service = calculate_annual_debt_service(
        deal["loan_amount"], deal["annual_interest_rate"], deal["amortization_years"]
    )
    engine_cash_flow = engine_noi - engine_debt_service
    year_one_matches = (
        abs(year_one["noi"] - engine_noi) < 1e-6
        and abs(year_one["annual_cash_flow"] - engine_cash_flow) < 1e-6
    )
    print(f"\n  Year-1 check vs single-year engine: "
          f"NOI {money(engine_noi)}, cash flow {money(engine_cash_flow)}  ->  "
          f"{'MATCH' if year_one_matches else 'MISMATCH'}")

    # Plain-English read on the leverage effect.
    first_year, last_year = pro_forma[0], pro_forma[-1]
    rent_change = last_year["gross_rental_income"] / first_year["gross_rental_income"] - 1
    noi_change = last_year["noi"] / first_year["noi"] - 1
    cash_flow_change = last_year["annual_cash_flow"] / first_year["annual_cash_flow"] - 1
    print(f"\n  Over the {hold_period_years}-year hold: gross rent +{rent_change * 100:.1f}%, "
          f"NOI +{noi_change * 100:.1f}%, cash flow +{cash_flow_change * 100:.1f}%.")
    print("  Cash flow climbs far faster than rent because the mortgage payment is")
    print("  fixed: it doesn't grow, so almost every dollar of NOI growth falls")
    print("  straight through to your cash flow. That amplification is leverage —")
    print("  it magnifies the upside here, and would magnify a downturn too.")


def print_exit(deal):
    """Run the exit + return metrics on a deal and print the full breakdown."""
    exit_result = calculate_exit(deal)
    pro_forma = build_pro_forma(deal)
    hold_period_years = deal["hold_period_years"]
    down_payment = deal["down_payment"]

    def money(amount):
        sign = "-" if amount < 0 else ""
        return f"{sign}${abs(amount):,.0f}"

    def line(label, value):
        print(f"  {label:<34}{value:>22}")

    width = 60
    print()
    print("=" * width)
    print(f"  EXIT & RETURNS  -  {deal['name']}")
    print(f"  sell after {hold_period_years} yrs at a {deal['exit_cap_rate'] * 100:.2f}% cap "
          f"| selling costs {deal['selling_cost_pct'] * 100:.0f}%")
    print("=" * width)

    print("\n  Sale at end of hold")
    line(f"Final-year (yr {hold_period_years}) NOI", money(exit_result["final_year_noi"]))
    line(f"Sale price (NOI / {deal['exit_cap_rate'] * 100:.2f}% cap)",
         money(exit_result["sale_price"]))
    line(f"Less selling costs ({deal['selling_cost_pct'] * 100:.0f}%)",
         money(-exit_result["selling_costs"]))
    line("Less loan payoff", money(-exit_result["ending_loan_balance"]))
    line("Net sale proceeds", money(exit_result["net_sale_proceeds"]))

    print("\n  Equity cash-flow stream (what IRR sees)")
    stream = exit_result["cash_flow_stream"]
    for year, cash_flow in enumerate(stream):
        if year == 0:
            note = "cash invested (-down payment)"
        elif year == hold_period_years:
            note = "operating cash flow + net sale proceeds"
        else:
            note = "operating cash flow"
        print(f"    Year {year}:  {money(cash_flow):>12}   {note}")

    print("\n  Returns")
    line("Total cash returned", money(exit_result["total_cash_returned"]))
    line("Equity multiple", f"{exit_result['equity_multiple']:.2f}x")
    if exit_result["irr_ok"]:
        line("IRR (annualized)", f"{exit_result['irr'] * 100:.2f}%")
    else:
        line("IRR (annualized)", "no real IRR")
    print("=" * width)

    # ---- Sanity checks the prompt asked to confirm ----
    print("\n  Sanity checks")
    year0_ok = abs(stream[0] - (-down_payment)) < 1e-6
    print(f"    year 0 == -down payment ({money(-down_payment)}):  "
          f"{'OK' if year0_ok else 'MISMATCH'}")

    final_cash_flow = pro_forma[-1]["annual_cash_flow"]
    expected_final = final_cash_flow + exit_result["net_sale_proceeds"]
    final_ok = abs(stream[-1] - expected_final) < 1e-6
    print(f"    final year == yr{hold_period_years} cash flow + net proceeds "
          f"({money(final_cash_flow)} + {money(exit_result['net_sale_proceeds'])}):  "
          f"{'OK' if final_ok else 'MISMATCH'}")

    multiple_ok = exit_result["equity_multiple"] > 1
    print(f"    equity multiple > 1x:  "
          f"{'OK' if multiple_ok else 'NO'} ({exit_result['equity_multiple']:.2f}x)")

    if exit_result["irr_ok"]:
        believable = 0 < exit_result["irr"] < 1.0  # between 0% and 100%/yr
        print(f"    IRR is a believable annual % (0-100%):  "
              f"{'OK' if believable else 'CHECK'} ({exit_result['irr'] * 100:.2f}%)")
    else:
        print(f"    IRR:  {exit_result['irr_reason']}")

    # ---- Plain-English read ----
    print("\n  What this means")
    print(f"  You put in {money(down_payment)} and got back about "
          f"{money(exit_result['total_cash_returned'])} over {hold_period_years} years.")
    print(f"  Equity multiple {exit_result['equity_multiple']:.2f}x: every $1 invested came back "
          f"as ${exit_result['equity_multiple']:.2f}, ignoring timing.")
    if exit_result["irr_ok"]:
        print(f"  IRR {exit_result['irr'] * 100:.1f}%: the annual compounding rate that makes those")
        print("  dated cash flows worth exactly your investment today — so it DOES count")
        print("  timing (a dollar at sale is worth less than a dollar in year 1). Most of")
        print("  the return is the sale itself, where NOI growth (capped at the exit rate)")
        print("  and five years of loan paydown both cash out at once.")
    print("=" * width)


def print_expenses(deal):
    """Phase 9 demo: show how total operating expenses are built from line items."""
    gross_rental_income = deal["gross_rental_income"]
    vacancy_rate = deal["vacancy_rate"]
    effective_gross_income = gross_rental_income * (1 - vacancy_rate)
    result = build_operating_expenses(deal, effective_gross_income)

    def money(amount):
        sign = "-" if amount < 0 else ""
        return f"{sign}${abs(amount):,.0f}"

    width = 62
    print()
    print("=" * width)
    print(f"  OPERATING EXPENSES ({result['mode']})  -  {deal['name']}")
    print(f"  effective gross income: {money(effective_gross_income)}")
    print("=" * width)
    for line in result["lines"]:
        print(f"  {line['name']:<26}{line['basis']:>22}{money(line['amount']):>12}")
    print("  " + "-" * (width - 2))
    print(f"  {'Total operating expenses':<26}{'':>22}{money(result['total']):>12}")
    ratio_pct = result["expense_ratio"] * 100
    print(f"  {'Expense ratio (opex / EGI)':<26}{'':>22}{ratio_pct:>11.1f}%")
    print("=" * width)

    # Confirm year-1 NOI stays in the ballpark of the prior $43,120 simple-total NOI.
    noi = calculate_noi(gross_rental_income, vacancy_rate, result["total"])
    in_band = TYPICAL_EXPENSE_RATIO_LOW <= result["expense_ratio"] <= TYPICAL_EXPENSE_RATIO_HIGH
    print(f"  Year-1 NOI from these expenses: {money(noi)}  (prior simple-total NOI: $43,120)")
    print(f"  Expense ratio {ratio_pct:.1f}% is "
          f"{'within' if in_band else 'OUTSIDE'} the typical 35-50% band.")
    print("=" * width)


def print_sensitivity(deal):
    """Phase 10 demo: print the IRR sensitivity grid (exit cap vs rent growth)."""
    result = run_sensitivity(deal)
    col_values = result["col_values"]   # exit cap rate (columns)
    row_values = result["row_values"]   # rent growth (rows)
    grid = result["irr_grid"]

    def cell_str(irr):
        return f"{'n/a':>10}" if irr is None else f"{irr * 100:>9.1f}%"

    print()
    print("=" * (9 + 10 * len(col_values)))
    print(f"  IRR SENSITIVITY  -  {deal['name']}")
    print(f"  columns = {result['col_variable']} (exit cap),  "
          f"rows = {result['row_variable']} (rent growth)")
    print("=" * (9 + 10 * len(col_values)))

    print(" " * 9 + "".join(f"{v * 100:>9.2f}%" for v in col_values))
    for r, row_value in enumerate(row_values):
        print(f"{row_value * 100:>7.1f}% " + "".join(cell_str(grid[r][c]) for c in range(len(col_values))))
    print("=" * (9 + 10 * len(col_values)))

    # Correctness check: the base / center cell must equal the engine's own IRR.
    base_r, base_c = result["base_row_index"], result["base_col_index"]
    center_irr = grid[base_r][base_c]
    engine_exit = calculate_exit(deal)
    engine_irr = engine_exit["irr"] if engine_exit["irr_ok"] else None
    match = (center_irr is not None and engine_irr is not None
             and abs(center_irr - engine_irr) < 1e-9)
    print(f"  Base cell [rent {row_values[base_r] * 100:.1f}%, "
          f"cap {col_values[base_c] * 100:.2f}%] IRR = {center_irr * 100:.2f}%")
    print(f"  calculate_exit IRR for the base deal     = {engine_irr * 100:.2f}%  ->  "
          f"{'MATCH' if match else 'MISMATCH'}")
    print("=" * (9 + 10 * len(col_values)))


if __name__ == "__main__":
    print_expenses(sample_deal)
    summarize_deal(sample_deal)
    print_pro_forma(sample_deal)
    print_exit(sample_deal)
    print_sensitivity(sample_deal)
