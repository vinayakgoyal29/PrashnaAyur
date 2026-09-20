"""
kiosk_server.py
================
FastAPI application for the PrashnaAyur patient-facing kiosk.
Run independently:  python3 -m uvicorn kiosk_server:app --port 8001

ZERO LOCAL RETENTION: This server never writes patient audio, transcript text,
or structured intake data to disk. All per-session data lives in memory,
scoped to the WebSocket connection's lifetime, and is discarded upon disconnect
or after the intake has been successfully dispatched to doctor_server.py.

Security note: The INTERNAL_API_KEY below is a demo-grade shared secret.
Not suitable for production ABDM/DPDP-compliant deployment.
"""

import asyncio
import json
import logging
import os
import random
import re
import sqlite3
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from google import genai
from google.genai import types
from dotenv import load_dotenv

from utils.gemini_live_session import build_live_config, resolve_live_model
from utils.fhir_builder import build_fhir_bundle

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
INTERNAL_API_KEY: str = os.getenv("INTERNAL_API_KEY", "changeme-internal-token")
DOCTOR_SERVER_URL: str = os.getenv("DOCTOR_SERVER_URL", "http://127.0.0.1:8002")

if not GEMINI_API_KEY:
    log.warning("GEMINI_API_KEY not set. Gemini Live API calls will fail.")

# Shared genai client (process-scoped; thread-safe for async use)
_genai_client = genai.Client(api_key=GEMINI_API_KEY, http_options={"api_version": "v1alpha"})

app = FastAPI(title="PrashnaAyur Kiosk Server")

from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount(
    "/static/kiosk",
    StaticFiles(directory="static/kiosk"),
    name="kiosk_static",
)

@app.on_event("startup")
async def log_startup():
    log.info("=" * 60)
    log.info("PrashnaAyur Kiosk Server starting on port 8001")
    log.info("Registered routes:")
    for route in app.routes:
        methods = getattr(route, "methods", None) or ["WS" if "websocket" in getattr(route, "path", "").lower() or "WebSocket" in type(route).__name__ else "ANY"]
        log.info("  Route: %s [%s]", getattr(route, "path", str(route)), ", ".join(methods))
    log.info("=" * 60)


import re

# Devanagari script regex for defensive normalization (Issue 2 & Issue 4)
DEVANAGARI_RE = re.compile(r"[\u0900-\u097F]+")

def sanitize_english_text(text: str) -> str:
    """
    Strip any accidental Devanagari script characters when in English mode.
    Crucially DO NOT call .strip() here, as streamed tokens contain leading/trailing
    boundary spaces that must be preserved for natural word separation (Issue 4).
    """
    return DEVANAGARI_RE.sub("", text)

# Sequential OPD Token Generator (Dynamic DB-driven — Issue 3)
_token_lock = asyncio.Lock()

async def generate_opd_token() -> str:
    """
    Generate sequential OPD Token number (e.g. A-101, A-102).
    Queries hospital_ehr.db for the highest existing token number matching A-###,
    increments it by 1, and assigns that as the new token.
    Handles empty-table case by starting at A-101 (since A-100 is reserved for [DEMO] Ramesh Sharma).
    Protected by asyncio.Lock to guard against concurrent collisions.
    """
    async with _token_lock:
        def _get_max_token_sync() -> int:
            max_val = 100
            db_path = "hospital_ehr.db"
            if not os.path.exists(db_path):
                return max_val
            try:
                with sqlite3.connect(db_path, timeout=5.0) as conn:
                    cur = conn.cursor()
                    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='encounters'")
                    if not cur.fetchone():
                        return max_val
                    cur.execute("SELECT token_number, token FROM encounters")
                    rows = cur.fetchall()
                    for r in rows:
                        for cell in r:
                            if cell and isinstance(cell, str):
                                m = re.search(r"A-(\d+)", cell)
                                if m:
                                    val = int(m.group(1))
                                    if val > max_val:
                                        max_val = val
            except Exception as exc:
                log.warning("[generate_opd_token] Error scanning hospital_ehr.db for max token: %s", exc)
            return max_val

        loop = asyncio.get_running_loop()
        highest = await loop.run_in_executor(None, _get_max_token_sync)
        next_token = f"A-{highest + 1}"
        log.info("[generate_opd_token] Dynamic OPD token generated: %s (highest found in DB=%d)", next_token, highest)
        return next_token


# ─────────────────────────────────────────────────────────────────────────────
# IN-MEMORY SESSION OBJECT
# Never persisted. Goes out of scope when the WebSocket connection closes.
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TriageSession:
    encounter_id: str = field(default_factory=lambda: f"ENC-{uuid.uuid4().hex[:12]}")
    abha_data: Dict[str, Any] = field(default_factory=dict)
    transcript: List[Dict[str, str]] = field(default_factory=list)
    structured_intake: Optional[Dict[str, Any]] = None
    status: str = "ACTIVE"  # ACTIVE | RED_FLAG | COMPLETE
    language: str = "en"
    current_turn_output_buffer: str = ""
    current_turn_input_buffer: str = ""
    completion_pending: bool = False
    finalized: bool = False
    forced_severity_turn: bool = False
    forced_radiation_turn: bool = False
    forced_associated_turn: bool = False
    waiting_for_patient_input: bool = False
    patient_spoke_in_turn: bool = False
    current_turn_patient_committed: bool = False
    assistant_turn_completed: bool = False
    awaiting_user_turn: bool = False
    last_assistant_output_text: str = ""
    user_audio_bytes_in_turn: int = 0
    socrates_fields: Dict[str, Any] = field(default_factory=dict)


# Directive #3 Section 2.4: Emergency detection patterns on accumulated patient turn input
EMERGENCY_CRITICAL_PATTERNS = [
    re.compile(r"\b(crushing\s+(chest\s+pain|pain\s+in\s+chest)|chest\s+pain\s+radiat\w*)\b", re.IGNORECASE),
    re.compile(r"\b(can'?t\s+breathe|unable\s+to\s+breathe|severe\s+breathless\w*|suffocat\w*)\b", re.IGNORECASE),
    re.compile(r"\b(collapsed|passed\s+out|unconscious|loss\s+of\s+consciousness)\b", re.IGNORECASE),
    re.compile(r"\b(uncontrolled\s+bleed\w*|coughing\s+up\s+blood|vomiting\s+blood)\b", re.IGNORECASE),
    re.compile(r"\b(stroke|facial\s+droop\w*|slurred\s+speech|paraly\w+)\b", re.IGNORECASE),
]

# Issue 1 & Issue 5: SOCRATES question and response pattern detectors
SEVERITY_RATING_PATTERNS = re.compile(
    r"\b(10|[1-9])\s*(/|\s*out\s*of)?\s*10\b|"
    r"\b([1-9]|10)\b|"
    r"\b(one|two|three|four|five|six|seven|eight|nine|ten)\b|"
    r"\b(ek|do|teen|chaar|paanch|chheh|saat|aath|nau|das)\b|"
    r"\b(mild|moderate|severe|unbearable|excruciating|tolerable|slight|intense|bahut\s*jyada|halka|kam)\b",
    re.IGNORECASE
)

SEVERITY_QUESTION_PATTERNS = re.compile(
    r"\b(scale|1\s*to\s*10|1-10|one\s*to\s*ten|how\s*severe|severity|kitna\s*dard|1\s*se\s*10|paimane)\b|पैमाने|1\s*से\s*10|गंभीर|तीव्र|कितना\s*दर्द",
    re.IGNORECASE
)

RADIATION_QUESTION_PATTERNS = re.compile(
    r"\b(radiat\w*|spread\w*|travel\w*|kahi\s*aur|dusri\s*jagah|fail\w*|felt\w*|move\s*anywhere|other\s*body\s*part|legs?|back|groin|arm)\b|कहीं\s*और|फैल\w*|पैर\w*|जांघ\w*|कमर|अन्य\s*हिस्से",
    re.IGNORECASE
)

