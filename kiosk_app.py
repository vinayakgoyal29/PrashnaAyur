"""
PrashnaAyur -- Patient-Facing Kiosk Application
=================================================
Voice-first AYUSH triage kiosk with multilingual support, OTP verification,
and local JSON persistence for inter-process communication with doctor_app.py.

Run independently: streamlit run kiosk_app.py --server.port 8501
"""

import io
import os
import random
import uuid
from datetime import datetime
from typing import Optional

import streamlit as st
from dotenv import load_dotenv
from streamlit_mic_recorder import mic_recorder
from gtts import gTTS

load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

st.set_page_config(page_title="PrashnaAyur Kiosk", layout="wide", initial_sidebar_state="collapsed")

if not GEMINI_API_KEY or GEMINI_API_KEY == "your_api_key_here":
    st.error("GEMINI_API_KEY not found. Add it to your .env file.")
    st.stop()

from utils.gemini_client import (
    get_genai_client,
    chat_turn,
    extract_document,
    transcribe_audio,
    extract_structured_case,
)
from utils.prompts import (
    TRIAGE_SYSTEM_PROMPT,
    VISION_EXTRACTION_PROMPT,
    TRANSCRIPTION_PROMPT,
    STRUCTURED_EXTRACTION_PROMPT,
    LANGUAGE_MAP,
    UI_STRINGS,
)
from utils.db_handler import append_record

# ── Session state defaults ───────────────────────────────────────────
_DEFAULTS = {
    "kiosk_stage": "AWAITING_ABHA",
    "abha_id": "",
    "abha_data": None,
    "demo_otp": "",
    "language": "English",
    "messages": [],
    "interview_status": "ACTIVE",
    "document_records": [],
    "structured_case": None,
    "active_model": None,
    "turn_counter": 0,
    "last_audio_response": None,
    "record_saved": False,
}
for key, default in _DEFAULTS.items():
    if key not in st.session_state:
        st.session_state[key] = default

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


# ── Resolve UI strings for selected language ─────────────────────────
def S(key: str) -> str:
    """Look up a UI string for the current language, falling back to English."""
    lang = st.session_state.get("language", "English")
    return UI_STRINGS.get(lang, UI_STRINGS["English"]).get(
        key, UI_STRINGS["English"].get(key, key)
    )


# ── TTS helper ───────────────────────────────────────────────────────
def _generate_tts(text: str) -> Optional[bytes]:
    """Convert text to speech audio bytes using current language. Returns None on failure."""
    lang = st.session_state.get("language", "English")
    gtts_code = LANGUAGE_MAP.get(lang, LANGUAGE_MAP["English"])["gtts_code"]
    try:
        tts = gTTS(text=text, lang=gtts_code)
        buf = io.BytesIO()
        tts.write_to_fp(buf)
        buf.seek(0)
        return buf.read()
    except Exception as exc:
        print(f"[tts] {type(exc).__name__}: {exc}")
        return None


# ══════════════════════════════════════════════════════════════════════
# LANGUAGE SELECTOR -- rendered globally at the top across ALL stages
# ══════════════════════════════════════════════════════════════════════
language = st.selectbox(
    S("language_selector_label"),
    options=list(LANGUAGE_MAP.keys()),
    index=list(LANGUAGE_MAP.keys()).index(
        st.session_state.get("language", "English")
    ),
    key="language_selector",
)
st.session_state["language"] = language
lang_config = LANGUAGE_MAP[language]

st.title(S("kiosk_title"))

stage = st.session_state["kiosk_stage"]

# ══════════════════════════════════════════════════════════════════════
# STAGE: AWAITING_ABHA
# ══════════════════════════════════════════════════════════════════════
if stage == "AWAITING_ABHA":
    with st.container():
        st.subheader(S("patient_verification"))
        abha_id = st.text_input(
            S("abha_label"),
            max_chars=14,
            placeholder=S("abha_placeholder"),
            value=st.session_state.get("abha_id", ""),
        )
        if st.button(S("generate_otp_button")):
            if not abha_id.isdigit() or len(abha_id) != 14:
                st.warning("Invalid ABHA ID: Must be exactly 14 numeric digits.")
            else:
                st.session_state["abha_id"] = abha_id
                otp = str(random.randint(100000, 999999))
                st.session_state["demo_otp"] = otp
                st.session_state["abha_data"] = MOCK_ABHA_DATA.copy()
                st.session_state["kiosk_stage"] = "AWAITING_OTP"
                st.rerun()

