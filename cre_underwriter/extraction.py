"""AI document extraction (Phase D).

Turn an uploaded rent roll, operating statement (T-12), and/or offering memorandum
(OM) into STRUCTURED data that PRE-FILLS the existing inputs: the rent-roll table,
the expense inputs, and (from an OM) the purchase price and vacancy rate. It also
pulls the OM's stated NOI as a display-only cross-check. The user then reviews and
edits every value, and the deterministic engine does the underwriting. AI does the
data-in only — it never makes the buy/pass decision, and the engine never changes.

Loan / financing terms are deliberately NOT extracted: interest rate, amortization,
LTV, and DSCR are the buyer's decisions, not facts in an OM. The user enters those.

Design notes (deliberately mirrors rentcast.py):
  - No Streamlit, no SQL, no engine import. This module only knows how to read a
    document, call the Anthropic API, and hand back plain Python dicts. Wiring it
    into the form is the dashboard's job.
  - The API key is read LIVE from the ANTHROPIC_API_KEY environment variable on
    every call, never hardcoded. A missing key returns a friendly error, not a crash.
  - We call the Anthropic Messages REST endpoint with `requests` — already a
    dependency, the same way rentcast.py talks to RentCast — rather than adding the
    `anthropic` SDK (the project rule is to ask before adding any dependency).
  - Every function returns a dict shaped like:
        {"ok": True, "model": ..., "rent_roll": [...], "operating_expenses": {...},
         "notes": [...]}
    or, on any failure:
        {"ok": False, "error_type": "...", "error": "friendly message"}
    so the caller can always show something instead of blowing up.

Model: claude-haiku-4-5 — the Haiku tier is the cheapest current Anthropic model
suitable for this extraction. (Confirmed against the Anthropic model catalog; model
IDs change, so it lives in one constant, MODEL, below.)

Try it directly (makes ONE real API call when ANTHROPIC_API_KEY is set):
    ANTHROPIC_API_KEY=xxx python cre_underwriter/extraction.py
"""

import base64
import json
import os

import requests


# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
API_KEY_ENV_VAR = "ANTHROPIC_API_KEY"
MESSAGES_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"        # the API version header the endpoint expects

# The cheapest current model suitable for extraction (Haiku tier). One constant so a
# future model rename is a one-line change.
MODEL = "claude-haiku-4-5"

# Plenty of room for a large rent roll's JSON; well under the size where a non-
# streaming request risks an HTTP timeout.
MAX_TOKENS = 8000

# Reading a PDF/image can take a few seconds; give it room without hanging forever.
REQUEST_TIMEOUT_SECONDS = 60

# The operating-expense line items the engine's expense model uses. The extraction
# returns these as ANNUAL dollar amounts (or None), in this fixed order.
EXPENSE_KEYS = ["taxes", "insurance", "management", "repairs", "utilities", "reserves", "hoa"]

# Property-summary fields pulled from an offering memorandum. offering_price and
# vacancy_rate_pct PRE-FILL inputs; the two NOI figures are display-only cross-checks.
SUMMARY_KEYS = ["offering_price", "vacancy_rate_pct", "current_noi", "pro_forma_noi"]

# File extensions we know how to send to the API.
PDF_EXTENSIONS = {"pdf"}
IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
CSV_EXTENSIONS = {"csv"}
EXCEL_EXTENSIONS = {"xlsx", "xls", "xlsm"}
TEXT_EXTENSIONS = {"txt", "tsv", "md"}

_IMAGE_MEDIA_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}


# -----------------------------------------------------------------------------
# The extraction prompt
#
# A system prompt that sets the "careful data-entry, never guess, JSON only" role,
# plus a user-turn instruction that pins the EXACT schema the app's fields expect.
# The schema field names match the engine's rent-roll keys (label, unit_type,
# square_footage, monthly_rent, market_rent, occupied) so the result drops straight
# into the form with no remapping.
# -----------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "You are a careful data-entry assistant for a commercial real estate "
    "underwriting tool. You read rent rolls, operating statements (T-12s), and "
    "offering memorandums (OMs), and report the figures EXACTLY as stated, as strict "
    "JSON. You never estimate, guess, or invent numbers: if a value is not in the "
    "document, you return null. You never report loan or financing terms (the buyer "
    "decides those). You return ONLY the JSON object, with no commentary, "
    "explanation, or markdown."
)

