"""
utils/fhir_builder.py
======================
Builds a demo-grade ABDM-style FHIR Bundle from structured intake data.
Clearly demo-grade: not spec-certified for production ABDM/FHIR compliance.
"""

import uuid
from datetime import datetime
from typing import Dict, Any, List, Optional


def build_fhir_bundle(
    abha_data: Dict[str, Any],
    structured_intake: Dict[str, Any],
    transcript: List[Dict[str, str]],
    status: str = "COMPLETE",
) -> Dict[str, Any]:
    """
    Construct a FHIR-style Bundle from session data.

    Args:
        abha_data:         Mock ABHA record (name, age, chronic_conditions, past_medications)
        structured_intake: SOCRATES + Prakriti fields from submit_structured_intake tool call
        transcript:        List of {"role": "patient"|"assistant", "text": "..."} dicts
        status:            "COMPLETE" or "RED_FLAG"

    Returns:
        A dict representing a FHIR Bundle (demo-grade, not ABDM-certified)
    """
    now_iso = datetime.utcnow().isoformat() + "Z"
    patient_id = str(uuid.uuid4())
    bundle_id  = str(uuid.uuid4())

    entries: List[Dict[str, Any]] = []

    # ── Patient resource ─────────────────────────────────────────────────────
    entries.append({
        "fullUrl": f"urn:uuid:{patient_id}",
        "resource": {
            "resourceType": "Patient",
            "id": patient_id,
            "identifier": [
                {
                    "system": "https://abha.abdm.gov.in",
                    "value": abha_data.get("abha_id", ""),
                }
            ],
            "name": [{"text": abha_data.get("name", "")}],
            "birthDate": None,  # age kept as extension for demo simplicity
            "extension": [
                {
                    "url": "patient-age-years",
                    "valueInteger": abha_data.get("age", 0),
                }
            ],
        },
    })

    # ── Chronic conditions (from ABHA mock record) ──────────────────────────
    for cond in abha_data.get("chronic_conditions", []):
        entries.append({
            "fullUrl": f"urn:uuid:{uuid.uuid4()}",
            "resource": {
                "resourceType": "Condition",
                "clinicalStatus": {"coding": [{"code": cond.get("status", "active").lower()}]},
                "code": {"text": cond.get("condition", "")},
                "onsetDateTime": cond.get("diagnosed_date", ""),
                "subject": {"reference": f"urn:uuid:{patient_id}"},
            },
        })

    # ── Past medications (from ABHA mock record) ─────────────────────────────
    for med in abha_data.get("past_medications", []):
        entries.append({
            "fullUrl": f"urn:uuid:{uuid.uuid4()}",
            "resource": {
                "resourceType": "MedicationStatement",
                "status": "unknown",
                "medicationCodeableConcept": {"text": med.get("drug", "")},
                "dosage": [
                    {
                        "text": f"{med.get('dosage', '')} {med.get('frequency', '')}".strip()
                    }
                ],
                "subject": {"reference": f"urn:uuid:{patient_id}"},
            },
        })

    # ── Chief complaint / SOCRATES observation ───────────────────────────────
    if structured_intake:
        obs_id = str(uuid.uuid4())
        # Flatten SOCRATES fields into a human-readable note for the Observation
        socrates_note = "\n".join(
            f"{k.replace('_', ' ').title()}: {v}"
            for k, v in structured_intake.items()
        )
        entries.append({
            "fullUrl": f"urn:uuid:{obs_id}",
            "resource": {
                "resourceType": "Observation",
                "id": obs_id,
                "status": "preliminary",
                "category": [
                    {
                        "coding": [
                            {
                                "system": "http://terminology.hl7.org/CodeSystem/observation-category",
                                "code": "survey",
                                "display": "Survey",
                            }
                        ]
                    }
                ],
                "code": {
                    "coding": [{"code": "chief-complaint", "display": "Chief Complaint / SOCRATES"}]
                },
                "subject": {"reference": f"urn:uuid:{patient_id}"},
                "effectiveDateTime": now_iso,
                "component": [
                    {
                        "code": {"text": k.replace("_", " ").title()},
                        "valueString": v,
                    }
                    for k, v in structured_intake.items()
                ],
                "note": [{"text": socrates_note}],
            },
        })

    # ── Full transcript (as a Communication resource) ─────────────────────────
    if transcript:
        transcript_text = "\n".join(
            f"[{t.get('role', 'unknown').upper()}] {t.get('text', '')}"
            for t in transcript
        )
        entries.append({
            "fullUrl": f"urn:uuid:{uuid.uuid4()}",
            "resource": {
                "resourceType": "Communication",
                "status": "completed",
                "subject": {"reference": f"urn:uuid:{patient_id}"},
                "sent": now_iso,
                "payload": [{"contentString": transcript_text}],
                "note": [{"text": "Auto-generated triage interview transcript via PrashnaAyur kiosk."}],
            },
        })

    # ── Bundle ────────────────────────────────────────────────────────────────
    bundle = {
        "resourceType": "Bundle",
        "id": bundle_id,
        "type": "collection",
        "timestamp": now_iso,
        "meta": {
            "tag": [
                {
                    "system": "https://prashnaayur.dev/tags",
                    "code": status,
                    "display": "Triage Status",
                },
                {
                    "system": "https://prashnaayur.dev/tags",
                    "code": "DEMO_GRADE",
                    "display": "Not ABDM-certified. Hackathon prototype only.",
                },
            ]
        },
        "entry": entries,
        # Convenience fields for the doctor dashboard (non-FHIR, clearly labelled)
        "_prashnaayur": {
            "status": status,
            "patient_name": abha_data.get("name", ""),
            "patient_age": abha_data.get("age", 0),
            "abha_id": abha_data.get("abha_id", ""),
            "structured_intake": structured_intake,
            "transcript": transcript,
            "saved_at": now_iso,
        },
    }

    return bundle

def build_prescription_fhir_bundle(
    abha_id: str,
    care_context_id: str,
    ayush_rx: list = None,
    pathya_apathya: str = "",
    prescription_id: str = None,
    encounter_id: str = None,
    doctor_id: str = None,
    ayush_rx_items: list = None,
    **kwargs
) -> dict:
    rx_list = ayush_rx if ayush_rx is not None else (ayush_rx_items or [])
    rx_id = prescription_id or f"RX-{care_context_id}"
    
    entries = [
        {"resource": {"resourceType": "Patient", "id": abha_id}}
    ]
    if doctor_id:
        entries.append({"resource": {"resourceType": "Practitioner", "id": doctor_id}})
    if encounter_id:
        entries.append({"resource": {"resourceType": "Encounter", "id": encounter_id}})
        
    for rx in rx_list:
        if isinstance(rx, dict):
            entries.append({
                "resource": {
                    "resourceType": "MedicationRequest",
                    "medicationName": rx.get("medicine_name", rx.get("name", "Ayurvedic Medicine")),
                    "dosage": rx.get("dose", rx.get("dosage", "1 dose")),
                    "vehicle": rx.get("vehicle", rx.get("anupana", "Warm water")),
                    "frequency": rx.get("frequency", "Twice daily"),
                }
            })
    entries.append({"resource": {"resourceType": "CarePlan", "instructions": pathya_apathya or "None"}})

    return {
        "resourceType": "Bundle",
        "type": "collection",
        "id": rx_id,
        "identifier": {"value": care_context_id},
        "entry": entries,
    }

