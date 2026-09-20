/**
 * kiosk.js
 * =========
 * Main-thread orchestration for the PrashnaAyur patient intake kiosk.
 *
 * ARCHITECTURAL FIX ROUND 4:
 * 1. Single-Pipeline Audio Architecture:
 *    - Browser SpeechRecognition API is COMPLETELY REMOVED from the patient speech path.
 *    - Patient audio flows strictly one-way: Mic -> AudioWorklet (16kHz PCM) -> WebSocket -> Gemini Live.
 *    - Gemini Live's native input audio transcription is the SINGLE SOURCE OF TRUTH for "You:" bubbles.
 * 2. Dedicated Worklet-Based Ring Buffer Playback:
 *    - Assistant audio plays via a dedicated PcmPlaybackProcessor AudioWorklet (no AudioBufferSourceNode chains).
 *    - Resamples 24kHz Float32 to hardware sample rate on the fly.
 * 3. Authoritative Session State Machine:
 *    DISCONNECTED -> CONNECTING -> SESSION_READY ->
 *      (loop: LISTENING -> USER_SPEAKING -> PROCESSING -> ASSISTANT_SPEAKING -> LISTENING) ->
 *      COMPLETING -> COMPLETE
 *    Reachable error states: CONNECT_FAILED, SESSION_FAILED.
 *    Only CONNECT_FAILED and SESSION_FAILED render the System Unavailable screen.
 * 4. Clean Teardown & Resilient Retry:
 *    - Discards and closes AudioContext, AudioWorkletNodes, MediaStream tracks, and WebSockets cleanly.
 */

// ── Mock ABHA database (same schema as prior builds) ─────────────────────────
const MOCK_ABHA_DB = {
  "12345678901234": {
    abha_id: "12345678901234",
    name: "Rajesh Kumar",
    age: 45,
    chronic_conditions: [
      { condition: "Type 2 Diabetes", diagnosed_date: "2022-04-15", status: "Active" }
    ],
    past_medications: [
      { drug: "Metformin", dosage: "500mg", frequency: "Twice daily" }
    ]
  }
};

// ── DOM refs ──────────────────────────────────────────────────────────────────
let pages = {};

let abhaInput      = null;
let abhaError      = null;
let btnGenOtp      = null;
let otpDisplay     = null;
let otpInput       = null;
let otpError       = null;
let btnVerifyOtp   = null;
let btnResendOtp   = null;

// Upload DOM elements
let dropZone         = null;
let fileInput        = null;
let fileChips        = null;
let btnProceedTriage = null;
let btnSkipUpload    = null;
let pendingFiles     = [];
let currentEncounterId = null;

let patientBar     = null;
let redFlagBanner  = null;
let micBtn         = null;
let micLabelText   = null;
let micStatusLabel = null;
let transcriptEl   = null;
let btnNextPatient = null;
let btnRetryReset  = null;

// ── I18N DICTIONARY & LANGUAGE LOGIC ──────────────────────────────────────────
const I18N_STRINGS = {
  en: {
    subtitle: "AYUSH Hospital — Patient Intake Kiosk",
    patientVerification: "Patient Verification",
    abhaInstructions: "Please enter your 14-digit ABHA Health ID to begin.",
    abhaIdLabel: "ABHA ID",
    abhaPlaceholder: "12345678901234",
    abhaError: "Invalid ABHA ID. Please enter exactly 14 digits.",
    generateOtp: "Generate OTP",
    disclaimer: "This kiosk collects intake information for your doctor. No diagnosis is provided.",
    verifyIdentity: "Verify Your Identity",
    otpInstructions: "A demo one-time code has been generated (no SMS gateway configured):",
    demoOtpLabel: "Demo OTP — enter this code below:",
    enterOtpLabel: "Enter the 6-digit code",
    otpError: "Incorrect code. Please try again.",
    verifyBtn: "Verify",
    resendOtp: "Regenerate Code",
    uploadRecords: "Upload Previous Prescription or Report (Optional)",
    uploadInstructions: "Upload any past prescriptions, lab reports, or scans.",
    dropZoneText: "Drag & drop files here or browse (PDF, JPG, PNG)",
    browseFilesBtn: "Browse Files",
    uploadFormats: "Supported formats: PDF, PNG, JPG",
    proceedTriage: "Proceed to Voice Consultation",
    skipUploadBtn: "Skip & Proceed",
    micReady: "Ready. Tap the button to begin your consultation.",
    micListening: "Listening... speak naturally.",
    micSpeaking: "Assistant is speaking... you can speak to interrupt.",
    micUserSpeaking: "Listening to you...",
    micProcessing: "Doctor Vaidya is processing your symptoms...",
    micTapToTalk: "Tap to Talk",
    micListeningBtn: "Listening",
    micSpeakingBtn: "Assistant",
    micEmergencyBtn: "Emergency",
    micStartingBtn: "Connecting",
    micStartingStatus: "Connecting to assistant...",
    retrying: "Having trouble connecting — retrying...",
    retryConnection: "Retry",
    redFlagBanner: "MEDICAL EMERGENCY DETECTED — Please proceed to the Emergency Room immediately. Do not wait.",
    opdTokenHeader: "OPD Consultation Token",
    verifiedRecord: "Verified Patient Record Dispatched",
    tokenWaitInstructions: "Your clinical intake has been submitted to the doctor. Please proceed to Waiting Area 2 — AYUSH OPD (Room 104). The doctor will call your token number shortly.",
    nextPatient: "Next Patient",
    sysUnavailableHeader: "System Unavailable",
    sysUnavailableText: "The triage system is temporarily unavailable. Please see the front desk to complete your registration.",
    unableSubmitHeader: "Unable to Submit",
    unableSubmitText: "Your intake could not be submitted to the doctor at this time. Please see the front desk for assistance.",
    returnStart: "Return to Start",
  },
  hi: {
    subtitle: "आयुष अस्पताल — मरीज पंजीकरण कियोस्क",
    patientVerification: "रोगी सत्यापन",
    abhaInstructions: "आरंभ करने के लिए कृपया अपना 14-अंकीय आभा स्वास्थ्य आईडी दर्ज करें।",
    abhaIdLabel: "आभा आईडी (ABHA ID)",
    abhaPlaceholder: "12345678901234",
    abhaError: "अमान्य आभा आईडी। कृपया ठीक 14 अंक दर्ज करें।",
    generateOtp: "ओटीपी प्राप्त करें",
    disclaimer: "यह कियोस्क आपके डॉक्टर के लिए प्रारंभिक जानकारी एकत्र करता है। कोई निदान नहीं दिया जाता है।",
    verifyIdentity: "अपनी पहचान सत्यापित करें",
    otpInstructions: "एक डेमो वन-टाइम पासवर्ड (OTP) बनाया गया है:",
    demoOtpLabel: "डेमो ओटीपी — नीचे यह कोड दर्ज करें:",
    enterOtpLabel: "6-अंकीय कोड दर्ज करें",
    otpError: "गलत कोड। कृपया पुनः प्रयास करें।",
    verifyBtn: "सत्यापित करें",
    resendOtp: "नया कोड बनाएं",
    uploadRecords: "पूर्व चिकित्सा पर्ची या रिपोर्ट अपलोड करें (वैकल्पिक)",
    uploadInstructions: "कोई भी पिछले नुस्खे, जांच रिपोर्ट या स्कैन अपलोड करें।",
    dropZoneText: "फ़ाइल यहाँ खींचें या चुनें (PDF, JPG, PNG)",
    browseFilesBtn: "फ़ाइल चुनें",
    uploadFormats: "समर्थित प्रारूप: PDF, PNG, JPG",
    proceedTriage: "परामर्श शुरू करें",
    skipUploadBtn: "छोड़ें और आगे बढ़ें",
    micReady: "तैयार हैं। परामर्श शुरू करने के लिए बटन दबाएं।",
    micListening: "सुन रहे हैं... कृपया स्वाभाविक रूप से बोलें।",
    micSpeaking: "डॉक्टर बोल रहे हैं... आप कभी भी बोल सकते हैं।",
    micUserSpeaking: "आपकी बात सुनी जा रही है...",
    micProcessing: "डॉक्टर वैद्य आपके लक्षणों का विश्लेषण कर रहे हैं...",
    micTapToTalk: "बोलने के लिए दबाएं",
    micListeningBtn: "सुन रहे हैं",
    micSpeakingBtn: "डॉक्टर",
    micEmergencyBtn: "आपातकाल",
    micStartingBtn: "जुड़ रहे हैं",
    micStartingStatus: "डॉक्टर से संपर्क किया जा रहा है...",
    retrying: "कनेक्शन में समस्या — पुनः प्रयास किया जा रहा है...",
    retryConnection: "पुनः प्रयास करें",
    redFlagBanner: "आपातकालीन चिकित्सा स्थिति — कृपया तुरंत आपातकालीन कक्ष (ER) में जाएं। प्रतीक्षा न करें।",
    opdTokenHeader: "ओपीडी परामर्श टोकन",
    verifiedRecord: "सत्यापित रोगी रिकॉर्ड डॉक्टर को प्रेषित",
    tokenWaitInstructions: "आपकी प्रारंभिक जांच डॉक्टर को भेज दी गई है। कृपया प्रतीक्षा क्षेत्र 2 — आयुष ओपीडी (कमरा 104) में जाएं। डॉक्टर जल्द ही आपका टोकन नंबर बुलाएंगे।",
    nextPatient: "अगला मरीज",
    sysUnavailableHeader: "सिस्टम अनुपलब्ध है",
    sysUnavailableText: "ट्रायज प्रणाली अस्थायी रूप से अनुपलब्ध है। कृपया अपना पंजीकरण पूरा करने के लिए स्वागत कक्ष पर संपर्क करें।",
    unableSubmitHeader: "जमा करने में असमर्थ",
    unableSubmitText: "आपकी जानकारी इस समय डॉक्टर को नहीं भेजी जा सकी। कृपया सहायता के लिए स्वागत कक्ष पर जाएं।",
    returnStart: "प्रारंभ पर लौटें",
  }
};

