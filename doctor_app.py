"""
PrashnaAyur -- Doctor-Facing Dashboard Application
====================================================
Reads completed patient records from local_db.json. No Gemini calls needed --
all structured data is pre-computed on the kiosk side.

Run independently: streamlit run doctor_app.py --server.port 8502

Authentication is demo-grade (hardcoded credentials). Do NOT use this
for production authentication.
"""

import json
from datetime import datetime
import streamlit as st

st.set_page_config(page_title="PrashnaAyur Doctor Dashboard", layout="wide", initial_sidebar_state="collapsed")

from utils.db_handler import read_all_records, read_record_by_id

# ── Demo credential store ────────────────────────────────────────────
# NOTE: This is a demo-grade credential store for hackathon/prototype use.
# In production, use a proper authentication provider (OAuth, LDAP, etc.).
DEMO_DOCTORS = {
    "DOC-AYUSH-101": "1234",
    "DOC-AYUSH-102": "5678",
}

# ── Session state defaults ───────────────────────────────────────────
if "doctor_authenticated" not in st.session_state:
    st.session_state["doctor_authenticated"] = False
if "doctor_id" not in st.session_state:
    st.session_state["doctor_id"] = ""
if "selected_record_id" not in st.session_state:
    st.session_state["selected_record_id"] = None


# ══════════════════════════════════════════════════════════════════════
# LOGIN SCREEN
# ══════════════════════════════════════════════════════════════════════
if not st.session_state["doctor_authenticated"]:
    st.title("PrashnaAyur -- Doctor Login")
    st.caption("Demo-grade authentication. Not for production use.")

    with st.container(border=True):
        doctor_id = st.text_input(
            "Doctor ID",
            placeholder="DOC-AYUSH-101",
        )
        pin = st.text_input(
            "PIN",
            type="password",
            placeholder="Enter PIN",
        )
        if st.button("Log In"):
            if doctor_id in DEMO_DOCTORS and DEMO_DOCTORS[doctor_id] == pin:
                st.session_state["doctor_authenticated"] = True
                st.session_state["doctor_id"] = doctor_id
                st.rerun()
            else:
                st.error("Invalid credentials.")

    st.stop()

# ══════════════════════════════════════════════════════════════════════
# AUTHENTICATED -- Header + Logout
# ══════════════════════════════════════════════════════════════════════
header_col1, header_col2 = st.columns([4, 1])
with header_col1:
    st.title("PrashnaAyur -- Doctor Dashboard")
with header_col2:
    if st.button("Log Out"):
        st.session_state["doctor_authenticated"] = False
        st.session_state["doctor_id"] = ""
        st.session_state["selected_record_id"] = None
        st.rerun()

st.caption(f"Logged in as: **{st.session_state['doctor_id']}**")
st.divider()

# ══════════════════════════════════════════════════════════════════════
# PATIENT DETAIL VIEW (if a record is selected)
# ══════════════════════════════════════════════════════════════════════
selected_id = st.session_state.get("selected_record_id")

