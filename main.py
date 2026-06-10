"""
main.py
-------
WinTexas.US — FastAPI backend
Handles voter search, magic link auth, recruiter dashboard, and live counter.

Endpoints:
    GET  /                        → serves index.html (QR landing page)
    GET  /health                  → Railway health check
    GET  /static/{filename}       → serves static HTML files
    POST /auth/request-link       → sends magic link via email or SMS
    GET  /auth/verify             → validates magic link token
    POST /verify/search           → searches voter file for recruiter signup
    POST /verify/confirm          → confirms recruiter's own voter record
    POST /voters/search           → searches voter file when adding a committed voter
    POST /voters/add              → adds a committed voter
    GET  /stats/count             → returns live committed voter count (public)
    GET  /stats/me                → returns recruiter's personal stats

Requirements:
    pip install fastapi uvicorn psycopg2-binary python-dotenv rapidfuzz twilio sendgrid
"""

import os
import secrets
import hashlib
from datetime import datetime, timedelta
from typing import Optional

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Cookie, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from rapidfuzz import fuzz

load_dotenv()

# ── App setup ─────────────────────────────────────────────────────────────────
app = FastAPI(title="WinTexas.US", docs_url=None, redoc_url=None)

# Serve static files (HTML screens)
app.mount("/static", StaticFiles(directory="static"), name="static")

# ── Database connection ────────────────────────────────────────────────────────
def get_conn():
    return psycopg2.connect(
        os.getenv("DATABASE_URL"),
        keepalives=1,
        keepalives_idle=60,
        keepalives_interval=10,
        keepalives_count=5,
        cursor_factory=psycopg2.extras.RealDictCursor
    )

# ── Constants ──────────────────────────────────────────────────────────────────
TOKEN_EXPIRY_MINUTES = 15
SESSION_EXPIRY_DAYS = 7
FUZZY_MATCH_THRESHOLD = 75       # minimum score (0-100) for a name match
MAX_SEARCH_RESULTS = 5           # max voter matches to return

# ── Pydantic models (request bodies) ──────────────────────────────────────────
class MagicLinkRequest(BaseModel):
    contact: str                  # phone number or email

class VoterSearchRequest(BaseModel):
    first_name: str
    last_name: str
    street_name: Optional[str] = None
    city: Optional[str] = None

class ConfirmVoterRequest(BaseModel):
    voter_id: str

class AddVoterRequest(BaseModel):
    voter_id: str

# ── Helpers ────────────────────────────────────────────────────────────────────
def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()