// ── AUTHORITATIVE SESSION STATE MACHINE ENUM ──────────────────────────────────
export const SESSION_STATES = Object.freeze({
  DISCONNECTED:       "DISCONNECTED",
  CONNECTING:         "CONNECTING",
  SESSION_READY:      "SESSION_READY",
  LISTENING:          "LISTENING",
  USER_SPEAKING:      "USER_SPEAKING",
  PROCESSING:         "PROCESSING",
  ASSISTANT_SPEAKING: "ASSISTANT_SPEAKING",
  RED_FLAG:           "RED_FLAG",
  COMPLETING:         "COMPLETING",
  COMPLETE:           "COMPLETE",
  CONNECT_FAILED:     "CONNECT_FAILED",
  SESSION_FAILED:     "SESSION_FAILED",
});

let currentSessionState          = SESSION_STATES.DISCONNECTED;
let stateWatchdogTimer           = null;
let connectionTimeoutTimer       = null;
let connectionRetryCount         = 0;
const MAX_CONNECTION_RETRIES     = 3;
const CONNECTION_RETRY_DELAYS    = [2000, 4000, 6000];
let lastFailureReason            = "";

let currentLanguage              = "en";
try {
  currentLanguage = (typeof sessionStorage !== "undefined" && sessionStorage.getItem("kioskLang")) || "en";
} catch (_) {}
let currentAbhaData              = null;
let currentOtp                   = "";
let ws                           = null;
let isConsultationActive         = false;
let isSessionReady               = false;
let sessionStarted               = false;
let triageCompletedSuccessfully  = false;
let intakeCompleted              = false;
let isCompletedNormally          = false;

// ── SINGLE AUDIO PIPELINE REFS (Dedicated AudioContext & Worklet Nodes) ──────
let audioContext             = null;
let playbackContext          = null;
let captureWorkletNode       = null;
let playbackWorkletNode      = null;
let micStream                = null;
let nextPlayTime             = 0;
let playbackQueueCount       = 0;
let serverTurnComplete       = false;
let isAssistantSpeaking      = false;
let activePlaybackSources    = [];

// Diagnostics counters
let diagChunksRx             = 0;
let diagChunksTx             = 0;
let diagQueueDepth           = 0;

// Active turn transcript state & single in-progress bubble per turn (Directives 1 & 4)
let currentTurnNumber        = 1;
let turnTranscriptEventsCount = 0;
let currentPatientBubbleEl   = null;
let currentPatientText       = "";
let currentAssistantBubbleEl = null;
let currentAssistantText     = "";
let isClosingSession         = false;
let transcriptHistory        = []; // in-memory turn array (Issue 3)
let isCurrentPatientTurnCommitted = false; // Issue 2c deduplication flag
let isCurrentAssistantTurnCommitted = false; // Issue 2a turn freeze flag
let committedTurnIds         = new Set(); // Issue 2c set of committed turn IDs

// ─────────────────────────────────────────────────────────────────────────────
// DIAGNOSTIC OVERLAY UPDATER
// ─────────────────────────────────────────────────────────────────────────────
function updateDiagnostics() {
  const elState = document.getElementById("diag-state");
  const elCtxState = document.getElementById("diag-ctx-state");
  const elRate = document.getElementById("diag-sample-rate");
  const elRx = document.getElementById("diag-chunks-rx");
  const elTx = document.getElementById("diag-chunks-tx");
  const elQueue = document.getElementById("diag-queue-depth");
  const elTurn = document.getElementById("diag-turn");

  if (elState) elState.textContent = currentSessionState;
  if (elCtxState) elCtxState.textContent = audioContext ? audioContext.state : "none";
  if (elRate) elRate.textContent = audioContext ? audioContext.sampleRate : "0";
  if (elRx) elRx.textContent = diagChunksRx;
  if (elTx) elTx.textContent = diagChunksTx;
  if (elQueue) elQueue.textContent = diagQueueDepth;
  if (elTurn) elTurn.textContent = currentTurnNumber;
}

window.toggleKioskDebug = () => {
  const overlay = document.getElementById("audio-diagnostic-overlay");
  if (overlay) {
    overlay.style.display = (overlay.style.display === "none" || !overlay.style.display) ? "block" : "none";
  }
};