if selected_id:
    record = read_record_by_id(selected_id)
    if not record:
        st.warning("Record not found. It may have been removed.")
        st.session_state["selected_record_id"] = None
        st.rerun()

    # ── Back button ──────────────────────────────────────────────────
    if st.button("Back to Queue"):
        st.session_state["selected_record_id"] = None
        st.rerun()

    # ── Top metrics bar ──────────────────────────────────────────────
    col_m1, col_m2, col_m3, col_m4 = st.columns(4)
    with col_m1:
        st.metric("Patient", record.get("name", "N/A"))
    with col_m2:
        st.metric("Age", record.get("age", "N/A"))
    with col_m3:
        st.metric("Language", record.get("language", "N/A"))
    with col_m4:
        status = record.get("interview_status", "ACTIVE")
        triage_label = "Urgent" if status == "RED_FLAG" else "Normal"
        st.metric("Triage Status", triage_label)

    if status == "RED_FLAG":
        st.error("Urgent -- Emergency escalation triggered during intake")
    else:
        st.success("Normal -- No emergency indicators detected")

    st.divider()

    # ── Two-column layout ────────────────────────────────────────────
    col_left, col_right = st.columns(2)

    # LEFT: Structured case + transcript
    with col_left:
        st.subheader("SOCRATES and Prakriti Assessment")

        structured = record.get("structured_case", {})
        field_labels = {
            "site": "Site",
            "onset": "Onset",
            "character": "Character",
            "radiation": "Radiation",
            "associations": "Associations",
            "time_course": "Time Course",
            "exacerbating_relieving_factors": "Exacerbating / Relieving Factors",
            "severity": "Severity",
            "prakriti_notes": "Prakriti Notes (Ayurvedic)",
        }

        if structured:
            for field_key, label in field_labels.items():
                value = structured.get(field_key, "Not yet assessed")
                col_l, col_v = st.columns([1, 2])
                with col_l:
                    st.markdown(f"**{label}**")
                with col_v:
                    st.markdown(value)
        else:
            st.info("No structured case data available.")

        st.divider()

        st.markdown("**Full Interview Transcript**")
        messages = record.get("messages", [])
        if messages:
            with st.expander("View Transcript", expanded=False):
                for msg in messages:
                    role_label = "Patient" if msg.get("role") == "user" else "Assistant"
                    st.markdown(f"**{role_label}:** {msg.get('content', '')}")
        else:
            st.caption("No interview transcript available.")

    # RIGHT: ABHA history + scanned documents
    with col_right:
        st.subheader("Patient History and Records")

        abha = record.get("abha_data", {})

        # Chronic Conditions
        conditions = abha.get("chronic_conditions", [])
        with st.expander("Chronic Conditions", expanded=True):
            if conditions:
                for c in conditions:
                    st.markdown(
                        f"- **{c.get('condition', 'N/A')}** -- "
                        f"Diagnosed: {c.get('diagnosed_date', 'N/A')} -- "
                        f"Status: {c.get('status', 'N/A')}"
                    )
            else:
                st.caption("No chronic conditions on record.")

        # Past Medications
        medications = abha.get("past_medications", [])
        with st.expander("Past Medications", expanded=True):
            if medications:
                for m in medications:
                    st.markdown(
                        f"- **{m.get('drug', 'N/A')}** {m.get('dosage', '')} -- "
                        f"{m.get('frequency', '')}"
                    )
            else:
                st.caption("No past medications on record.")

        st.divider()

        # Scanned Documents
        st.markdown("**Scanned Documents**")
        doc_records = record.get("document_records", [])

        if not doc_records:
            st.caption("No scanned documents.")
        else:
            def _sort_key(rec):
                try:
                    return datetime.strptime(rec.get("date", ""), "%Y-%m-%d")
                except (ValueError, TypeError):
                    return datetime.min

            sorted_docs = sorted(doc_records, key=_sort_key)
            for doc in sorted_docs:
                doc_type = doc.get("document_type", "Document")
                doc_date = doc.get("date", "Unknown date")
                expander_title = f"{doc_type} -- {doc_date}"

                with st.expander(expander_title, expanded=False):
                    if doc.get("source_filename"):
                        st.caption(f"Source: {doc['source_filename']}")

                    vitals = doc.get("extracted_vitals_or_labs", [])
                    if vitals:
                        st.markdown("**Vitals / Lab Results:**")
                        for v in vitals:
                            abnormal_tag = " **[Abnormal]**" if v.get("is_abnormal") else ""
                            st.markdown(
                                f"- **{v.get('test_name', 'N/A')}**: "
                                f"{v.get('value', 'N/A')}{abnormal_tag}"
                            )
                            if v.get("is_abnormal"):
                                st.warning(
                                    f"{v.get('test_name', 'Value')} is abnormal"
                                )

                    meds = doc.get("extracted_medications", [])
                    if meds:
                        st.markdown("**Medications:**")
                        for med in meds:
                            st.markdown(
                                f"- **{med.get('medicine_name', 'N/A')}** "
                                f"{med.get('dosage', '')}"
                            )

                    if not vitals and not meds:
                        st.caption("No structured data extracted.")

    # ── Footer: FHIR Export ──────────────────────────────────────────
    st.divider()

    def _build_fhir_bundle(rec: dict) -> dict:
        """Build a demo-grade FHIR-style Bundle from a record dict."""
        entries = []

        # Patient resource
        entries.append({
            "resource": {
                "resourceType": "Patient",
                "name": rec.get("name", ""),
                "id": rec.get("abha_id", ""),
                "age": str(rec.get("age", "")),
            }
        })

        abha_data = rec.get("abha_data", {})

        # Conditions from ABHA
        for c in abha_data.get("chronic_conditions", []):
            entries.append({
                "resource": {
                    "resourceType": "Condition",
                    "code": c.get("condition", ""),
                    "onsetDate": c.get("diagnosed_date", ""),
                    "status": c.get("status", ""),
                }
            })

        # Medications from ABHA
        for m in abha_data.get("past_medications", []):
            entries.append({
                "resource": {
                    "resourceType": "MedicationStatement",
                    "medicationName": m.get("drug", ""),
                    "dosage": m.get("dosage", ""),
                    "frequency": m.get("frequency", ""),
                }
            })

        # Observations from scanned documents
        for doc in rec.get("document_records", []):
            for v in doc.get("extracted_vitals_or_labs", []):
                entries.append({
                    "resource": {
                        "resourceType": "Observation",
                        "testName": v.get("test_name", ""),
                        "value": v.get("value", ""),
                        "abnormal": v.get("is_abnormal", False),
                        "sourceDate": doc.get("date", ""),
                    }
                })

        # Chief complaint from structured case
        sc = rec.get("structured_case", {})
        if sc and sc.get("site") != "Not yet assessed":
            entries.append({
                "resource": {
                    "resourceType": "Condition",
                    "code": f"Chief complaint at {sc.get('site', 'unspecified site')}",
                    "onset": sc.get("onset", ""),
                    "severity": sc.get("severity", ""),
                    "category": "chief-complaint",
                }
            })

        return {
            "resourceType": "Bundle",
            "type": "collection",
            "entry": entries,
        }

    fhir_bundle = _build_fhir_bundle(record)
    fhir_json = json.dumps(fhir_bundle, indent=2, ensure_ascii=False)

    st.download_button(
        label="Export to FHIR JSON",
        data=fhir_json,
        file_name=f"prashnaayur_fhir_{record.get('record_id', 'export')}.json",
        mime="application/json",
    )

    st.stop()