def get_session_recruiter(session_token: Optional[str]) -> Optional[dict]:
    """Look up recruiter by session token. Returns recruiter row or None."""
    if not session_token:
        return None
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, voter_id, voters_added, verified
                FROM recruiters
                WHERE magic_token = %s
                AND token_expires > NOW()
                AND verified = TRUE
            """, (hash_token(session_token),))
            return cur.fetchone()
    except Exception:
        return None
    finally:
        conn.close()

def search_voter_file(first_name: str, last_name: str,
                      street_name: str = None, city: str = None) -> list:
    """
    Fuzzy search voter_file by name + optional street/city.
    Returns up to MAX_SEARCH_RESULTS candidates scored by match quality.
    """
    first_upper = first_name.strip().upper()
    last_upper = last_name.strip().upper()

    try:
        conn = get_conn()
        with conn.cursor() as cur:
            # Start with exact last name match for speed, then fuzzy filter
            if city:
                cur.execute("""
                    SELECT voter_id, first_name, last_name, house_number,
                           dir_prefix, street_name, street_type, unit_number,
                           city, zipcode, primary_2026
                    FROM voter_file
                    WHERE last_name_upper = %s
                    AND city_upper = %s
                    LIMIT 100
                """, (last_upper, city.strip().upper()))
            else:
                cur.execute("""
                    SELECT voter_id, first_name, last_name, house_number,
                           dir_prefix, street_name, street_type, unit_number,
                           city, zipcode, primary_2026
                    FROM voter_file
                    WHERE last_name_upper = %s
                    LIMIT 100
                """, (last_upper,))

            rows = cur.fetchall()

        # Fuzzy score each result
        scored = []
        for row in rows:
            row = dict(row)
            # Score first name match
            first_score = fuzz.ratio(first_upper, (row["first_name"] or "").upper())
            # Score street name match if provided
            street_score = 100
            if street_name and row["street_name"]:
                street_score = fuzz.partial_ratio(
                    street_name.strip().upper(),
                    (row["street_name"] or "").upper()
                )
            # Combined score — name weighted more than street
            combined = (first_score * 0.7) + (street_score * 0.3)
            if combined >= FUZZY_MATCH_THRESHOLD:
                row["match_score"] = round(combined)
                # Build display address
                parts = [
                    row.get("house_number") or "",
                    row.get("dir_prefix") or "",
                    row.get("street_name") or "",
                    row.get("street_type") or "",
                ]
                row["display_address"] = " ".join(p for p in parts if p).strip()
                scored.append(row)

        # Sort by score descending, return top results
        scored.sort(key=lambda x: x["match_score"], reverse=True)
        return scored[:MAX_SEARCH_RESULTS]

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Search error: {str(e)}")
    finally:
        conn.close()

def send_magic_link(contact: str, token: str):
    """
    Send magic link via Twilio (SMS) or SendGrid (email).
    Falls back to printing the link if credentials aren't set yet.
    """
    link = f"{os.getenv('APP_URL', 'http://localhost:8000')}/auth/verify?token={token}"

    # Detect email vs phone
    is_email = "@" in contact

    if is_email:
        sendgrid_key = os.getenv("SENDGRID_KEY")
        if sendgrid_key:
            try:
                import sendgrid
                from sendgrid.helpers.mail import Mail
                sg = sendgrid.SendGridAPIClient(sendgrid_key)
                message = Mail(
                    from_email=os.getenv("SENDGRID_FROM", "noreply@wintexas.us"),
                    to_emails=contact,
                    subject="Your WinTexas login link",
                    html_content=f"""
                        <p>Tap the link below to log in to WinTexas.US.</p>
                        <p><a href="{link}" style="background:#1a4fa0;color:white;padding:12px 24px;
                        border-radius:6px;text-decoration:none;font-size:16px;">Log in to WinTexas</a></p>
                        <p style="color:#888;font-size:12px;">This link expires in 15 minutes.</p>
                    """
                )
                sg.send(message)
                return
            except Exception as e:
                print(f"SendGrid error: {e}")
    else:
        twilio_sid = os.getenv("TWILIO_SID")
        if twilio_sid:
            try:
                from twilio.rest import Client
                client = Client(twilio_sid, os.getenv("TWILIO_TOKEN"))
                client.messages.create(
                    body=f"Your WinTexas login link: {link} (expires in 15 min)",
                    from_=os.getenv("TWILIO_FROM"),
                    to=contact
                )
                return
            except Exception as e:
                print(f"Twilio error: {e}")

    # Development fallback — print to console
    print(f"\n{'='*60}")
    print(f"MAGIC LINK (dev mode — no SMS/email credentials set):")
    print(f"{link}")
    print(f"{'='*60}\n")


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def root():
    """Serve the QR landing page."""
    with open("static/index.html") as f:
        return f.read()

@app.get("/health")
async def health():
    """Railway health check."""
    return {"status": "ok"}

@app.get("/recruiter", response_class=HTMLResponse)
async def recruiter_page():
    """Serve the recruiter dashboard."""
    with open("static/recruiter.html") as f:
        return f.read()

@app.get("/verify-page", response_class=HTMLResponse)
async def verify_page():
    """Serve the voter verification page."""
    with open("static/verify.html") as f:
        return f.read()


# ── Auth ───────────────────────────────────────────────────────────────────────

@app.post("/auth/request-link")
async def request_magic_link(body: MagicLinkRequest):
    """
    Accept phone or email. Create/update recruiter record.
    Send magic link. Token expires in 15 minutes.
    """
    contact = body.contact.strip()
    if not contact:
        raise HTTPException(status_code=400, detail="Phone or email required")

    # Generate token
    raw_token = secrets.token_urlsafe(32)
    hashed = hash_token(raw_token)
    expires = datetime.utcnow() + timedelta(minutes=TOKEN_EXPIRY_MINUTES)

    try:
        conn = get_conn()
        with conn.cursor() as cur:
            # Upsert recruiter record
            cur.execute("""
                INSERT INTO recruiters (contact, magic_token, token_expires)
                VALUES (%s, %s, %s)
                ON CONFLICT (contact) DO UPDATE SET
                    magic_token = EXCLUDED.magic_token,
                    token_expires = EXCLUDED.token_expires
                RETURNING id
            """, (contact, hashed, expires))
            conn.commit()

        send_magic_link(contact, raw_token)
        return {"message": "Magic link sent"}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()


@app.get("/auth/verify")
async def verify_magic_link(token: str, response: Response):
    """
    Validate magic link token.
    Set session cookie and redirect to verify page (if not yet verified)
    or recruiter dashboard (if already verified).
    """
    hashed = hash_token(token)

    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, verified, token_expires
                FROM recruiters
                WHERE magic_token = %s
            """, (hashed,))
            recruiter = cur.fetchone()

        if not recruiter:
            raise HTTPException(status_code=400, detail="Invalid or expired link")

        if recruiter["token_expires"] < datetime.utcnow():
            raise HTTPException(status_code=400, detail="Link has expired — please request a new one")

        # Issue a longer-lived session token
        session_token = secrets.token_urlsafe(32)
        session_hashed = hash_token(session_token)
        session_expires = datetime.utcnow() + timedelta(days=SESSION_EXPIRY_DAYS)

        with conn.cursor() as cur:
            cur.execute("""
                UPDATE recruiters
                SET magic_token = %s, token_expires = %s
                WHERE id = %s
            """, (session_hashed, session_expires, recruiter["id"]))
            conn.commit()

        # Set session cookie
        redirect_url = "/recruiter" if recruiter["verified"] else "/verify-page"
        resp = RedirectResponse(url=redirect_url, status_code=302)
        resp.set_cookie(
            key="session",
            value=session_token,
            httponly=True,
            max_age=SESSION_EXPIRY_DAYS * 86400,
            samesite="lax"
        )
        return resp

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()


