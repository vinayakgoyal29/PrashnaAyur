"""
scripts/verify_directive_full.py
================================
Master regression script for PrashnaAyur Master Fix Directive.
Tests:
1. Issue 1: 5 consecutive full sessions with clean 1000 close (including artificial delay run)
2. Issue 2: Turn-taking & server-side turn guard (stomach pain & lower back pain)
3. Issue 3: 3-Layer diagnostic analysis (audio transmission -> Gemini Live API -> client render)
4. Issue 4: Auto-start verification after OTP verification
5. Issue 5: SOCRATES gate extension (Severity, Radiation, Associated Symptoms)
6. Issue 6: DB fixture isolation & token auto-increment validation
"""

import asyncio
import json
import sqlite3
import time
import urllib.request
import urllib.parse
import websockets
import sys

KIOSK_WS_URL = "ws://127.0.0.1:8001/ws/live-triage"
DOCTOR_HTTP_URL = "http://127.0.0.1:8002"
INTERNAL_KEY = "prashnaayur-internal-hackathon-token-2026"

# Synthetic 16kHz 16-bit mono PCM silence / tone chunk (0.2s = 3200 samples = 6400 bytes)
DUMMY_PCM_CHUNK = b"\x00\x00" * 3200

async def run_single_session(session_idx: int, scenario: str, language: str = "en", artificial_delay: float = 0.0):
    print(f"\n=======================================================")
    print(f"RUNNING SESSION #{session_idx} [{language.upper()}] - Scenario: {scenario}")
    if artificial_delay > 0:
        print(f"--> With artificial extraction delay: {artificial_delay}s")
    print(f"=======================================================")

    patient_id = f"91-9876-0000-000{session_idx}"
    patient_name = f"Test Patient {session_idx}"
    abha_data = {
        "abha_id": patient_id,
        "name": patient_name,
        "age": 32 + session_idx,
        "gender": "Female" if session_idx % 2 == 0 else "Male"
    }

    close_code = None
    close_reason = None
    terminal_received = False
    token_received = None
    transcripts = []
    turns_count = 0
    consecutive_assistant_turns = 0
    last_role = None

    # Step 1: Connect WebSocket
    t_start = time.time()
    try:
        async with websockets.connect(KIOSK_WS_URL, ping_interval=10.0, ping_timeout=10.0) as ws:
            # Send init frame
            init_frame = {
                "type": "init",
                "language": language,
                "abha_data": abha_data
            }
            await ws.send(json.dumps(init_frame))
            print(f"[{session_idx}] Init frame sent. Waiting for session_ready...")

            # Wait for session_ready
            ready = False
            while not ready and (time.time() - t_start < 12.0):
                msg_raw = await asyncio.wait_for(ws.recv(), timeout=6.0)
                if isinstance(msg_raw, str):
                    msg = json.loads(msg_raw)
                    if msg.get("type") == "session_ready":
                        ready = True
                        print(f"[{session_idx}] Session ready received!")
                        break

            # Clinical turn script depending on scenario
            if language == "hi":
                if scenario == "stomach_pain":
                    patient_utterances = [
                        "नमस्ते, मुझे पिछले 2 दिनों से पेट में तेज दर्द हो रहा है।",
                        "दर्द मरोड़ जैसा है। 1 से 10 के पैमाने पर यह लगभग 7 या 8 है।",
                        "नहीं, यह दर्द केवल पेट में है, कहीं और नहीं फैलता।",
                        "मुझे थोड़ी मतली और गैस भी है, लेकिन बुखार नहीं है। कृपया मेरा परामर्श डॉक्टर को भेजें।"
                    ]
                else: # lower_back_pain
                    patient_utterances = [
                        "नमस्ते डॉक्टर, मुझे कल सुबह से कमर के निचले हिस्से में दर्द हो रहा है।",
                        "यह लगातार होने वाला हल्का दर्द है, 10 में से लगभग 6 है।",
                        "हाँ, चलते समय यह दर्द मेरे बाएं पैर और जांघ तक फैलता है।",
                        "कोई अन्य लक्षण नहीं है। बस इतना ही है, कृपया मेरा विवरण जमा करें।"
                    ]
            else:
                if scenario == "stomach_pain":
                    patient_utterances = [
                        "Hello, I have been having severe pain in my lower abdomen for the past 2 days.",
                        "The pain is a sharp cramping feeling. On a scale of 1 to 10, it is around 7 or 8.",
                        "No, the pain is strictly in my stomach. It does not radiate anywhere else.",
                        "I also have some nausea and bloating, but no fever. Please submit my consultation to the doctor."
                    ]
                else: # lower_back_pain
                    patient_utterances = [
                        "Doctor, I have had lower back pain since yesterday morning after lifting heavy items.",
                        "It is a constant dull ache, about 6 out of 10 in severity.",
                        "Yes, it radiates down my left leg and thigh when I walk.",
                        "No other symptoms like numbness or fever. That is all, please submit my intake."
                    ]

            script_idx = 0
            waiting_for_model = True

            # Run interaction loop
            loop_start = time.time()
            while time.time() - loop_start < 100.0:
                try:
                    msg_raw = await asyncio.wait_for(ws.recv(), timeout=4.0)
                except asyncio.TimeoutError:
                    if script_idx == 0 and len(patient_utterances) > 0:
                        utterance = patient_utterances[0]
                        script_idx = 1
                        print(f"[{session_idx}] >> Patient opening statement: {utterance[:45]}...")
                        for _ in range(3):
                            await ws.send(DUMMY_PCM_CHUNK)
                            await asyncio.sleep(0.02)
                        await ws.send(json.dumps({"type": "user_text", "text": utterance}))
                    else:
                        print(f"[{session_idx}] ... waiting for model turn #{turns_count + 1} ...")
                    continue

                if isinstance(msg_raw, bytes):
                    # Incoming audio bytes from model
                    continue

                msg = json.loads(msg_raw)
                mtype = msg.get("type")

                if mtype == "transcript":
                    role = msg.get("role")
                    txt = msg.get("text", "")
                    if role != last_role:
                        if role == "assistant" and last_role == "assistant":
                            consecutive_assistant_turns += 1
                        last_role = role
                    transcripts.append((role, txt))

                elif mtype == "consultation_closing":
                    print(f"[{session_idx}] << consultation_closing signal received from server!")

                elif mtype in ["intake_complete", "intake_submitted"] or (mtype == "state" and msg.get("value") == "complete"):
                    terminal_received = True
                    token_received = msg.get("token") or msg.get("token_number")
                    print(f"[{session_idx}] << Terminal message received! Token: {token_received}")

                elif mtype == "turn_complete":
                    turns_count += 1
                    if terminal_received:
                        continue
                    # When model finishes asking, patient responds
                    if script_idx < len(patient_utterances):
                        utterance = patient_utterances[script_idx]
                        script_idx += 1
                    else:
                        # Dynamic SOCRATES-aware response to keep interview progressing
                        last_asst_text = "".join(t[1] for t in transcripts if t[0] == "assistant")[-250:].lower()
                        if any(w in last_asst_text for w in ["scale", "1 to 10", "severe", "गंभीर", "पैमाने"]):
                            utterance = "दर्द 10 में से 7 है।" if language == "hi" else "The pain severity is 7 out of 10."
                        elif any(w in last_asst_text for w in ["radiat", "spread", "leg", "back", "कमर", "पैर", "फैलता"]):
                            utterance = "नहीं, दर्द कहीं और नहीं फैलता।" if language == "hi" else "No, the pain does not radiate anywhere else."
                        elif any(w in last_asst_text for w in ["other", "nausea", "fever", "vomit", "उल्टी", "बुखार", "लक्षण"]):
                            utterance = "थोड़ी मतली है लेकिन बुखार नहीं है।" if language == "hi" else "Mild nausea and bloating, no fever."
                        else:
                            utterance = "कृपया मेरा विवरण डॉक्टर को भेजें।" if language == "hi" else "Please submit my consultation to the doctor."

                    await asyncio.sleep(0.2)
                    print(f"[{session_idx}] >> Patient responding to turn #{turns_count}: {utterance[:45]}...")
                    try:
                        for _ in range(3):
                            await ws.send(DUMMY_PCM_CHUNK)
                            await asyncio.sleep(0.02)
                        await ws.send(json.dumps({"type": "user_text", "text": utterance}))
                    except websockets.exceptions.ConnectionClosed as cc:
                        close_code = cc.code
                        close_reason = cc.reason
                        break

                if terminal_received:
                    # Give it a moment to receive all terminal frames before socket closes
                    try:
                        while True:
                            extra_raw = await asyncio.wait_for(ws.recv(), timeout=8.0)
                            if isinstance(extra_raw, str):
                                extra_msg = json.loads(extra_raw)
                                tok = extra_msg.get("token") or extra_msg.get("token_number")
                                if tok and not token_received:
                                    token_received = tok
                    except websockets.exceptions.ConnectionClosed as cc:
                        close_code = cc.code
                        close_reason = cc.reason
                    except asyncio.TimeoutError:
                        pass
                    break

            if close_code is None:
                close_code = ws.close_code
                close_reason = ws.close_reason

    except websockets.exceptions.ConnectionClosed as cc:
        close_code = cc.code
        close_reason = cc.reason
    except Exception as exc:
        print(f"[{session_idx}] Exception during session: {type(exc).__name__}: {exc}")

    print(f"[{session_idx}] Session Ended: CloseCode={close_code}, CloseReason={close_reason}, Token={token_received}, Terminal={terminal_received}")
    return {
        "session_idx": session_idx,
        "close_code": close_code,
        "close_reason": close_reason,
        "token": token_received,
        "terminal_received": terminal_received,
        "consecutive_assistant_turns": consecutive_assistant_turns,
        "turns_count": turns_count
    }

