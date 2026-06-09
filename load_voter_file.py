"""
load_voter_file.py
------------------
Loads the Travis County voter CSV into the voter_file table in PostgreSQL.
Also seeds committed_voters from March 2024 D primary voters, and flags
2026 primary voters via the 2026P column.

2026P column values:
    ED = Early voting Democrat
    XD = In-person Democrat
    ER = Early voting Republican
    XR = In-person Republican
    NaN = Did not vote in 2026 primary

Usage:
    python load_voter_file.py --file path/to/travis_county.csv
    python load_voter_file.py --file path/to/travis_county.csv --seed path/to/march_primary.csv

Requirements:
    pip install pandas psycopg2-binary python-dotenv

Environment variables (put in a .env file):
    DATABASE_URL=postgresql://user:password@host:5432/wintexas
"""

import argparse
import os
import sys
import pandas as pd
import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv

load_dotenv()

# ── Column mapping: CSV column name → DB column name ──────────────────────────
COLUMN_MAP = {
    "PRECINCT":                 "precinct",
    "VUID":                     "voter_id",
    "FIRST NAME":               "first_name",
    "LAST NAME":                "last_name",
    "GENDER":                   "gender",
    "DOB":                      "dob",
    "PERM HOUSE NUMBER":        "house_number",
    "PERM DIRECTIONAL PREFIX":  "dir_prefix",
    "PERM STREET NAME":         "street_name",
    "PERM STREET TYPE":         "street_type",
    "PERM UNIT NUMBER":         "unit_number",
    "PERM CITY":                "city",
    "PERM ZIPCODE":             "zipcode",
    "2026P":                    "primary_2026",
}

# Valid 2026 primary codes and their meanings
PRIMARY_2026_CODES = {
    "ED": "Early Democrat",
    "XD": "In-person Democrat",
    "ER": "Early Republican",
    "XR": "In-person Republican",
}

# Democrat codes — used for auto-seeding committed_voters
DEMOCRAT_CODES = {"ED", "XD"}

# ── SQL ────────────────────────────────────────────────────────────────────────
CREATE_VOTER_FILE = """
CREATE TABLE IF NOT EXISTS voter_file (
    voter_id          VARCHAR(20)  PRIMARY KEY,
    precinct          VARCHAR(20),
    first_name        VARCHAR(100),
    last_name         VARCHAR(100),
    gender            VARCHAR(1),
    dob               VARCHAR(20),
    house_number      VARCHAR(20),
    dir_prefix        VARCHAR(10),
    street_name       VARCHAR(100),
    street_type       VARCHAR(20),
    unit_number       VARCHAR(20),
    city              VARCHAR(100),
    zipcode           VARCHAR(10),
    primary_2026      VARCHAR(5),   -- ED, XD, ER, XR, or NULL
    -- Search columns: uppercased + stripped for fast fuzzy matching
    first_name_upper  VARCHAR(100) GENERATED ALWAYS AS (UPPER(TRIM(first_name))) STORED,
    last_name_upper   VARCHAR(100) GENERATED ALWAYS AS (UPPER(TRIM(last_name))) STORED,
    street_name_upper VARCHAR(100) GENERATED ALWAYS AS (UPPER(TRIM(street_name))) STORED,
    city_upper        VARCHAR(100) GENERATED ALWAYS AS (UPPER(TRIM(city))) STORED
);
"""