// Check query param for initial debug overlay
if (typeof window !== "undefined") {
  const params = new URLSearchParams(window.location.search);
  if (params.get("debug") === "1") {
    setTimeout(() => {
      const overlay = document.getElementById("audio-diagnostic-overlay");
      if (overlay) overlay.style.display = "block";
    }, 200);
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// STATE MACHINE DRIVER (All UI transitions are driven strictly from here)
// ─────────────────────────────────────────────────────────────────────────────
function setSessionState(newState, meta = null) {
  // Issue 5: Lock state machine once consultation closing begins
  if (isClosingSession && (
    newState === SESSION_STATES.LISTENING ||
    newState === SESSION_STATES.USER_SPEAKING ||
    newState === SESSION_STATES.SESSION_READY ||
    newState === SESSION_STATES.ASSISTANT_SPEAKING
  )) {
    console.log(`[state-machine] Blocked transition to ${newState} because consultation is closing`);
    return;
  }
  console.log(`[state-machine] Transition: ${currentSessionState} -> ${newState}`, meta || "");
  currentSessionState = newState;
  const strings = I18N_STRINGS[currentLanguage] || I18N_STRINGS.en;

  const micB = document.getElementById("mic-btn") || micBtn;
  const micLText = document.getElementById("mic-label-text") || micLabelText;
  const micSLabel = document.getElementById("mic-status-label") || micStatusLabel;
  const rfBanner = document.getElementById("red-flag-banner") || redFlagBanner;

  // Clear previous watchdog timer on every transition
  if (stateWatchdogTimer) {
    clearTimeout(stateWatchdogTimer);
    stateWatchdogTimer = null;
  }

  // Reset visual class modifiers on mic button
  if (micB) {
    micB.classList.remove("state-idle", "state-listening", "state-speaking", "state-red-flag", "state-processing");
  }

  // Clear error reason container for active states
  if (newState !== SESSION_STATES.CONNECT_FAILED && newState !== SESSION_STATES.SESSION_FAILED) {
    const reasonEl = document.getElementById("sys-unavailable-reason");
    if (reasonEl) {
      reasonEl.style.display = "none";
      reasonEl.textContent = "";
    }
  }

  switch (newState) {
    case SESSION_STATES.DISCONNECTED:
      if (micB) {
        micB.classList.add("state-idle");
        micB.disabled = false;
      }
      if (micLText) micLText.textContent = strings.micTapToTalk;
      if (micSLabel) micSLabel.textContent = strings.micReady;
      break;

    case SESSION_STATES.CONNECTING:
      if (micB) micB.disabled = true;
      if (micLText) micLText.textContent = strings.micStartingBtn;
      if (micSLabel) {
        if (connectionRetryCount > 0) {
          micSLabel.textContent = `${strings.retrying} (${connectionRetryCount}/${MAX_CONNECTION_RETRIES})`;
        } else {
          micSLabel.textContent = strings.micStartingStatus;
        }
      }
      break;

    case SESSION_STATES.SESSION_READY:
      if (micB) {
        micB.classList.add("state-listening");
        micB.disabled = false;
      }
      if (micLText) micLText.textContent = strings.micListeningBtn;
      if (micSLabel) micSLabel.textContent = strings.micListening;
      break;

    case SESSION_STATES.LISTENING:
      if (micB) {
        micB.classList.add("state-listening");
        micB.disabled = false;
      }
      if (micLText) micLText.textContent = strings.micListeningBtn;
      if (micSLabel) micSLabel.textContent = strings.micListening;
      break;

    case SESSION_STATES.USER_SPEAKING:
      if (micB) {
        micB.classList.add("state-listening");
        micB.disabled = false;
      }
      if (micLText) micLText.textContent = strings.micListeningBtn;
      if (micSLabel) micSLabel.textContent = strings.micUserSpeaking;
      break;

    case SESSION_STATES.PROCESSING:
      if (micB) {
        micB.classList.add("state-idle");
        micB.disabled = true;
      }
      if (micLText) micLText.textContent = strings.micProcessing;
      if (micSLabel) micSLabel.textContent = strings.micProcessing;

      // Hard watchdog timeout (10s): if stuck in PROCESSING, force back to LISTENING
      stateWatchdogTimer = setTimeout(() => {
        if (currentSessionState === SESSION_STATES.PROCESSING) {
          console.warn("[watchdog] Timeout in PROCESSING (10s): forcing state back to LISTENING");
          setSessionState(SESSION_STATES.LISTENING);
        }
      }, 10000);
      break;

    case SESSION_STATES.ASSISTANT_SPEAKING:
      if (micB) {
        micB.classList.add("state-speaking");
        micB.disabled = false; // tap to interrupt (barge-in)
      }
      if (micLText) micLText.textContent = strings.micSpeakingBtn;
      if (micSLabel) micSLabel.textContent = strings.micSpeaking;

      // Hard watchdog timeout (10s): if assistant audio queue hangs, force back to LISTENING
      stateWatchdogTimer = setTimeout(() => {
        if (currentSessionState === SESSION_STATES.ASSISTANT_SPEAKING && diagQueueDepth === 0) {
          console.warn("[watchdog] Timeout in ASSISTANT_SPEAKING (10s): queue drained, forcing back to LISTENING");
          if (playbackWorkletNode) {
            try { playbackWorkletNode.port.postMessage({ command: "flush" }); } catch(_) {}
          }
          setSessionState(SESSION_STATES.LISTENING);
        }
      }, 10000);
      break;

    case SESSION_STATES.RED_FLAG:
      if (micB) {
        micB.classList.add("state-red-flag");
        micB.disabled = true;
      }
      if (micLText) micLText.textContent = strings.micEmergencyBtn;
      if (micSLabel) micSLabel.textContent = "";
      if (rfBanner) rfBanner.classList.add("visible");
      if (playbackWorkletNode) {
        try { playbackWorkletNode.port.postMessage({ command: "flush" }); } catch(_) {}
      }
      break;

    case SESSION_STATES.COMPLETING:
      if (micB) micB.disabled = true;
      if (micSLabel) micSLabel.textContent = "Submitting clinical intake...";
      break;

    case SESSION_STATES.COMPLETE:
      if (micB) micB.disabled = true;
      showTokenSuccessCard(meta);
      break;

    // The ONLY two states that render System Unavailable screen:
    case SESSION_STATES.CONNECT_FAILED:
    case SESSION_STATES.SESSION_FAILED:
      if (micB) micB.disabled = true;
      const reasonEl = document.getElementById("sys-unavailable-reason");
      if (reasonEl) {
        const displayReason = meta?.reason || lastFailureReason || "Could not reach the triage server (connection refused/closed).";
        reasonEl.textContent = `Diagnostic info: ${displayReason}`;
        reasonEl.style.display = "block";
      }
      showPage("unavailable");
      break;
  }

  updateDiagnostics();
}

// ─────────────────────────────────────────────────────────────────────────────
// WORKLET-BASED AUDIO PIPELINE REWRITE (Capture & Playback Ring Buffer)
// ─────────────────────────────────────────────────────────────────────────────
async function setupAudioPipeline() {
  await teardownAudioPipeline();

  const AudioCtxClass = window.AudioContext || window.webkitAudioContext;
  if (!AudioCtxClass) {
    throw new Error("Web Audio API is not supported in this browser.");
  }

  // Create single AudioContext for both capture and playback at hardware rate
  audioContext = new AudioCtxClass();

  // Attempt to resume on user interaction to satisfy autoplay policies
  if (audioContext.state === "suspended") {
    audioContext.resume().catch(() => {});
    const resumeOnGesture = () => {
      if (audioContext && audioContext.state === "suspended") {
        audioContext.resume().then(() => {
          console.log("[audio] AudioContext resumed on user gesture");
          updateDiagnostics();
        }).catch(() => {});
      }
    };
    window.addEventListener("click", resumeOnGesture, { once: true });
    window.addEventListener("keydown", resumeOnGesture, { once: true });
  }

  console.log(`[audio] AudioContext initialized in state '${audioContext.state}' at ${audioContext.sampleRate}Hz`);

  // Load the worklet file defining both PcmCaptureProcessor and PcmPlaybackProcessor
  await audioContext.audioWorklet.addModule("/static/kiosk/pcm-worklet.js");

  // 1. Microphone capture setup (reuse micStream if already acquired on user gesture)
  if (!micStream || !micStream.active) {
    micStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
      video: false,
    });
  }

  const micSource = audioContext.createMediaStreamSource(micStream);
  captureWorkletNode = new AudioWorkletNode(audioContext, "pcm-capture-processor");

  captureWorkletNode.port.onmessage = (e) => {
    // Half-duplex gating: do NOT transmit user audio while assistant is actively speaking
    if (isAssistantSpeaking || currentSessionState === SESSION_STATES.ASSISTANT_SPEAKING) {
      return;
    }
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(e.data); // Zero-copy Transferable ArrayBuffer (16kHz PCM)
      diagChunksTx++;
      if (diagChunksTx % 50 === 1) {
        console.log(`[audio-diag] Layer 1 OK: Sent PCM chunk #${diagChunksTx} to server (${e.data.byteLength} bytes)`);
      }
      updateDiagnostics();
    }
  };

  micSource.connect(captureWorkletNode);

  // 2. Playback worklet ring-buffer setup
  playbackWorkletNode = new AudioWorkletNode(audioContext, "pcm-playback-processor");
  playbackWorkletNode.connect(audioContext.destination);

  playbackWorkletNode.port.onmessage = (e) => {
    const data = e.data;
    if (!data) return;

    if (data.type === "playback_started") {
      setSessionState(SESSION_STATES.ASSISTANT_SPEAKING);
    } else if (data.type === "drained") {
      // Small 150ms guard to ensure speaker decay doesn't trip mic
      setTimeout(() => {
        if (currentSessionState === SESSION_STATES.ASSISTANT_SPEAKING) {
          setSessionState(SESSION_STATES.LISTENING);
        }
      }, 150);
    } else if (data.type === "diagnostic") {
      diagQueueDepth = data.queueLength || 0;
      updateDiagnostics();
    }
  };

  updateDiagnostics();
}

async function teardownAudioPipeline() {
  if (captureWorkletNode) {
    try { captureWorkletNode.disconnect(); } catch (_) {}
    captureWorkletNode = null;
  }
  if (playbackWorkletNode) {
    try {
      playbackWorkletNode.port.postMessage({ command: "flush" });
      playbackWorkletNode.disconnect();
    } catch (_) {}
    playbackWorkletNode = null;
  }
  if (micStream) {
    try {
      micStream.getTracks().forEach(track => track.stop());
    } catch (_) {}
    micStream = null;
  }
  if (audioContext) {
    try {
      if (audioContext.state !== "closed") {
        await audioContext.close();
      }
    } catch (_) {}
    audioContext = null;
  }
  flushPlaybackAudio();
  if (playbackContext && playbackContext !== audioContext) {
    try {
      if (playbackContext.state !== "closed") {
        await playbackContext.close();
      }
    } catch (_) {}
    playbackContext = null;
  }
  diagChunksRx = 0;
  diagChunksTx = 0;
  diagQueueDepth = 0;
  updateDiagnostics();
}

// ─────────────────────────────────────────────────────────────────────────────
// GAPLESS PCM AUDIO PLAYBACK & AUTO-PACING (Directive Sections 1 & 2)
// ─────────────────────────────────────────────────────────────────────────────
function setAssistantSpeaking(speaking) {
  isAssistantSpeaking = speaking;
  if (speaking) {
    if (currentSessionState !== SESSION_STATES.ASSISTANT_SPEAKING &&
        currentSessionState !== SESSION_STATES.COMPLETING &&
        currentSessionState !== SESSION_STATES.COMPLETE &&
        currentSessionState !== SESSION_STATES.RED_FLAG) {
      setSessionState(SESSION_STATES.ASSISTANT_SPEAKING);
    }
  } else {
    if (currentSessionState === SESSION_STATES.ASSISTANT_SPEAKING) {
      setSessionState(SESSION_STATES.LISTENING);
    }
  }
  updateDiagnostics();
}

function onServerListeningState() {
  if (isClosingSession || isCompletedNormally || intakeCompleted) return;
  serverTurnComplete = true;
  maybeUnmute();
}

function onPlaybackQueueDrained() {
  if (isClosingSession || isCompletedNormally || intakeCompleted) return;
  maybeUnmute();
}

function maybeUnmute() {
  if (isClosingSession || isCompletedNormally || intakeCompleted) return;
  if (serverTurnComplete && playbackQueueCount === 0) {
    setAssistantSpeaking(false);
    serverTurnComplete = false;
    if (currentSessionState !== SESSION_STATES.COMPLETE &&
        currentSessionState !== SESSION_STATES.COMPLETING &&
        currentSessionState !== SESSION_STATES.RED_FLAG) {
      setSessionState(SESSION_STATES.LISTENING);
    }
  }
}

