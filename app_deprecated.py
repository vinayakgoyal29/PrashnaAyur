"""
PrashnaAyur -- Entry point and Patient Intake Kiosk
=====================================================
Voice-first AYUSH triage kiosk with multilingual support.

Uses st.navigation for clean sidebar page names.
"""

import os
import io
import streamlit as st
from dotenv import load_dotenv

# ── Load environment ─────────────────────────────────────────────────
load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# ── Page config (must be the first Streamlit command) ────────────────
st.set_page_config(page_title="PrashnaAyur", layout="wide")

# ── API key guard ────────────────────────────────────────────────────
if not GEMINI_API_KEY or GEMINI_API_KEY == "your_api_key_here":
    st.error("GEMINI_API_KEY not found. Add it to your .env file.")
    st.stop()

# ── Imports ──────────────────────────────────────────────────────────
from streamlit_mic_recorder import mic_recorder
from gtts import gTTS

from utils.gemini_client import (
    get_genai_client,
    chat_turn,
    extract_document,
    transcribe_audio,
)
from utils.prompts import (
    TRIAGE_SYSTEM_PROMPT,
    VISION_EXTRACTION_PROMPT,
    TRANSCRIPTION_PROMPT,
    LANGUAGE_MAP,
)

# ── Initialise session state (never blind overwrite) ─────────────────
_DEFAULTS = {
    "abha_logged_in": False,
    "abha_data": None,
    "document_records": [],
    "messages": [],
    "interview_status": "ACTIVE",
    "active_model": None,
    "case_summary": None,
    "language": "English",
    "structured_case": None,
    "last_audio_response": None,
}
for key, default in _DEFAULTS.items():
    if key not in st.session_state:
        st.session_state[key] = default

# ── Get the cached Gemini client ─────────────────────────────────────
client = get_genai_client(GEMINI_API_KEY)

# ── Mock ABHA data ───────────────────────────────────────────────────
MOCK_ABHA_DATA = {
    "patient_id": "91-1234-5678-9012",
    "name": "Rajesh Kumar",
    "age": 45,
    "chronic_conditions": [
        {
            "condition": "Type 2 Diabetes",
            "diagnosed_date": "2022-04-15",
            "status": "Active",
        }
    ],
    "past_medications": [
        {"drug": "Metformin", "dosage": "500mg", "frequency": "Twice daily"}
    ],
}


def _reset_session():
    """Reset all session state keys to defaults for a fresh patient."""
    for key, default in _DEFAULTS.items():
        st.session_state[key] = (
            default.copy() if isinstance(default, (list, dict)) else default
        )


def _login_patient():
    """Load mock ABHA data and flag as logged in."""
    st.session_state["abha_data"] = MOCK_ABHA_DATA.copy()
    st.session_state["abha_logged_in"] = True


# ══════════════════════════════════════════════════════════════════════
# NAVIGATION (st.navigation for clean sidebar page names)
# ══════════════════════════════════════════════════════════════════════
kiosk_page = st.Page("kiosk_page.py", title="Patient Kiosk", default=True)
dashboard_page = st.Page(
    "pages/1_Doctor_Dashboard.py", title="Doctor Dashboard"
)
pg = st.navigation([kiosk_page, dashboard_page])

# ══════════════════════════════════════════════════════════════════════
# SIDEBAR -- ABHA Login / Profile Card
# ══════════════════════════════════════════════════════════════════════
with st.sidebar:
    if st.session_state["abha_logged_in"]:
        # ── Post-login: Digital ABHA Profile Card ─────────────────────
        patient = st.session_state["abha_data"]
        with st.container():
            st.subheader("Patient Profile")
            col_label, col_value = st.columns([1, 2])
            with col_label:
                st.caption("Name")
                st.caption("ABHA ID")
                st.caption("Age")
            with col_value:
                st.markdown(f"**{patient['name']}**")
                st.markdown(f"**{patient['patient_id']}**")
                st.markdown(f"**{patient['age']}**")
            st.success("ABHA Verified")

        if st.button("Reset Session"):
            _reset_session()
            st.rerun()
    else:
        # ── Pre-login: Patient Verification ───────────────────────────
        with st.container():
            st.subheader("Patient Verification")
            abha_id = st.text_input(
                "ABHA Health ID",
                max_chars=14,
                placeholder="Enter 14-digit ABHA ID",
            )
            if st.button("Fetch Records"):
                if abha_id and abha_id.isdigit() and len(abha_id) == 14:
                    _login_patient()
                    st.rerun()
                else:
                    st.warning("Please enter exactly 14 digits.")

            st.divider()

            if st.button("Quick Demo Login"):
                _login_patient()
                st.rerun()

# ── Run the selected page ────────────────────────────────────────────
pg.run()