# ── Voter verification (recruiter verifying themselves) ───────────────────────

@app.post("/verify/search")
async def verify_search(body: VoterSearchRequest, session: Optional[str] = Cookie(None)):
    """Search voter file to find the recruiter's own record."""
    results = search_voter_file(
        body.first_name, body.last_name,
        body.street_name, body.city
    )
    return {"results": results}


@app.post("/verify/confirm")
async def verify_confirm(body: ConfirmVoterRequest, session: Optional[str] = Cookie(None)):
    """
    Recruiter confirms their own voter record.
    Links their recruiter record to voter_file and marks them verified.
    Also adds them to committed_voters if not already there.
    """
    recruiter = get_session_recruiter(session)
    # Allow unverified recruiters to confirm (that's the point of this endpoint)
    if not session:
        raise HTTPException(status_code=401, detail="Not logged in")

    hashed = hash_token(session)

    try:
        conn = get_conn()
        with conn.cursor() as cur:
            # Check voter exists
            cur.execute("SELECT voter_id FROM voter_file WHERE voter_id = %s", (body.voter_id,))
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="Voter not found")

            # Link recruiter to voter record and mark verified
            cur.execute("""
                UPDATE recruiters
                SET voter_id = %s, verified = TRUE
                WHERE magic_token = %s
            """, (body.voter_id, hashed))

            # Add to committed_voters if not already there
            cur.execute("""
                INSERT INTO committed_voters (voter_id, source)
                VALUES (%s, 'recruiter_self')
                ON CONFLICT (voter_id) DO NOTHING
            """, (body.voter_id,))

            conn.commit()

        return {"message": "Verified successfully"}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()


# ── Voter entry (recruiter adding committed voters) ───────────────────────────