function flushPlaybackAudio() {
  for (const src of activePlaybackSources) {
    try { src.stop(); src.disconnect(); } catch (_) {}
  }
  activePlaybackSources = [];
  playbackQueueCount = 0;
  serverTurnComplete = false;
  if (playbackContext) {
    nextPlayTime = playbackContext.currentTime;
  } else {
    nextPlayTime = 0;
  }
  updateDiagnostics();
}

function playAudioChunk(arrayBuffer) {
  if (!arrayBuffer || arrayBuffer.byteLength < 2) return;
  commitPatientBubble();
  diagChunksRx++;

  const AudioCtxClass = window.AudioContext || window.webkitAudioContext;
  if (!playbackContext) {
    playbackContext = audioContext || new AudioCtxClass({ sampleRate: 24000 });
  }
  if (playbackContext.state === "suspended") {
    playbackContext.resume().catch(() => {});
  }

  const alignedBytes = Math.floor(arrayBuffer.byteLength / 2) * 2;
  const int16 = new Int16Array(arrayBuffer, 0, alignedBytes / 2);
  const float32 = new Float32Array(int16.length);
  for (let i = 0; i < int16.length; i++) {
    float32[i] = int16[i] < 0 ? int16[i] / 32768.0 : int16[i] / 32767.0;
  }

  const audioBuffer = playbackContext.createBuffer(1, float32.length, 24000);
  audioBuffer.copyToChannel(float32, 0);

  const source = playbackContext.createBufferSource();
  source.buffer = audioBuffer;
  source.connect(playbackContext.destination);

  const startAt = Math.max(nextPlayTime, playbackContext.currentTime);
  source.start(startAt);
  nextPlayTime = startAt + audioBuffer.duration;

  playbackQueueCount++;
  activePlaybackSources.push(source);
  setAssistantSpeaking(true);

  source.onended = () => {
    const idx = activePlaybackSources.indexOf(source);
    if (idx !== -1) activePlaybackSources.splice(idx, 1);
    playbackQueueCount--;
    if (playbackQueueCount <= 0) {
      playbackQueueCount = 0;
      onPlaybackQueueDrained();
    }
    updateDiagnostics();
  };

  updateDiagnostics();
}

function handleIncomingAudioChunk(arrayBuffer) {
  playAudioChunk(arrayBuffer);
}