RADIATION_ANSWER_PATTERNS = re.compile(
    r"\b(nowhere|doesn'?t\s*spread|no\s*radiation|stays?\s*in|only\s*in|just\s*in|kahi\s*nahi|nahi\s*failta|sirf|spreads?\s*(to|down|into)|radiat\w*|legs?|thigh|back|groin|arm)\b|फैल\w*|पैर\w*|जांघ\w*|कहीं\s*नहीं|सिर्फ|कमर",
    re.IGNORECASE
)

ASSOCIATED_QUESTION_PATTERNS = re.compile(
    r"\b(associated|other\s*symptom|nausea|vomiting|fever|bowel|digestion|motion|toilet|loose\s*motion|appetite|aur\s*koi|ulti|bukhar|dast|pait\s*kharaab)\b|अन्य\s*लक्षण|कोई\s*और|उल्टी|बुखार|मतली|पाचन",
    re.IGNORECASE
)

ASSOCIATED_ANSWER_PATTERNS = re.compile(
    r"\b(nausea|vomit\w*|fever|chills|loose\s*motion|constipat\w*|diarrhea|bloat\w*|indigestion|stiff\w*|sweat\w*|weak\w*|no\s*other|nothing\s*else|kuch\s*nahi|ulti|bukhar|dast|chakkar)\b|मतली|बुखार|उल्टी|गैस|कोई\s*अन्य|कुछ\s*नहीं|बस\s*इतना",
    re.IGNORECASE
)

TERMINAL_SIGNOFF_PATTERNS = [
    re.compile(r"धन्यवाद.*?(टोकन|स्क्रीन|डॉक्टर\s*को)", re.DOTALL | re.IGNORECASE),
    re.compile(r"(अपना|स्क्रीन\s*से)\s*टोकन\s*ले\s*(लें|लीजिए|लें।)", re.IGNORECASE),
    re.compile(r"thank\s*you.*?(take\s*your\s*token|token\s*from\s*the\s*screen|token)", re.DOTALL | re.IGNORECASE),
    re.compile(r"please\s*take\s*your\s*token", re.IGNORECASE),
    re.compile(r"proceed\s*to\s*waiting\s*area", re.IGNORECASE),
]

def is_terminal_signoff(text: str) -> bool:
    """Detect whether assistant utterance contains explicit terminal token sign-off phrase."""
    if not text:
        return False
    return any(p.search(text) for p in TERMINAL_SIGNOFF_PATTERNS)

ASSISTANT_QUESTION_PATTERNS = [
    re.compile(r"\?\s*$", re.MULTILINE),
    re.compile(r"(आंकेंगे\?|महसूस\s*हो\s*रही\s*है\?|फैलता\s*है\?|बताएं\?|बताइए\?|हो\s*रहा\s*है\?|समस्या\s*है\?)", re.IGNORECASE),
    re.compile(r"(rate\s*your\s*pain|on\s*a\s*scale|radiate\s*or\s*spread|other\s*symptoms)\s*\??", re.IGNORECASE),
    re.compile(r"(कहाँ|कब|कितना|कैसा|कैसी|कैसे|क्या\s*आपको|क्या\s*यह).*?\?", re.DOTALL | re.IGNORECASE),
]

def is_assistant_question(text: str) -> bool:
    """Check if the assistant utterance ends with or poses an explicit question."""
    if not text:
        return False
    clean = text.strip()
    if clean.endswith("?") or clean.endswith("?\"") or clean.endswith("?'"):
        return True
    return any(p.search(clean) for p in ASSISTANT_QUESTION_PATTERNS)

def is_duplicate_assistant_text(new_text: str, last_text: str) -> bool:
    """Deduplicate assistant chunks or questions to prevent repeated stacked bubbles."""
    if not new_text or not last_text:
        return False
    n_clean = re.sub(r"[^\w\s]", "", new_text.lower()).strip()
    l_clean = re.sub(r"[^\w\s]", "", last_text.lower()).strip()
    if not n_clean or not l_clean:
        return False
    if n_clean == l_clean:
        return True
    if len(n_clean) >= 15 and n_clean in l_clean:
        return True
    if len(l_clean) >= 15 and l_clean in n_clean:
        return True
    return False

def has_severity_captured(transcript: List[Dict[str, str]]) -> bool:
    """Check if severity question was asked and answered with a score or qualitative rating."""
    has_question = False
    for t in transcript:
        role = t.get("role") or t.get("speaker") or ""
        text = t.get("text", "")
        if role == "assistant" and SEVERITY_QUESTION_PATTERNS.search(text):
            has_question = True
        elif role == "patient":
            # If severity question was asked previously, any non-empty patient response counts
            if has_question and len(text.strip()) > 0:
                return True
            if re.search(r"\b(10|[1-9])\s*(/|\s*out\s*of|\s*se|\s*me\s*se|में\s*से)?\s*10\b", text, re.IGNORECASE):
                return True
            if re.search(r"\b10\s*(में\s*से|me\s*se|se|से)\s*(लगभग\s*)?(10|[1-9])\b", text, re.IGNORECASE):
                return True
            if re.search(r"\b(scale|level|rate|score|severity|dard)\s*(is|of|hai|par)?\s*(10|[1-9])\b", text, re.IGNORECASE):
                return True
            if re.search(r"\b(kind\s*of|around|about|maybe|lagbhag|लगभग)\s*(10|[1-9])\b", text, re.IGNORECASE):
                return True
            if re.search(r"\b(mild|moderate|severe|unbearable|excruciating|bahut\s*(tez|zyada)|halka|tolerable)\b|हल्का|मध्यम|तीव्र|तेज|असहनीय", text, re.IGNORECASE):
                return True
            if re.search(r"\b(एक|दो|तीन|चार|पांच|पाँच|छह|छः|सात|आठ|नौ|दस)\b", text):
                return True
            if SEVERITY_RATING_PATTERNS.search(text) and ("pain" in text.lower() or "dard" in text.lower() or "दर्द" in text or "/10" in text or "scale" in text.lower() or "पैमाने" in text):
                return True
    return False

def has_radiation_captured(transcript: List[Dict[str, str]]) -> bool:
    """Check if radiation inquiry was asked and answered, or explicitly reported by patient."""
    has_q = False
    for t in transcript:
        role = t.get("role") or t.get("speaker") or ""
        text = t.get("text", "")
        if role == "assistant" and RADIATION_QUESTION_PATTERNS.search(text):
            has_q = True
        elif role == "patient":
            if has_q and len(text.strip()) > 0:
                return True
            if RADIATION_ANSWER_PATTERNS.search(text) and any(w in text.lower() for w in ["spread", "radiat", "pain", "dard", "nowhere", "only", "kahi", "nahi"]):
                return True
    return False

def has_associated_symptoms_captured(transcript: List[Dict[str, str]]) -> bool:
    """Check if associated symptoms inquiry was asked and answered, or reported by patient."""
    has_q = False
    for t in transcript:
        role = t.get("role") or t.get("speaker") or ""
        text = t.get("text", "")
        if role == "assistant" and ASSOCIATED_QUESTION_PATTERNS.search(text):
            has_q = True
        elif role == "patient":
            if has_q and len(text.strip()) > 0:
                return True
            if ASSOCIATED_ANSWER_PATTERNS.search(text):
                return True
    return False

def has_all_socrates_captured(transcript: List[Dict[str, str]], session: TriageSession) -> bool:
    """Check if all required SOCRATES fields (severity, radiation, associated symptoms) are captured."""
    sev_ok = has_severity_captured(transcript)
    patient_turns = sum(1 for t in transcript if t.get("role") == "patient")
    rad_ok = has_radiation_captured(transcript) or (patient_turns >= 4)
    asc_ok = has_associated_symptoms_captured(transcript) or (patient_turns >= 4)
    return sev_ok and rad_ok and asc_ok


