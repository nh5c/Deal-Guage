"""Storage for saving and loading deals (Phase 3; Postgres backend added for deploy).

Two interchangeable backends, chosen at runtime by the DATABASE_URL environment variable:
  - DATABASE_URL set (production / Render): connect to PostgreSQL at that URL.
  - DATABASE_URL unset (local dev): the built-in sqlite3 file at data/deals.db, exactly
    as before — no Postgres server or extra setup needed locally.

All database access is isolated here; the engine and dashboard don't know which backend
is active. The public functions (initialize_database, save_deal, load_deal, list_deals)
have identical signatures and behavior on both. The SQL dialect differences (placeholder
style, autoincrement keys, returning the new id) are handled by a few small helpers.

We store only the raw inputs a user enters (the same keys as engine.sample_deal),
never the computed metrics. NOI, cap rate, DSCR, and the buy/pass verdict are
recomputed by the engine whenever a deal is loaded, so they can never go stale.

Run the demo with:  python cre_underwriter/database.py
"""

import json
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path


# -----------------------------------------------------------------------------
# Where the database file lives.
#
# We build the path relative to THIS file, not the current working directory, so
# the demo finds the same data/deals.db no matter which folder you run it from.
#   __file__              -> cre_underwriter/database.py
#   .parent               -> cre_underwriter/
#   .parent.parent        -> the project root
# -----------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DATABASE_PATH = DATA_DIR / "deals.db"


