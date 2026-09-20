"""
PrashnaAyur -- Patient Kiosk Page
==================================
Voice-first triage interview with document scanning.
Routed via st.navigation from app.py.
"""

import io
import os
from typing import Optional
import streamlit as st
from dotenv import load_dotenv
from streamlit_mic_recorder import mic_recorder
from gtts import gTTS

load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

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

# ── Get the cached Gemini client ─────────────────────────────────────
client = get_genai_client(GEMINI_API_KEY)

# ══════════════════════════════════════════════════════════════════════
# PAGE HEADER
# ══════════════════════════════════════════════════════════════════════
st.title("PrashnaAyur -- Patient Intake Kiosk")

if not st.session_state.get("abha_logged_in"):
    st.info(
        "Please log in with your ABHA Health ID in the sidebar to begin."
    )
    st.stop()

# ══════════════════════════════════════════════════════════════════════
# LANGUAGE SELECTOR
# ══════════════════════════════════════════════════════════════════════
language = st.selectbox(
    "Interview Language",
    options=list(LANGUAGE_MAP.keys()),
    index=list(LANGUAGE_MAP.keys()).index(
        st.session_state.get("language", "English")
    ),
    key="language_selector",
)
st.session_state["language"] = language
lang_config = LANGUAGE_MAP[language]

# ══════════════════════════════════════════════════════════════════════
# DOCUMENT SCANNER
# ══════════════════════════════════════════════════════════════════════
st.subheader("Document Scanner")
st.caption(
    "Upload a photo of a prescription or lab report for automatic extraction."
)

