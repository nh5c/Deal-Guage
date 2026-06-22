"""RentCast API client (Phase 5).

A thin, framework-agnostic wrapper around three RentCast endpoints:

    - Value Estimate  (GET /avm/value)          -> what the property is worth
    - Rent Estimate   (GET /avm/rent/long-term) -> what it should rent for (monthly)
    - Market Stats    (GET /markets)            -> a zip's historical rent/price trends

Both return an estimate, a low/high range, and a list of nearby comparable
properties ("comps") so you can sanity-check the number. Treat these as STARTING
ESTIMATES that validate your own inputs — not as final truth.

Design notes:
  - No Streamlit, no SQL, no engine import here. This module only knows how to
    talk to RentCast and hand back plain Python dicts. Caching is the caller's job
    (the dashboard wraps these in st.cache_data).
  - The API key is read from the RENTCAST_API_KEY environment variable, never
    hardcoded. If it is missing we return a friendly error, not a crash.
  - Every function returns a dict shaped like:
        {"ok": True,  "estimate": ..., "range_low": ..., "range_high": ...,
         "subject": {...}, "comps": [{...}, ...]}
    or, on any failure:
        {"ok": False, "error_type": "...", "error": "friendly message"}
    so the caller can always display something instead of blowing up.

Try the client directly (makes two real API calls — counts against your quota):
    RENTCAST_API_KEY=xxx python cre_underwriter/rentcast.py "123 Main St, Austin, TX, 78701"
"""

import os
import sys

import requests


API_KEY_ENV_VAR = "RENTCAST_API_KEY"
BASE_URL = "https://api.rentcast.io/v1"
VALUE_ENDPOINT = "/avm/value"
RENT_ENDPOINT = "/avm/rent/long-term"
MARKET_ENDPOINT = "/markets"            # market statistics by zip (Phase 11)
# Request far more history than RentCast actually has so it returns ALL that's
# available: the docs cap the response to months that exist and document no maximum
# on historyRange (Phase 12). We then compute over whatever window actually comes back.
MAX_HISTORY_MONTHS = 600

# RentCast's AVM can take a few seconds; give it room but don't hang forever.
REQUEST_TIMEOUT_SECONDS = 20

# How many comps to pull. The API minimum is 5, which is plenty to eyeball and
# keeps responses small. (compCount does not change cost — every call is one
# request against your monthly quota regardless.)
DEFAULT_COMP_COUNT = 5


def has_api_key():
    """True if RENTCAST_API_KEY is set, so the UI can warn when it isn't."""
    return bool(os.environ.get(API_KEY_ENV_VAR))


def _error(error_type, message):
    """The standard failure shape the caller knows how to display."""
    return {"ok": False, "error_type": error_type, "error": message}


# The "no API key" result lives in one place so callers can return it directly.
# Crucially, the dashboard returns this WITHOUT caching it — a no-key state must
# never be cached, or a key you set later would be ignored for addresses already
# tried while keyless.
MISSING_KEY_MESSAGE = (
    "No RentCast API key found. Set the RENTCAST_API_KEY environment variable "
    "(a free key is at developers.rentcast.io) and restart the app."
)


def missing_key_error():
    """Return the standard missing-key error result dict."""
    return _error("missing_key", MISSING_KEY_MESSAGE)


def _friendly_message(response):
    """Turn a non-200 RentCast response into a plain-English message."""
    # RentCast error bodies look like {"status", "error", "message"}; surface the
    # message when we can, but never let parsing failure hide the real problem.
    detail = ""
    try:
        body = response.json()
        if isinstance(body, dict) and body.get("message"):
            detail = f" (RentCast: {body['message']})"
    except ValueError:
        detail = ""

    code = response.status_code
    if code == 400:
        return "That address didn't parse. Use “Street, City, State, Zip”." + detail
    if code == 401:
        return ("RentCast rejected the API key. Check RENTCAST_API_KEY and that your "
                "subscription/billing is active (a used-up free quota can look like "
                "this too)." + detail)
    if code == 404:
        return "RentCast has no data for that address. Try a full, nearby address." + detail
    if code == 429:
        return ("Too many requests too fast (RentCast allows ~20/second). Wait a "
                "moment and try again — and remember the free plan is 50 calls a "
                "month, so look up sparingly." + detail)
    if code in (500, 504):
        return "RentCast had a server error or timed out. Try again in a moment." + detail
    return f"RentCast returned an unexpected error (HTTP {code})." + detail