CREATE_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_voter_last     ON voter_file (last_name_upper);
CREATE INDEX IF NOT EXISTS idx_voter_first    ON voter_file (first_name_upper);
CREATE INDEX IF NOT EXISTS idx_voter_city     ON voter_file (city_upper);
CREATE INDEX IF NOT EXISTS idx_voter_zip      ON voter_file (zipcode);
CREATE INDEX IF NOT EXISTS idx_voter_2026p    ON voter_file (primary_2026);
"""

CREATE_COMMITTED = """
CREATE TABLE IF NOT EXISTS committed_voters (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    voter_id    VARCHAR(20) NOT NULL REFERENCES voter_file(voter_id),
    added_by    UUID,           -- FK to recruiters table (added later)
    source      VARCHAR(20) NOT NULL DEFAULT 'seed',  -- 'seed' or 'recruiter'
    created_at  TIMESTAMP NOT NULL DEFAULT NOW(),
    UNIQUE(voter_id)            -- no duplicates ever
);
"""

CREATE_RECRUITERS = """
CREATE TABLE IF NOT EXISTS recruiters (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    voter_id        VARCHAR(20) REFERENCES voter_file(voter_id),
    referred_by     UUID REFERENCES recruiters(id),
    contact         VARCHAR(200),   -- phone or email
    magic_token     VARCHAR(200),
    token_expires   TIMESTAMP,
    voters_added    INTEGER NOT NULL DEFAULT 0,
    verified        BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMP NOT NULL DEFAULT NOW()
);
"""

CREATE_EVENTS = """
CREATE TABLE IF NOT EXISTS referral_events (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    recruiter_id    UUID REFERENCES recruiters(id),
    event_type      VARCHAR(50),    -- 'link_clicked', 'signup_completed', 'voter_added'
    created_at      TIMESTAMP NOT NULL DEFAULT NOW()
);
"""

UPSERT_VOTER = """
INSERT INTO voter_file (
    voter_id, precinct, first_name, last_name, gender, dob,
    house_number, dir_prefix, street_name, street_type,
    unit_number, city, zipcode, primary_2026
) VALUES %s
ON CONFLICT (voter_id) DO UPDATE SET
    precinct      = EXCLUDED.precinct,
    first_name    = EXCLUDED.first_name,
    last_name     = EXCLUDED.last_name,
    gender        = EXCLUDED.gender,
    dob           = EXCLUDED.dob,
    house_number  = EXCLUDED.house_number,
    dir_prefix    = EXCLUDED.dir_prefix,
    street_name   = EXCLUDED.street_name,
    street_type   = EXCLUDED.street_type,
    unit_number   = EXCLUDED.unit_number,
    city          = EXCLUDED.city,
    zipcode       = EXCLUDED.zipcode,
    primary_2026  = EXCLUDED.primary_2026;