uploaded_file = st.file_uploader(
    "Upload a medical document image",
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
        with st.spinner("Analysing document..."):
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
                st.success(f"Extracted: **{doc_type}** dated {doc_date}")
            except ValueError as ve:
                print(f"[doc-extract] JSON parse failed: {ve}")
                st.warning(
                    "Could not read this document -- please try a clearer photo."
                )
            except Exception as exc:
                print(f"[doc-extract] API error: {type(exc).__name__}: {exc}")
                st.warning(
                    "AI service unavailable -- could not process the document. "
                    "Please try again in a moment."
                )

    if st.session_state["document_records"]:
        with st.expander("Scanned Documents So Far", expanded=False):
            for rec in st.session_state["document_records"]:
                st.markdown(
                    f"- **{rec.get('document_type', 'N/A')}** "
                    f"({rec.get('date', 'N/A')}) -- "
                    f"_{rec.get('source_filename', '')}_"
                )

st.divider()

# ══════════════════════════════════════════════════════════════════════
# TRIAGE INTERVIEW -- Voice-First
# ══════════════════════════════════════════════════════════════════════
st.subheader("AI Triage Interview")

# ── Helper: generate TTS audio bytes ─────────────────────────────────
def _generate_tts(text: str, gtts_code: str) -> Optional[bytes]:
    """Convert text to speech audio bytes. Returns None on failure."""
    try:
        tts = gTTS(text=text, lang=gtts_code)
        buf = io.BytesIO()
        tts.write_to_fp(buf)
        buf.seek(0)
        return buf.read()
    except Exception as exc:
        print(f"[tts] {type(exc).__name__}: {exc}")
        return None


# ── Helper: process assistant response (red-flag / completion) ───────
def _process_response(raw_response: str) -> bool:
    """
    Apply red-flag and completion detection to the raw response.
    Returns True if rerun is needed, False otherwise.
    """
    if "[RED_FLAG_DETECTED]" in raw_response:
        st.session_state["interview_status"] = "RED_FLAG"
        st.session_state["messages"].append(
            {
                "role": "assistant",
                "content": "[Interview halted -- emergency escalation triggered]",
            }
        )
        return True

    elif "[INTERVIEW_COMPLETE]" in raw_response:
        st.session_state["interview_status"] = "COMPLETE"
        cleaned = raw_response.replace("[INTERVIEW_COMPLETE]", "").strip()
        if cleaned:
            st.session_state["messages"].append(
                {"role": "assistant", "content": cleaned}
            )
            # Generate TTS for the final message
            audio = _generate_tts(cleaned, lang_config["gtts_code"])
            if audio:
                st.session_state["last_audio_response"] = audio
        st.session_state["case_summary"] = None
        st.session_state["structured_case"] = None
        return True

    else:
        st.session_state["messages"].append(
            {"role": "assistant", "content": raw_response}
        )
        # Generate TTS for the response
        audio = _generate_tts(raw_response, lang_config["gtts_code"])
        if audio:
            st.session_state["last_audio_response"] = audio
        return False


# ── Proactive opening turn ───────────────────────────────────────────
if (
    st.session_state["abha_logged_in"]
    and len(st.session_state["messages"]) == 0
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
        needs_rerun = _process_response(opening_response)
        if needs_rerun:
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
        audio = _generate_tts(fallback, lang_config["gtts_code"])
        if audio:
            st.session_state["last_audio_response"] = audio

# ── Render chat history ──────────────────────────────────────────────
for msg in st.session_state["messages"]:
    with st.chat_message(msg["role"]):
        st.markdown(f"**{msg['content']}**" if msg["role"] == "user" else msg["content"])

# ── Status banners ───────────────────────────────────────────────────
if st.session_state["interview_status"] == "RED_FLAG":
    st.error(
        "**MEDICAL EMERGENCY DETECTED**\n\n"
        "Please proceed to the Emergency Room (ER) immediately "
        "or call your local emergency number. Do not wait."
    )
elif st.session_state["interview_status"] == "COMPLETE":
    st.success(
        "Interview complete. You may now proceed to see the doctor.\n\n"
        "The doctor can view your case on the **Doctor Dashboard** page."
    )

# ── TTS playback for last response ──────────────────────────────────
if st.session_state.get("last_audio_response"):
    st.audio(st.session_state["last_audio_response"], format="audio/mp3", autoplay=True)
    if st.button("Play Response"):
        st.audio(
            st.session_state["last_audio_response"],
            format="audio/mp3",
            autoplay=True,
        )

# ── Microphone input -- only if interview is ACTIVE ──────────────────
if st.session_state["interview_status"] == "ACTIVE":
    st.markdown("---")
    st.markdown("**Speak your response:**")

    audio_data = mic_recorder(
        start_prompt="Tap to Speak",
        stop_prompt="Recording -- Tap to Stop",
        use_container_width=True,
        format="webm",
        key=f"mic_{len(st.session_state['messages'])}",
    )

    if audio_data and audio_data.get("bytes"):
        audio_bytes = audio_data["bytes"]

        # Guard: skip empty recordings
        if len(audio_bytes) > 0:
            with st.spinner("Processing your response..."):
                # Step 1: Transcribe audio via Gemini
                try:
                    transcribed_text = transcribe_audio(
                        client,
                        audio_bytes,
                        "audio/webm",
                        TRANSCRIPTION_PROMPT,
                    )
                except Exception as exc:
                    print(f"[transcribe] {type(exc).__name__}: {exc}")
                    st.warning(
                        "Could not process the recording -- please try again."
                    )
                    transcribed_text = None

                if transcribed_text and transcribed_text.strip():
                    # Append user message
                    st.session_state["messages"].append(
                        {"role": "user", "content": transcribed_text.strip()}
                    )

                    # Step 2: Feed into existing chat pipeline
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
                        fallback_msg = (
                            "Sorry, I am having trouble right now -- "
                            "please wait a moment and try again."
                        )
                        st.session_state["messages"].append(
                            {"role": "assistant", "content": fallback_msg}
                        )

                    if raw_response is not None:
                        needs_rerun = _process_response(raw_response)

                    # Always rerun to refresh the UI with new messages
                    st.rerun()
