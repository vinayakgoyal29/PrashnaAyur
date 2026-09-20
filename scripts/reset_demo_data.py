# scripts/reset_demo_data.py — run once manually: python scripts/reset_demo_data.py
import sqlite3
import json
import time

conn = sqlite3.connect("hospital_ehr.db")

# Ensure schema has token_number and transcript_json columns
for statement in [
    "ALTER TABLE encounters ADD COLUMN transcript_json TEXT",
    "ALTER TABLE encounters ADD COLUMN token_number TEXT",
    "ALTER TABLE encounters ADD COLUMN token TEXT",
]:
    try:
        conn.execute(statement)
        conn.commit()
    except sqlite3.OperationalError:
        pass

# Ensure doctor_sessions table exists
conn.execute("""
    CREATE TABLE IF NOT EXISTS doctor_sessions (
        token TEXT PRIMARY KEY,
        doctor_id TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
""")

# Ensure uploaded_documents table exists
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

# Delete all mock/test records, specifically any row with name = 'Test User' or abha_id = 'UNKNOWN'
conn.execute("DELETE FROM encounters WHERE abha_id = 'UNKNOWN' OR encounter_id LIKE 'ENC-REG-%' OR token LIKE 'ENC-REG-%' OR abha_id IN (SELECT abha_id FROM patients WHERE name = 'Test User')")
conn.execute("DELETE FROM patients WHERE name = 'Test User' OR abha_id = 'UNKNOWN' OR abha_id LIKE '91-1001-%' OR abha_id LIKE '91-2002-%' OR abha_id LIKE '91-9999-%'")

# Clean tables to ensure exactly one baseline record exists
conn.execute("DELETE FROM encounters")
conn.execute("DELETE FROM patients")
conn.execute("DELETE FROM prescriptions")
conn.execute("DELETE FROM uploaded_documents")

now = time.time()

conn.execute(
    "INSERT INTO patients (abha_id, name, age, gender, registered_at) VALUES (?, ?, ?, ?, ?)",
    ("91-0000-0000-0001", "[DEMO] Ramesh Sharma", 52, "Male", now),
)
conn.execute(
    "INSERT INTO encounters (encounter_id, abha_id, timestamp, socrates_json, prakriti_json, "
    "urgency, uploaded_doc_paths, status, token_number, token, transcript_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
    (
        "ENC-DEMO-0001", "91-0000-0000-0001", now,
        json.dumps({
            "site": "Lower back",
            "onset": "3 days ago",
            "character": "Dull, aching",
            "radiation": "None",
            "associations": "Mild stiffness in the morning",
            "time_course": "Constant, slightly worse in the evening",
            "exacerbating_relieving_factors": "Worse on prolonged sitting, better with light movement",
            "severity": "4/10",
            "prakriti_notes": "Vata-Kapha tendencies reported",
            "chief_complaint": "Patient presents with dull aching lower back pain for 3 days.",
            "is_emergency": False,
        }),
        json.dumps({"prakriti_notes": "Vata-Kapha tendencies reported"}),
        "ROUTINE", json.dumps([]), "WAITING", "A-100", "A-100",
        json.dumps([
            {"role": "assistant", "text": "Good morning. Could you tell me where the discomfort is?"},
            {"role": "patient", "text": "It's in my lower back."},
        ]),
    ),
)

conn.commit()
conn.close()
print("Database reset: Exactly 1 demo record seeded ([DEMO] Ramesh Sharma, Token #A-100).")