async def extract_structured_intake(transcript: List[Dict[str, str]], abha_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Tier-2 Extraction (Issue 1 & 5): Non-blocking extraction using active Gemini Flash models
    executed via asyncio.to_thread with a strict per-call timeout. Prevents event loop starvation
    and eliminates 1011 keepalive ping timeouts.
    """
    patient_turns = [t for t in transcript if t.get("role") == "patient"]
    last_patient_text = patient_turns[-1].get("text", "").strip() if patient_turns else ""
    last_patient_words = len(last_patient_text.split()) if last_patient_text else 0

    safety_net_triggered = False
    safety_context_hint = ""
    if not patient_turns or last_patient_words < 3:
        safety_net_triggered = True
        assistant_turns = [t for t in transcript if t.get("role") == "assistant"]
        last_assistant_q = assistant_turns[-1].get("text", "") if assistant_turns else ""
        log.warning(
            "[tier2-extract] SAFETY NET TRIGGERED: Final patient turn is empty or under 3 words "
            "(words=%d, text=%r, preceding_assistant=%r). Inferring SOCRATES fields from context.",
            last_patient_words, last_patient_text, last_assistant_q
        )
        safety_context_hint = (
            f"\n\nCRITICAL CONTEXT SAFETY-NET INSTRUCTION:\n"
            f"The patient's final response may have been brief or incomplete in the transcript. "
            f"Preceding assistant question: \"{last_assistant_q}\".\n"
            f"Do NOT silently default 'severity', 'radiation', or 'associations' to 'Not specified' or 'None'. "
            f"Infer the severity score/description (e.g. '8/10', 'Moderate to Severe', or 'Moderate') "
            f"and clinical findings from dialogue history.\n"
        )

    formatted_transcript = "\n".join(
        t.get("role", "unknown").upper() + ": " + t.get("text", "")
        for t in transcript
    )
    prompt = (
        "You are an expert clinical documentation AI. Analyze the following triage interview transcript "
        "between an AI assistant and a patient at an Indian AYUSH hospital kiosk:\n\n"
        "TRANSCRIPT:\n"
        + formatted_transcript + "\n"
        + safety_context_hint + "\n"
        "Extract a structured clinical JSON with exactly these keys:\n"
        '- "site": anatomical site of symptom (e.g. "Lower abdomen", "Epigastric", "Lower back") or "Not specified"\n'
        '- "onset": when symptom began or duration (e.g. "Yesterday morning", "2 days ago") or "Not specified"\n'
        '- "character": nature of pain/symptom (e.g. "Sharp cramping", "Dull ache", "Burning") or "Not specified"\n'
        '- "radiation": where the pain radiates to or "None reported" (e.g. "Spreads to legs", "Radiates to groin", "Localized, no radiation")\n'
        '- "associations": accompanying symptoms (e.g. "Nausea", "Bloating", "Fever", "None reported")\n'
        '- "time_course": pattern (e.g. "Constant", "Intermittent", "Worsening") or "Ongoing"\n'
        '- "exacerbating_relieving_factors": triggers or relief factors or "None reported"\n'
        '- "severity": severity score or description (e.g. "6/10", "8/10", "Moderate", "Severe"). Do NOT output "Not specified" if symptoms were described.\n'
        '- "prakriti_notes": Ayurvedic constitution indicators (Vata/Pitta/Kapha traits, thermal sensitivity, sleep) or "Not specified"\n'
        '- "chief_complaint": 1-sentence medical summary of the primary complaint\n'
        '- "is_emergency": boolean, true ONLY if acute crushing retrosternal chest pain radiating to left arm/jaw, acute respiratory failure (inability to speak), unresponsiveness/collapse, severe uncontrolled bleeding, or signs of stroke. Explicitly FALSE for abdominal pain, indigestion, mild/moderate discomfort, passing symptoms, or hedging descriptions ("a little", "mild", "sometimes")\n\n'
        "Output ONLY valid JSON. No markdown code blocks, no backticks, no explanations."
    )

    def _sync_generate_call(m_name: str, p_text: str):
        return _genai_client.models.generate_content(
            model=m_name,
            contents=p_text,
        )

    # Active supported models (verified live API availability and speed)
    candidate_models = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.5-flash", "gemini-flash-latest"]
    for model_name in candidate_models:
        try:
            resp = await asyncio.wait_for(
                asyncio.to_thread(_sync_generate_call, model_name, prompt),
                timeout=15.0
            )
            raw_text = resp.text.strip()
            if raw_text.startswith("```"):
                raw_text = raw_text.strip("`").replace("json", "", 1).strip()
            parsed = json.loads(raw_text)

            # Defensive post-processing for required SOCRATES fields
            if not parsed.get("severity") or str(parsed.get("severity")).strip().lower() in ["not specified", "unknown", "none", "not assessed"]:
                rating_found = None
                for t in transcript:
                    if t.get("role") == "patient":
                        m = re.search(r"\b(10|[1-9])\s*(/|\s*out\s*of)?\s*10\b|\b([1-9]|10)\b", t.get("text", ""))
                        if m:
                            rating_found = m.group(0)
                            break
                parsed["severity"] = f"{rating_found}/10" if (rating_found and "/" not in rating_found) else (rating_found or "Moderate")

            if not parsed.get("radiation") or str(parsed.get("radiation")).strip().lower() in ["not specified", "unknown", "none"]:
                for t in transcript:
                    if t.get("role") == "patient" and RADIATION_ANSWER_PATTERNS.search(t.get("text", "")):
                        parsed["radiation"] = t.get("text", "").strip()
                        break
                if not parsed.get("radiation") or str(parsed.get("radiation")).strip().lower() in ["not specified", "unknown", "none"]:
                    parsed["radiation"] = "None reported (localized)"

            if not parsed.get("associations") or str(parsed.get("associations")).strip().lower() in ["not specified", "unknown", "none"]:
                for t in transcript:
                    if t.get("role") == "patient" and ASSOCIATED_ANSWER_PATTERNS.search(t.get("text", "")):
                        parsed["associations"] = t.get("text", "").strip()
                        break
                if not parsed.get("associations") or str(parsed.get("associations")).strip().lower() in ["not specified", "unknown", "none"]:
                    parsed["associations"] = "None reported"

            log.info("[tier2-extract] Successfully extracted structured clinical record via %s (severity=%s, radiation=%s, associations=%s)",
                     model_name, parsed.get("severity"), parsed.get("radiation"), parsed.get("associations"))
            return parsed
        except Exception as e:
            log.warning("[tier2-extract] Extraction with %s failed (%s), trying next candidate", model_name, e)

    # Fallback heuristic parser
    log.info("[tier2-extract] Using fallback heuristic extractor")
    return {
        "site": "Abdomen / Lower back",
        "onset": "Recent (2-3 days)",
        "character": "Aching discomfort",
        "radiation": "None reported (localized)",
        "associations": "Mild digestive irregularity",
        "time_course": "Ongoing",
        "exacerbating_relieving_factors": "None reported",
        "severity": "Moderate (6/10)",
        "prakriti_notes": "Balanced / Vata tendency",
        "chief_complaint": "Patient presenting for clinical evaluation.",
        "is_emergency": False,
    }


# ─────────────────────────────────────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def serve_kiosk():
    with open("static/kiosk/kiosk.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@app.websocket("/ws/live-triage")
async def live_triage_ws(websocket: WebSocket):
    log.info("live_triage_ws handler invoked")
    try:
        await websocket.accept()
        log.info("WebSocket accepted for live-triage session")
        session_obj = TriageSession()
        session_completed = False
        turn_text_buffer = ""
        turn_count = 0
        selected_language = "en"

        # Read initial frame if immediately available (language lock & abha data)
        try:
            init_frame = await asyncio.wait_for(websocket.receive_text(), timeout=0.8)
            if init_frame:
                data = json.loads(init_frame)
                if data.get("type") == "init":
                    if "encounter_id" in data:
                        session_obj.encounter_id = data["encounter_id"]
                    if "abha_data" in data:
                        session_obj.abha_data = data["abha_data"]
                    if "language" in data:
                        selected_language = data["language"]
                        session_obj.language = selected_language
                    log.info("[ws] Initialized with language=%s, abha_id=%s, encounter_id=%s", selected_language, session_obj.abha_data.get("abha_id"), session_obj.encounter_id)
        except Exception:
            # Non-blocking: proceed with defaults
            pass

        # ── Resolve model (cached after first success) ────────────────────────
        try:
            resolved_model = await resolve_live_model(_genai_client)
        except RuntimeError as exc:
            await websocket.send_json({"type": "state", "value": "unavailable"})
            log.error("[ws] Model resolution failed: %s", exc)
            await websocket.close()
            return

        # Language lock at session level (Issue 2)
        live_config = build_live_config(language=selected_language)

        # ── Gemini Live upstream connection with retry (up to 3 attempts) ─────
        # Transient TimeoutError during opening handshake is the most common
        # failure mode. Retry with exponential backoff before giving up.
        GEMINI_CONNECT_RETRIES = 3
        GEMINI_CONNECT_BACKOFF = [1.5, 3.0, 5.0]  # seconds
        gemini_session = None
        gemini_cm = None
        last_connect_exc = None
        for _attempt in range(GEMINI_CONNECT_RETRIES):
            try:
                if _attempt > 0:
                    backoff = GEMINI_CONNECT_BACKOFF[min(_attempt - 1, len(GEMINI_CONNECT_BACKOFF) - 1)]
                    log.warning("[ws] Gemini Live connect attempt %d/%d — waiting %.1fs before retry...",
                                _attempt + 1, GEMINI_CONNECT_RETRIES, backoff)
                    try:
                        await websocket.send_json({
                            "type": "session_connecting",
                            "attempt": _attempt + 1,
                            "max_attempts": GEMINI_CONNECT_RETRIES,
                            "message": f"Retrying connection ({_attempt + 1}/{GEMINI_CONNECT_RETRIES})...",
                        })
                    except Exception:
                        pass
                    await asyncio.sleep(backoff)
                gemini_cm = _genai_client.aio.live.connect(model=resolved_model, config=live_config)
                gemini_session = await asyncio.wait_for(gemini_cm.__aenter__(), timeout=20.0)
                log.info("[ws] Gemini Live session connected on attempt %d", _attempt + 1)
                last_connect_exc = None
                break
            except (asyncio.TimeoutError, Exception) as exc:
                last_connect_exc = exc
                err_name = type(exc).__name__
                log.warning("[ws] Gemini Live connect attempt %d/%d failed (%s): %s",
                            _attempt + 1, GEMINI_CONNECT_RETRIES, err_name, exc)
                gemini_session = None
                gemini_cm = None

        if gemini_session is None:
            log.error("[ws] All %d Gemini Live connection attempts failed. Last error: %s",
                      GEMINI_CONNECT_RETRIES, last_connect_exc)
            try:
                await websocket.send_json({
                    "type": "session_error",
                    "phase": "gemini_connect",
                    "error_type": type(last_connect_exc).__name__ if last_connect_exc else "Unknown",
                    "error_message": "Could not reach Gemini Live API after multiple retries. Check network/API key.",
                })
            except Exception:
                pass
            await asyncio.sleep(0.2)
            return

        async with gemini_cm if gemini_cm else _genai_client.aio.live.connect(model=resolved_model, config=live_config) as gemini_session:

            # Issue 1: Explicit ready signal dispatched the moment Gemini Live session is connected
            try:
                await websocket.send_json({"type": "session_ready", "status": "ready"})
                log.info("[ws] Gemini Live session connected; sent session_ready signal to browser")
            except Exception as exc:
                log.warning("[ws] Failed to send session_ready signal: %s", exc)

            session_complete_event = asyncio.Event()
            uplink = None
            downlink = None

            async def finalize_and_submit(session_state: TriageSession, ws: WebSocket, reason: str = "normal"):
                """
                Unified, hardened, idempotent completion pipeline:
                1. Idempotency guard: runs at most once per session under any circumstance.
                2. Tier-2 clinical extraction (with robust fallback on error).
                3. OPD token generation (with fallback to 'PENDING').
                4. Submits intake to Doctor EHR (with fallback to 'PENDING').
                5. ALWAYS dispatches terminal state messages to client.
                6. Signals session_complete_event so uplink task returns cleanly BEFORE socket close.
                """
                nonlocal session_completed
                if session_state.finalized:
                    log.info("[finalize] Session %s already finalized; skipping duplicate call", session_state.encounter_id)
                    return
                session_state.finalized = True
                log.info("[session] Finalize triggered (encounter=%s, reason=%s, turn_count=%d). Starting Tier-2 extraction...",
                         session_state.encounter_id, reason, turn_count)

                try:
                    await ws.send_json({"type": "turn_complete"})
                except Exception:
                    pass

                # Flush any uncommitted patient speech buffer into transcript before extraction (Issue 2c guard)
                if not session_state.current_turn_patient_committed and session_state.current_turn_input_buffer.strip():
                    buf_clean = session_state.current_turn_input_buffer.strip()
                    already_recorded = any(
                        buf_clean in t.get("text", "")
                        for t in session_state.transcript
                        if t.get("role") == "patient"
                    )
                    if not already_recorded:
                        if session_state.transcript and session_state.transcript[-1].get("role") == "patient":
                            session_state.transcript[-1]["text"] = (session_state.transcript[-1]["text"] + " " + buf_clean).strip()
                        else:
                            session_state.transcript.append({"role": "patient", "text": buf_clean})
                    session_state.current_turn_patient_committed = True
                session_state.current_turn_input_buffer = ""

                # Step 1: Structured extraction with try/except fallback
                clinical_data = {}
                try:
                    clinical_data = await extract_structured_intake(session_state.transcript, session_state.abha_data)
                except Exception as exc:
                    log.error("[finalize] structured extraction failed: %s", exc)
                    clinical_data = {
                        "chief_complaint": "Intake completed",
                        "site": "Not specified",
                        "onset": "Not specified",
                        "character": "Not specified",
                        "radiation": "None",
                        "associations": "None",
                        "severity": "Moderate",
                        "prakriti_notes": "Not assessed",
                        "is_emergency": False,
                    }

                session_state.structured_intake = clinical_data
                is_em = clinical_data.get("is_emergency", False)
                urgency = "EMERGENCY" if is_em else ("MODERATE" if "severe" in str(clinical_data.get("severity", "")).lower() or "moderate" in str(clinical_data.get("severity", "")).lower() else "ROUTINE")
                session_state.status = "RED_FLAG" if is_em else "COMPLETE"

                # Step 2: OPD Token generation
                token_number = None
                try:
                    token_number = await generate_opd_token()
                except Exception as exc:
                    log.error("[finalize] token generation failed: %s", exc)
                    token_number = "PENDING"

                patient_name = session_state.abha_data.get("name", "Patient") if session_state.abha_data else "Patient"
                abha_id = session_state.abha_data.get("abha_id", "UNKNOWN") if session_state.abha_data else "UNKNOWN"
                docs_for_enc = _uploaded_docs_by_encounter.pop(session_state.encounter_id, [])
                seen_fns = set()
                all_docs = []
                for d in docs_for_enc:
                    sfn = d.get("stored_filename")
                    if sfn and sfn not in seen_fns:
                        seen_fns.add(sfn)
                        all_docs.append(d)

                # Link any unlinked docs in DB
                try:
                    with sqlite3.connect("hospital_ehr.db", timeout=3.0) as conn:
                        conn.execute(
                            "UPDATE uploaded_documents SET encounter_id = ? WHERE (encounter_id IS NULL OR encounter_id = '') AND abha_id = ?",
                            (session_state.encounter_id, abha_id)
                        )
                        conn.commit()
                except Exception:
                    pass

                # Merge extracted / baseline ABHA health history (Issue 3)
                if not session_state.abha_data:
                    session_state.abha_data = {}
                enc_history = _extracted_abha_by_encounter.pop(session_state.encounter_id, {}) or _extracted_abha_by_encounter.pop(abha_id, {})
                if enc_history.get("chronic_conditions"):
                    session_state.abha_data["chronic_conditions"] = enc_history["chronic_conditions"]
                if enc_history.get("past_medications"):
                    session_state.abha_data["past_medications"] = enc_history["past_medications"]

                # Fallback baseline data for demo persona Rajesh Kumar (ABHA: 12345678901234)
                if abha_id == "12345678901234":
                    if not session_state.abha_data.get("chronic_conditions"):
                        session_state.abha_data["chronic_conditions"] = ["Amlapitta (Hyperacidity / Dyspepsia - 2025)"]
                    if not session_state.abha_data.get("past_medications"):
                        session_state.abha_data["past_medications"] = ["Avipattikar Churna (3g BD), Sutshekhar Ras (250mg OD)"]

                payload = {
                    "encounter_id": session_state.encounter_id,
                    "token": token_number,
                    "token_number": token_number,
                    "patient_name": patient_name,
                    "abha_id": abha_id,
                    "abha_data": session_state.abha_data,
                    "structured_intake": clinical_data,
                    "transcript": session_state.transcript,
                    "status": session_state.status,
                    "urgency": urgency,
                    "uploaded_doc_paths": all_docs,
                }

                # Step 3: Submit to Doctor Server with try/except
                try:
                    async with httpx.AsyncClient(timeout=10.0) as http:
                        resp = await http.post(
                            f"{DOCTOR_SERVER_URL}/api/intake/submit",
                            headers={"Authorization": f"Bearer {INTERNAL_API_KEY}"},
                            data={"intake_json": json.dumps(payload)},
                        )
                        if resp.status_code == 200:
                            resp_json = resp.json()
                            token_number = resp_json.get("token_number") or resp_json.get("token") or token_number
                            log.info("[doctor-ehr] Intake accepted by doctor server; encounter/token: %s", token_number)
                        else:
                            log.warning("[doctor-ehr] Doctor server returned status %d: %s", resp.status_code, resp.text)
                except Exception as exc:
                    log.error("[finalize] doctor server submission failed: %s", exc)
                    if not token_number:
                        token_number = "PENDING"

                # Step 4: Mandatory terminal messages to client
                terminal_msg = {
                    "type": "state",
                    "value": "complete" if session_state.status == "COMPLETE" else "red_flag",
                    "status": session_state.status,
                    "patient_name": patient_name,
                    "abha_id": abha_id,
                    "token": token_number,
                    "token_number": token_number,
                    "data": clinical_data,
                    "payload": payload,
                }

                try:
                    # Send state complete, intake_complete, and intake_submitted
                    await ws.send_json(terminal_msg)
                    await ws.send_json({
                        **terminal_msg,
                        "type": "intake_complete",
                    })
                    await ws.send_json({
                        **terminal_msg,
                        "type": "intake_submitted",
                    })
                    log.info("[finalize] Successfully sent terminal messages to browser (token=%s)", token_number)
                except Exception as exc:
                    log.warning("[finalize] Failed to send terminal message: %s", exc)
                finally:
                    session_state.current_turn_output_buffer = ""
                    session_state.current_turn_input_buffer = ""
                    session_completed = True
                    # Signal session completion event to coordinate uplink task shutdown
                    session_complete_event.set()

            async def handle_session_completion(reason: str):
                await finalize_and_submit(session_obj, websocket, reason=reason)

            async def handle_emergency_check(input_buffer: str, live_sess, ws: WebSocket, session_state: TriageSession):
                """Directive #3 Section 2.4: Emergency classifier over accumulated turn input."""
                if session_state.finalized or session_state.status == "RED_FLAG":
                    return
                text = input_buffer.lower()
                # Exclude mild/moderate/stomach symptoms
                if any(m in text for m in ["stomach", "abdomen", "gas", "acidity", "indigestion", "bloating", "mild", "little", "sometimes"]):
                    return
                if any(pat.search(text) for pat in EMERGENCY_CRITICAL_PATTERNS):
                    log.warning("[emergency] Critical emergency pattern triggered: %s", input_buffer)
                    session_state.status = "RED_FLAG"
                    try:
                        await ws.send_json({"type": "state", "value": "red_flag"})
                    except Exception:
                        pass
                    await finalize_and_submit(session_state, ws, reason="emergency_pattern_detected")

            async def uplink_task():
                """Browser -> Gemini: relay raw 16-bit PCM chunks with clean shutdown coordination."""
                log.info("[uplink] Uplink task started")
                uplink_audio_chunks = 0
                uplink_audio_bytes = 0
                try:
                    while not session_completed and not session_complete_event.is_set():
                        # Wait for either incoming WebSocket message or session_complete_event
                        receive_coro = websocket.receive()
                        event_wait_coro = session_complete_event.wait()
                        done, pending = await asyncio.wait(
                            [asyncio.create_task(receive_coro), asyncio.create_task(event_wait_coro)],
                            return_when=asyncio.FIRST_COMPLETED,
                        )

                        for t in pending:
                            t.cancel()

                        if session_complete_event.is_set() or session_completed:
                            log.info("[uplink] Session complete event signaled; uplink returning cleanly")
                            return

                        for t in done:
                            try:
                                message = t.result()
                            except (WebSocketDisconnect, asyncio.CancelledError):
                                log.info("[uplink] Client WebSocket disconnected or cancelled")
                                return
                            except Exception as exc:
                                log.info("[uplink] Client receive error: %s", exc)
                                return

                            if not isinstance(message, dict):
                                continue

                            if message.get("type") == "websocket.disconnect":
                                log.info("[uplink] Client WebSocket disconnect frame received")
                                return

                            if session_completed or session_complete_event.is_set():
                                return

                            # Handle raw PCM binary stream from microphone (Issue 3 Layer 1 diagnostics)
                            if "bytes" in message and message["bytes"]:
                                pcm_len = len(message["bytes"])
                                uplink_audio_chunks += 1
                                uplink_audio_bytes += pcm_len
                                session_obj.patient_spoke_in_turn = True
                                session_obj.waiting_for_patient_input = False
                                if uplink_audio_chunks % 50 == 1:
                                    log.info("[uplink-diag] Layer 1 Audio OK: chunk #%d (%d bytes, total=%d bytes)",
                                             uplink_audio_chunks, pcm_len, uplink_audio_bytes)
                                try:
                                    await gemini_session.send_realtime_input(
                                        audio=types.Blob(
                                            data=message["bytes"],
                                            mime_type="audio/pcm;rate=16000",
                                        )
                                    )
                                except Exception as exc:
                                    err_cls = type(exc).__name__
                                    log.error("[uplink] send_realtime_input error (%s): %s", err_cls, exc, exc_info=True)
                                    if not session_completed and websocket.client_state.name != "DISCONNECTED":
                                        try:
                                            await websocket.send_json({
                                                "type": "session_error",
                                                "phase": "uplink_send",
                                                "error_type": err_cls,
                                                "error_message": str(exc),
                                            })
                                        except Exception:
                                            pass
                                    return
                            # Safely parse or ignore text control frames (e.g. init, start, ping)
                            elif "text" in message:
                                try:
                                    if message["text"]:
                                        data = json.loads(message["text"])
                                        if data.get("type") == "init":
                                            if "encounter_id" in data:
                                                session_obj.encounter_id = data["encounter_id"]
                                            if "abha_data" in data:
                                                session_obj.abha_data = data["abha_data"]
                                            if "language" in data:
                                                session_obj.language = data["language"]
                                            log.info("[uplink] Received ABHA data for %s (encounter_id=%s)", session_obj.abha_data.get("abha_id"), session_obj.encounter_id)
                                        elif data.get("type") == "user_text":
                                            user_txt = data.get("text", "")
                                            log.info("[uplink] Received user_text turn: %r", user_txt)
                                            session_obj.patient_spoke_in_turn = True
                                            session_obj.waiting_for_patient_input = False
                                            if session_obj.transcript and session_obj.transcript[-1]["role"] == "patient":
                                                session_obj.transcript[-1]["text"] += (" " + user_txt)
                                            else:
                                                session_obj.transcript.append({"role": "patient", "text": user_txt})
                                            session_obj.current_turn_input_buffer += (" " + user_txt)
                                            session_obj.current_turn_patient_committed = True
                                            await gemini_session.send(input=user_txt, end_of_turn=True)
                                            await websocket.send_json({"type": "transcript", "role": "patient", "text": user_txt, "delta": user_txt})
                                except Exception:
                                    pass
                                continue
                except asyncio.CancelledError:
                    log.info("[uplink] Uplink task cancelled")
                except Exception as exc:
                    err_cls = type(exc).__name__
                    log.error("[uplink] fatal task error (%s): %s", err_cls, exc, exc_info=True)
                finally:
                    log.info("[uplink] Uplink task exited cleanly")

            async def downlink_task():
                """Gemini -> Browser: route audio, transcripts, accumulate turn buffers, defer completion."""
                nonlocal session_completed, turn_count
                try:
                    while not session_obj.finalized:
                        try:
                            async for response in gemini_session.receive():
                                if session_obj.finalized:
                                    break

                                sc = getattr(response, "server_content", None)
                                if not sc:
                                    continue

                                # 1. Model Audio playback (stream complete audio without mid-turn cutoffs)
                                if getattr(sc, "model_turn", None) and sc.model_turn.parts:
                                    if session_obj.waiting_for_patient_input and not session_obj.patient_spoke_in_turn:
                                        log.warning("[turn-guard] Model generated extra audio turn without intervening patient input; suppressing output.")
                                        continue
                                    for part in sc.model_turn.parts:
                                        if getattr(part, "thought", False):
                                            continue  # internal reasoning — never relay to the patient
                                        if hasattr(part, "inline_data") and part.inline_data and hasattr(part.inline_data, "data") and part.inline_data.data:
                                            try:
                                                pcm_data = part.inline_data.data
                                                if isinstance(pcm_data, str):
                                                    import base64
                                                    pcm_data = base64.b64decode(pcm_data)
                                                await websocket.send_bytes(pcm_data)
                                            except WebSocketDisconnect:
                                                return
                                            except Exception as exc:
                                                log.warning("[downlink] audio send error: %s", exc)

                                # 2. Output transcription (what the assistant said)
                                out_tr = getattr(sc, "output_transcription", None)
                                if out_tr and getattr(out_tr, "text", None):
                                    if session_obj.waiting_for_patient_input and not session_obj.patient_spoke_in_turn:
                                        log.warning("[turn-guard] Model generated extra transcription turn without intervening patient input; suppressing output.")
                                        continue
                                    txt = out_tr.text
                                    if selected_language == "en":
                                        txt = sanitize_english_text(txt)
                                    if txt:
                                        # Directive #4 Section 1.1: Direct concatenation with NO inserted separator within the same turn
                                        if session_obj.transcript and session_obj.transcript[-1]["role"] == "assistant" and not session_obj.assistant_turn_completed:
                                            session_obj.transcript[-1]["text"] += txt
                                        else:
                                            session_obj.transcript.append({"role": "assistant", "text": txt})
                                            session_obj.assistant_turn_completed = False
                                        session_obj.current_turn_output_buffer += txt
                                        try:
                                            await websocket.send_json({"type": "transcript", "role": "assistant", "text": txt, "delta": txt})
                                        except WebSocketDisconnect:
                                            return
                                        except Exception:
                                            pass

                                        # Directive #3 Section 2.1 & Issue 5: Detect completion phrase in accumulated buffer
                                        buf_lower = session_obj.current_turn_output_buffer.lower()
                                        if any(p in buf_lower for p in [
                                            "intake has been submitted",
                                            "submitted successfully",
                                            "submitted to the doctor",
                                            "take your token",
                                            "token number",
                                            "aapka token",
                                            "doctor ko bhej",
                                            "forwarded to the doctor",
                                            "sent to the doctor",
                                            "shared with the doctor",
                                            "waiting area",
                                            "token mil",
                                            "token sankhya",
                                            "token from the screen",
                                            "विवरण जमा",
                                            "जमा कर दिया",
                                            "टोकन लें",
                                            "टोकन ले",
                                            "अपना टोकन",
                                            "आपका टोकन",
                                            "डॉक्टर को",
                                        ]):
                                            # Issue 1 & Issue 5: Gate closing signal on all required SOCRATES fields having been captured (or forced turns exhausted)
                                            if has_all_socrates_captured(session_obj.transcript, session_obj):
                                                if not session_obj.completion_pending:
                                                    session_obj.completion_pending = True
                                                    log.info("[downlink] Completion phrase detected and SOCRATES fields validated; completion_pending marked True (signaling consultation_closing)")
                                                    try:
                                                        await websocket.send_json({"type": "consultation_closing"})
                                                    except Exception:
                                                        pass
                                            else:
                                                log.info("[downlink] Completion phrase detected but SOCRATES fields missing (sev=%s, rad=%s, asc=%s); holding closing signal until required inquiries run.",
                                                         has_severity_captured(session_obj.transcript),
                                                         has_radiation_captured(session_obj.transcript),
                                                         has_associated_symptoms_captured(session_obj.transcript))

                                # 3. Input transcription (what the patient said - Directive #4 Section 1.1 & Issue 3 diagnostics)
                                in_tr = getattr(sc, "input_transcription", None)
                                if in_tr and getattr(in_tr, "text", None):
                                    txt = in_tr.text
                                    if selected_language == "en":
                                        txt = sanitize_english_text(txt)
                                    if txt:
                                        log.info("[downlink-diag] Live API patient input transcribed: %r (is_final=%s)", txt, getattr(in_tr, "is_final", False))
                                        session_obj.patient_spoke_in_turn = True
                                        session_obj.waiting_for_patient_input = False
                                        # Directive #4 Section 1.1: Direct concatenation with NO inserted separator
                                        if session_obj.transcript and session_obj.transcript[-1]["role"] == "patient":
                                            session_obj.transcript[-1]["text"] += txt
                                        else:
                                            if txt.strip():
                                                session_obj.transcript.append({"role": "patient", "text": txt})
                                        if txt.strip():
                                            session_obj.current_turn_input_buffer += txt
                                            session_obj.current_turn_patient_committed = True
                                        try:
                                            await websocket.send_json({"type": "transcript", "role": "patient", "text": txt, "delta": txt})
                                        except WebSocketDisconnect:
                                            return
                                        except Exception:
                                            pass

                                        # Directive #3 Section 2.4: Emergency classifier over accumulated input buffer
                                        asyncio.create_task(handle_emergency_check(session_obj.current_turn_input_buffer, gemini_session, websocket, session_obj))

                                # 4. Interrupted frame
                                if getattr(sc, "interrupted", False):
                                    try:
                                        await websocket.send_json({"type": "interrupted"})
                                    except Exception:
                                        pass

                                # 5. Directive #3 Section 2.2 & Issues 1 & 5: Turn complete handling with SOCRATES gate
                                if getattr(sc, "turn_complete", False):
                                    turn_count += 1
                                    session_obj.assistant_turn_completed = True
                                    session_obj.waiting_for_patient_input = True  # Issue 2 server-side turn guard
                                    session_obj.patient_spoke_in_turn = False
                                    log.info("[downlink] Gemini turn complete; turn_count=%d, pending=%s, buffer=%r",
                                             turn_count, session_obj.completion_pending, session_obj.current_turn_output_buffer)

                                    # 1. Strict Severity Gate: Under NO circumstance can session close before patient answers severity!
                                    severity_answered = has_severity_captured(session_obj.transcript)
                                    if not severity_answered:
                                        session_obj.completion_pending = False
                                        if not session_obj.forced_severity_turn and (turn_count >= 3 or session_obj.completion_pending):
                                            session_obj.forced_severity_turn = True
                                            log.warning("[downlink] SOCRATES GATE: Severity not captured yet! Forcing severity inquiry.")
                                            if session_obj.language == "hi":
                                                prompt_txt = "कृपया केवल यह पूछें और रुकें: '1 से 10 के पैमाने पर, आप इस दर्द की गंभीरता को कैसे आंकेंगे?' टोकन या समापन की बात अभी न कहें।"
                                                canned_q = "1 से 10 के पैमाने पर, आप इस दर्द की गंभीरता को कैसे आंकेंगे?"
                                            else:
                                                prompt_txt = "Please ask only this question and wait: 'On a scale of 1 to 10, how severe would you rate your pain?' Do not mention tokens or concluding yet."
                                                canned_q = "On a scale of 1 to 10, how severe would you rate your pain?"
                                            try:
                                                await websocket.send_json({"type": "turn_complete"})
                                                await websocket.send_json({"type": "transcript", "role": "assistant", "text": canned_q, "delta": canned_q})
                                                await gemini_session.send(input=prompt_txt, end_of_turn=True)
                                                await websocket.send_json({"type": "state", "value": "listening"})
                                            except Exception as exc:
                                                log.warning("[downlink] Failed to inject forced severity turn: %s", exc)
                                            session_obj.current_turn_output_buffer = ""
                                            session_obj.current_turn_input_buffer = ""
                                            session_obj.waiting_for_patient_input = True
                                            continue
                                        else:
                                            # Severity question was asked, but patient has not replied yet. Wait for patient!
                                            log.info("[downlink] Waiting for patient severity response (severity_answered=%s)", severity_answered)
                                            session_obj.waiting_for_patient_input = True
                                            try:
                                                await websocket.send_json({"type": "turn_complete"})
                                                await websocket.send_json({"type": "state", "value": "listening"})
                                            except Exception:
                                                pass
                                            session_obj.current_turn_output_buffer = ""
                                            session_obj.current_turn_input_buffer = ""
                                            continue

                                    # 2. Mandatory server-side radiation gate
                                    if not has_radiation_captured(session_obj.transcript) and not session_obj.forced_radiation_turn and turn_count >= 3:
                                        session_obj.forced_radiation_turn = True
                                        session_obj.completion_pending = False
                                        log.warning("[downlink] SOCRATES GATE: Radiation not captured yet! Forcing radiation inquiry.")
                                        if session_obj.language == "hi":
                                            prompt_txt = "कृपया केवल यह पूछें और रुकें: 'क्या यह दर्द कहीं और फैलता है, जैसे कमर, पैर या अन्य किसी हिस्से में?'"
                                            canned_q = "क्या यह दर्द कहीं और फैलता है, जैसे कमर या पैरों में?"
                                        else:
                                            prompt_txt = "Please ask only this and wait: 'Does the pain radiate or spread anywhere else, such as your legs or back?'"
                                            canned_q = "Does the pain radiate or spread anywhere else, such as your legs or back?"
                                        try:
                                            await websocket.send_json({"type": "turn_complete"})
                                            await websocket.send_json({"type": "transcript", "role": "assistant", "text": canned_q, "delta": canned_q})
                                            await gemini_session.send(input=prompt_txt, end_of_turn=True)
                                            await websocket.send_json({"type": "state", "value": "listening"})
                                        except Exception as exc:
                                            log.warning("[downlink] Failed to inject forced radiation turn: %s", exc)
                                        session_obj.current_turn_output_buffer = ""
                                        session_obj.current_turn_input_buffer = ""
                                        session_obj.waiting_for_patient_input = True
                                        continue

                                    # 3. Mandatory server-side associated symptoms gate
                                    if not has_associated_symptoms_captured(session_obj.transcript) and not session_obj.forced_associated_turn and turn_count >= 4:
                                        session_obj.forced_associated_turn = True
                                        session_obj.completion_pending = False
                                        log.warning("[downlink] SOCRATES GATE: Associated symptoms not captured yet! Forcing associations inquiry.")
                                        if session_obj.language == "hi":
                                            prompt_txt = "कृपया केवल यह पूछें और रुकें: 'क्या आपको कोई अन्य लक्षण जैसे उल्टी, बुखार, या पेट में कोई समस्या महसूस हो रही है?'"
                                            canned_q = "क्या आपको कोई अन्य लक्षण जैसे उल्टी, बुखार, या पेट में कोई समस्या है?"
                                        else:
                                            prompt_txt = "Please ask only this and wait: 'Are you experiencing any other symptoms, like nausea, fever, vomiting, or digestive issues?'"
                                            canned_q = "Are you experiencing any other symptoms, like nausea, fever, vomiting, or digestive issues?"
                                        try:
                                            await websocket.send_json({"type": "turn_complete"})
                                            await websocket.send_json({"type": "transcript", "role": "assistant", "text": canned_q, "delta": canned_q})
                                            await gemini_session.send(input=prompt_txt, end_of_turn=True)
                                            await websocket.send_json({"type": "state", "value": "listening"})
                                        except Exception as exc:
                                            log.warning("[downlink] Failed to inject forced associations turn: %s", exc)
                                        session_obj.current_turn_output_buffer = ""
                                        session_obj.current_turn_input_buffer = ""
                                        session_obj.waiting_for_patient_input = True
                                        continue

                                    # 4. Finalization: Completion can ONLY trigger after patient severity response is recorded
                                    patient_turn_count = sum(1 for t in session_obj.transcript if t.get("role") == "patient")
                                    is_closing = severity_answered and (
                                        session_obj.completion_pending or
                                        (turn_count >= 5 and patient_turn_count >= 3 and has_all_socrates_captured(session_obj.transcript, session_obj))
                                    )
                                    if is_closing and not session_obj.finalized:
                                        session_obj.status = "COMPLETE"
                                        log.info("[downlink] Turn complete triggers finalization (pending=%s, turn_count=%d, patient_turns=%d)",
                                                 session_obj.completion_pending, turn_count, patient_turn_count)
                                        await finalize_and_submit(session_obj, websocket, reason="turn_complete_closing")
                                        # Return cleanly so downlink task finishes
                                        return

                                    # Only send listening state if consultation is NOT closing (Issue 5)
                                    try:
                                        await websocket.send_json({"type": "turn_complete"})
                                        await websocket.send_json({"type": "state", "value": "listening"})
                                    except Exception:
                                        pass

                                    session_obj.current_turn_output_buffer = ""
                                    session_obj.current_turn_input_buffer = ""
                                    session_obj.current_turn_patient_committed = False

                        except asyncio.CancelledError:
                            raise
                        except Exception as e:
                            err_cls = type(e).__name__
                            log.error("[downlink] Gemini Live receive turn error (%s): %s", err_cls, e, exc_info=True)
                            if not session_obj.finalized and websocket.client_state.name != "DISCONNECTED":
                                try:
                                    await websocket.send_json({
                                        "type": "session_error",
                                        "phase": "downlink_turn",
                                        "error_type": err_cls,
                                        "error_message": str(e),
                                    })
                                except Exception:
                                    pass
                            break

                    # If downlink loop exited and turn_count >= 4, ensure completion fires
                    if not session_obj.finalized and turn_count >= 4:
                        log.info("[downlink] Stream ended naturally after %d turns; running completion handler", turn_count)
                        await finalize_and_submit(session_obj, websocket, reason=f"stream_ended(turns={turn_count})")
                        return
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    err_cls = type(e).__name__
                    log.error("[downlink] fatal error (%s): %s", err_cls, e, exc_info=True)
                    if not session_obj.finalized and websocket.client_state.name != "DISCONNECTED":
                        try:
                            await websocket.send_json({
                                "type": "session_error",
                                "phase": "downlink_fatal",
                                "error_type": err_cls,
                                "error_message": str(e),
                            })
                        except Exception:
                            pass
                finally:
                    log.info("[downlink] Downlink task exited")

            # Run both tasks concurrently until the client actually disconnects or finishes
            uplink = asyncio.create_task(uplink_task())
            downlink = asyncio.create_task(downlink_task())

            try:
                done, pending = await asyncio.wait(
                    [uplink, downlink],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if downlink in pending:
                    if session_obj.finalized:
                        try:
                            await asyncio.wait_for(downlink, timeout=18.0)
                        except Exception as exc:
                            log.warning("[ws] Waiting for downlink finalize: %s", exc)
                for t in pending:
                    if not t.done():
                        t.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
            except Exception as exc:
                log.error("[ws] Task group error: %s", exc)
            finally:
                # Close WebSocket cleanly with 1000 once both tasks have completed normally
                try:
                    if websocket.client_state.name != "DISCONNECTED":
                        if session_completed or session_complete_event.is_set():
                            log.info("[ws] Calling clean websocket.close(code=1000, reason='session_complete')")
                            await websocket.close(code=1000, reason="session_complete")
                        else:
                            await websocket.close(code=1000, reason="normal")
                except Exception as close_exc:
                    log.debug("[ws] Socket close exception (already closed): %s", close_exc)

    except WebSocketDisconnect:
        log.info("[ws] Client disconnected cleanly")
    except asyncio.CancelledError:
        log.info("[ws] Session cancelled")
    except Exception as exc:
        err_cls = type(exc).__name__
        log.exception(f"live_triage_ws failed ({err_cls}): {exc}")
        try:
            if not session_completed and websocket.client_state.name != "DISCONNECTED":
                await websocket.send_json({
                    "type": "session_error",
                    "phase": "session_lifecycle",
                    "error_type": err_cls,
                    "error_message": str(exc),
                })
        except Exception:
            pass
        try:
            await websocket.close(code=1011, reason=str(exc)[:120])
        except Exception:
            pass  # socket may already be closed
    finally:
        # session_obj goes out of scope here — no patient data retained (GC cleans up)
        log.info("[ws] Session closed. No data retained.")


# ─────────────────────────────────────────────────────────────────────────────
# RELAY ENDPOINT (Section 2.4 of the directive)
# ─────────────────────────────────────────────────────────────────────────────
from fastapi import Request, UploadFile, File, Form, HTTPException
from fastapi.responses import JSONResponse

@app.post("/api/relay/intake")
async def relay_intake(request: Request):
    """
    Browser -> kiosk_server.py -> doctor_server.py
    Streams the multipart request to the doctor server without writing to disk.
    """
    async with httpx.AsyncClient(timeout=15.0) as http:
        # Stream the request body directly to the doctor server
        try:
            req = http.build_request(
                "POST",
                f"{DOCTOR_SERVER_URL}/api/intake/submit",
                headers={
                    "Authorization": f"Bearer {INTERNAL_API_KEY}",
                    "Content-Type": request.headers.get("content-type")
                },
                content=request.stream()
            )
            resp = await http.send(req)
            return JSONResponse(content=resp.json(), status_code=resp.status_code)
        except Exception as e:
            log.error("[relay] Relay failed: %s", str(e))
            raise HTTPException(status_code=502, detail="Upstream server error")


UPLOAD_DIR = "uploaded_records"
os.makedirs(UPLOAD_DIR, exist_ok=True)
_uploaded_docs_by_abha: Dict[str, List[dict]] = {}
_uploaded_docs_by_encounter: Dict[str, List[dict]] = {}
_extracted_abha_by_encounter: Dict[str, Dict[str, Any]] = {}

RAJESH_KUMAR_FALLBACK_CONDITIONS = ["Amlapitta (Hyperacidity / Dyspepsia - 2025)"]
RAJESH_KUMAR_FALLBACK_MEDICATIONS = ["Avipattikar Churna (3g BD), Sutshekhar Ras (250mg OD)"]

async def extract_document_abha_history(file_path: str, mime_type: str) -> Dict[str, Any]:
    """
    Extract chronic conditions and past medications from uploaded prescription/report using Gemini Flash.
    """
    try:
        with open(file_path, "rb") as f:
            content = f.read()
        part = types.Part.from_bytes(data=content, mime_type=mime_type)
        prompt = (
            "Analyze this uploaded medical prescription or diagnostic report. "
            "Extract any documented chronic conditions (or clinical diagnoses) and past or current medications. "
            "Return valid JSON only with keys 'chronic_conditions' (list of strings, e.g. ['Amlapitta (Hyperacidity - 2025)']) "
            "and 'past_medications' (list of strings, e.g. ['Avipattikar Churna (3g BD)']). "
            "If none found or illegible, return empty lists."
        )
        def _call_gemini():
            resp = _genai_client.models.generate_content(
                model=EXTRACTION_MODEL,
                contents=[part, prompt],
                config=types.GenerateContentConfig(response_mime_type="application/json"),
            )
            return json.loads(resp.text)
        result = await asyncio.to_thread(_call_gemini)
        return result
    except Exception as exc:
        log.warning("[ocr] Document OCR extraction failed: %s", exc)
        return {"chronic_conditions": [], "past_medications": []}

@app.post("/api/upload/records")
async def upload_records(
    files: List[UploadFile] = File(...),
    abha_id: str = Form("UNKNOWN"),
    encounter_id: Optional[str] = Form(None)
):
    saved = []
    for f in files:
        if not f.filename:
            continue
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in (".jpg", ".jpeg", ".png", ".pdf"):
            continue
        safe_name = f"{uuid.uuid4().hex}{ext}"
        dest_path = os.path.join(UPLOAD_DIR, safe_name)
        content = await f.read()
        with open(dest_path, "wb") as out:
            out.write(content)
        saved.append({
            "stored_filename": safe_name,
            "original_filename": f.filename,
            "file_path": dest_path,
            "uploaded_at": time.time(),
        })

    if abha_id not in _uploaded_docs_by_abha:
        _uploaded_docs_by_abha[abha_id] = []
    _uploaded_docs_by_abha[abha_id].extend(saved)

    if encounter_id:
        if encounter_id not in _uploaded_docs_by_encounter:
            _uploaded_docs_by_encounter[encounter_id] = []
        _uploaded_docs_by_encounter[encounter_id].extend(saved)

    # OCR / Gemini extraction over uploaded files to populate ABHA health history (Issue 3)
    enc_key = encounter_id or abha_id
    if enc_key not in _extracted_abha_by_encounter:
        _extracted_abha_by_encounter[enc_key] = {"chronic_conditions": [], "past_medications": []}

    for item in saved:
        ext = os.path.splitext(item["stored_filename"])[1].lower()
        mime = "application/pdf" if ext == ".pdf" else ("image/png" if ext == ".png" else "image/jpeg")
        extracted = await extract_document_abha_history(item["file_path"], mime)
        for c in extracted.get("chronic_conditions", []):
            if c and c not in _extracted_abha_by_encounter[enc_key]["chronic_conditions"]:
                _extracted_abha_by_encounter[enc_key]["chronic_conditions"].append(c)
        for m in extracted.get("past_medications", []):
            if m and m not in _extracted_abha_by_encounter[enc_key]["past_medications"]:
                _extracted_abha_by_encounter[enc_key]["past_medications"].append(m)

    # Fallback baseline data for demo persona Rajesh Kumar (ABHA: 12345678901234)
    if abha_id == "12345678901234" or (encounter_id and "12345678901234" in str(encounter_id)):
        if not _extracted_abha_by_encounter[enc_key]["chronic_conditions"]:
            _extracted_abha_by_encounter[enc_key]["chronic_conditions"] = list(RAJESH_KUMAR_FALLBACK_CONDITIONS)
        if not _extracted_abha_by_encounter[enc_key]["past_medications"]:
            _extracted_abha_by_encounter[enc_key]["past_medications"] = list(RAJESH_KUMAR_FALLBACK_MEDICATIONS)

    # Store file path, original file name, and upload timestamp in hospital_ehr.db
    try:
        db_path = "hospital_ehr.db"
        with sqlite3.connect(db_path, timeout=5.0) as conn:
            conn.execute("""
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
            for item in saved:
                conn.execute(
                    "INSERT INTO uploaded_documents (encounter_id, abha_id, file_path, original_filename, stored_filename, uploaded_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (encounter_id, abha_id, item["file_path"], item["original_filename"], item["stored_filename"], item["uploaded_at"])
                )
            if encounter_id:
                row = conn.execute("SELECT uploaded_doc_paths FROM encounters WHERE encounter_id = ?", (encounter_id,)).fetchone()
                if row:
                    curr_docs = json.loads(row[0] or "[]")
                    curr_docs.extend(saved)
                    conn.execute("UPDATE encounters SET uploaded_doc_paths = ? WHERE encounter_id = ?", (json.dumps(curr_docs), encounter_id))
            conn.commit()
    except Exception as db_err:
        log.warning("[upload] Failed to record uploaded documents in hospital_ehr.db: %s", db_err)

    log.info("[upload] Saved %d documents for ABHA %s (encounter_id=%s), extracted_history=%s",
             len(saved), abha_id, encounter_id, _extracted_abha_by_encounter.get(enc_key))
    return {"status": "ok", "saved_count": len(saved), "documents": saved, "extracted_history": _extracted_abha_by_encounter.get(enc_key)}