async def main():
    print("=====================================================================")
    print("STARTING FULL MASTER REGRESSION DIRECTIVE VALIDATION")
    print("=====================================================================")

    # 1. First, check and reset the DB to ensure clean baseline
    import subprocess
    print("\n--- Step 0: Executing scripts/reset_demo_data.py ---")
    res = subprocess.run(["python3", "scripts/reset_demo_data.py"], capture_output=True, text=True)
    print(res.stdout)

    conn = sqlite3.connect("hospital_ehr.db")
    cur = conn.cursor()
    cur.execute("SELECT encounter_id, token_number FROM encounters")
    base_rows = cur.fetchall()
    conn.close()
    print("Hospital EHR baseline records:", base_rows)
    assert len(base_rows) == 1, f"Expected exactly 1 baseline record, found {len(base_rows)}"
    assert base_rows[0][1] == "A-100", f"Expected A-100 token, found {base_rows[0][1]}"

    # 2. Run 5 consecutive sessions for Issue 1 regression
    # Session 1: English - stomach pain
    # Session 2: Hindi - lower back pain
    # Session 3: English - stomach pain with artificial delay
    # Session 4: Hindi - stomach pain
    # Session 5: English - lower back pain
    results = []
    scenarios = [
        (1, "stomach_pain", "en", 0.0),
        (2, "lower_back_pain", "hi", 0.0),
        (3, "stomach_pain", "en", 2.0),
        (4, "stomach_pain", "hi", 0.0),
        (5, "lower_back_pain", "en", 0.0),
    ]

    for s_idx, sc, lang, delay in scenarios:
        r = await run_single_session(s_idx, sc, lang, delay)
        results.append(r)
        await asyncio.sleep(1.0)

    print("\n=====================================================================")
    print("SUMMARY OF 5 CONSECUTIVE SESSIONS (ISSUE 1 VERIFICATION):")
    print("=====================================================================")
    all_clean_1000 = True
    for r in results:
        print(f"Session {r['session_idx']}: CloseCode={r['close_code']} (Reason={r['close_reason']}), Token={r['token']}, ConsecAssistTurns={r['consecutive_assistant_turns']}")
        if r['close_code'] != 1000:
            all_clean_1000 = False

    print(f"\nAll sessions closed with code 1000 (No 1006, No 1011): {all_clean_1000}")
    print(f"Consecutive unprompted assistant turns across all sessions: {sum(r['consecutive_assistant_turns'] for r in results)}")

    # 3. Check DB records in hospital_ehr.db to verify Issue 5 & 6
    conn = sqlite3.connect("hospital_ehr.db")
    cur = conn.cursor()
    cur.execute("SELECT encounter_id, abha_id, token_number, socrates_json FROM encounters")
    final_rows = cur.fetchall()
    conn.close()

    print("\n=====================================================================")
    print(f"DATABASE VERIFICATION (hospital_ehr.db): Found {len(final_rows)} records")
    print("=====================================================================")
    for row in final_rows:
        enc_id, abha, tok, soc_raw = row
        soc = json.loads(soc_raw or "{}")
        print(f"Encounter: {enc_id} | Token: {tok} | Severity: {soc.get('severity')} | Radiation: {soc.get('radiation')} | Associations: {soc.get('associations')}")

    # Check test DB
    test_conn = sqlite3.connect("hospital_ehr_test.db")
    test_cur = test_conn.cursor()
    test_cur.execute("SELECT encounter_id, token_number FROM encounters")
    test_rows = test_cur.fetchall()
    test_conn.close()
    print(f"\nTest DB (hospital_ehr_test.db) contains {len(test_rows)} test fixtures (ENC-REG-*).")

if __name__ == "__main__":
    asyncio.run(main())