EXTRACTION_INSTRUCTIONS = """\
Extract the property summary, rent roll, and operating expenses from the attached \
document(s) into this EXACT JSON shape:

{
  "summary": {
    "offering_price": <the stated Offering Price / Asking Price / List Price in dollars, or null>,
    "vacancy_rate_pct": <the stated vacancy or vacancy-allowance PERCENT, e.g. 3 for "3% vacancy", or null>,
    "current_noi": <the stated CURRENT net operating income in annual dollars, or null>,
    "pro_forma_noi": <the stated PRO FORMA / projected net operating income in annual dollars, or null>
  },
  "rent_roll": [
    {
      "label": "unit label or number exactly as written, e.g. \\"101\\" or \\"Unit A\\"",
      "unit_type": "unit type if stated, e.g. \\"2BR/1BA\\", else null",
      "square_footage": <number of square feet, or null>,
      "monthly_rent": <CURRENT monthly rent in dollars, or null>,
      "market_rent": <MARKET monthly rent in dollars if the document states one, else null>,
      "occupied": <true if the unit is occupied/leased, false if vacant>
    }
  ],
  "operating_expenses": {
    "taxes": <annual property taxes in dollars, or null>,
    "insurance": <annual insurance in dollars, or null>,
    "management": <annual property management in dollars, or null>,
    "repairs": <annual repairs and maintenance in dollars, or null>,
    "utilities": <annual owner-paid utilities in dollars, or null>,
    "reserves": <annual replacement reserves in dollars, or null>,
    "hoa": <annual HOA / condo dues in dollars, or null>
  },
  "notes": "one short sentence about anything ambiguous or unreadable, or null"
}

Rules:
- Return ONLY the JSON object above. No prose, no markdown code fences, no explanation.
- Use null for anything not found. Do NOT guess or estimate a value the document does not state.
- Every money value is a PLAIN NUMBER in dollars (e.g. 1450, not "$1,450.00").
- offering_price: the asking/offering/list price from the property summary, as a number.
- vacancy_rate_pct: ONLY a clearly stated vacancy or vacancy-allowance rate, as a percent number
  (e.g. 3 for "3% vacancy allowance"). An OCCUPANCY figure is NOT a vacancy rate — "100% occupied"
  or "95% occupied" describes current occupancy, so return null for vacancy_rate_pct in that case.
- current_noi / pro_forma_noi: the stated NOI figures, for cross-checking only.
- Do NOT report any loan or financing terms (interest rate, amortization, LTV, DSCR, debt service).
  Those are the buyer's decisions and are not part of this extraction — never include them.
- All operating-expense amounts must be ANNUAL. If a figure is monthly, multiply by 12;
  if it covers a partial year, annualize it and say so in "notes".
- If the document has only a rent roll, return "operating_expenses" with every value null.
  If it has only an operating statement, return "rent_roll" as an empty list [].
- Treat a unit as occupied (true) unless it is clearly marked vacant or empty.
"""


# -----------------------------------------------------------------------------
# API-key helpers (same contract as rentcast.py)
# -----------------------------------------------------------------------------
def has_api_key():
    """True if ANTHROPIC_API_KEY is set, so the UI can warn when it isn't."""
    return bool(os.environ.get(API_KEY_ENV_VAR))


def _error(error_type, message):
    """The standard failure shape the caller knows how to display."""
    return {"ok": False, "error_type": error_type, "error": message}


MISSING_KEY_MESSAGE = (
    "No Anthropic API key found. Set the ANTHROPIC_API_KEY environment variable "
    "and restart the app to import documents with AI. Manual entry still works "
    "without it."
)


def missing_key_error():
    """Return the standard missing-key error result dict."""
    return _error("missing_key", MISSING_KEY_MESSAGE)