# ══════════════════════════════════════════════════════════════════════
# STAGE: AWAITING_OTP
# ══════════════════════════════════════════════════════════════════════
elif stage == "AWAITING_OTP":
    with st.container():
        st.subheader(S("patient_verification"))
        # Display the demo OTP prominently
        st.info(S("otp_sent_msg").format(code=st.session_state["demo_otp"]))

        otp_input = st.text_input(
            S("otp_label"),
            max_chars=6,
            placeholder="------",
        )
        col_verify, col_resend = st.columns(2)
        with col_verify:
            if st.button(S("verify_otp_button")):
                if otp_input == st.session_state["demo_otp"]:
                    # OTP match -- advance directly to IN_TRIAGE
                    st.session_state["kiosk_stage"] = "IN_TRIAGE"
                    st.rerun()
                else:
                    st.error(S("otp_error"))
        with col_resend:
            if st.button(S("resend_otp_button")):
                # Regenerate OTP without losing ABHA ID
                otp = str(random.randint(100000, 999999))
                st.session_state["demo_otp"] = otp
                st.session_state["kiosk_stage"] = "AWAITING_OTP"
                st.rerun()

# ══════════════════════════════════════════════════════════════════════
# STAGE: IN_TRIAGE -- Voice-first conversation
# ══════════════════════════════════════════════════════════════════════
elif stage == "IN_TRIAGE":
    # Show patient identity bar
    patient = st.session_state.get("abha_data", {})
    if patient:
        st.success(
            f"{S('verified_status')} -- {patient.get('name', '')} "
            f"(ABHA: {st.session_state.get('abha_id', '')})"
        )

    # ── Document Scanner (collapsible, optional) ─────────────────────
    with st.container(border=True):
        with st.expander(S("scanner_heading"), expanded=False):
            st.caption(S("scanner_caption"))
            uploaded_file = st.file_uploader(
                S("scanner_label"),
                type=["png", "jpg", "jpeg"],
                accept_multiple_files=False,
                key="doc_uploader",
            )
            if uploaded_file is not None:
                already_processed = any(
                    rec.get("source_filename") == uploaded_file.name
                    for rec in st.session_state["document_records"]
                )
                if not already_processed:
                    with st.spinner(S("processing_audio")):
                        try:
                            image_bytes = uploaded_file.read()
                            mime_type = uploaded_file.type or "image/png"
                            parsed = extract_document(
                                client, image_bytes, mime_type, VISION_EXTRACTION_PROMPT
                            )
                            parsed["source_filename"] = uploaded_file.name
                            st.session_state["document_records"].append(parsed)
                            doc_type = parsed.get("document_type", "Document")
                            doc_date = parsed.get("date", "unknown date")
                            st.success(
                                S("doc_extracted").format(
                                    doc_type=doc_type, doc_date=doc_date
                                )
                            )
                        except ValueError:
                            st.warning(S("doc_extract_failed"))
                        except Exception:
                            st.warning(S("doc_service_unavailable"))

            if st.session_state["document_records"]:
                st.markdown(f"**{S('scanned_docs_heading')}**")
                for rec in st.session_state["document_records"]:
                    st.markdown(
                        f"- **{rec.get('document_type', 'N/A')}** "
                        f"({rec.get('date', 'N/A')}) -- "
                        f"_{rec.get('source_filename', '')}_"
                    )

    st.divider()
    with st.container(border=True):
        st.subheader(S("triage_heading"))

        # ── Helper: process response (red-flag / completion) ─────────────
        def _process_response(raw_response: str) -> bool:
            """Apply red-flag and completion detection. Returns True if stage should advance."""
            if "[RED_FLAG_DETECTED]" in raw_response:
                st.session_state["interview_status"] = "RED_FLAG"
                st.session_state["messages"].append(
                    {
                        "role": "assistant",
                        "content": "[Interview halted -- emergency escalation triggered]",
                    }
                )
                st.session_state["kiosk_stage"] = "COMPLETE"
                return True

            elif "[INTERVIEW_COMPLETE]" in raw_response:
                st.session_state["interview_status"] = "COMPLETE"
                cleaned = raw_response.replace("[INTERVIEW_COMPLETE]", "").strip()
                if cleaned:
                    st.session_state["messages"].append(
                        {"role": "assistant", "content": cleaned}
                    )
                    audio = _generate_tts(cleaned)
                    if audio:
                        st.session_state["last_audio_response"] = audio
                st.session_state["kiosk_stage"] = "COMPLETE"
                return True

            else:
                st.session_state["messages"].append(
                    {"role": "assistant", "content": raw_response}
                )
                audio = _generate_tts(raw_response)
                if audio:
                    st.session_state["last_audio_response"] = audio
                st.session_state["turn_counter"] = st.session_state.get("turn_counter", 0) + 1
                return False

        # ── Proactive opening turn ───────────────────────────────────────
        if (
            len(st.session_state["messages"]) == 0
            and st.session_state["interview_status"] == "ACTIVE"
        ):
            opening_user = "Hello, I am here for my consultation."
            st.session_state["messages"].append({"role": "user", "content": opening_user})
            try:
                opening_response = chat_turn(
                    client,
                    st.session_state["messages"],
                    TRIAGE_SYSTEM_PROMPT,
                    language_name=lang_config["gemini_name"],
                )
                if _process_response(opening_response):
                    st.rerun()
            except Exception as exc:
                print(f"[chat-init] {type(exc).__name__}: {exc}")
                fallback = (
                    "Welcome. I am your triage assistant. Could you please tell me "
                    "what brings you in today?"
                )
                st.session_state["messages"].append(
                    {"role": "assistant", "content": fallback}
                )
                audio = _generate_tts(fallback)
                if audio:
                    st.session_state["last_audio_response"] = audio

        # ── Render chat history ──────────────────────────────────────────
        for msg in st.session_state["messages"]:
            with st.chat_message(msg["role"]):
                if msg["role"] == "user":
                    st.markdown(f"**{msg['content']}**")
                else:
                    st.markdown(msg["content"])

        # ── TTS autoplay ─────────────────────────────────────────────────
        turn_counter = st.session_state.get("turn_counter", 0)
        if st.session_state.get("last_audio_response"):
            st.audio(
                st.session_state["last_audio_response"],
                format="audio/mp3",
                autoplay=True,
            )
        elif st.session_state["messages"] and st.session_state["messages"][-1]["role"] == "assistant":
            st.caption(S("audio_unavailable"))

        # ── Microphone input ─────────────────────────────────────────────
        if st.session_state["interview_status"] == "ACTIVE":
            st.markdown("---")
            st.markdown(f"**{S('speak_prompt')}**")

            st.html("""
            <style>
            iframe[title="streamlit_mic_recorder.mic_recorder"] {
                width: 120px !important;
                height: 120px !important;
                border-radius: 50% !important;
                background-color: #005C97 !important;
                border: none !important;
                display: block !important;
                margin: 0 auto !important;
            }
            div[data-testid="stHorizontalBlock"] button {
                width: 120px !important;
                height: 120px !important;
                border-radius: 50% !important;
                background-color: #005C97 !important;
                color: white !important;
                border: none !important;
                font-weight: bold !important;
                display: flex !important;
                justify-content: center !important;
                align-items: center !important;
                margin: 0 auto !important;
            }
            </style>
            """)
            col1, col2, col3 = st.columns([1, 1, 1])
            with col2:
                audio_data = mic_recorder(
                    start_prompt=S("mic_start_prompt"),
                    stop_prompt=S("mic_stop_prompt"),
                    use_container_width=True,
                    format="webm",
                    key=f"mic_recorder_{turn_counter}",
                )

            if audio_data and audio_data.get("bytes"):
                audio_bytes = audio_data["bytes"]
                if len(audio_bytes) > 0:
                    with st.spinner(S("processing_audio")):
                        # Step 1: Transcribe
                        try:
                            transcribed_text = transcribe_audio(
                                client, audio_bytes, "audio/webm", TRANSCRIPTION_PROMPT
                            )
                        except Exception as exc:
                            print(f"[transcribe] {type(exc).__name__}: {exc}")
                            st.warning(S("transcription_failed"))
                            transcribed_text = None

                        if transcribed_text and transcribed_text.strip():
                            st.session_state["messages"].append(
                                {"role": "user", "content": transcribed_text.strip()}
                            )
                            # Step 2: Chat pipeline
                            try:
                                raw_response = chat_turn(
                                    client,
                                    st.session_state["messages"],
                                    TRIAGE_SYSTEM_PROMPT,
                                    language_name=lang_config["gemini_name"],
                                )
                            except Exception as exc:
                                print(f"[chat] {type(exc).__name__}: {exc}")
                                raw_response = None
                                st.session_state["messages"].append(
                                    {
                                        "role": "assistant",
                                        "content": "Sorry, I am having trouble right now -- please wait a moment and try again.",
                                    }
                                )

                            if raw_response is not None:
                                _process_response(raw_response)

                            st.rerun()

    # ══════════════════════════════════════════════════════════════════════
    # STAGE: COMPLETE -- Save record and offer reset
    # ══════════════════════════════════════════════════════════════════════