# ══════════════════════════════════════════════════════════════════════
# PATIENT QUEUE (no record selected)
# ══════════════════════════════════════════════════════════════════════
st.subheader("Patient Records Queue")

if st.button("Refresh Queue"):
    pass  # Button click triggers rerun, which re-reads the DB

records = read_all_records()

if not records:
    st.info("No patient records yet.")
    st.stop()

# Sort most recent first
records_sorted = sorted(
    records,
    key=lambda r: r.get("saved_at", ""),
    reverse=True,
)

for rec in records_sorted:
    status = rec.get("interview_status", "ACTIVE")
    name = rec.get("name", "Unknown")
    abha_id = rec.get("abha_id", "N/A")
    saved_at = rec.get("saved_at", "N/A")
    record_id = rec.get("record_id", "")

    # Format saved time for display
    try:
        dt = datetime.fromisoformat(saved_at)
        time_display = dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        time_display = saved_at

    # Use st.error container for red-flag, st.container for normal
    if status == "RED_FLAG":
        with st.container():
            cols = st.columns([3, 2, 2, 2, 1])
            with cols[0]:
                st.markdown(f"**{name}**")
            with cols[1]:
                st.markdown(f"ABHA: {abha_id}")
            with cols[2]:
                st.markdown(f"**Urgent**")
            with cols[3]:
                st.markdown(time_display)
            with cols[4]:
                if st.button("View", key=f"view_{record_id}"):
                    st.session_state["selected_record_id"] = record_id
                    st.rerun()
            st.error("This patient triggered an emergency escalation during intake.")
    else:
        with st.container():
            cols = st.columns([3, 2, 2, 2, 1])
            with cols[0]:
                st.markdown(f"**{name}**")
            with cols[1]:
                st.markdown(f"ABHA: {abha_id}")
            with cols[2]:
                st.markdown("Normal")
            with cols[3]:
                st.markdown(time_display)
            with cols[4]:
                if st.button("View", key=f"view_{record_id}"):
                    st.session_state["selected_record_id"] = record_id
                    st.rerun()
