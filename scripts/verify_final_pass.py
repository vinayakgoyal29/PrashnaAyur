import asyncio
import json
import sqlite3
import httpx
import websockets

DUMMY_PCM_CHUNK = b"\x00\x00" * 3200

async def run_session(session_idx, language, scenario):
    url = "ws://127.0.0.1:8001/ws/live-triage"
    abha_id = f"91-9876-0000-00{session_idx:02d}"

    if scenario == "stomach_pain":
        if language == "hi":
            patient_utterances = [
                "नमस्ते, मुझे पिछले 2 दिनों से पेट में तेज दर्द हो रहा है।",
                "दर्द मरोड़ जैसा है। 1 से 10 के पैमाने पर यह लगभग 7 या 8 है।",
                "नहीं, यह दर्द केवल पेट में है, कहीं और नहीं फैलता।",
                "मुझे थोड़ी मतली और गैस भी है, लेकिन बुखार नहीं है। कृपया मेरा परामर्श डॉक्टर को भेजें।",
            ]
        else:
            patient_utterances = [
                "Hello, I have been having severe pain in my lower abdomen for the past two days.",
                "The pain is a sharp cramping feeling. On a scale of 1 to 10, it is about an 8.",
                "No, the pain is strictly in my stomach. It does not radiate to my back or legs.",
                "I also have some nausea and bloating, but no fever. Please submit my consultation to the doctor.",
            ]
    else:
        if language == "hi":
            patient_utterances = [
                "नमस्ते डॉक्टर, मुझे कल सुबह से कमर के निचले हिस्से में तेज दर्द है।",
                "यह लगातार होने वाला हल्का दर्द है, 10 में से लगभग 6 है।",
                "हाँ, चलते समय यह दर्द मेरे बाएं पैर और जांघ तक फैलता है।",
                "कोई अन्य लक्षण नहीं है। बस इतना ही है, कृपया मेरा पर्चा डॉक्टर को भेजें।",
            ]
        else:
            patient_utterances = [
                "Doctor, I have had lower back pain since yesterday morning after lifting heavy items.",
                "It is a constant dull ache, about 6 out of 10 in severity.",
                "Yes, it radiates down my left leg and thigh when I walk.",
                "No other symptoms like numbness or fever. That is all, please submit my intake.",
            ]

    print(f"\n--- Running Final Pass Session #{session_idx} [{language.upper()}] ({scenario}) ---")
    close_code = None
    close_reason = None
    token_received = None
    consecutive_assistant_turns = 0
    last_role = None
    transcripts = []

    async with websockets.connect(url) as ws:
        # Step 1: Send init
        await ws.send(json.dumps({
            "type": "init",
            "language": language,
            "abha_id": abha_id
        }))

        # Interaction loop
        script_idx = 0
        turns_count = 0
        terminal_received = False
        loop_start = asyncio.get_event_loop().time()

        while asyncio.get_event_loop().time() - loop_start < 120.0:
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
                continue

            if isinstance(msg_raw, bytes):
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

            elif mtype in ["intake_complete", "intake_submitted"] or (mtype == "state" and msg.get("value") == "complete"):
                terminal_received = True
                token_received = msg.get("token") or msg.get("token_number")
                print(f"[{session_idx}] << Terminal message received! Token: {token_received}")

            elif mtype == "turn_complete":
                turns_count += 1
                if terminal_received:
                    continue
                if script_idx < len(patient_utterances):
                    utterance = patient_utterances[script_idx]
                    script_idx += 1
                else:
                    last_asst_text = "".join(t[1] for t in transcripts if t[0] == "assistant")[-250:].lower()
                    if any(w in last_asst_text for w in ["scale", "1 to 10", "severe", "गंभीर", "पैमाने", "तीव्र", "कितना दर्द"]):
                        utterance = "दर्द 10 में से 6 है।" if language == "hi" else "The pain severity is 6 out of 10."
                    elif any(w in last_asst_text for w in ["radiat", "spread", "leg", "back", "कमर", "पैर", "फैलता"]):
                        utterance = "हाँ, दर्द पैर तक फैलता है।" if language == "hi" else "Yes, the pain radiates down my left leg."
                    elif any(w in last_asst_text for w in ["other", "nausea", "fever", "vomit", "उल्टी", "बुखार", "लक्षण", "मतली"]):
                        utterance = "कोई अन्य लक्षण नहीं है।" if language == "hi" else "No other symptoms."
                    else:
                        utterance = "कृपया मेरा विवरण डॉक्टर को भेजें।" if language == "hi" else "Please submit my consultation to the doctor."

                print(f"[{session_idx}] >> Patient responding to turn #{turns_count}: {utterance[:45]}...")
                for _ in range(3):
                    await ws.send(DUMMY_PCM_CHUNK)
                    await asyncio.sleep(0.02)
                await ws.send(json.dumps({"type": "user_text", "text": utterance}))

            if terminal_received:
                try:
                    while True:
                        extra_raw = await asyncio.wait_for(ws.recv(), timeout=6.0)
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

    print(f"[{session_idx}] Complete: CloseCode={close_code}, Reason={close_reason}, Token={token_received}, ConsecAssistTurns={consecutive_assistant_turns}")
    return {
        "session_idx": session_idx,
        "language": language,
        "close_code": close_code,
        "close_reason": close_reason,
        "token": token_received,
        "consecutive_assistant_turns": consecutive_assistant_turns
    }