// ─────────────────────────────────────────────────────────────────────────────
// PRE-SESSION FAILURE & EXPONENTIAL RETRY LOGIC
// ─────────────────────────────────────────────────────────────────────────────
async function handlePreSessionFailure(reason, event = null) {
  lastFailureReason = reason;
  if (event && (event.code !== undefined || event.reason)) {
    lastFailureReason = `${reason} (code: ${event.code || 'N/A'}${event.reason ? ', ' + event.reason : ''})`;
  }
  console.warn(`[ws] Pre-session handshake failure: ${lastFailureReason}. Attempt ${connectionRetryCount}/${MAX_CONNECTION_RETRIES}`);
  if (connectionTimeoutTimer) {
    clearTimeout(connectionTimeoutTimer);
    connectionTimeoutTimer = null;
  }

  if (ws) {
    try { ws.close(); } catch (_) {}
    ws = null;
  }

  await teardownAudioPipeline();
  isConsultationActive = false;

  if (connectionRetryCount < MAX_CONNECTION_RETRIES) {
    const delay = CONNECTION_RETRY_DELAYS[connectionRetryCount] || 2000;
    connectionRetryCount++;
    console.log(`[ws] Scheduling retry #${connectionRetryCount}/${MAX_CONNECTION_RETRIES} in ${delay}ms...`);
    setSessionState(SESSION_STATES.CONNECTING);
    setTimeout(() => {
      if (!sessionStarted && !triageCompletedSuccessfully && !intakeCompleted) {
        startTriageSession();
      }
    }, delay);
  } else {
    console.error("[ws] Exhausted all pre-session connection retries. Routing to CONNECT_FAILED.");
    setSessionState(SESSION_STATES.CONNECT_FAILED, { reason: lastFailureReason });
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// TRIAGE SESSION LIFECYCLE (Single code path for first-time connect and retry)
// ─────────────────────────────────────────────────────────────────────────────
async function startTriageSession() {
  if (isConsultationActive) return;
  isConsultationActive = true;
  sessionStarted = false;
  triageCompletedSuccessfully = false;
  intakeCompleted = false;
  currentTurnNumber = 1;
  turnTranscriptEventsCount = 0;

  setSessionState(SESSION_STATES.CONNECTING);

  // Clear previous connection timeout
  if (connectionTimeoutTimer) {
    clearTimeout(connectionTimeoutTimer);
    connectionTimeoutTimer = null;
  }

  // Bounded 9s connection timeout waiting for Gemini Live session_ready
  connectionTimeoutTimer = setTimeout(() => {
    if (!sessionStarted && !triageCompletedSuccessfully && !intakeCompleted) {
      console.warn("[ws] Connection timeout reached (9s) waiting for session_ready signal");
      handlePreSessionFailure("Handshake timeout (9s waiting for server session_ready)");
    }
  }, 9000);

  // FIX 1: Construct WebSocket URL dynamically and log explicitly before new WebSocket
  const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  let host = window.location.host;
  if (!host || host === "" || window.location.protocol === "file:") {
    host = "127.0.0.1:8001";
  }
  const wsUrl = `${wsProtocol}//${host}/ws/live-triage`;
  console.log(`[ws] Step 1: Connecting WebSocket to URL: ${wsUrl}`);

  ws = new WebSocket(wsUrl);
  ws.binaryType = "arraybuffer";

  // Initialize audio pipeline non-fatally so WebSocket connect is never delayed or aborted
  setupAudioPipeline().catch((err) => {
    console.warn("[audio] Audio pipeline setup notice (will resume on user interaction):", err);
  });

  ws.addEventListener("open", () => {
    console.log("[ws] Step 2: Transport open; sending session init frame with ABHA and language lock...");
    try {
      ws.send(JSON.stringify({
        type: "init",
        encounter_id: currentEncounterId,
        abha_data: currentAbhaData,
        language: currentLanguage,
      }));
    } catch (sendErr) {
      console.error("[ws] Failed to send init frame:", sendErr);
    }
  });

  ws.addEventListener("message", (event) => {
    // 1. Binary PCM audio frame from Gemini assistant
    if (event.data instanceof ArrayBuffer) {
      handleIncomingAudioChunk(event.data);
      return;
    } else if (event.data instanceof Blob) {
      event.data.arrayBuffer().then(buf => handleIncomingAudioChunk(buf));
      return;
    }

    // 2. Structured JSON control / transcription frame
    if (typeof event.data === "string") {
      let msg;
      try {
        msg = JSON.parse(event.data);
        console.log("[ws-diag] Layer 3 message:", msg.type, msg.role || "", (msg.delta || msg.text || "").slice(0, 50));
      } catch (_) {
        return;
      }

      switch (msg.type) {
        case "session_ready":
          console.log("[ws] Step 3: Explicit 'session_ready' received from backend. Live session active!");
          if (connectionTimeoutTimer) {
            clearTimeout(connectionTimeoutTimer);
            connectionTimeoutTimer = null;
          }
          sessionStarted = true;
          connectionRetryCount = 0;
          setSessionState(SESSION_STATES.SESSION_READY);
          setTimeout(() => {
            if (currentSessionState === SESSION_STATES.SESSION_READY) {
              setSessionState(SESSION_STATES.LISTENING);
            }
          }, 300);
          break;

        case "transcript":
        case "input_audio_transcription":
        case "output_audio_transcription":
        case "input_transcription":
        case "output_transcription":
          handleTranscriptMessage(msg);
          break;

        case "consultation_closing":
          startClosingSession();
          break;

        case "turn_complete":
          console.log(`[ws] turn_complete received for Turn #${currentTurnNumber}`);
          commitPatientBubble();
          commitAssistantBubble();
          currentTurnNumber++;
          isCurrentPatientTurnCommitted = false;
          isCurrentAssistantTurnCommitted = false;
          turnTranscriptEventsCount = 0;
          updateDiagnostics();
          if (!isClosingSession && !isCompletedNormally && !intakeCompleted) {
            onServerListeningState();
          }
          break;

        case "interrupted":
          commitPatientBubble();
          commitAssistantBubble();
          isCurrentPatientTurnCommitted = false;
          isCurrentAssistantTurnCommitted = false;
          flushPlaybackAudio();
          setAssistantSpeaking(false);
          if (!isClosingSession && !isCompletedNormally && !intakeCompleted) {
            setSessionState(SESSION_STATES.LISTENING);
          }
          break;

        case "intake_complete":
        case "intake_submitted":
          isCompletedNormally = true;
          commitPatientBubble();
          commitAssistantBubble();
          showTokenSuccessCard(msg);
          break;

        case "session_error":
          console.error("[ws] Server reported session_error:", msg);
          if (!sessionStarted) {
            handlePreSessionFailure(`${msg.error_type}: ${msg.error_message}`);
          } else if (!triageCompletedSuccessfully && !intakeCompleted) {
            setSessionState(SESSION_STATES.SESSION_FAILED);
          }
          break;

        case "state":
          if (msg.value === "red_flag") {
            setSessionState(SESSION_STATES.RED_FLAG);
          } else if (msg.value === "complete") {
            isCompletedNormally = true;
            commitPatientBubble();
            showTokenSuccessCard(msg);
          } else if (msg.value === "listening") {
            onServerListeningState();
          }
          break;
      }
    }
  });

  ws.addEventListener("close", (e) => {
    console.log(`[ws] Socket closed: code=${e?.code}, reason=${e?.reason || 'none'}, wasClean=${e?.wasClean}`);
    if (connectionTimeoutTimer) {
      clearTimeout(connectionTimeoutTimer);
      connectionTimeoutTimer = null;
    }
    teardownAudioPipeline();
    isConsultationActive = false;

    // Directive Section 1.2:
    // If isCompletedNormally === true, return immediately — do not call showSystemUnavailable(), regardless of event.code.
    if (isCompletedNormally) {
      console.log(`[ws] Normal close after successful intake submission (code: ${e?.code})`);
      return;
    }

    // Suppress error screen on valid completion
    if (triageCompletedSuccessfully || intakeCompleted || currentSessionState === SESSION_STATES.COMPLETE) {
      console.log("[ws] Normal close after successful consultation completion.");
      return;
    }

    // If isCompletedNormally === false and event.code !== 1000, call showSystemUnavailable() as before
    if (!isCompletedNormally && e?.code !== 1000) {
      const disconnectReason = !sessionStarted
        ? "WebSocket closed before session ready"
        : `Unexpected mid-interview disconnect (code: ${e?.code || 'N/A'}, reason: ${e?.reason || 'none'})`;
      console.warn(`[ws] ${disconnectReason}`);
      showSystemUnavailable(disconnectReason);
      return;
    }

    console.log("[ws] Clean close handshake (code: 1000)");
  });

  ws.addEventListener("error", (e) => {
    console.error("[ws] Transport error event:", e);
    if (isCompletedNormally || triageCompletedSuccessfully || intakeCompleted || currentSessionState === SESSION_STATES.COMPLETE) {
      return;
    }
    if (!sessionStarted) {
      handlePreSessionFailure("WebSocket transport connection error (failed to connect to host/port)", e);
      return;
    }
    showSystemUnavailable("WebSocket transport error");
  });
}

// ─────────────────────────────────────────────────────────────────────────────
// FULL CLEAN RETRY ACTION (Used by "Retry" button on System Unavailable screen)
// ─────────────────────────────────────────────────────────────────────────────
async function handleRetryAction() {
  console.log("[retry] Performing clean teardown and fresh initialization...");
  connectionRetryCount = 0;
  lastFailureReason = "";

  const reasonEl = document.getElementById("sys-unavailable-reason");
  if (reasonEl) {
    reasonEl.style.display = "none";
    reasonEl.textContent = "";
  }

  if (connectionTimeoutTimer) {
    clearTimeout(connectionTimeoutTimer);
    connectionTimeoutTimer = null;
  }

  if (ws) {
    try { ws.close(); } catch (_) {}
    ws = null;
  }

  await teardownAudioPipeline();

  // Clear DOM transcript completely to avoid stale references (Issue 3)
  const txContainers = [
    document.getElementById("transcript-panel"),
    document.getElementById("transcript"),
    transcriptEl
  ];
  txContainers.forEach(container => {
    if (container) {
      while (container.firstChild) {
        container.removeChild(container.firstChild);
      }
      container.innerHTML = "";
    }
  });

  transcriptHistory = [];
  isCurrentPatientTurnCommitted = false;
  isCurrentAssistantTurnCommitted = false;
  committedTurnIds.clear();

  currentPatientBubbleEl = null;
  currentPatientText = "";
  currentAssistantBubbleEl = null;
  currentAssistantText = "";
  currentTurnNumber = 1;
  turnTranscriptEventsCount = 0;

  sessionStarted = false;
  triageCompletedSuccessfully = false;
  intakeCompleted = false;
  isCompletedNormally = false;

  showPage("triage");
  await startTriageSession();
}

// ─────────────────────────────────────────────────────────────────────────────
// SYSTEM UNAVAILABLE PRESENTATION HELPER
// ─────────────────────────────────────────────────────────────────────────────
function showSystemUnavailable(reason = "") {
  console.warn("[kiosk] Showing System Unavailable screen:", reason);
  setSessionState(SESSION_STATES.SESSION_FAILED, { reason });
}

// ─────────────────────────────────────────────────────────────────────────────
// CLIENT-SIDE TEARDOWN & TOKEN SUCCESS CARD (Directive Section 1.2)
// ─────────────────────────────────────────────────────────────────────────────
function showTokenSuccessCard(msg = {}) {
  try {
    // Directive 1: Ensure any in-progress transcript bubble is committed before tearing down
    commitPatientBubble();
    commitAssistantBubble();

    isClosingSession = true;
    isCompletedNormally = true;
    triageCompletedSuccessfully = true;
    intakeCompleted = true;
    isConsultationActive = false;

    // Hide loading card if displayed (Issue 5)
    const loadingCard = document.getElementById("intake-loading-card");
    if (loadingCard) {
      loadingCard.style.display = "none";
    }

    if (connectionTimeoutTimer) {
      clearTimeout(connectionTimeoutTimer);
      connectionTimeoutTimer = null;
    }

    // 1. Tear down the mic cleanly (stop the audio input stream / MediaStreamTrack.stop() on all tracks)
    if (micStream) {
      try {
        micStream.getTracks().forEach((track) => track.stop());
      } catch (_) {}
      micStream = null;
    }

    if (captureWorkletNode) {
      try { captureWorkletNode.disconnect(); } catch (_) {}
      captureWorkletNode = null;
    }
    if (playbackWorkletNode) {
      try { playbackWorkletNode.disconnect(); } catch (_) {}
      playbackWorkletNode = null;
    }
    if (audioContext && audioContext.state !== "closed") {
      try { audioContext.close(); } catch (_) {}
      audioContext = null;
    }

    // 2. Hide active triage container: document.getElementById('triage-active-container').classList.add('hidden')
    const triageActive = document.getElementById("triage-active-container");
    if (triageActive) {
      triageActive.classList.add("hidden");
      triageActive.style.display = "none";
    }
    const voiceTriageContainer = document.getElementById("voice-triage-container") || document.getElementById("mic-stage-container") || document.querySelector(".mic-stage");
    if (voiceTriageContainer) {
      voiceTriageContainer.classList.add("hidden");
      voiceTriageContainer.style.display = "none";
    }

    const micBtnEl = document.getElementById("mic-button") || document.getElementById("mic-btn") || micBtn;
    if (micBtnEl) {
      micBtnEl.classList.add("hidden");
      micBtnEl.style.display = "none";
    }

    // 3. Reveal the OPD Token Success Card: document.getElementById('intake-success-card').classList.remove('hidden')
    const card = document.getElementById("intake-success-card") || document.getElementById("token-success-card");
    if (card) {
      const dataPayload = msg.data || msg.payload || {};
      const patient_name = dataPayload.patient_name || dataPayload.name || msg.patient_name || msg.name || (currentAbhaData ? currentAbhaData.name : "Verified Patient");
      const abha_id = dataPayload.abha_id || msg.abha_id || (currentAbhaData ? currentAbhaData.abha_id : "UNKNOWN");
      
      const rawToken = msg.token || msg.token_number || dataPayload.token || dataPayload.token_number || "A-101";
      const token_number = String(rawToken).startsWith("Token #") ? rawToken : `Token #${String(rawToken).replace(/^OPD-/, "")}`;

      const nameEl = card.querySelector(".patient-name");
      if (nameEl) nameEl.textContent = patient_name;
      const abhaEl = card.querySelector(".abha-id");
      if (abhaEl) abhaEl.textContent = abha_id;
      const tokenEl = card.querySelector(".token-number") || card.querySelector(".token-value") || document.getElementById("token-display");
      if (tokenEl) tokenEl.textContent = token_number;

      card.classList.remove("hidden");
      card.style.display = "block";
    }

    setSessionState(SESSION_STATES.COMPLETE, msg);
    console.log("[kiosk] OPD Token Success Card displayed normally:", msg);
  } catch (e) {
    console.error("showTokenSuccessCard failed:", e);
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// TRANSCRIPT STREAMING & SINGLE-BUBBLE PER TURN (Directive Issues 1, 2 & 5)
// ─────────────────────────────────────────────────────────────────────────────
function startClosingSession() {
  if (isClosingSession) return;
  isClosingSession = true;
  console.log("[kiosk] Consultation closing detected. Permanently stopping mic & showing loading card (Issue 5).");

  // Permanently stop microphone
  if (micStream) {
    try {
      micStream.getTracks().forEach(track => {
        track.stop();
        track.enabled = false;
      });
    } catch (_) {}
    micStream = null;
  }
  if (captureWorkletNode) {
    try { captureWorkletNode.disconnect(); } catch (_) {}
    captureWorkletNode = null;
  }
  isMicMuted = true;
  isConsultationActive = false;

  // Hide active triage mic stage immediately
  const triageActive = document.getElementById("triage-active-container") || document.querySelector(".mic-stage");
  if (triageActive) {
    triageActive.classList.add("hidden");
    triageActive.style.display = "none";
  }
  const micBtnEl = document.getElementById("mic-btn") || document.getElementById("mic-button") || micBtn;
  if (micBtnEl) {
    micBtnEl.classList.add("hidden");
    micBtnEl.style.display = "none";
    micBtnEl.disabled = true;
  }

  // Show loading card immediately
  const loadingCard = document.getElementById("intake-loading-card");
  if (loadingCard) {
    loadingCard.classList.remove("hidden");
    loadingCard.style.display = "block";
  }
  const successCard = document.getElementById("intake-success-card");
  if (successCard) {
    successCard.classList.add("hidden");
    successCard.style.display = "none";
  }

  setSessionState(SESSION_STATES.COMPLETING);
}

function commitPatientBubble() {
  if (currentPatientBubbleEl) {
    const trimmed = (currentPatientText || "").trim();
    if (!trimmed) {
      // Eliminate ghost empty blue bubbles (Issue 2b)
      currentPatientBubbleEl.remove();
    } else {
      currentPatientBubbleEl.textContent = trimmed;
      if (!isCurrentPatientTurnCommitted) {
        transcriptHistory.push({ role: "patient", text: trimmed, turn: currentTurnNumber });
        isCurrentPatientTurnCommitted = true;
        committedTurnIds.add(`patient-${currentTurnNumber}`);
      }
      console.log(`[transcript] Patient bubble finalized (Turn #${currentTurnNumber}):`, trimmed);
    }
    currentPatientBubbleEl = null;
    currentPatientText = "";
  }
}

function commitAssistantBubble() {
  if (currentAssistantBubbleEl) {
    const trimmed = (currentAssistantText || "").trim();
    if (!trimmed) {
      currentAssistantBubbleEl.remove();
    } else {
      currentAssistantBubbleEl.textContent = trimmed;
      if (!isCurrentAssistantTurnCommitted) {
        transcriptHistory.push({ role: "assistant", text: trimmed, turn: currentTurnNumber });
        isCurrentAssistantTurnCommitted = true;
        committedTurnIds.add(`assistant-${currentTurnNumber}`);
      }
      console.log(`[transcript] Assistant bubble finalized (Turn #${currentTurnNumber}):`, trimmed);
    }
    currentAssistantBubbleEl = null;
    currentAssistantText = "";
  }
}

function handleTranscriptMessage(msg = {}) {
  console.log("[ws-transcript-raw]", JSON.stringify(msg));

  let role = msg.role;
  if (!role) {
    if (msg.type && msg.type.includes("input")) role = "patient";
    else if (msg.type && msg.type.includes("output")) role = "assistant";
    else role = "patient";
  }
  const normalizedRole = (role === "assistant" || role === "model") ? "assistant" : "patient";
  let textChunk = msg.text ?? msg.delta ?? msg.transcript ?? "";
  if (!textChunk && typeof msg.content === "string") textChunk = msg.content;
  if (!textChunk) return;

  // Strip stray Devanagari in English mode
  if (currentLanguage === "en" && /[\u0900-\u097F]/.test(textChunk)) {
    textChunk = textChunk.replace(/[\u0900-\u097F]+/g, "");
    if (!textChunk) return;
  }

  const isFinal = Boolean(msg.finished || msg.is_final || msg.final || msg.turn_complete);

  if (normalizedRole === "patient") {
    // Assistant speaker ended: freeze any active assistant bubble (Issue 2a)
    commitAssistantBubble();

    const panel = document.getElementById("transcript-panel") || document.getElementById("transcript") || transcriptEl;
    if (!panel) return;

    if (!isClosingSession) {
      setSessionState(SESSION_STATES.USER_SPEAKING);
    }

    if (isCurrentPatientTurnCommitted) {
      isCurrentPatientTurnCommitted = false;
      currentPatientBubbleEl = null;
      currentPatientText = "";
    }

    currentPatientText += textChunk;
    // Issue 2b: Do not create bubble until non-whitespace text is present
    if (!currentPatientText.trim()) return;

    if (!currentPatientBubbleEl || !panel.contains(currentPatientBubbleEl)) {
      currentPatientBubbleEl = document.createElement("div");
      currentPatientBubbleEl.className = "chat-bubble bubble-user self-end";
      panel.appendChild(currentPatientBubbleEl);
    }

    currentPatientBubbleEl.textContent = currentPatientText;
    panel.scrollTop = panel.scrollHeight;

    if (isFinal) {
      commitPatientBubble();
    }
  } else {
    // Assistant speaker started: commit/freeze patient bubble immediately (Issue 2a)
    commitPatientBubble();

    const panel = document.getElementById("transcript-panel") || document.getElementById("transcript") || transcriptEl;
    if (!panel) return;

    // Issue 2a: If previous assistant turn was already frozen, start brand new assistant bubble
    if (isCurrentAssistantTurnCommitted) {
      currentAssistantBubbleEl = null;
      currentAssistantText = "";
      isCurrentAssistantTurnCommitted = false;
    }

    // Issue 5: Detect closing phrase in assistant speech
    const lower = textChunk.toLowerCase();
    if (
      lower.includes("submitted to the doctor") ||
      lower.includes("take your token") ||
      lower.includes("intake has been submitted") ||
      lower.includes("submitted successfully") ||
      lower.includes("aapka token") ||
      lower.includes("doctor ko bhej")
    ) {
      startClosingSession();
    } else if (!isClosingSession) {
      setSessionState(SESSION_STATES.ASSISTANT_SPEAKING);
    }

    currentAssistantText += textChunk;
    if (!currentAssistantText.trim()) return;

    // Issue 2a: Maintain single in-progress bubble per assistant turn
    if (!currentAssistantBubbleEl || !panel.contains(currentAssistantBubbleEl)) {
      currentAssistantBubbleEl = document.createElement("div");
      currentAssistantBubbleEl.className = "chat-bubble bubble-assistant self-start";
      panel.appendChild(currentAssistantBubbleEl);
    }

    currentAssistantBubbleEl.textContent = currentAssistantText;
    panel.scrollTop = panel.scrollHeight;

    if (isFinal) {
      commitAssistantBubble();
    }
  }
}

function handleTranscriptChunk(role, textChunk) {
  handleTranscriptMessage({ role, text: textChunk });
}

// ─────────────────────────────────────────────────────────────────────────────
// LANGUAGE TOGGLE & DYNAMIC STRING REPLACEMENT
// ─────────────────────────────────────────────────────────────────────────────
function setLanguage(lang) {
  currentLanguage = lang === "hi" ? "hi" : "en";
  try {
    sessionStorage.setItem("kioskLang", currentLanguage);
  } catch (_) {}

  const btnEn = document.getElementById("lang-btn-en");
  const btnHi = document.getElementById("lang-btn-hi");
  if (btnEn && btnHi) {
    if (currentLanguage === "en") {
      btnEn.classList.add("active");
      btnHi.classList.remove("active");
    } else {
      btnHi.classList.add("active");
      btnEn.classList.remove("active");
    }
  }

  const lookup = I18N_STRINGS[currentLanguage] || I18N_STRINGS.en;
  document.querySelectorAll("[data-i18n]").forEach((el) => {
    const key = el.getAttribute("data-i18n");
    if (lookup[key]) {
      el.textContent = lookup[key];
    }
  });

  document.querySelectorAll("[data-i18n-placeholder]").forEach((el) => {
    const key = el.getAttribute("data-i18n-placeholder");
    if (lookup[key]) {
      el.placeholder = lookup[key];
    }
  });

  setSessionState(currentSessionState);
  console.log("[i18n] Language set to:", currentLanguage);
}

// ─────────────────────────────────────────────────────────────────────────────
// PAGE NAVIGATION
// ─────────────────────────────────────────────────────────────────────────────
function showPage(name) {
  Object.values(pages).forEach((p) => {
    if (p) p.classList.remove("active");
  });
  if (pages[name]) pages[name].classList.add("active");
}

// ─────────────────────────────────────────────────────────────────────────────
// PAGE 1: ABHA ENTRY
// ─────────────────────────────────────────────────────────────────────────────
function handleGenerateOtp() {
  const inputEl = document.getElementById("abha-input");
  const errorEl = document.getElementById("abha-error");
  if (!inputEl) return;

  const id = inputEl.value.trim();
  if (errorEl) errorEl.classList.remove("visible");

  if (!/^\d{14}$/.test(id)) {
    if (errorEl) errorEl.classList.add("visible");
    return;
  }

  currentAbhaData = MOCK_ABHA_DB[id] ?? {
    abha_id: id,
    name: "Patient",
    age: 0,
    chronic_conditions: [],
    past_medications: []
  };

  currentOtp = String(Math.floor(100000 + Math.random() * 900000));
  const dispEl = document.getElementById("demo-otp-display") || document.getElementById("otp-display");
  if (dispEl) dispEl.textContent = currentOtp;

  showPage("otp");
}

// ─────────────────────────────────────────────────────────────────────────────
// PAGE 2: OTP VERIFICATION
// ─────────────────────────────────────────────────────────────────────────────
function handleVerifyOtp() {
  const inputEl = document.getElementById("otp-input");
  const errorEl = document.getElementById("otp-error");
  if (errorEl) errorEl.classList.remove("visible");

  if (!inputEl || inputEl.value.trim() !== currentOtp) {
    if (errorEl) errorEl.classList.add("visible");
    return;
  }

  if (!currentEncounterId) {
    currentEncounterId = "ENC-" + Array.from(crypto.getRandomValues(new Uint8Array(6))).map(b => b.toString(16).padStart(2, '0')).join('');
  }
  // Transition to pre-consultation document upload screen
  showPage("upload");
}

function handleResendOtp() {
  currentOtp = String(Math.floor(100000 + Math.random() * 900000));
  const dispEl = document.getElementById("demo-otp-display") || document.getElementById("otp-display");
  if (dispEl) dispEl.textContent = currentOtp;
  const inputEl = document.getElementById("otp-input");
  if (inputEl) inputEl.value = "";
  const errorEl = document.getElementById("otp-error");
  if (errorEl) errorEl.classList.remove("visible");
}

// ─────────────────────────────────────────────────────────────────────────────
// PAGE 2.5: DOCUMENT UPLOAD
// ─────────────────────────────────────────────────────────────────────────────
function isAllowedType(file) {
  if (!file) return false;
  const ext = file.name ? file.name.split('.').pop().toLowerCase() : "";
  if (["pdf", "png", "jpg", "jpeg"].includes(ext)) return true;
  return ["image/jpeg", "image/png", "application/pdf"].includes(file.type);
}

function formatFileSize(bytes) {
  if (!bytes || bytes === 0) return "0 B";
  const k = 1024;
  const sizes = ["B", "KB", "MB", "GB"];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + " " + sizes[i];
}

function renderFileChips() {
  const chipsContainer = document.getElementById("file-chips") || fileChips;
  if (!chipsContainer) return;
  chipsContainer.innerHTML = "";
  if (pendingFiles.length === 0) return;

  pendingFiles.forEach((file, index) => {
    const badge = document.createElement("div");
    badge.className = "file-preview-badge";
    const isPdf = file.name.toLowerCase().endsWith(".pdf") || file.type === "application/pdf";
    const iconSvg = isPdf
      ? `<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#d32f2f" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline><line x1="16" y1="13" x2="8" y2="13"></line><line x1="16" y1="17" x2="8" y2="17"></line><polyline points="10 9 9 9 8 9"></polyline></svg>`
      : `<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#005C97" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"></rect><circle cx="8.5" cy="8.5" r="1.5"></circle><polyline points="21 15 16 10 5 21"></polyline></svg>`;

    badge.innerHTML = `
      <div class="file-preview-info">
        <div class="file-preview-icon">${iconSvg}</div>
        <div>
          <div class="file-preview-name" title="${escapeHtml(file.name)}">${escapeHtml(file.name)}</div>
          <div class="file-preview-size">${formatFileSize(file.size)}</div>
        </div>
      </div>
      <button type="button" class="file-preview-remove" title="Remove file" onclick="removeFile(${index})" aria-label="Remove file">&times;</button>
    `;
    chipsContainer.appendChild(badge);
  });
}

function escapeHtml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

window.removeFile = (index) => {
  pendingFiles.splice(index, 1);
  renderFileChips();
};

function advanceToTriage() {
  showPage("triage");
  isConsultationActive = false;
  sessionStarted = false;
  const btn = document.getElementById("mic-btn") || document.getElementById("mic-button") || document.querySelector(".mic-btn");
  if (btn) {
    btn.disabled = false;
  }
  const pBar = document.getElementById("patient-bar") || patientBar;
  if (pBar && currentAbhaData) {
    pBar.textContent = `${currentAbhaData.name} | ABHA: ${currentAbhaData.abha_id}`;
  }
  // Immediately update status to Connecting so screen doesn't look idle (Issue 4)
  setSessionState(SESSION_STATES.CONNECTING);
  attachMicButtonListener();

  // Auto-start consultation session without requiring manual tap (Issue 4)
  initiateVoiceSession().catch(err => {
    console.warn("[auto-start] Error auto-initiating voice session:", err);
  });
}

async function handleProceedTriage() {
  const fInput = document.getElementById("file-input") || fileInput;
  if (fInput && fInput.files && fInput.files.length > 0) {
    for (const f of fInput.files) {
      if (isAllowedType(f) && !pendingFiles.some(pf => pf.name === f.name && pf.size === f.size)) {
        pendingFiles.push(f);
      }
    }
  }
  if (pendingFiles.length > 0) {
    const formData = new FormData();
    const abhaIdVal = (currentAbhaData && currentAbhaData.abha_id) ? currentAbhaData.abha_id : "UNKNOWN";
    formData.append("abha_id", abhaIdVal);
    if (currentEncounterId) {
      formData.append("encounter_id", currentEncounterId);
    }
    pendingFiles.forEach(f => formData.append("files", f));
    try {
      await fetch("/api/upload/records", { method: "POST", body: formData });
      console.log("[upload] Successfully uploaded documents before consultation in language:", currentLanguage);
    } catch (err) {
      console.warn("[upload] Error uploading records:", err);
    }
  }
  advanceToTriage();
}

function handleSkipUpload() {
  pendingFiles = [];
  const fInput = document.getElementById("file-input") || fileInput;
  if (fInput) fInput.value = "";
  renderFileChips();
  advanceToTriage();
}

// ─────────────────────────────────────────────────────────────────────────────
// FULL RESET TO INITIAL SCREEN
// ─────────────────────────────────────────────────────────────────────────────
async function fullReset() {
  if (connectionTimeoutTimer) {
    clearTimeout(connectionTimeoutTimer);
    connectionTimeoutTimer = null;
  }
  if (stateWatchdogTimer) {
    clearTimeout(stateWatchdogTimer);
    stateWatchdogTimer = null;
  }

  connectionRetryCount         = 0;
  currentAbhaData              = null;
  currentOtp                   = "";
  currentEncounterId           = null;
  pendingFiles                 = [];
  renderFileChips();
  isConsultationActive         = false;
  sessionStarted               = false;
  intakeCompleted              = false;
  triageCompletedSuccessfully  = false;
  isCompletedNormally          = false;
  isClosingSession             = false;

  // Issue 3: Reassign in-memory transcript/turn array & tracking state to empty
  transcriptHistory            = [];
  isCurrentPatientTurnCommitted = false;
  isCurrentAssistantTurnCommitted = false;
  committedTurnIds.clear();

  currentPatientBubbleEl       = null;
  currentPatientText           = "";
  currentAssistantBubbleEl     = null;
  currentAssistantText         = "";

  // Issue 3: Cleanly close WebSocket and clear all callbacks
  if (ws) {
    try {
      ws.onopen = null;
      ws.onclose = null;
      ws.onerror = null;
      ws.onmessage = null;
      ws.close(1000, "next_patient");
    } catch (_) {}
    ws = null;
  }

  await teardownAudioPipeline();

  const abhaIn = document.getElementById("abha-input");
  const otpIn  = document.getElementById("otp-input");
  // Issue 3: Explicitly clear DOM transcript containers completely
  const txContainers = [
    document.getElementById("transcript-panel"),
    document.getElementById("transcript"),
    transcriptEl
  ];
  txContainers.forEach(container => {
    if (container) {
      while (container.firstChild) {
        container.removeChild(container.firstChild);
      }
      container.innerHTML = "";
    }
  });

  const abhaErr = document.getElementById("abha-error");
  const otpErr = document.getElementById("otp-error");
  const rfBanner = document.getElementById("red-flag-banner");

  if (abhaIn) abhaIn.value = "";
  if (otpIn) otpIn.value = "";
  if (abhaErr) abhaErr.classList.remove("visible");
  if (otpErr) otpErr.classList.remove("visible");
  if (rfBanner) rfBanner.classList.remove("visible");

  pendingFiles = [];
  renderFileChips();
  currentTurnNumber = 1;
  turnTranscriptEventsCount = 0;

  // Reset UI components to clean initial Tap to Talk state (Issue 3)
  const micStage = document.getElementById("triage-active-container") || document.querySelector(".mic-stage");
  if (micStage) {
    micStage.classList.remove("hidden");
    micStage.style.display = "";
  }
  const micBtnEl = document.getElementById("mic-btn") || document.getElementById("mic-button") || micBtn;
  if (micBtnEl) {
    micBtnEl.classList.remove("hidden", "state-listening", "state-speaking", "state-red-flag", "state-processing");
    micBtnEl.classList.add("state-idle");
    micBtnEl.style.display = "";
    micBtnEl.disabled = false;
  }
  const micLabel = document.getElementById("mic-label-text") || micLabelText;
  if (micLabel) {
    const strings = I18N_STRINGS[currentLanguage] || I18N_STRINGS.en;
    micLabel.textContent = strings.micTapToTalk || "Tap to Talk";
  }
  const micStatus = document.getElementById("mic-status-label") || micStatusLabel;
  if (micStatus) {
    micStatus.textContent = "Ready. Tap the button to begin your consultation.";
  }

  const loadingCard = document.getElementById("intake-loading-card");
  if (loadingCard) loadingCard.style.display = "none";
  const successCard = document.getElementById("intake-success-card");
  if (successCard) {
    successCard.classList.add("hidden");
    successCard.style.display = "none";
  }

  setSessionState(SESSION_STATES.DISCONNECTED);
  showPage("abha");
}

// ─────────────────────────────────────────────────────────────────────────────
// DOM INITIALIZATION & EVENT BINDING
// ─────────────────────────────────────────────────────────────────────────────
let isInitialized = false;
function initKiosk() {
  if (isInitialized) return;
  isInitialized = true;

  pages = {
    abha:        document.getElementById("page-abha"),
    otp:         document.getElementById("page-otp") || document.getElementById("otp-modal"),
    upload:      document.getElementById("page-upload"),
    triage:      document.getElementById("page-triage"),
    complete:    document.getElementById("page-complete"),
    unavailable: document.getElementById("page-unavailable"),
    failed:      document.getElementById("page-failed"),
  };

  abhaInput      = document.getElementById("abha-input");
  abhaError      = document.getElementById("abha-error");
  btnGenOtp      = document.getElementById("generate-otp-btn") || document.getElementById("btn-generate-otp");
  otpDisplay     = document.getElementById("demo-otp-display") || document.getElementById("otp-display");
  otpInput       = document.getElementById("otp-input");
  otpError       = document.getElementById("otp-error");
  btnVerifyOtp   = document.getElementById("btn-verify-otp");
  btnResendOtp   = document.getElementById("btn-resend-otp");

  dropZone         = document.getElementById("drop-zone");
  fileInput        = document.getElementById("file-input");
  fileChips        = document.getElementById("file-chips");
  btnProceedTriage = document.getElementById("btn-proceed-triage");
  btnSkipUpload    = document.getElementById("btn-skip-upload");

  patientBar     = document.getElementById("patient-bar");
  redFlagBanner  = document.getElementById("red-flag-banner");
  micBtn         = document.getElementById("mic-btn") || document.getElementById("mic-button") || document.querySelector(".mic-btn");
  micLabelText   = document.getElementById("mic-label-text");
  micStatusLabel = document.getElementById("mic-status-label");
  transcriptEl   = document.getElementById("transcript-panel") || document.getElementById("transcript");
  btnNextPatient = document.getElementById("btn-next-patient");
  btnRetryReset  = document.getElementById("btn-retry-reset");

  if (abhaInput) {
    abhaInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") handleGenerateOtp();
    });
  }

  if (btnVerifyOtp) btnVerifyOtp.addEventListener("click", handleVerifyOtp);
  if (otpInput) {
    otpInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") handleVerifyOtp();
    });
  }
  if (btnResendOtp) btnResendOtp.addEventListener("click", handleResendOtp);

  const btnBrowseFiles = document.getElementById("btn-browse-files");
  if (btnBrowseFiles && fileInput) {
    btnBrowseFiles.addEventListener("click", (e) => {
      e.stopPropagation();
      fileInput.click();
    });
  }

  if (dropZone && fileInput) {
    dropZone.addEventListener("click", (e) => {
      if (e.target !== btnBrowseFiles) {
        fileInput.click();
      }
    });
    dropZone.addEventListener("dragover", (e) => {
      e.preventDefault();
      dropZone.classList.add("dragover");
    });
    dropZone.addEventListener("dragleave", () => {
      dropZone.classList.remove("dragover");
    });
    dropZone.addEventListener("drop", (e) => {
      e.preventDefault();
      dropZone.classList.remove("dragover");
      if (e.dataTransfer && e.dataTransfer.files) {
        for (const f of e.dataTransfer.files) {
          if (isAllowedType(f) && !pendingFiles.some(pf => pf.name === f.name && pf.size === f.size)) {
            pendingFiles.push(f);
          }
        }
        renderFileChips();
      }
    });
    fileInput.addEventListener("change", (e) => {
      if (e.target && e.target.files) {
        for (const f of e.target.files) {
          if (isAllowedType(f) && !pendingFiles.some(pf => pf.name === f.name && pf.size === f.size)) {
            pendingFiles.push(f);
          }
        }
        renderFileChips();
      }
    });
  }

  if (btnProceedTriage) btnProceedTriage.addEventListener("click", handleProceedTriage);
  if (btnSkipUpload) btnSkipUpload.addEventListener("click", handleSkipUpload);

  attachMicButtonListener();

  if (btnNextPatient) btnNextPatient.addEventListener("click", fullReset);
  if (btnRetryReset) btnRetryReset.addEventListener("click", fullReset);
  const btnDone = document.getElementById("btn-done-intake");
  if (btnDone) btnDone.addEventListener("click", fullReset);

  // Clean retry button on System Unavailable screen
  const btnUnavailableRetry = document.getElementById("btn-unavailable-retry");
  if (btnUnavailableRetry) {
    btnUnavailableRetry.addEventListener("click", handleRetryAction);
  }
  const btnUnavailableStart = document.getElementById("btn-unavailable-start");
  if (btnUnavailableStart) {
    btnUnavailableStart.addEventListener("click", fullReset);
  }

  // Language selector button listeners
  const langEnBtn = document.getElementById("lang-btn-en");
  const langHiBtn = document.getElementById("lang-btn-hi");
  if (langEnBtn) langEnBtn.addEventListener("click", () => setLanguage("en"));
  if (langHiBtn) langHiBtn.addEventListener("click", () => setLanguage("hi"));

  // Initial language setup
  setLanguage(currentLanguage);
}