"""

SEED_COMMITTED = """
INSERT INTO committed_voters (voter_id, source)
SELECT unnest(%s::varchar[]), 'seed'
ON CONFLICT (voter_id) DO NOTHING;
"""


def clean(val):
    """Normalize a value: strip whitespace, convert NaN to None."""
    if pd.isna(val):
        return None
    return str(val).strip() or None


def clean_2026p(val):
    """Validate the 2026P code — only accept known values."""
    cleaned = clean(val)
    if cleaned is None:
        return None
    upper = cleaned.upper()
    if upper in PRIMARY_2026_CODES:
        return upper
    return None  # ignore unknown codes


def load_voter_file(conn, filepath):
    """Read CSV and upsert all rows into voter_file."""
    print(f"\n── Loading voter file: {filepath}")
    df = pd.read_csv(filepath, dtype=str)

    # Rename columns to match DB
    df = df.rename(columns=COLUMN_MAP)

    # Keep only columns we care about
    db_cols = list(COLUMN_MAP.values())
    df = df[[c for c in db_cols if c in df.columns]]

    # Fill any missing optional columns with None
    for col in db_cols:
        if col not in df.columns:
            df[col] = None

    total = len(df)
    print(f"   Rows in file: {total:,}")

    # Report 2026P breakdown
    if "primary_2026" in df.columns:
        counts = df["primary_2026"].fillna("(no vote)").str.strip().str.upper().value_counts()
        print(f"   2026 primary breakdown:")
        for code, count in counts.items():
            label = PRIMARY_2026_CODES.get(code, "Did not vote in 2026 primary")
            print(f"     {code:>8}  {count:>8,}  — {label}")

    # Build list of tuples in column order
    rows = []
    for _, row in df.iterrows():
        rows.append((
            clean(row.get("voter_id")),
            clean(row.get("precinct")),
            clean(row.get("first_name")),
            clean(row.get("last_name")),
            clean(row.get("gender")),
            clean(row.get("dob")),
            clean(row.get("house_number")),
            clean(row.get("dir_prefix")),
            clean(row.get("street_name")),
            clean(row.get("street_type")),
            clean(row.get("unit_number")),
            clean(row.get("city")),
            clean(row.get("zipcode")),
            clean_2026p(row.get("primary_2026")),
        ))

    # Remove rows with no voter_id
    rows = [r for r in rows if r[0] is not None]
    skipped = total - len(rows)
    if skipped:
        print(f"   Skipped {skipped} rows with missing VUID")

    # Deduplicate on voter_id (keep last occurrence — has most recent 2026P data)
    seen = {}
    for row in rows:
        seen[row[0]] = row
    dupes = len(rows) - len(seen)
    rows = list(seen.values())
    if dupes:
        print(f"   Deduplicated {dupes:,} duplicate VUIDs (kept most recent)")

    # Batch upsert in chunks of 1000
    with conn.cursor() as cur:
        chunk_size = 1000
        loaded = 0
        for i in range(0, len(rows), chunk_size):
            chunk = rows[i:i + chunk_size]
            execute_values(cur, UPSERT_VOTER, chunk)
            loaded += len(chunk)
            print(f"   Upserted {loaded:,} / {len(rows):,} rows...", end="\r")
        conn.commit()

    print(f"\n   ✓ Done — {loaded:,} voter records loaded")
    return loaded


def seed_committed(conn, filepath=None, auto_seed_2026=True):
    """
    Seed committed_voters two ways:

    1. Auto-seed from 2026P column (ED or XD) — always runs if auto_seed_2026=True
    2. From a separate March 2024 D primary CSV if filepath is provided
    """
    with conn.cursor() as cur:

        # Auto-seed from 2026P column in voter_file
        if auto_seed_2026:
            print(f"\n── Auto-seeding committed_voters from 2026 Democrat primary voters...")
            cur.execute("""
                INSERT INTO committed_voters (voter_id, source)
                SELECT voter_id, 'seed_2026'
                FROM voter_file
                WHERE primary_2026 IN ('ED', 'XD')
                ON CONFLICT (voter_id) DO NOTHING;
            """)
            seeded_2026 = cur.rowcount
            conn.commit()
            print(f"   ✓ {seeded_2026:,} 2026 Democrat primary voters seeded")

        # Also seed from separate March 2024 primary file if provided
        if filepath:
            print(f"\n── Seeding from March 2024 primary file: {filepath}")
            df = pd.read_csv(filepath, dtype=str)
            df.columns = [c.strip().upper() for c in df.columns]

            # Filter if election columns present
            if "ELECTION_PARTY" in df.columns and "ELECTION_DATE" in df.columns:
                before = len(df)
                df = df[
                    (df["ELECTION_PARTY"].str.strip().str.upper() == "DEM") &
                    (df["ELECTION_DATE"].str.strip() == "03052024")
                ]
                print(f"   Filtered to March 2024 D primary: {before:,} → {len(df):,} rows")

            if "VUID" not in df.columns:
                print("   ERROR: No VUID column found in seed file. Skipping.")
                return

            vuids = [str(v).strip() for v in df["VUID"].dropna().unique()]
            print(f"   Unique VUIDs to seed: {len(vuids):,}")
            cur.execute(SEED_COMMITTED, (vuids,))
            seeded_2024 = cur.rowcount
            conn.commit()
            print(f"   ✓ {seeded_2024:,} March 2024 D primary voters seeded (duplicates skipped)")


def main():
    parser = argparse.ArgumentParser(description="Load WinTexas voter data into PostgreSQL")
    parser.add_argument("--file",  required=True, help="Path to voter registration CSV")
    parser.add_argument("--seed",  help="Path to March 2024 D primary CSV (optional — 2026P column auto-seeds by default)")
    parser.add_argument("--db",    help="Database URL (overrides DATABASE_URL env var)")
    parser.add_argument("--no-auto-seed", action="store_true", help="Skip auto-seeding from 2026P column")
    args = parser.parse_args()

    db_url = args.db or os.getenv("DATABASE_URL")
    if not db_url:
        print("ERROR: No database URL provided. Set DATABASE_URL in .env or pass --db")
        sys.exit(1)

    print(f"\nConnecting to database...")
    try:
        conn = psycopg2.connect(db_url)
        print("✓ Connected")
    except Exception as e:
        print(f"ERROR: Could not connect to database: {e}")
        sys.exit(1)

    # Create all tables
    print("\n── Creating tables if not exists...")
    with conn.cursor() as cur:
        cur.execute(CREATE_VOTER_FILE)
        cur.execute(CREATE_INDEXES)
        cur.execute(CREATE_COMMITTED)
        cur.execute(CREATE_RECRUITERS)
        cur.execute(CREATE_EVENTS)
        conn.commit()
    print("   ✓ Tables ready")

    # Load voter file
    load_voter_file(conn, args.file)

    # Seed committed voters
    seed_committed(
        conn,
        filepath=args.seed,
        auto_seed_2026=not args.no_auto_seed
    )

    # Final counts
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM voter_file;")
        vf_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM committed_voters;")
        cv_count = cur.fetchone()[0]
        cur.execute("SELECT primary_2026, COUNT(*) FROM voter_file GROUP BY primary_2026 ORDER BY primary_2026;")
        breakdown = cur.fetchall()

    print(f"\n── Final counts:")
    print(f"   voter_file:       {vf_count:,}")
    print(f"   committed_voters: {cv_count:,}")
    print(f"\n── 2026 primary breakdown in DB:")
    for code, count in breakdown:
        label = PRIMARY_2026_CODES.get(code, "Did not vote / not set")
        print(f"     {str(code):>5}  {count:>8,}  — {label}")
    print(f"\n✓ All done!\n")

    conn.close()


if __name__ == "__main__":
    main()
