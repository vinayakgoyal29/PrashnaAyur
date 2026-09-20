# PrashnAyur: Autonomous Vernacular AYUSH Intake Kiosk & Doctor EHR

[![Python](https://img.shields.io/badge/Python-3.10+-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-009688?style=for-the-badge&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![WebSockets](https://img.shields.io/badge/WebSockets-Full--Duplex-010101?style=for-the-badge&logo=socketdotio&logoColor=white)](https://websockets.readthedocs.io/)
[![Web Audio API](https://img.shields.io/badge/Web_Audio_API-24kHz_PCM-F16529?style=for-the-badge&logo=html5&logoColor=white)](https://developer.mozilla.org/en-US/docs/Web/API/Web_Audio_API)
[![Gemini Live API](https://img.shields.io/badge/Gemini_Live_API-Multimodal_v1alpha-4285F4?style=for-the-badge&logo=google&logoColor=white)](https://ai.google.dev/)
[![SQLite](https://img.shields.io/badge/SQLite-hospital__ehr.db-003B57?style=for-the-badge&logo=sqlite&logoColor=white)](https://www.sqlite.org/)
[![Tailwind CSS](https://img.shields.io/badge/Tailwind_CSS-Modern_UI-38B2AC?style=for-the-badge&logo=tailwind-css&logoColor=white)](https://tailwindcss.com/)
[![ABDM / FHIR R4](https://img.shields.io/badge/ABDM-FHIR_R4_Bundle-E11D48?style=for-the-badge)](https://abdm.gov.in/)

> **Smart India Hackathon (SIH 2026)**  
> **Problem Statement ID:** 26047  
> **Team:** Tech Rookies  

---

##  Executive Summary

**PrashnAyur** is an autonomous, multilingual intake kiosk and clinical decision-support EHR engineered to eliminate outpatient department (OPD) congestion in public AYUSH healthcare facilities. By conducting empathetic, full-duplex voice triage in regional languages and auto-synthesizing structured SOCRATES and Ayurvedic Prakriti profiles into ABDM-compliant FHIR records, PrashnAyur reduces doctor intake overhead by over 70% while preserving holistic diagnostic depth.

---

##  Core Features

*  **Full-Duplex Live Voice Triage (Hindi & English)**: Ultra-low-latency, natural conversational speech-to-speech interaction powered by the Gemini Multimodal Live API and a custom Web Audio API Worklet streaming 24kHz raw PCM audio bidirectionally.
*  **Dual Clinical Ontologies**: Concurrently structures vernacular patient narratives into both allopathic frameworks (**SOCRATES** pain assessment: *Site, Onset, Character, Radiation, Associations, Time course, Exacerbating/Relieving, Severity*) and holistic AYUSH diagnostics (**Ayurvedic Prakriti** phenotypic tendencies: *Vata, Pitta, Kapha*).
*  **Multimodal Document OCR**: Integrated document scanning for physical prescriptions and prior laboratory diagnostic reports, extracting relevant clinical history using multimodal vision models.
*  **Real-Time Doctor EHR Sync via SSE**: Live OPD token generation and instant queue updates streamed to the practitioner's console via Server-Sent Events (SSE), eliminating manual polling.
*  **ABDM / ABHA Integration (FHIR R4)**: Automatic generation of standardized, interoperable FHIR R4 JSON bundles mapped to patient Care Contexts for seamless national health ecosystem integration.
*  **Zero Local Voice Retention**: Kiosk streams voice directly into memory buffers without caching sensitive patient audio on kiosk disks, upholding DPDP and healthcare confidentiality standards.

---

##  System Architecture

```
                                  +----------------------------------------------------+
                                  |         PATIENT-FACING INTAKE KIOSK                |
                                  |                 (Port 8001)                        |
                                  +----------------------------------------------------+
                                       |                                    ^
                   24kHz PCM Audio     |                                    | 24kHz PCM
                   via Web Audio API   |                                    | Audio Stream
                                       v                                    |
                                  +----------------------------------------------------+
                                  |         FastAPI WebSocket Relay Gateway            |
                                  |               (kiosk_server.py)                    |
                                  +----------------------------------------------------+
                                       |                                    ^
                   Bi-directional      |                                    | Multimodal Live
                   Streaming Protocol  |                                    | Audio / Tool Calls
                                       v                                    |
                                  +----------------------------------------------------+
                                  |           GOOGLE GEMINI MULTIMODAL LIVE            |
                                  |      (Speech-to-Speech + OCR + Tool Directives)     |
                                  +----------------------------------------------------+
                                       |
                                       | Intake Finalized (SOCRATES + Prakriti + OCR)
                                       v
                                  +----------------------------------------------------+
                                  |         Internal Secure Dispatch (HMAC / Key)      |
                                  +----------------------------------------------------+
                                       |
                                       v
                                  +----------------------------------------------------+
                                  |              LOCAL EHR DATABASE                    |
                                  |              (hospital_ehr.db)                     |
                                  +----------------------------------------------------+
                                       |
                                       | Server-Sent Events (SSE) Broadcast
                                       v
                                  +----------------------------------------------------+
                                  |           DOCTOR CONSULTATION DASHBOARD            |
                                  |                 (Port 8002)                        |
                                  |  - Live Queue Sync      - Prescription Writer      |
                                  |  - FHIR R4 JSON Export  - Prakriti / Pain Matrix   |
                                  +----------------------------------------------------+
```

---

##  Tech Stack & Dependencies

| Layer | Technologies |
|---|---|
| **Backend Framework** | [FastAPI](https://fastapi.tiangolo.com/), [Uvicorn (ASGI)](https://www.uvicorn.org/), Python 3.10+ |
| **AI & Multimodal Engine** | [Google GenAI SDK](https://github.com/googleapis/python-genai) (`google-genai`), Gemini 2.0 Multimodal Live API (`v1alpha`), Gemini Vision OCR |
| **Client Audio Pipeline** | Web Audio API (`AudioWorkletNode`, custom `pcm-worklet.js`), 24,000 Hz 16-bit linear PCM |
| **Real-time Protocol** | WebSockets (Kiosk Speech Relay) & Server-Sent Events (EHR Queue Streaming) |
| **Storage & Data Model** | SQLite3 (`hospital_ehr.db`), Thread-safe connection pooling |
| **Interoperability** | HL7 FHIR R4 JSON Bundle Builder, Ayushman Bharat Digital Mission (ABDM) Schema |
| **Frontend Styling** | Vanilla JavaScript (ES6+), Modern Responsive CSS / Tailwind CSS |

---

##  Setup Instructions

### 1. Prerequisites
- Python 3.10 or higher installed
- Valid [Google Gemini API Key](https://aistudio.google.com/app/apikey) with Gemini 2.0 Multimodal Live API access
- Modern Web Browser with microphone and audio output permissions (Chrome/Edge recommended)

### 2. Clone the Repository
```bash
git clone https://github.com/your-org/PrashnAyur.git
cd PrashnAyur
```

### 3. Set Up Python Virtual Environment
```bash
# Create virtual environment
python3 -m venv .venv

# Activate virtual environment
# macOS / Linux:
source .venv/bin/activate
# Windows:
# .venv\Scripts\activate
```

### 4. Install Dependencies
```bash
pip install -r requirements.txt
```

### 5. Configure Environment Variables
Copy `.env.example` to `.env` and configure your API key:
```bash
cp .env.example .env
```
Edit `.env` to supply your credentials:
```env
# Google Gemini API Key (Required for Multimodal Live Speech)
GEMINI_API_KEY="AIzaSyYourGeminiApiKeyHere"

# Internal dispatch secret between Kiosk and Doctor EHR
INTERNAL_API_KEY="changeme-internal-token"

# Target Doctor Server URL
DOCTOR_SERVER_URL="http://127.0.0.1:8002"

# Database path
DB_PATH="hospital_ehr.db"
```

### 6. Initialize Baseline Demo Database (Optional)
Run the demo seeding script to populate an initial test patient and ensure schema alignment:
```bash
python scripts/reset_demo_data.py
```

### 7. Launch the Portals

Open two separate terminal tabs with the virtual environment activated:

#### Terminal 1: Patient Intake Kiosk (Port 8001)
```bash
python3 -m uvicorn kiosk_server:app --port 8001 --reload
```
* Access Kiosk Interface: **[http://localhost:8001](http://localhost:8001)**

#### Terminal 2: Doctor EHR Console (Port 8002)
```bash
python3 -m uvicorn doctor_server:app --port 8002 --reload
```
* Access Doctor Dashboard: **[http://localhost:8002](http://localhost:8002)**
  
---

## 📁 Repository Structure

```
PrashnaAyur/
├── kiosk_server.py            # FastAPI patient kiosk app & Gemini Live WebSocket gateway (Port 8001)
├── doctor_server.py           # FastAPI doctor dashboard, SSE queue & FHIR exporter (Port 8002)
├── hospital_ehr.db            # Local SQLite database (schema auto-created on launch)
├── requirements.txt           # Project Python dependencies
├── .env.example               # Environment variables template
├── .gitignore                 # Clean repository ignore configuration
│
├── utils/
│   ├── fhir_builder.py        # ABDM FHIR R4 Bundle generator from intake sessions
│   └── gemini_live_session.py # Gemini Live API configuration and session manager
│
├── static/
│   ├── kiosk/                 # Kiosk frontend assets
│   │   ├── kiosk.html         # Multilingual patient voice terminal UI
│   │   ├── kiosk.css          # Kiosk responsive styling
│   │   ├── kiosk.js           # Full-duplex WebSocket & session state logic
│   │   └── pcm-worklet.js     # Web Audio API 24kHz raw PCM recorder/processor
│   └── doctor/                # Doctor EHR dashboard assets
│       ├── doctor.html        # Doctor consultation & queue management console
│       ├── doctor.css         # Clinical EHR layout & design
│       └── doctor.js          # SSE listener, queue manager & prescription writer
│
├── scripts/
│   ├── reset_demo_data.py     # Database reset and baseline demo seeder
│   ├── verify_directive_full.py # Integration test suite for intake flow
│   └── verify_final_pass.py   # Final end-to-end verification script
│
└── uploaded_records/          # User-uploaded prescription & report scans
    └── .gitkeep               # Directory anchor
```

---

## 📄 License & Disclaimer

This project was built for the **Smart India Hackathon 2026**.  
*Disclaimer: All ABDM endpoints, ABHA token verifications, and clinical recommendations in this repository are demo-grade prototypes designed for hackathon evaluation and must undergo clinical certification before real-world patient deployment.*