async function initiateVoiceSession() {
  console.log("[kiosk] Initiating voice consultation session... Current state:", currentSessionState);

  // 1. Explicitly resume AudioContexts on user gesture
  const AudioCtxClass = window.AudioContext || window.webkitAudioContext;
  if (!audioContext && AudioCtxClass) {
    try {
      audioContext = new AudioCtxClass();
    } catch (ctxErr) {
      console.warn("[kiosk] Could not instantiate AudioContext:", ctxErr);
    }
  }
  if (audioContext && audioContext.state === "suspended") {
    await audioContext.resume().catch((err) => console.warn("[kiosk] AudioContext resume error:", err));
  }
  if (playbackContext && playbackContext.state === "suspended") {
    await playbackContext.resume().catch((err) => console.warn("[kiosk] PlaybackContext resume error:", err));
  }

  // 2. Explicitly request microphone access if not yet acquired
  if (!micStream || !micStream.active) {
    try {
      if (navigator.mediaDevices && navigator.mediaDevices.getUserMedia) {
        micStream = await navigator.mediaDevices.getUserMedia({
          audio: {
            echoCancellation: true,
            noiseSuppression: true,
            autoGainControl: true,
          },
          video: false,
        });
        console.log("[kiosk] Microphone access acquired successfully.");
      }
    } catch (micErr) {
      console.error("[kiosk] getUserMedia failed:", micErr);
      const statusLabel = document.getElementById("mic-status-label") || micStatusLabel;
      if (statusLabel) {
        statusLabel.textContent = "Microphone access blocked. Please allow mic permissions in your browser.";
      }
      return;
    }
  }

  // 3. Transition visual state to Connecting and launch triage session / WebSocket
  if (!isConsultationActive || !sessionStarted || !ws || ws.readyState !== WebSocket.OPEN) {
    isConsultationActive = false;
    await startTriageSession();
  }
}