# -----------------------------------------------------------------------------
# File-type detection and content-block preparation
#
# PDFs and images go to the API as base64 (a document/image block). CSV, Excel, and
# plain text are read to text first and sent as a text block. Excel needs a reader
# (pandas + openpyxl); we import it lazily and degrade to a friendly message if it
# isn't installed, so the common PDF/CSV path never depends on it.
# -----------------------------------------------------------------------------
def detect_file_kind(filename):
    """Classify a filename as 'pdf', 'image', 'csv', 'excel', 'text', or None."""
    extension = (filename or "").rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""
    if extension in PDF_EXTENSIONS:
        return "pdf"
    if extension in IMAGE_EXTENSIONS:
        return "image"
    if extension in CSV_EXTENSIONS:
        return "csv"
    if extension in EXCEL_EXTENSIONS:
        return "excel"
    if extension in TEXT_EXTENSIONS:
        return "text"
    return None


def _image_media_type(filename):
    """The image/* media type for an image filename (defaults to PNG)."""
    extension = filename.rsplit(".", 1)[-1].lower()
    return _IMAGE_MEDIA_TYPES.get(extension, "image/png")


def _decode_text(data):
    """Decode uploaded bytes to text (UTF-8, dropping a BOM; never raises)."""
    if isinstance(data, str):
        return data
    return data.decode("utf-8-sig", errors="replace")


def _excel_to_text(data):
    """Read every sheet of an Excel workbook into CSV text. Raises on a missing
    reader (pandas/openpyxl) so the caller can show a friendly message."""
    import io

    import pandas as pd  # lazy: only Excel uploads need it

    sheets = pd.read_excel(io.BytesIO(data), sheet_name=None)  # {sheet_name: DataFrame}
    parts = []
    for sheet_name, frame in sheets.items():
        parts.append(f"# Sheet: {sheet_name}\n{frame.to_csv(index=False)}")
    return "\n\n".join(parts)


def _content_block_for(filename, data):
    """Build one API content block for an uploaded file.

    Returns (block, None) on success, or (None, note) when the file can't be used
    (unsupported type, or Excel without a reader installed)."""
    kind = detect_file_kind(filename)

    if kind == "pdf":
        encoded = base64.standard_b64encode(data).decode("ascii")
        block = {
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": encoded},
        }
        return block, None

    if kind == "image":
        encoded = base64.standard_b64encode(data).decode("ascii")
        block = {
            "type": "image",
            "source": {"type": "base64", "media_type": _image_media_type(filename), "data": encoded},
        }
        return block, None

    if kind in ("csv", "text"):
        text = _decode_text(data)
        return {"type": "text", "text": f"Document: {filename}\n\n{text}"}, None

    if kind == "excel":
        try:
            text = _excel_to_text(data)
        except ImportError:
            return None, (f"Couldn't read the Excel file “{filename}” — Excel support needs "
                          "the openpyxl package, which isn't installed. Export the sheet to "
                          "CSV and upload that instead.")
        except Exception:
            return None, f"Couldn't read the Excel file “{filename}”. Try exporting it to CSV."
        return {"type": "text", "text": f"Document: {filename}\n\n{text}"}, None

    return None, (f"Skipped “{filename}” — unsupported file type. Use PDF, image "
                  "(PNG/JPG), CSV, or Excel.")


# -----------------------------------------------------------------------------
# Calling the Anthropic Messages API (raw HTTP via requests)
# -----------------------------------------------------------------------------
def _friendly_api_message(response):
    """Turn a non-200 Anthropic response into a plain-English message."""
    # Anthropic error bodies look like {"type": "error", "error": {"message": ...}}.
    detail = ""
    try:
        body = response.json()
        message = (body.get("error") or {}).get("message") if isinstance(body, dict) else None
        if message:
            detail = f" (Anthropic: {message})"
    except ValueError:
        detail = ""

    code = response.status_code
    if code == 400:
        return "The document couldn't be processed as sent (bad request)." + detail
    if code == 401:
        return "Anthropic rejected the API key. Check ANTHROPIC_API_KEY and restart the app." + detail
    if code == 403:
        return "The API key isn't allowed to use this model or endpoint." + detail
    if code == 413:
        return "The document is too large to send. Try a smaller file or split it up." + detail
    if code == 429:
        return "Too many requests right now. Wait a moment and try again." + detail
    if code == 529:
        return "The Anthropic API is overloaded. Try again in a moment." + detail
    if code >= 500:
        return "The Anthropic API had a server error. Try again in a moment." + detail
    return f"The Anthropic API returned an unexpected error (HTTP {code})." + detail


def _text_from_response(body):
    """Concatenate the text from an Anthropic response's content blocks."""
    parts = []
    for block in (body.get("content") or []):
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text") or "")
    return "".join(parts).strip()