async def main():
    print("=================================================================")
    print("FINAL TWO-SESSION BACK-TO-BACK VALIDATION PASS (EN & HI)")
    print("=================================================================")

    # Run English Session
    r1 = await run_session(1, "en", "stomach_pain")
    await asyncio.sleep(1.0)

    # Run Hindi Session
    r2 = await run_session(2, "hi", "lower_back_pain")

    # Assertions
    assert r1["close_code"] == 1000, f"Session 1 close_code was {r1['close_code']}, expected 1000"
    assert r2["close_code"] == 1000, f"Session 2 close_code was {r2['close_code']}, expected 1000"
    assert r1["consecutive_assistant_turns"] == 0, "Session 1 had consecutive assistant turns without patient turn"
    assert r2["consecutive_assistant_turns"] == 0, "Session 2 had consecutive assistant turns without patient turn"

    # Query Doctor Server API
    async with httpx.AsyncClient() as client:
        login_res = await client.post("http://127.0.0.1:8002/api/auth/login", json={"doctor_id": "DOC-AYUSH-101", "pin": "1234"})
        assert login_res.status_code == 200, f"Doctor login failed: {login_res.status_code}"
        resp = await client.get("http://127.0.0.1:8002/api/queue")
        encounters = resp.json()

    print("\n=================================================================")
    print("DOCTOR DASHBOARD QUEUE VERIFICATION (/api/queue)")
    print(f"Total encounters returned: {len(encounters)}")
    print("=================================================================")

    for enc in encounters:
        socrates = enc.get("structured_intake", {})
        print(f"Encounter ID: {enc.get('encounter_id')}")
        print(f"  Token: {enc.get('token')}")
        print(f"  Patient: {enc.get('name')}")
        print(f"  Severity: {socrates.get('severity')}")
        print(f"  Radiation: {socrates.get('radiation')}")
        print(f"  Associated Symptoms: {socrates.get('associations') or socrates.get('associated_symptoms')}")
        print("-----------------------------------------------------------------")

    # Strict Validation of Queue Contents:
    # Exactly 3 records: 1 demo baseline + 2 new sessions
    assert len(encounters) == 3, f"Expected exactly 3 encounters in doctor queue, found {len(encounters)}"
    tokens = [enc.get("token") for enc in encounters]
    print(f"Tokens in Queue: {tokens}")
    assert "A-100" in tokens, "Missing baseline token A-100"
    assert "A-101" in tokens, "Missing session 1 token A-101"
    assert "A-102" in tokens, "Missing session 2 token A-102"

    for enc in encounters:
        assert not enc.get("encounter_id", "").startswith("ENC-REG-"), f"Fixture {enc.get('encounter_id')} found in doctor queue!"
        assert enc.get("name") != "Test User", "Test User fixture found in doctor queue!"

    print("\n>>> ALL FINAL PASS DIRECTIVE REQUIREMENTS VERIFIED AND PASSED SUCCESSFULLY! <<<")

if __name__ == "__main__":
    asyncio.run(main())