def _request(endpoint, params):
    """Make one authenticated GET. Returns {'ok': True, 'data': json} or an error."""
    # Read the key LIVE on every request (never captured at import), so a key set
    # before launching Streamlit is always seen by the running process.
    api_key = os.environ.get(API_KEY_ENV_VAR)
    if not api_key:
        return missing_key_error()

    headers = {"X-Api-Key": api_key, "Accept": "application/json"}
    try:
        response = requests.get(
            BASE_URL + endpoint,
            headers=headers,
            params=params,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.exceptions.Timeout:
        return _error("network", "RentCast didn't respond in time. Check your connection and try again.")
    except requests.exceptions.RequestException as exc:
        return _error("network", f"Couldn't reach RentCast: {exc}")

    if response.status_code == 200:
        try:
            return {"ok": True, "data": response.json()}
        except ValueError:
            return _error("bad_response", "RentCast sent a response we couldn't read as JSON.")

    return _error(f"http_{response.status_code}", _friendly_message(response))


def _build_params(address, property_type, bedrooms, bathrooms, square_footage, comp_count):
    """Assemble query params, leaving out anything we don't have."""
    params = {"address": address, "compCount": comp_count}
    if property_type:
        params["propertyType"] = property_type
    if bedrooms is not None:
        params["bedrooms"] = bedrooms
    if bathrooms is not None:
        params["bathrooms"] = bathrooms
    if square_footage is not None:
        params["squareFootage"] = square_footage
    return params


def _clean_comp(raw):
    """Pick just the comp fields the UI shows from RentCast's larger object."""
    return {
        "address": raw.get("formattedAddress"),
        "price": raw.get("price"),            # sale price for value comps; monthly rent for rent comps
        "bedrooms": raw.get("bedrooms"),
        "bathrooms": raw.get("bathrooms"),
        "squareFootage": raw.get("squareFootage"),
        "distance": raw.get("distance"),      # miles from the subject property
        "daysOld": raw.get("daysOld"),        # how stale the listing is
        "correlation": raw.get("correlation"),  # 0-1 similarity to the subject
    }


def _clean_subject(raw):
    """A few subject-property attributes RentCast inferred, if present."""
    if not isinstance(raw, dict):
        return {}
    return {
        "address": raw.get("formattedAddress"),
        "propertyType": raw.get("propertyType"),
        "bedrooms": raw.get("bedrooms"),
        "bathrooms": raw.get("bathrooms"),
        "squareFootage": raw.get("squareFootage"),
        "yearBuilt": raw.get("yearBuilt"),
    }


def get_value_estimate(address, property_type=None, bedrooms=None, bathrooms=None,
                       square_footage=None, comp_count=DEFAULT_COMP_COUNT):
    """Estimate what a property is worth, with nearby SALE comps.

    On success: 'estimate' is the value in dollars, with 'range_low'/'range_high'
    bounds and a 'comps' list of recent nearby sales/listings.
    """
    result = _request(
        VALUE_ENDPOINT,
        _build_params(address, property_type, bedrooms, bathrooms, square_footage, comp_count),
    )
    if not result["ok"]:
        return result

    data = result["data"]
    return {
        "ok": True,
        "kind": "value",
        "estimate": data.get("price"),
        "range_low": data.get("priceRangeLow"),
        "range_high": data.get("priceRangeHigh"),
        "subject": _clean_subject(data.get("subjectProperty")),
        "comps": [_clean_comp(c) for c in (data.get("comparables") or [])],
    }


def get_rent_estimate(address, property_type=None, bedrooms=None, bathrooms=None,
                      square_footage=None, comp_count=DEFAULT_COMP_COUNT):
    """Estimate the long-term MONTHLY rent, with nearby RENTAL comps.

    On success: 'estimate' is the monthly rent in dollars (multiply by 12 for an
    annual figure), with 'range_low'/'range_high' bounds and a 'comps' list.
    """
    result = _request(
        RENT_ENDPOINT,
        _build_params(address, property_type, bedrooms, bathrooms, square_footage, comp_count),
    )
    if not result["ok"]:
        return result

    data = result["data"]
    return {
        "ok": True,
        "kind": "rent",
        "estimate": data.get("rent"),
        "range_low": data.get("rentRangeLow"),
        "range_high": data.get("rentRangeHigh"),
        "subject": _clean_subject(data.get("subjectProperty")),
        "comps": [_clean_comp(c) for c in (data.get("comparables") or [])],
    }


def _history_series(history, field):
    """Turn RentCast's history object {"YYYY-MM": {field: value, ...}} into a sorted
    list of (month, value), oldest first, skipping months that lack the value."""
    if not isinstance(history, dict):
        return []
    series = []
    for month in sorted(history.keys()):
        entry = history.get(month) or {}
        value = entry.get(field)
        if value is not None:
            series.append((month, value))
    return series


def _months_between(first_month, last_month):
    """Whole months between two "YYYY-MM" strings (last - first)."""
    first_year, first_mo = (int(p) for p in first_month.split("-")[:2])
    last_year, last_mo = (int(p) for p in last_month.split("-")[:2])
    return (last_year - first_year) * 12 + (last_mo - first_mo)


def _annualized_growth(series):
    """Annualized growth from the first to the last point of a (month, value)
    series. Returns a fraction (0.03 = 3%/yr) or None if it can't be derived."""
    if len(series) < 2:
        return None
    first_month, first_value = series[0]
    last_month, last_value = series[-1]
    if not first_value or first_value <= 0 or not last_value or last_value <= 0:
        return None
    months = _months_between(first_month, last_month)
    if months <= 0:
        return None
    return (last_value / first_value) ** (12 / months) - 1


def _series_span_months(series):
    """How many months a (month, value) series actually covers (0 if < 2 points)."""
    if len(series) < 2:
        return 0
    return _months_between(series[0][0], series[-1][0])


def get_market_trends(zip_code, history_months=MAX_HISTORY_MONTHS):
    """Fetch a zip's historical rent + sale-price trends from RentCast's market
    statistics endpoint. ONE API call. Returns a clean dict; never raises.

    Requests the maximum available history (RentCast returns only the months that
    actually exist). On success it includes the current average/median rent + price,
    the full monthly rent and price history (oldest first), the actual number of
    months each series spans, and an annualized rent-growth figure computed over that
    full window (a fraction, or None if it can't be derived).
    """
    result = _request(MARKET_ENDPOINT, {
        "zipCode": zip_code,
        "dataType": "All",
        "historyRange": history_months,
    })
    if not result["ok"]:
        return result

    data = result["data"]
    rental = data.get("rentalData") or {}
    sale = data.get("saleData") or {}

    rent_history = _history_series(rental.get("history"), "averageRent")
    price_history = _history_series(sale.get("history"), "averagePrice")

    return {
        "ok": True,
        "zip_code": zip_code,
        "current_average_rent": rental.get("averageRent"),
        "current_median_rent": rental.get("medianRent"),
        "current_average_price": sale.get("averagePrice"),
        "current_median_price": sale.get("medianPrice"),
        "rent_history": rent_history,       # [(month, avg rent), ...] oldest first
        "price_history": price_history,
        "rent_history_months": _series_span_months(rent_history),     # actual span returned
        "price_history_months": _series_span_months(price_history),
        "annualized_rent_growth": _annualized_growth(rent_history),   # fraction, full window
        "annualized_price_growth": _annualized_growth(price_history),
        "requested_history_months": history_months,
    }


def _format_for_demo(result, unit):
    """Compact one-line summary for the command-line smoke test below."""
    if not result["ok"]:
        return result["error"]
    estimate = result.get("estimate")
    low, high = result.get("range_low"), result.get("range_high")
    parts = [f"${estimate:,.0f}{unit}" if estimate is not None else "no estimate"]
    if low is not None and high is not None:
        parts.append(f"(range ${low:,.0f}-${high:,.0f})")
    parts.append(f"{len(result.get('comps', []))} comps")
    return "  ".join(parts)


if __name__ == "__main__":
    # Tiny manual smoke test. NOTE: this makes two real API calls, so it counts
    # against your monthly quota. Usage:
    #   RENTCAST_API_KEY=xxx python cre_underwriter/rentcast.py "123 Main St, Austin, TX, 78701"
    address_arg = sys.argv[1] if len(sys.argv) > 1 else "5500 Grand Lake Dr, San Antonio, TX, 78244"
    print(f"Looking up: {address_arg}\n")
    print("VALUE:", _format_for_demo(get_value_estimate(address_arg), ""))
    print("RENT: ", _format_for_demo(get_rent_estimate(address_arg), "/mo"))

    # Optional market-trends demo (one EXTRA call) when a zip is passed as arg 2:
    #   python cre_underwriter/rentcast.py "123 Main St, Austin, TX, 78701" 78701
    if len(sys.argv) > 2:
        zip_arg = sys.argv[2]
        trends = get_market_trends(zip_arg)
        if trends["ok"]:
            growth = trends["annualized_rent_growth"]
            growth_txt = "n/a" if growth is None else f"{growth * 100:.1f}%/yr"
            print(f"TRENDS({zip_arg}): annualized rent growth {growth_txt} over "
                  f"{trends['rent_history_months']} months of history")
        else:
            print(f"TRENDS({zip_arg}):", trends["error"])