function attachMicButtonListener() {
  const btn = document.getElementById("mic-btn") || document.getElementById("mic-button") || document.querySelector(".mic-btn");
  if (!btn) {
    console.warn("[mic-attach] Could not find mic button element.");
    return;
  }
  btn.onclick = async (e) => {
    if (e && e.preventDefault) e.preventDefault();
    try {
      console.log("[mic-click] Tap to Talk clicked! State:", currentSessionState);

      if (btn.classList.contains("state-red-flag") || btn.disabled) {
        console.warn("[mic-click] Button is in disabled/red-flag state.");
        return;
      }

      // 1. If assistant is currently speaking, user tap acts as barge-in / interrupt
      if (isAssistantSpeaking || currentSessionState === SESSION_STATES.ASSISTANT_SPEAKING) {
        flushPlaybackAudio();
        setAssistantSpeaking(false);
        setSessionState(SESSION_STATES.LISTENING);
        return;
      }

      await initiateVoiceSession();
    } catch (err) {
      console.error("[Kiosk Click Error]", err);
    }
  };
}

document.addEventListener("DOMContentLoaded", () => {
  initKiosk();
  const genBtn = document.getElementById("generate-otp-btn") || document.getElementById("btn-generate-otp");
  if (genBtn) genBtn.addEventListener("click", handleGenerateOtp);
});

if (document.readyState === "interactive" || document.readyState === "complete") {
  initKiosk();
  const genBtn = document.getElementById("generate-otp-btn") || document.getElementById("btn-generate-otp");
  if (genBtn) genBtn.addEventListener("click", handleGenerateOtp);
}
