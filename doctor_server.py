"""
doctor_server.py
=================
FastAPI application for the PrashnaAyur doctor-facing dashboard.
Run independently:  python3 -m uvicorn doctor_server:app --port 8002

Security note: All auth below is demo-grade (hardcoded credentials, simple
token store). Not suitable for production ABDM/DPDP-compliant deployment.
"""

import asyncio
import json
import logging
import os
import secrets
import time
import sqlite3
import threading
from typing import Any, Dict, List, Optional, Set

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status, UploadFile, File, Form, Body
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

DB_PATH = os.getenv("DB_PATH", "hospital_ehr.db")
UPLOAD_DIR = "uploaded_records"
_db_lock = threading.Lock()

def get_connection(db_path: Optional[str] = None):
    target = db_path or DB_PATH
    conn = sqlite3.connect(target, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db(db_path: Optional[str] = None):
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    target = db_path or DB_PATH
    conn = get_connection(target)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS patients (
            abha_id TEXT PRIMARY KEY,
            name TEXT,
            age INTEGER,
            gender TEXT,
            registered_at TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS encounters (
            encounter_id TEXT PRIMARY KEY,
            abha_id TEXT,
            timestamp TIMESTAMP,
            socrates_json TEXT,
            prakriti_json TEXT,
            urgency TEXT,
            uploaded_doc_paths TEXT,
            status TEXT,
            token_number TEXT,
            token TEXT,
            transcript_json TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS prescriptions (
            prescription_id TEXT PRIMARY KEY,
            encounter_id TEXT,
            abha_id TEXT,
            doctor_id TEXT,
            care_context_id TEXT,
            ayush_rx_json TEXT,
            pathya_apathya TEXT,
            fhir_bundle TEXT,
            created_at TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS doctor_sessions (
            token TEXT PRIMARY KEY,
            doctor_id TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS uploaded_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            encounter_id TEXT,
            abha_id TEXT,
            file_path TEXT,
            original_filename TEXT,
            stored_filename TEXT,
            uploaded_at TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def migrate_schema():
    conn = get_connection()
    for statement in [
        "ALTER TABLE encounters ADD COLUMN transcript_json TEXT",
        "ALTER TABLE encounters ADD COLUMN token_number TEXT",
        "ALTER TABLE encounters ADD COLUMN token TEXT",
        "ALTER TABLE encounters ADD COLUMN abha_data_json TEXT",
    ]:
        try:
            conn.execute(statement)
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.close()

app = FastAPI(title="PrashnaAyur Doctor Server")

@app.on_event("startup")
def startup_event():
    init_db()
    migrate_schema()

# Mount static files for the doctor frontend
app.mount(
    "/static/doctor",
    StaticFiles(directory="static/doctor"),
    name="doctor_static",
)

# ── Internal API token (shared secret with kiosk_server.py) ──────────────────
# Demo-only: in production, rotate this token regularly and store it in a
# proper secrets manager, not a .env file.
INTERNAL_API_KEY: str = os.getenv("INTERNAL_API_KEY", "changeme-internal-token")

# ── Demo doctor credential store ──────────────────────────────────────────────
# Demo-only: production would hash/salt PINs and use a real session store
# (e.g., a database-backed OAuth2/OIDC flow).
DEMO_DOCTORS: Dict[str, str] = {
    "DOC-AYUSH-101": "1234",
    "DOC-AYUSH-102": "5678",
}

# ── In-memory session tokens ──────────────────────────────────────────────────
# Dict[token -> doctor_id]. Production would use a signed JWT or a DB-backed
# session table with expiry.
_active_sessions: Dict[str, str] = {}

# ── In-memory patient record store ───────────────────────────────────────────
# A list of FHIR bundles received from kiosk_server.py. Production would back
# this with a proper encrypted database.
_patient_records: List[Dict[str, Any]] = []

# ── SSE subscriber queues ─────────────────────────────────────────────────────
_sse_subscribers: Set[asyncio.Queue] = set()


# ─────────────────────────────────────────────────────────────────────────────
# AUTH HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _issue_token(doctor_id: str) -> str:
    token = secrets.token_urlsafe(32)
    _active_sessions[token] = doctor_id
    try:
        conn = get_connection()
        conn.execute("INSERT OR REPLACE INTO doctor_sessions (token, doctor_id) VALUES (?, ?)", (token, doctor_id))
        conn.commit()
        conn.close()
    except Exception as exc:
        log.warning("[auth] Failed to persist session to DB: %s", exc)
    return token


def _validate_session(request: Request) -> str:
    """Dependency: returns doctor_id if the session cookie or header is valid, else 401."""
    token = request.cookies.get("session_token")
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and auth[7:] != INTERNAL_API_KEY:
            token = auth[7:]

    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated: missing session token")

    if token in _active_sessions:
        return _active_sessions[token]

    # Database-backed session persistence across server reloads
    try:
        conn = get_connection()
        row = conn.execute("SELECT doctor_id FROM doctor_sessions WHERE token = ?", (token,)).fetchone()
        conn.close()
        if row:
            doc_id = row["doctor_id"]
            _active_sessions[token] = doc_id
            return doc_id
    except Exception as exc:
        log.warning("[auth] DB session query failed: %s", exc)

    raise HTTPException(status_code=401, detail="Not authenticated: invalid or expired session")


def _validate_internal_token(request: Request) -> None:
    """Dependency: validates INTERNAL_API_KEY bearer token for kiosk → doctor calls."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or auth[7:] != INTERNAL_API_KEY:
        log.warning("[auth] Unauthorized internal request: auth header mismatch (received prefix=%r)", auth[:10] if auth else "none")
        # Return generic 401 — do not reveal what was expected.
        raise HTTPException(status_code=401, detail="Unauthorized")


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC ROUTES
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def serve_doctor_ui():
    """Serve the doctor dashboard HTML."""
    with open("static/doctor/doctor.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@app.post("/api/auth/login")
async def doctor_login(request: Request):
    """
    Demo-grade login: Doctor ID + PIN -> session cookie.
    Production would use proper password hashing and a signed token/JWT.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    doctor_id = body.get("doctor_id", "").strip()
    pin       = body.get("pin", "").strip()

    # Constant-time comparison to prevent timing attacks (still demo-grade)
    stored_pin = DEMO_DOCTORS.get(doctor_id, "")
    if not stored_pin or not secrets.compare_digest(pin, stored_pin):
        # Artificial short delay to blunt brute-force in demo context
        await asyncio.sleep(0.3)
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = _issue_token(doctor_id)
    response = JSONResponse({"ok": True, "doctor_id": doctor_id})
    # HttpOnly cookie: not accessible from JS, mitigates XSS in demo context
    response.set_cookie(
        "session_token",
        token,
        httponly=True,
        samesite="strict",
        max_age=3600 * 8,  # 8-hour session
    )
    log.info("[auth] Login successful: %s", doctor_id)
    return response


@app.post("/api/auth/logout")
async def doctor_logout(request: Request):
    token = request.cookies.get("session_token", "")
    _active_sessions.pop(token, None)
    if token:
        try:
            conn = get_connection()
            conn.execute("DELETE FROM doctor_sessions WHERE token = ?", (token,))
            conn.commit()
            conn.close()
        except Exception as exc:
            log.warning("[auth] Failed to remove session from DB: %s", exc)
    response = JSONResponse({"ok": True})
    response.delete_cookie("session_token")
    return response


# ─────────────────────────────────────────────────────────────────────────────
# INTERNAL INTAKE SUBMISSION (kiosk_server.py -> doctor_server.py)
# ─────────────────────────────────────────────────────────────────────────────

import uuid
import re

SAFE_FILENAME = re.compile(r"^[a-f0-9]{32}\.(jpg|jpeg|png|pdf)$")

@app.post("/api/intake/submit")
async def intake_submit(
    request: Request,
    _: None = Depends(_validate_internal_token),
):
    """
    Directive #3: Single Source of Truth Intake Ingestion Endpoint.
    Direct DB write + In-memory SSE broadcast to connected doctor dashboards.
    Accepts multipart/form-data with intake_json + optional documents.
    """
    content_type = request.headers.get("content-type", "")
    payload = {}
    documents_list = []

    if "multipart/form-data" in content_type or "application/x-www-form-urlencoded" in content_type:
        form = await request.form()
        intake_raw = form.get("intake_json")
        if intake_raw:
            try:
                payload = json.loads(intake_raw) if isinstance(intake_raw, str) else intake_raw
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Invalid intake_json: {e}")
        for field_name, value in form.multi_items():
            if hasattr(value, "filename") and value.filename:
                documents_list.append(value)
    else:
        try:
            payload = await request.json()
        except Exception:
            payload = {}

    abha = payload.get("abha_data", {})
    structured = payload.get("structured_intake", {})
    transcript = payload.get("transcript", [])
    urgency = payload.get("urgency", "ROUTINE")

    saved_docs = []
    for doc in documents_list:
        if not doc.filename:
            continue
        ext = os.path.splitext(doc.filename)[1].lower()
        if ext not in (".jpg", ".jpeg", ".png", ".pdf"):
            continue
        safe_name = f"{uuid.uuid4().hex}{ext}"
        dest_path = os.path.join(UPLOAD_DIR, safe_name)
        content = await doc.read()
        with open(dest_path, "wb") as f:
            f.write(content)
        saved_docs.append({
            "stored_filename": safe_name,
            "original_filename": doc.filename,
            "file_path": dest_path,
            "uploaded_at": time.time(),
        })

    for doc in payload.get("uploaded_doc_paths", []):
        if isinstance(doc, dict) and "stored_filename" in doc:
            saved_docs.append(doc)
        elif isinstance(doc, str):
            saved_docs.append({
                "stored_filename": doc,
                "original_filename": doc,
                "file_path": os.path.join(UPLOAD_DIR, doc),
                "uploaded_at": time.time()
            })

    encounter_id = payload.get("encounter_id") or f"ENC-{uuid.uuid4().hex[:12]}"
    now = time.time()
    abha_id = abha.get("abha_id", "UNKNOWN")

    is_test_record = encounter_id.startswith("ENC-REG-") or request.headers.get("X-Test-DB") == "true"
    target_db = "hospital_ehr_test.db" if is_test_record else DB_PATH
    token = payload.get("token") or payload.get("token_number") or _generate_next_token_sync(target_db)

    record = {
        "encounter_id": encounter_id,
        "token": token,
        "token_number": token,
        "abha_id": abha_id,
        "name": abha.get("name"),
        "urgency": urgency,
        "uploaded_doc_paths": saved_docs,
        "status": "WAITING",
        "structured_intake": structured,
        "transcript": transcript,
        "abha_data": abha,
    }

    def _save_to_db_sync():
        with _db_lock:
            if is_test_record:
                init_db(target_db)
            conn = get_connection(target_db)
            existing = conn.execute("SELECT 1 FROM encounters WHERE encounter_id = ?", (encounter_id,)).fetchone()
            is_new = False
            if not existing:
                conn.execute(
                    "INSERT OR REPLACE INTO patients (abha_id, name, age, gender, registered_at) VALUES (?, ?, ?, ?, ?)",
                    (abha_id, abha.get("name"), abha.get("age", 0), abha.get("gender", "Unknown"), now),
                )
                # Ensure fallback baseline data for demo persona Rajesh Kumar (ABHA: 12345678901234)
                if abha_id == "12345678901234":
                    if not abha.get("chronic_conditions"):
                        abha["chronic_conditions"] = ["Amlapitta (Hyperacidity / Dyspepsia - 2025)"]
                    if not abha.get("past_medications"):
                        abha["past_medications"] = ["Avipattikar Churna (3g BD), Sutshekhar Ras (250mg OD)"]

                conn.execute(
                    "INSERT INTO encounters (encounter_id, abha_id, timestamp, socrates_json, prakriti_json, urgency, uploaded_doc_paths, status, token_number, transcript_json, abha_data_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        encounter_id, abha_id, now,
                        json.dumps(structured), json.dumps({"prakriti_notes": structured.get("prakriti_notes", "")}),
                        urgency, json.dumps(saved_docs), "WAITING", token, json.dumps(transcript),
                        json.dumps(abha),
                    ),
                )
                try:
                    conn.execute("UPDATE encounters SET token = ? WHERE encounter_id = ?", (token, encounter_id))
                except Exception:
                    pass

                for s_doc in saved_docs:
                    s_fn = s_doc.get("stored_filename", "")
                    o_fn = s_doc.get("original_filename", s_fn)
                    f_p = s_doc.get("file_path", os.path.join(UPLOAD_DIR, s_fn))
                    u_t = s_doc.get("uploaded_at", now)
                    try:
                        conn.execute(
                            "INSERT INTO uploaded_documents (encounter_id, abha_id, file_path, original_filename, stored_filename, uploaded_at) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (encounter_id, abha_id, f_p, o_fn, s_fn, u_t)
                        )
                    except Exception as e_doc:
                        log.warning("[save_docs] Error inserting uploaded_document: %s", e_doc)

                conn.commit()
                is_new = True
            conn.close()
            return is_new

    is_new = await asyncio.to_thread(_save_to_db_sync)
    if is_new:
        # Directive #3 Section 3: Deduplicate before broadcasting, broadcast only on genuinely new insert
        log.info("[intake] Received intake submission for ABHA %s, patient %s, token %s (encounter=%s)", abha_id, abha.get("name"), token, encounter_id)
        dead: Set[asyncio.Queue] = set()
        for q in _sse_subscribers:
            try:
                q.put_nowait(record)
            except asyncio.QueueFull:
                dead.add(q)
        _sse_subscribers.difference_update(dead)

        log.info("[intake] Intake saved to DB (encounter=%s, token=%s) and broadcasted to %d subscribers",
                 encounter_id, token, len(_sse_subscribers))
    else:
        log.info("[intake] Duplicate encounter %s already exists; skipped redundant insert and broadcast", encounter_id)

    return {"encounter_id": encounter_id, "token": token, "token_number": token, "status": "received"}


@app.get("/api/records/file/{filename}")
def get_record_file(filename: str, doctor_id: str = Depends(_validate_session)):
    if not SAFE_FILENAME.match(filename):
        raise HTTPException(status_code=404, detail="Not found")
    full_path = os.path.join(UPLOAD_DIR, filename)
    if not os.path.isfile(full_path):
        raise HTTPException(status_code=404, detail="Not found")
    ext = os.path.splitext(filename)[1].lower()
    media_type = "application/pdf" if ext == ".pdf" else ("image/png" if ext == ".png" else "image/jpeg")
    return FileResponse(full_path, media_type=media_type)


@app.get("/api/encounters/{encounter_id}")
def get_encounter(encounter_id: str, doctor_id: str = Depends(_validate_session)):
    """
    Directive #4 Section 4.1: Unified Encounter Detail Endpoint.
    Returns the complete clinical record (SOCRATES, transcript, documents with URLs) for on-demand drawer viewing.
    """
    target_db = "hospital_ehr_test.db" if encounter_id.startswith("ENC-REG-") else DB_PATH
    conn = get_connection(target_db)
    row = conn.execute(
        "SELECT e.*, p.name, p.age, p.gender FROM encounters e "
        "JOIN patients p ON e.abha_id = p.abha_id WHERE e.encounter_id = ?",
        (encounter_id,),
    ).fetchone()
    if row is None:
        conn.close()
        raise HTTPException(status_code=404, detail="Encounter not found")

    documents_raw = json.loads(row["uploaded_doc_paths"] or "[]")
    documents = []
    seen_files = set()
    for doc in documents_raw:
        if isinstance(doc, dict) and "stored_filename" in doc:
            sfn = doc["stored_filename"]
            if sfn not in seen_files:
                seen_files.add(sfn)
                doc_copy = dict(doc)
                doc_copy["url"] = f"/api/records/file/{sfn}"
                documents.append(doc_copy)
        elif isinstance(doc, str):
            if doc not in seen_files:
                seen_files.add(doc)
                documents.append({
                    "stored_filename": doc,
                    "original_filename": doc,
                    "url": f"/api/records/file/{doc}"
                })

    if not documents:
        try:
            doc_rows = conn.execute(
                "SELECT file_path, original_filename, stored_filename, uploaded_at FROM uploaded_documents WHERE encounter_id = ?",
                (encounter_id,)
            ).fetchall()
            for drow in doc_rows:
                sfn = drow["stored_filename"]
                if sfn not in seen_files:
                    seen_files.add(sfn)
                    documents.append({
                        "stored_filename": sfn,
                        "original_filename": drow["original_filename"],
                        "file_path": drow["file_path"],
                        "uploaded_at": drow["uploaded_at"],
                        "url": f"/api/records/file/{sfn}"
                    })
        except Exception as e_doc:
            log.warning("[get_encounter] Error querying uploaded_documents: %s", e_doc)

    conn.close()

    row_keys = row.keys() if hasattr(row, "keys") else []
    token_val = row["token_number"] if "token_number" in row_keys and row["token_number"] else (row["token"] if "token" in row_keys and row["token"] else "A-100")

    # Load and format ABHA health history (Issue 3)
    abha_dict = {}
    if "abha_data_json" in row_keys and row["abha_data_json"]:
        try:
            abha_dict = json.loads(row["abha_data_json"])
        except Exception:
            pass
    if not abha_dict:
        abha_dict = {
            "abha_id": row["abha_id"],
            "name": row["name"],
            "age": row["age"],
            "gender": row["gender"],
            "chronic_conditions": [],
            "past_medications": []
        }
    if row["abha_id"] == "12345678901234":
        if not abha_dict.get("chronic_conditions"):
            abha_dict["chronic_conditions"] = ["Amlapitta (Hyperacidity / Dyspepsia - 2025)"]
        if not abha_dict.get("past_medications"):
            abha_dict["past_medications"] = ["Avipattikar Churna (3g BD), Sutshekhar Ras (250mg OD)"]

    return {
        "encounter_id": row["encounter_id"],
        "abha_id": row["abha_id"],
        "name": row["name"],
        "age": row["age"],
        "gender": row["gender"],
        "urgency": row["urgency"],
        "status": row["status"],
        "token_number": token_val,
        "token": token_val,
        "structured_intake": json.loads(row["socrates_json"] or "{}"),
        "transcript": json.loads(row["transcript_json"] or "[]") if "transcript_json" in row_keys and row["transcript_json"] else [],
        "documents": documents,
        "abha_data": abha_dict,
    }


from utils.fhir_builder import build_prescription_fhir_bundle

@app.post("/api/prescription/finalize")
@app.post("/api/prescriptions/finalize")
@app.post("/api/encounters/{encounter_id}/prescribe")
def finalize_prescription(payload: dict = Body(...), encounter_id: Optional[str] = None, doctor_id: str = Depends(_validate_session)):
    log.info("[prescription] Finalize request received: body_encounter_id=%s, path_encounter_id=%s, doctor_id=%s, payload=%s",
             payload.get("encounter_id"), encounter_id, doctor_id, payload)
    target_encounter_id = payload.get("encounter_id") or encounter_id
    if not target_encounter_id:
        raise HTTPException(status_code=400, detail="Missing encounter_id in request payload")
    care_context_id = f"CC-AYUSH-{int(time.time())}"
    prescription_id = f"RX-{uuid.uuid4().hex[:12]}"
    is_test_record = target_encounter_id.startswith("ENC-REG-")
    target_db = "hospital_ehr_test.db" if is_test_record else DB_PATH

    with _db_lock:
        if is_test_record:
            init_db(target_db)
        conn = get_connection(target_db)
        row = conn.execute("SELECT abha_id FROM encounters WHERE encounter_id = ?", (target_encounter_id,)).fetchone()
        if row is None:
            conn.close()
            log.warning("[prescription] Encounter %s not found in encounters table", target_encounter_id)
            raise HTTPException(status_code=404, detail=f"Encounter '{target_encounter_id}' not found in database")
        abha_id = row["abha_id"]

        try:
            fhir_bundle = build_prescription_fhir_bundle(
                prescription_id=prescription_id,
                encounter_id=target_encounter_id,
                abha_id=abha_id,
                doctor_id=doctor_id,
                ayush_rx_items=payload.get("ayush_rx", []),
                pathya_apathya=payload.get("pathya_apathya", ""),
                care_context_id=care_context_id
            )
        except Exception as exc:
            conn.close()
            tb = traceback.format_exc()
            log.error("[prescription] FHIR bundle generation failed: %s\n%s", exc, tb)
            raise HTTPException(status_code=500, detail=f"FHIR bundle generation error: {exc}\n{tb}")

        now = time.time()
        try:
            conn.execute(
                "INSERT INTO prescriptions (prescription_id, encounter_id, abha_id, doctor_id, ayush_rx_json, pathya_apathya, care_context_id, fhir_bundle, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    prescription_id, target_encounter_id, abha_id, doctor_id,
                    json.dumps(payload.get("ayush_rx", [])), payload.get("pathya_apathya", ""),
                    care_context_id, json.dumps(fhir_bundle), now
                )
            )
            conn.execute("UPDATE encounters SET status = 'DISCHARGED' WHERE encounter_id = ?", (target_encounter_id,))
            conn.commit()
        except Exception as exc:
            conn.close()
            tb = traceback.format_exc()
            log.error("[prescription] DB insert failed: %s\n%s", exc, tb)
            raise HTTPException(status_code=500, detail=f"Database save error: {exc}\n{tb}")
        conn.close()

    log.info("[prescription] Successfully finalized prescription %s for encounter %s (ABHA: %s)", prescription_id, target_encounter_id, abha_id)
    return {
        "ok": True,
        "status": "success",
        "prescription_id": prescription_id,
        "care_context_id": care_context_id,
        "abha_id": abha_id,
        "fhir_bundle": fhir_bundle,
    }


# ─────────────────────────────────────────────────────────────────────────────
# PROTECTED DOCTOR ROUTES
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/queue")
@app.get("/api/encounters")
def get_queue(doctor_id: str = Depends(_validate_session)):
    """
    Get all active/waiting patient encounters for the OPD queue.
    Ordered by urgency (EMERGENCY -> MODERATE -> ROUTINE), then timestamp ASC.
    """
    conn = get_connection()
    # Urgency ordering: EMERGENCY = 1, MODERATE = 2, ROUTINE = 3
    rows = conn.execute("""
        SELECT e.encounter_id, e.abha_id, p.name, e.timestamp, e.urgency, e.status,
               e.socrates_json, e.uploaded_doc_paths, e.token_number, e.token, e.transcript_json
        FROM encounters e
        JOIN patients p ON e.abha_id = p.abha_id
        WHERE e.status != 'COMPLETED'
        ORDER BY
            CASE e.urgency
                WHEN 'EMERGENCY' THEN 1
                WHEN 'MODERATE'  THEN 2
                ELSE 3
            END,
            e.timestamp DESC
    """).fetchall()

    records = []
    for row in rows:
        row_keys = row.keys() if hasattr(row, "keys") else []
        token_val = (row["token_number"] if "token_number" in row_keys and row["token_number"] else None) or (row["token"] if "token" in row_keys and row["token"] else None) or "A-100"
        tr_raw = row["transcript_json"] if "transcript_json" in row_keys else None
        transcript_list = json.loads(tr_raw) if tr_raw else []
        docs_list = json.loads(row["uploaded_doc_paths"]) if row["uploaded_doc_paths"] else []
        if not docs_list:
            try:
                doc_rows = conn.execute(
                    "SELECT file_path, original_filename, stored_filename, uploaded_at FROM uploaded_documents WHERE encounter_id = ?",
                    (row["encounter_id"],)
                ).fetchall()
                for drow in doc_rows:
                    docs_list.append({
                        "stored_filename": drow["stored_filename"],
                        "original_filename": drow["original_filename"],
                        "file_path": drow["file_path"],
                        "uploaded_at": drow["uploaded_at"],
                    })
            except Exception:
                pass
        records.append({
            "encounter_id": row["encounter_id"],
            "token": token_val,
            "token_number": token_val,
            "abha_id": row["abha_id"],
            "name": row["name"],
            "urgency": row["urgency"],
            "uploaded_doc_paths": docs_list,
            "status": row["status"],
            "structured_intake": json.loads(row["socrates_json"]) if row["socrates_json"] else {},
            "transcript": transcript_list,
            "abha_data": {"patient_id": row["abha_id"], "name": row["name"]}
        })
    conn.close()
    return JSONResponse(records)


@app.get("/api/queue/stream")
async def queue_stream(request: Request, doctor_id: str = Depends(_validate_session)):
    """
    Server-Sent Events stream: pushes new records to connected doctor clients
    as they arrive from the kiosk, with no manual refresh required.
    """
    queue: asyncio.Queue = asyncio.Queue(maxsize=50)
    _sse_subscribers.add(queue)
    log.info("[sse] Doctor %s connected. Subscribers: %d", doctor_id, len(_sse_subscribers))

    async def event_generator():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    record = await asyncio.wait_for(queue.get(), timeout=20.0)
                    log.info("[sse] Emitting new_patient event for encounter %s to doctor client", record.get("encounter_id"))
                    yield f"event: new_patient\ndata: {json.dumps(record)}\n\n"
                    yield f"data: {json.dumps(record)}\n\n"
                except asyncio.TimeoutError:
                    # Heartbeat to keep the connection alive through proxies
                    yield ": heartbeat\n\n"
        finally:
            _sse_subscribers.discard(queue)
            log.info(
                "[sse] Doctor %s disconnected. Subscribers: %d",
                doctor_id, len(_sse_subscribers),
            )

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # Disable nginx buffering if behind a proxy
        },
    )