@app.post("/voters/search")
async def voters_search(body: VoterSearchRequest, session: Optional[str] = Cookie(None)):
    """
    Search voter file when recruiter is adding a committed voter.
    Flags results that are already on the committed list.
    """
    recruiter = get_session_recruiter(session)
    if not recruiter:
        raise HTTPException(status_code=401, detail="Not logged in")

    results = search_voter_file(
        body.first_name, body.last_name,
        body.street_name, body.city
    )

    # Flag already-committed voters
    if results:
        voter_ids = [r["voter_id"] for r in results]
        try:
            conn = get_conn()
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT voter_id FROM committed_voters
                    WHERE voter_id = ANY(%s)
                """, (voter_ids,))
                committed = {row["voter_id"] for row in cur.fetchall()}
            for r in results:
                r["already_committed"] = r["voter_id"] in committed
        finally:
            conn.close()

    return {"results": results}


@app.post("/voters/add")
async def voters_add(body: AddVoterRequest, session: Optional[str] = Cookie(None)):
    """
    Add a voter to the committed list.
    Increments the recruiter's voters_added count.
    Returns the new total committed count.
    """
    recruiter = get_session_recruiter(session)
    if not recruiter:
        raise HTTPException(status_code=401, detail="Not logged in")

    try:
        conn = get_conn()
        with conn.cursor() as cur:
            # Check not already committed
            cur.execute("""
                SELECT id FROM committed_voters WHERE voter_id = %s
            """, (body.voter_id,))
            if cur.fetchone():
                # Already committed — return current count without error
                cur.execute("SELECT COUNT(*) as total FROM committed_voters")
                total = cur.fetchone()["total"]
                return {"already_committed": True, "total": total}

            # Add to committed_voters
            cur.execute("""
                INSERT INTO committed_voters (voter_id, added_by, source)
                VALUES (%s, %s, 'recruiter')
            """, (body.voter_id, recruiter["id"]))

            # Increment recruiter's count
            cur.execute("""
                UPDATE recruiters SET voters_added = voters_added + 1
                WHERE id = %s
            """, (recruiter["id"],))

            # Get new total
            cur.execute("SELECT COUNT(*) as total FROM committed_voters")
            total = cur.fetchone()["total"]

            conn.commit()

        return {"already_committed": False, "total": total}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()


# ── Stats ──────────────────────────────────────────────────────────────────────

@app.get("/stats/count")
async def stats_count():
    """
    Public endpoint — returns live committed voter count.
    Called every 30s by the homepage counter.
    """
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) as total FROM committed_voters")
            total = cur.fetchone()["total"]
        return {"total": total}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()


@app.get("/stats/me")
async def stats_me(session: Optional[str] = Cookie(None)):
    """
    Returns the current recruiter's personal stats:
    - voters_added: direct adds
    - network_size: everyone downstream in their referral tree
    """
    recruiter = get_session_recruiter(session)
    if not recruiter:
        raise HTTPException(status_code=401, detail="Not logged in")

    try:
        conn = get_conn()
        with conn.cursor() as cur:
            # Recursive CTE to count entire downstream network
            cur.execute("""
                WITH RECURSIVE tree AS (
                    SELECT id FROM recruiters WHERE id = %s
                    UNION ALL
                    SELECT r.id FROM recruiters r
                    JOIN tree t ON r.referred_by = t.id
                )
                SELECT COUNT(*) - 1 as network_size FROM tree
            """, (str(recruiter["id"]),))
            network_size = cur.fetchone()["network_size"]

        return {
            "voters_added": recruiter["voters_added"],
            "network_size": max(0, network_size)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()


# ── Add unique constraint on recruiters.contact (run once) ────────────────────
@app.on_event("startup")
async def startup():
    """Add any missing constraints or indexes on startup."""
    conn = None
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                ALTER TABLE recruiters
                ADD COLUMN IF NOT EXISTS contact VARCHAR(200) UNIQUE;
            """)
            conn.commit()
    except Exception:
        pass  # column already exists
    finally:
        if conn:
            conn.close()
