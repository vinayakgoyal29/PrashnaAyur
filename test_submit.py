import requests
import json
import io

url = "http://localhost:8002/api/intake/submit"

payload = {
    "abha_data": {
        "patient_id": "91-1234-5678-9012",
        "name": "Test User",
        "age": 30,
        "gender": "Male"
    },
    "structured_intake": {
        "severity": "MODERATE",
        "site": "Head",
        "onset": "Yesterday",
        "prakriti_notes": "Vata"
    },
    "transcript": [
        {"role": "patient", "text": "I have a headache"}
    ],
    "status": "COMPLETE"
}

files = [
    ("documents", ("test.pdf", io.BytesIO(b"Fake PDF content"), "application/pdf")),
    ("documents", ("test.png", io.BytesIO(b"Fake PNG content"), "image/png"))
]

headers = {
    "Authorization": "Bearer prashnaayur-internal-hackathon-token-2026"
}

response = requests.post(url, data={"intake_json": json.dumps(payload)}, files=files, headers=headers)
print(response.status_code)
print(response.json())