elif stage == "COMPLETE":
    interview_status = st.session_state.get("interview_status", "COMPLETE")

    # Show appropriate completion message
    if interview_status == "RED_FLAG":
        st.error(S("red_flag_alert"))
    else:
        st.success(S("interview_complete"))

    # Display final transcript
    for msg in st.session_state["messages"]:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    # ── Compute structured_case and save record ──────────────────────
    if not st.session_state.get("record_saved"):
        with st.spinner(S("saving_record")):
            # Compute structured_case if not cached
            if not st.session_state.get("structured_case"):
                transcript_lines = []
                for msg in st.session_state["messages"]:
                    role_label = "Patient" if msg["role"] == "user" else "Assistant"
                    transcript_lines.append(f"{role_label}: {msg['content']}")
                transcript_text = "\n".join(transcript_lines)
                prompt = STRUCTURED_EXTRACTION_PROMPT.format(
                    transcript=transcript_text
                )
                # extract_structured_case never raises -- returns defaults on failure
                structured = extract_structured_case(client, prompt)
                st.session_state["structured_case"] = structured

            # Assemble record per the Section 2 schema
            patient = st.session_state.get("abha_data", {})
            record = {
                "record_id": str(uuid.uuid4()),
                "saved_at": datetime.now().isoformat(),
                "abha_id": st.session_state.get("abha_id", ""),
                "name": patient.get("name", ""),
                "age": patient.get("age", 0),
                "language": st.session_state.get("language", "English"),
                "interview_status": interview_status,
                "messages": st.session_state.get("messages", []),
                "structured_case": st.session_state.get("structured_case", {}),
                "abha_data": {
                    "chronic_conditions": patient.get("chronic_conditions", []),
                    "past_medications": patient.get("past_medications", []),
                },
                "document_records": st.session_state.get("document_records", []),
            }

            saved = append_record(record)
            st.session_state["record_saved"] = saved
            if saved:
                st.success(S("record_saved"))
            else:
                st.error(S("record_save_failed"))

    else:
        st.success(S("record_saved"))

    # ── Reset button ─────────────────────────────────────────────────
    if st.button(S("finish_button")):
        st.session_state.clear()
        # Re-init defaults after clear
        for key, default in _DEFAULTS.items():
            st.session_state[key] = (
                default.copy() if isinstance(default, (list, dict)) else default
            )
        st.rerun()