# -----------------------------------------------------------------------------
# The "deals" table — schema design, in plain English
#
# One row = one deal. We store ONLY the raw inputs a person enters (the same keys
# as engine.sample_deal), plus a name, an id, and a timestamp. We deliberately do
# NOT store NOI, cap rate, DSCR, or the buy/pass verdict. Those are recomputed by
# the engine every time a deal is loaded, so they can never drift out of sync with
# the inputs — or with a later change to the thresholds. The inputs are the single
# source of truth; the metrics are always derived from them.
#
# Why a column per field (instead of one JSON blob)?
#   Each input gets its own typed column. That keeps the data queryable — later you
#   could ask "show every deal under $500k" or sort by any field in plain SQL. A
#   single JSON blob would force you to parse every row in Python to ask anything.
#
# Column choices:
#   id            INTEGER PRIMARY KEY AUTOINCREMENT
#                 SQLite auto-assigns a unique, increasing integer to each row.
#                 INTEGER PRIMARY KEY alone already does that; adding AUTOINCREMENT
#                 also guarantees a deleted id is never reused, so ids stay stable
#                 and unambiguous in a running ledger of deals.
#   name          TEXT — a human-readable label for the deal.
#   money & rate  REAL (floating point). SQLite has no dedicated money type; REAL
#   fields        fits both dollars and fractions like a 0.05 vacancy rate. (Floats
#                 can carry tiny rounding error. That is fine here because we always
#                 recompute from inputs and never test for exact equality; a
#                 bank-grade app would instead store whole cents as integers.)
#   amortization_years  INTEGER — a whole number of years.
#   created_at    TEXT — SQLite has no dedicated date type. The standard trick is an
#                 ISO-8601 string like "2026-06-08T13:45:00": human-readable AND it
#                 sorts chronologically as plain text.
#   NOT NULL      Core inputs are required, so a half-entered deal cannot be saved.
#
# Phase 9 (structured expenses): the operating-expense inputs are stored too.
#   expense_mode  TEXT NOT NULL — "simple" (single total) or "detailed" (line items).
#   operating_expenses plus the line-item columns (property_tax_rate, insurance_annual,
#   management_pct, repairs_pct, utilities_annual, reserves_per_unit) and
#   number_of_units are NULLABLE: a deal fills in whichever set its mode uses, and the
#   engine recomputes the actual expense total from these whenever the deal is loaded.
#
# Phase A (rent roll): income_mode TEXT NOT NULL ("simple" or "detailed"), and the
# rent roll itself stored as JSON text in a single rent_roll column (NULL in simple
# mode). Why JSON-on-the-deal rather than a separate one-to-many units table: the
# rent roll is a variable-length, nested list that is ALWAYS read and written as one
# piece with its deal — we never query or sort individual units in SQL. A units table
# would add a second table, a join, and cascade-delete bookkeeping for zero query
# benefit, and would complicate the single-row save_deal/load_deal contract. JSON in
# one column keeps "one row = one deal" intact; load_deal parses it back to a list.
# -----------------------------------------------------------------------------
CREATE_DEALS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS deals (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    name                 TEXT    NOT NULL,
    purchase_price       REAL    NOT NULL,
    number_of_units      INTEGER,
    gross_rental_income  REAL    NOT NULL,
    income_mode          TEXT    NOT NULL DEFAULT 'simple',
    rent_roll            TEXT,
    vacancy_rate         REAL    NOT NULL,
    expense_mode         TEXT    NOT NULL DEFAULT 'simple',
    property_type        TEXT    NOT NULL DEFAULT 'small_multifamily',
    operating_expenses   REAL,
    property_tax_rate    REAL,
    insurance_annual     REAL,
    hoa_annual           REAL,
    management_pct       REAL,
    repairs_pct          REAL,
    utilities_annual     REAL,
    reserves_per_unit    REAL,
    loan_amount          REAL    NOT NULL,
    annual_interest_rate REAL    NOT NULL,
    amortization_years   INTEGER NOT NULL,
    down_payment         REAL    NOT NULL,
    financing_mode       TEXT    NOT NULL DEFAULT 'manual',
    ltv_max              REAL,
    dscr_min             REAL,
    renovation_cost_per_unit REAL,
    renovation_pace          REAL,
    created_at           TEXT    NOT NULL,
    owner                TEXT
)
"""

# The same table for PostgreSQL. Two dialect differences from the SQLite version above:
#   - id: SERIAL PRIMARY KEY (Postgres auto-increment) instead of INTEGER PRIMARY KEY
#     AUTOINCREMENT.
#   - money/rate columns: DOUBLE PRECISION (Postgres has no flexible REAL affinity).
# Everything else (TEXT, INTEGER, NOT NULL, DEFAULT, the JSON-text rent_roll, the owner
# column) is identical, so a fresh Postgres database initializes itself with the full
# per-user schema on first run.
CREATE_DEALS_TABLE_POSTGRES = """
CREATE TABLE IF NOT EXISTS deals (
    id                   SERIAL  PRIMARY KEY,
    name                 TEXT    NOT NULL,
    purchase_price       DOUBLE PRECISION NOT NULL,
    number_of_units      INTEGER,
    gross_rental_income  DOUBLE PRECISION NOT NULL,
    income_mode          TEXT    NOT NULL DEFAULT 'simple',
    rent_roll            TEXT,
    vacancy_rate         DOUBLE PRECISION NOT NULL,
    expense_mode         TEXT    NOT NULL DEFAULT 'simple',
    property_type        TEXT    NOT NULL DEFAULT 'small_multifamily',
    operating_expenses   DOUBLE PRECISION,
    property_tax_rate    DOUBLE PRECISION,
    insurance_annual     DOUBLE PRECISION,
    hoa_annual           DOUBLE PRECISION,
    management_pct       DOUBLE PRECISION,
    repairs_pct          DOUBLE PRECISION,
    utilities_annual     DOUBLE PRECISION,
    reserves_per_unit    DOUBLE PRECISION,
    loan_amount          DOUBLE PRECISION NOT NULL,
    annual_interest_rate DOUBLE PRECISION NOT NULL,
    amortization_years   INTEGER NOT NULL,
    down_payment         DOUBLE PRECISION NOT NULL,
    financing_mode       TEXT    NOT NULL DEFAULT 'manual',
    ltv_max              DOUBLE PRECISION,
    dscr_min             DOUBLE PRECISION,
    renovation_cost_per_unit DOUBLE PRECISION,
    renovation_pace          DOUBLE PRECISION,
    created_at           TEXT    NOT NULL,
    owner                TEXT
)
"""

# Named (:field) placeholders, so values are safely parameterized rather than
# pasted into the SQL string. The names match the keys in a deal dict. These are
# SQLite-style; _q() rewrites them to %(field)s for Postgres.
INSERT_DEAL_SQL = """
INSERT INTO deals (
    name, purchase_price, number_of_units, gross_rental_income, income_mode, rent_roll,
    vacancy_rate, expense_mode, property_type, operating_expenses, property_tax_rate,
    insurance_annual, hoa_annual, management_pct, repairs_pct, utilities_annual,
    reserves_per_unit, loan_amount, annual_interest_rate, amortization_years,
    down_payment, financing_mode, ltv_max, dscr_min,
    renovation_cost_per_unit, renovation_pace, created_at, owner
) VALUES (
    :name, :purchase_price, :number_of_units, :gross_rental_income, :income_mode, :rent_roll,
    :vacancy_rate, :expense_mode, :property_type, :operating_expenses, :property_tax_rate,
    :insurance_annual, :hoa_annual, :management_pct, :repairs_pct, :utilities_annual,
    :reserves_per_unit, :loan_amount, :annual_interest_rate, :amortization_years,
    :down_payment, :financing_mode, :ltv_max, :dscr_min,
    :renovation_cost_per_unit, :renovation_pace, :created_at, :owner
)
"""

# Optional columns default to these when a deal doesn't supply them (e.g. a simple-
# mode deal has no line items). Merged UNDER the deal so any value the deal carries
# wins, while keeping every named placeholder in INSERT_DEAL_SQL bound.
OPTIONAL_COLUMN_DEFAULTS = {
    "number_of_units": None,
    "income_mode": "simple",
    "rent_roll": None,
    "expense_mode": "simple",
    "property_type": "small_multifamily",
    "operating_expenses": None,
    "property_tax_rate": None,
    "insurance_annual": None,
    "hoa_annual": None,
    "management_pct": None,
    "repairs_pct": None,
    "utilities_annual": None,
    "reserves_per_unit": None,
    "financing_mode": "manual",
    "ltv_max": None,
    "dscr_min": None,
    "renovation_cost_per_unit": None,
    "renovation_pace": None,
    "owner": None,
}


# -----------------------------------------------------------------------------
# Backend selection + dialect helpers.
#
# DATABASE_URL set  -> PostgreSQL (production / Render).  DATABASE_URL unset -> SQLite.
# Queries are written in the SQLite style (:name and ?), and translated for Postgres.
# -----------------------------------------------------------------------------
DATABASE_URL_ENV = "DATABASE_URL"


def _database_url():
    """The Postgres connection URL from the environment, or None for local SQLite.

    Render (and some providers) hand out 'postgres://...' URLs, but psycopg2/SQLAlchemy
    want the 'postgresql://...' scheme — normalize that here. No credential is ever
    hardcoded; this only reads the env var the platform provides."""
    url = os.environ.get(DATABASE_URL_ENV)
    if not url:
        return None
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url


def _is_postgres():
    """True when a DATABASE_URL is configured (use PostgreSQL), else SQLite."""
    return _database_url() is not None


def _q(query):
    """Translate a query's placeholders for the active backend.

    Queries here use the SQLite style: :name for named params, ? for positional.
    Postgres/psycopg2 wants %(name)s and %s. Each query uses ONE style, so the mapping
    is unambiguous. (For SQLite the query is returned unchanged.)"""
    if not _is_postgres():
        return query
    query = re.sub(r":(\w+)", r"%(\1)s", query)   # :name -> %(name)s
    return query.replace("?", "%s")               # ?     -> %s


def _connect(db_path=DATABASE_PATH):
    """Open a connection to the active backend.

    PostgreSQL when DATABASE_URL is set (psycopg2 is imported lazily, so local dev needs
    no Postgres driver loaded); otherwise the local SQLite file. On both, rows read back
    behave like dicts (row["name"]) so the calling code is identical."""
    url = _database_url()
    if url:
        import psycopg2   # lazy: only needed in production / when DATABASE_URL is set
        return psycopg2.connect(url)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row   # rows behave like dicts: row["name"], row["id"]
    return connection


def _dict_cursor(connection):
    """A cursor whose rows behave like dicts on both backends."""
    if _is_postgres():
        import psycopg2.extras
        return connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    return connection.cursor()   # inherits the sqlite3.Row factory from the connection


def _add_missing_columns(connection):
    """Add Phase 12 columns to a pre-existing table (no reset needed). SQLite's
    ALTER TABLE ADD COLUMN backfills existing rows with the column default, so any
    already-saved deal loads cleanly as a small_multifamily."""
    existing = {row["name"] for row in connection.execute("PRAGMA table_info(deals)")}
    if "property_type" not in existing:
        connection.execute(
            "ALTER TABLE deals ADD COLUMN property_type TEXT NOT NULL DEFAULT 'small_multifamily'"
        )
    if "hoa_annual" not in existing:
        connection.execute("ALTER TABLE deals ADD COLUMN hoa_annual REAL")
    if "income_mode" not in existing:
        connection.execute("ALTER TABLE deals ADD COLUMN income_mode TEXT NOT NULL DEFAULT 'simple'")
    if "rent_roll" not in existing:
        connection.execute("ALTER TABLE deals ADD COLUMN rent_roll TEXT")
    if "financing_mode" not in existing:
        connection.execute("ALTER TABLE deals ADD COLUMN financing_mode TEXT NOT NULL DEFAULT 'manual'")
    if "ltv_max" not in existing:
        connection.execute("ALTER TABLE deals ADD COLUMN ltv_max REAL")
    if "dscr_min" not in existing:
        connection.execute("ALTER TABLE deals ADD COLUMN dscr_min REAL")
    if "renovation_cost_per_unit" not in existing:
        connection.execute("ALTER TABLE deals ADD COLUMN renovation_cost_per_unit REAL")
    if "renovation_pace" not in existing:
        connection.execute("ALTER TABLE deals ADD COLUMN renovation_pace REAL")
    # Per-user ownership (auth). Existing rows get NULL owner — legacy/global deals
    # that the app hides from signed-in users (they only see their own).
    if "owner" not in existing:
        connection.execute("ALTER TABLE deals ADD COLUMN owner TEXT")


def initialize_database(db_path=DATABASE_PATH):
    """Create the deals table for the active backend if it doesn't already exist.

    Safe to call every time the app starts: 'CREATE TABLE IF NOT EXISTS' does nothing
    when the table is already there, so it never wipes existing data. A fresh Postgres
    database (e.g. a new Render instance) initializes itself with the full per-user schema.
    """
    if _is_postgres():
        connection = _connect(db_path)
        try:
            cursor = connection.cursor()
            cursor.execute(CREATE_DEALS_TABLE_POSTGRES)
            connection.commit()
        finally:
            connection.close()
        return

    # SQLite (local dev): same as before, including the in-place column upgrades that
    # bring an older local data/deals.db up to the current schema.
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)   # make the data/ folder
    connection = _connect(db_path)
    try:
        connection.execute(CREATE_DEALS_TABLE_SQL)
        _add_missing_columns(connection)   # in-place upgrade for older databases
        connection.commit()
    finally:
        connection.close()


def save_deal(deal, owner=None, db_path=DATABASE_PATH):
    """Insert one deal (a dict of raw inputs) and return its new integer id.

    `owner` tags the deal with the signed-in user's identity (their email) so the app
    can show each user only their own deals. The owner argument always wins over any
    owner key the deal dict might carry.
    """
    # Stamp the moment we saved it as ISO-8601 local time (sorts chronologically).
    created_at = datetime.now().isoformat(timespec="seconds")

    # Combine the timestamp with the deal's input fields into one parameter dict.
    # Optional-column defaults fill any expense fields the deal omits; extra keys a
    # deal might carry (id, hold/exit assumptions) are ignored by the named
    # placeholders in INSERT_DEAL_SQL.
    parameters = {**OPTIONAL_COLUMN_DEFAULTS, **deal, "created_at": created_at, "owner": owner}
    # The rent roll is a list of unit dicts — store it as JSON text (see load_deal).
    rent_roll = parameters.get("rent_roll")
    parameters["rent_roll"] = json.dumps(rent_roll) if rent_roll is not None else None

    connection = _connect(db_path)
    try:
        cursor = _dict_cursor(connection)
        if _is_postgres():
            # psycopg2 has no lastrowid — ask Postgres to hand the new id straight back.
            cursor.execute(_q(INSERT_DEAL_SQL) + " RETURNING id", parameters)
            new_id = cursor.fetchone()["id"]
        else:
            cursor.execute(_q(INSERT_DEAL_SQL), parameters)
            new_id = cursor.lastrowid   # the id SQLite just auto-assigned to this row
        connection.commit()
    finally:
        connection.close()

    return new_id


def load_deal(deal_id, owner=None, db_path=DATABASE_PATH):
    """Load one deal by id and return it as a dict (or None if no such id).

    When `owner` is given, the load is scoped to that owner: a deal belonging to a
    different user (or a legacy NULL-owner deal) returns None rather than loading,
    so a user can never open another user's deal by guessing its id.
    """
    connection = _connect(db_path)
    try:
        cursor = _dict_cursor(connection)
        if owner is None:
            cursor.execute(_q("SELECT * FROM deals WHERE id = ?"), (deal_id,))
        else:
            cursor.execute(
                _q("SELECT * FROM deals WHERE id = ? AND owner = ?"), (deal_id, owner)
            )
        row = cursor.fetchone()
    finally:
        connection.close()

    if row is None:
        return None

    # The row's keys are the column names, which match the engine's input keys, so
    # this dict can be fed straight back into the engine to recompute the metrics.
    deal = dict(row)
    # The rent roll is stored as JSON text — parse it back to a list (None if absent).
    if deal.get("rent_roll"):
        try:
            deal["rent_roll"] = json.loads(deal["rent_roll"])
        except (TypeError, ValueError):
            deal["rent_roll"] = None
    return deal


def list_deals(owner=None, db_path=DATABASE_PATH):
    """Return saved deals as a list of dicts (id, name, created_at), oldest first.

    When `owner` is given, only that owner's deals are returned — legacy deals with a
    NULL owner are never matched, so they stay hidden from signed-in users. When owner
    is None (e.g. the module demo), every deal is returned.
    """
    connection = _connect(db_path)
    try:
        cursor = _dict_cursor(connection)
        if owner is None:
            cursor.execute(_q("SELECT id, name, created_at FROM deals ORDER BY id"))
        else:
            cursor.execute(
                _q("SELECT id, name, created_at FROM deals WHERE owner = ? ORDER BY id"),
                (owner,),
            )
        rows = cursor.fetchall()
    finally:
        connection.close()

    return [dict(row) for row in rows]


if __name__ == "__main__":
    # Import the engine only inside the demo. The try/except lets this file run
    # both as a plain script (python cre_underwriter/database.py) and as a module
    # (python -m cre_underwriter.database).
    try:
        from cre_underwriter.engine import sample_deal, summarize_deal
    except ModuleNotFoundError:
        from engine import sample_deal, summarize_deal

    print(f"Database file: {DATABASE_PATH}")

    # 1. Make sure the table exists.
    initialize_database()
    print("Initialized database (table ready).")

    # 2. Save the hardcoded sample deal and get back its new id.
    #    Note: save always inserts, so re-running this demo appends another copy.
    new_id = save_deal(sample_deal)
    print(f"Saved sample deal  ->  id {new_id}")

    # 3. List everything currently stored (id, date, name).
    print("\nSaved deals:")
    for row in list_deals():
        print(f"  id {row['id']:>3}  |  {row['created_at']}  |  {row['name']}")

    # 4. Load that deal back out of the database by its id.
    loaded_deal = load_deal(new_id)
    print(f"\nLoaded deal id {new_id} back from the database.")

    # 5. Prove the round trip: the loaded inputs run straight through the engine
    #    and buy/pass logic. No metrics were stored — the engine derives them fresh.
    summarize_deal(loaded_deal)