def _call_api(content_blocks):
    """Make one Messages API call. Returns {'ok': True, 'text': ...} or an error dict.

    Reads the key LIVE here (never captured at import), so a key set before launching
    the app is always seen by the running process."""
    api_key = os.environ.get(API_KEY_ENV_VAR)
    if not api_key:
        return missing_key_error()

    headers = {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    payload = {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": content_blocks}],
    }

    try:
        response = requests.post(
            MESSAGES_URL, headers=headers, json=payload, timeout=REQUEST_TIMEOUT_SECONDS
        )
    except requests.exceptions.Timeout:
        return _error("network", "The Anthropic API didn't respond in time. Try again, "
                                 "or use a smaller document.")
    except requests.exceptions.RequestException as exc:
        return _error("network", f"Couldn't reach the Anthropic API: {exc}")

    if response.status_code != 200:
        return _error(f"http_{response.status_code}", _friendly_api_message(response))

    try:
        body = response.json()
    except ValueError:
        return _error("bad_response", "The Anthropic API sent a response we couldn't read as JSON.")

    text = _text_from_response(body)
    if not text:
        return _error("empty", "The Anthropic API returned an empty response. Try again.")
    return {"ok": True, "text": text}


# -----------------------------------------------------------------------------
# Parsing + validating the model's JSON
#
# The model is asked for raw JSON, but we never trust that blindly: we locate the
# JSON, parse it, and coerce every field to the expected type, dropping anything
# malformed and noting what we dropped. Numbers must be non-negative. Nothing here
# raises — a garbled reply returns what was usable plus clear notes.
# -----------------------------------------------------------------------------
def _to_amount(value):
    """Coerce a value to a non-negative float, or None if missing/blank/invalid.

    Rejects booleans (True would otherwise read as 1) and negatives."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if number >= 0 else None
    if isinstance(value, str):
        cleaned = value.strip().replace(",", "").replace("$", "")
        if cleaned == "":
            return None
        try:
            number = float(cleaned)
        except ValueError:
            return None
        return number if number >= 0 else None
    return None


def _locate_json(text):
    """Find and parse the JSON object in the model's reply. Returns a dict, or None
    if no valid JSON object can be recovered. Tolerates stray prose or code fences."""
    if not text:
        return None

    # 1. The clean case: the whole reply is the JSON object.
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except ValueError:
        pass

    # 2. Fallback: grab the outermost {...} span and try that (handles code fences or
    #    a stray sentence around the JSON).
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        parsed = json.loads(text[start:end + 1])
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _clean_rent_roll(raw, notes):
    """Validate the model's rent_roll into a clean list of unit dicts (engine keys).

    A vacant/blank rent becomes 0.0 (engine treats it as $0 current income); square
    footage and market rent stay None when absent. Non-dict rows are skipped + noted."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        notes.append("The rent roll wasn't a list, so no units were imported.")
        return []

    units = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            notes.append(f"Skipped a rent-roll row that wasn't a unit object (row {index + 1}).")
            continue

        label = item.get("label")
        label = str(label).strip() if label not in (None, "") else f"Unit {index + 1}"

        unit_type = item.get("unit_type")
        unit_type = str(unit_type).strip() if unit_type not in (None, "") else ""

        occupied = item.get("occupied")
        occupied = bool(occupied) if isinstance(occupied, bool) else True  # default to occupied

        units.append({
            "label": label,
            "unit_type": unit_type,
            "square_footage": _to_amount(item.get("square_footage")),
            "monthly_rent": _to_amount(item.get("monthly_rent")) or 0.0,
            "market_rent": _to_amount(item.get("market_rent")),
            "occupied": occupied,
        })
    return units


def _clean_operating_expenses(raw, notes):
    """Validate the model's operating_expenses into the fixed {key: amount|None} dict."""
    expenses = {key: None for key in EXPENSE_KEYS}
    if raw is None:
        return expenses
    if not isinstance(raw, dict):
        notes.append("The operating expenses weren't an object, so none were imported.")
        return expenses

    for key in EXPENSE_KEYS:
        raw_value = raw.get(key)
        amount = _to_amount(raw_value)
        if amount is None and raw_value not in (None, ""):
            notes.append(f"Couldn't read the {key} expense ({raw_value!r}); left it blank.")
        expenses[key] = amount
    return expenses


def _clean_summary(raw, notes):
    """Validate the property-summary block into the fixed {key: value|None} dict.

    offering_price / current_noi / pro_forma_noi are non-negative dollar amounts;
    vacancy_rate_pct is a percent in 0-100 (an out-of-range value is dropped + noted)."""
    summary = {key: None for key in SUMMARY_KEYS}
    if not isinstance(raw, dict):
        return summary

    summary["offering_price"] = _to_amount(raw.get("offering_price"))
    summary["current_noi"] = _to_amount(raw.get("current_noi"))
    summary["pro_forma_noi"] = _to_amount(raw.get("pro_forma_noi"))

    vacancy = _to_amount(raw.get("vacancy_rate_pct"))
    if vacancy is not None and vacancy > 100:
        notes.append(f"Ignored an out-of-range vacancy rate ({vacancy}%).")
        vacancy = None
    summary["vacancy_rate_pct"] = vacancy

    # Note any present-but-unreadable dollar figures (vacancy is handled above).
    for key in ("offering_price", "current_noi", "pro_forma_noi"):
        raw_value = raw.get(key)
        if summary[key] is None and raw_value not in (None, ""):
            notes.append(f"Couldn't read {key.replace('_', ' ')} ({raw_value!r}).")
    return summary


def parse_extraction(text):
    """Parse + validate a model reply into structured data. Never raises.

    Returns {"summary": {...}, "rent_roll": [...], "operating_expenses": {...},
    "notes": [...]} on success, or None if no JSON object could be recovered at all."""
    parsed = _locate_json(text)
    if parsed is None:
        return None

    notes = []
    summary = _clean_summary(parsed.get("summary"), notes)
    rent_roll = _clean_rent_roll(parsed.get("rent_roll"), notes)
    operating_expenses = _clean_operating_expenses(parsed.get("operating_expenses"), notes)

    model_note = parsed.get("notes")
    if isinstance(model_note, str) and model_note.strip():
        notes.append(model_note.strip())

    return {
        "summary": summary,
        "rent_roll": rent_roll,
        "operating_expenses": operating_expenses,
        "notes": notes,
    }


# -----------------------------------------------------------------------------
# Public entry point
# -----------------------------------------------------------------------------
def extract_from_documents(files):
    """Extract structured data from one or more uploaded documents in ONE API call.

    `files` is a list of (filename, data_bytes). All readable files are sent together
    as a single request — one extraction = one API call. Returns the standard ok/error
    dict; on success it carries the validated rent_roll, operating_expenses, and notes."""
    if not has_api_key():
        return missing_key_error()
    if not files:
        return _error("no_files", "No documents to extract — upload at least one file.")

    content_blocks = []
    skipped_notes = []
    for filename, data in files:
        block, note = _content_block_for(filename, data)
        if block is None:
            skipped_notes.append(note)
        else:
            content_blocks.append(block)

    if not content_blocks:
        return _error(
            "unsupported",
            "None of the uploaded files could be read. " + " ".join(skipped_notes),
        )

    # The instruction text goes AFTER the documents so the model reads them first.
    content_blocks.append({"type": "text", "text": EXTRACTION_INSTRUCTIONS})

    api_result = _call_api(content_blocks)
    if not api_result["ok"]:
        return api_result  # already a friendly error dict

    structured = parse_extraction(api_result["text"])
    if structured is None:
        return _error(
            "unparseable",
            "Claude's reply wasn't valid JSON, so nothing could be imported. Try again, "
            "or enter the numbers manually.",
        )

    return {
        "ok": True,
        "model": MODEL,
        "summary": structured["summary"],
        "rent_roll": structured["rent_roll"],
        "operating_expenses": structured["operating_expenses"],
        # Skipped-file notes first, then any validation/model notes.
        "notes": skipped_notes + structured["notes"],
    }


# -----------------------------------------------------------------------------
# Manual smoke test
#
# Runs OFFLINE checks (file detection + JSON validation) that need no API key, then —
# only if ANTHROPIC_API_KEY is set — makes ONE real API call on a tiny sample so you
# can see the live round trip. Usage:
#     python cre_underwriter/extraction.py            (offline checks only)
#     ANTHROPIC_API_KEY=xxx python cre_underwriter/extraction.py   (+ one live call)
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    print(f"Extraction model: {MODEL}\n")

    # ---- Offline check 1: file-type detection ----
    print("File-type detection:")
    for name in ["rent_roll.pdf", "scan.PNG", "t12.csv", "book.xlsx", "notes.txt", "weird.dat"]:
        print(f"  {name:<16} -> {detect_file_kind(name)}")

    # ---- Offline check 2: parse + validate a realistic (messy) model reply ----
    sample_reply = """Here is the data:
    ```json
    {
      "summary": {
        "offering_price": "$3,150,000", "vacancy_rate_pct": 3,
        "current_noi": 188000, "pro_forma_noi": 215000
      },
      "rent_roll": [
        {"label": "101", "unit_type": "2BR/1BA", "square_footage": 850,
         "monthly_rent": 1450, "market_rent": 1600, "occupied": true},
        {"label": "102", "unit_type": "1BR/1BA", "square_footage": "650",
         "monthly_rent": "$1,200", "market_rent": null, "occupied": false},
        "not a unit",
        {"label": "103", "monthly_rent": -50, "occupied": true}
      ],
      "operating_expenses": {
        "taxes": 7200, "insurance": 4000, "management": "5,568",
        "repairs": 3500, "utilities": 3000, "reserves": 1200, "hoa": null
      },
      "notes": "Unit 103 had an unreadable rent."
    }
    ```"""
    structured = parse_extraction(sample_reply)
    print("\nParsed sample reply:")
    print(f"  summary: {structured['summary']}")
    print(f"  units: {len(structured['rent_roll'])}")
    for unit in structured["rent_roll"]:
        print(f"    {unit}")
    print(f"  expenses: {structured['operating_expenses']}")
    print(f"  notes: {structured['notes']}")

    # ---- Offline check 2b: occupancy must NOT become a vacancy rate, out-of-range dropped ----
    occ_reply = '{"summary": {"vacancy_rate_pct": null}, "rent_roll": [], "operating_expenses": {}}'
    print(f"\n'100% occupied' style reply -> vacancy: "
          f"{parse_extraction(occ_reply)['summary']['vacancy_rate_pct']} (should be None)")
    bad_vac = parse_extraction('{"summary": {"vacancy_rate_pct": 250}}')
    print(f"out-of-range vacancy (250) -> {bad_vac['summary']['vacancy_rate_pct']} "
          f"(should be None), notes: {bad_vac['notes']}")

    # ---- Offline check 3: _to_amount edge cases ----
    print("\n_to_amount edge cases:")
    for value in [1450, "$1,450.00", "1,200", -5, True, None, "", "abc", 0]:
        print(f"  {value!r:<14} -> {_to_amount(value)}")

    # ---- Offline check 4: garbage reply + the missing-key path ----
    print(f"\nNon-JSON reply -> {parse_extraction('sorry, I cannot help with that')}")
    if not has_api_key():
        print("\nNo ANTHROPIC_API_KEY set — skipping the live call.")
        print(f"  extract_from_documents([]) -> {extract_from_documents([('x.csv', b'a')])['error_type']}")
    else:
        # ---- Live call: one tiny OM-style document through the real API ----
        sample_csv = (
            "Maplewood Apartments — Offering Summary\n"
            "Offering Price: $3,150,000\n"
            "Vacancy allowance: 3%\n"
            "Current NOI: $188,000   Pro Forma NOI: $215,000\n"
            "\n"
            "Rent roll:\n"
            "Unit,Type,SqFt,Current Rent,Market Rent,Status\n"
            "101,2BR/1BA,850,1450,1600,Occupied\n"
            "102,2BR/1BA,850,1400,1600,Occupied\n"
            "103,1BR/1BA,650,,1250,Vacant\n"
            "\n"
            "Operating expenses (annual):\n"
            "Property taxes,6875\n"
            "Insurance,4000\n"
            "Management,5568\n"
            "Repairs,3480\n"
            "Utilities,3000\n"
            "Reserves,1200\n"
        ).encode("utf-8")
        print("\nMaking ONE live API call on a sample OM…")
        result = extract_from_documents([("sample.csv", sample_csv)])
        print(json.dumps(result, indent=2))
