// ===== WebSocket + audio streaming =====
//
// Input (browser -> server):
//  - Binary frames: PCM16 mono @ 16kHz
//  - Text frames: JSON control messages
//
// Output (server -> browser):
//  - Binary frames: PCM16 mono @ 24kHz
//  - Text frames: JSON events/transcriptions/status

const connectBtn = document.getElementById("connectBtn");
const disconnectBtn = document.getElementById("disconnectBtn");
const micBtn = document.getElementById("micBtn");
const micLabel = document.getElementById("micLabel");
const statusPill = document.getElementById("statusPill");

const voiceNameEl = document.getElementById("voiceName");
const systemInstructionEl = document.getElementById("systemInstruction");
const affectiveEl = document.getElementById("affective");
const proactiveEl = document.getElementById("proactive");

const capIn = document.getElementById("capIn");
const capOut = document.getElementById("capOut");

const chat = document.getElementById("chat");
const textInput = document.getElementById("textInput");
const sendTextBtn = document.getElementById("sendTextBtn");

let ws = null;

// Capture
let micStream = null;
let audioCtx = null;
let sourceNode = null;
let processorNode = null;

// Playback
let playCtx = null;
let nextPlayTime = 0;
let scheduled = []; // AudioBufferSourceNodes
const OUT_SAMPLE_RATE = 24000;

// UI state
let micOn = false;
let lastInFinal = "";
let lastOutFinal = "";

function setStatus(mode, text) {
  statusPill.textContent = text;
  statusPill.className = "pill " + mode;
}

function addBubble(who, text) {
  const div = document.createElement("div");
  div.className = "bubble " + (who === "user" ? "user" : "ai");
  div.textContent = text;
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
}

// --- PCM helpers ---
function downsampleBuffer(buffer, inputSampleRate, outputSampleRate) {
  if (outputSampleRate === inputSampleRate) return buffer;
  const ratio = inputSampleRate / outputSampleRate;
  const newLength = Math.round(buffer.length / ratio);
  const result = new Float32Array(newLength);

  let offsetResult = 0;
  let offsetBuffer = 0;

  while (offsetResult < result.length) {
    const nextOffsetBuffer = Math.round((offsetResult + 1) * ratio);
    let sum = 0;
    let count = 0;
    for (let i = offsetBuffer; i < nextOffsetBuffer && i < buffer.length; i++) {
      sum += buffer[i];
      count++;
    }
    result[offsetResult] = count > 0 ? (sum / count) : 0;
    offsetResult++;
    offsetBuffer = nextOffsetBuffer;
  }
  return result;
}

function floatTo16BitPCM(float32Array) {
  const buffer = new ArrayBuffer(float32Array.length * 2);
  const view = new DataView(buffer);
  for (let i = 0; i < float32Array.length; i++) {
    let s = Math.max(-1, Math.min(1, float32Array[i]));
    view.setInt16(i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return buffer;
}

function pcm16ToFloat32(arrayBuffer) {
  const int16 = new Int16Array(arrayBuffer);
  const float32 = new Float32Array(int16.length);
  for (let i = 0; i < int16.length; i++) {
    float32[i] = int16[i] / 32768;
  }
  return float32;
}

// --- Playback scheduling ---
function clearPlayback() {
  for (const node of scheduled) {
    try { node.stop(); } catch {}
  }
  scheduled = [];
  if (playCtx) nextPlayTime = playCtx.currentTime;
}

async function ensurePlayContext() {
  if (!playCtx) {
    // Some browsers may ignore the requested sampleRate; still works (browser will resample)
    playCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: OUT_SAMPLE_RATE });
    nextPlayTime = playCtx.currentTime;
  }
  if (playCtx.state !== "running") {
    await playCtx.resume();
  }
}

async function playPcmChunk(arrayBuffer) {
  await ensurePlayContext();

  const f32 = pcm16ToFloat32(arrayBuffer);
  const audioBuffer = playCtx.createBuffer(1, f32.length, OUT_SAMPLE_RATE);
  audioBuffer.copyToChannel(f32, 0);

  const src = playCtx.createBufferSource();
  src.buffer = audioBuffer;
  src.connect(playCtx.destination);

  const startAt = Math.max(playCtx.currentTime, nextPlayTime);
  src.start(startAt);
  nextPlayTime = startAt + audioBuffer.duration;
  scheduled.push(src);

  src.onended = () => {
    scheduled = scheduled.filter((n) => n !== src);
  };
}

// --- WebSocket ---
function wsUrl() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${location.host}/ws`;
}

function sendJson(obj) {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify(obj));
}

async function connect() {
  if (ws && ws.readyState === WebSocket.OPEN) return;

  setStatus("pill-warn", "Connecting...");
  ws = new WebSocket(wsUrl());
  ws.binaryType = "arraybuffer";

  ws.onopen = async () => {
    // Init message
    sendJson({
      type: "init",
      voice_name: voiceNameEl.value.trim(),
      system_instruction: systemInstructionEl.value.trim(),
      enable_affective_dialog: affectiveEl.checked,
      enable_proactive_audio: proactiveEl.checked,
    });

    connectBtn.disabled = true;
    disconnectBtn.disabled = false;
    micBtn.disabled = false;
    sendTextBtn.disabled = false;
    setStatus("pill-on", "Connected");
  };

  ws.onmessage = async (evt) => {
    if (typeof evt.data === "string") {
      let msg = null;
      try { msg = JSON.parse(evt.data); } catch { return; }

      if (msg.type === "status") return;

      if (msg.type === "error") {
        setStatus("pill-err", "Error");
        addBubble("ai", `Error: ${msg.message}`);
        return;
      }

      if (msg.type === "interrupt") {
        clearPlayback();
        capOut.textContent = "—";
        return;
      }

      if (msg.type === "transcript_in") {
        capIn.textContent = msg.text || "—";
        if (msg.final && msg.text && msg.text.trim()) {
          if (msg.text.trim() !== lastInFinal) {
            addBubble("user", msg.text.trim());
            lastInFinal = msg.text.trim();
          }
          capIn.textContent = "—";
        }
        return;
      }

      if (msg.type === "transcript_out") {
        capOut.textContent = msg.text || "—";
        if (msg.final && msg.text && msg.text.trim()) {
          if (msg.text.trim() !== lastOutFinal) {
            addBubble("ai", msg.text.trim());
            lastOutFinal = msg.text.trim();
          }
          capOut.textContent = "—";
        }
        return;
      }

      return;
    }

    // Binary audio chunk from server
    if (evt.data instanceof ArrayBuffer) {
      await playPcmChunk(evt.data);
    }
  };

  ws.onerror = () => {
    setStatus("pill-err", "Socket error");
  };

  ws.onclose = () => {
    setStatus("pill-off", "Disconnected");
    connectBtn.disabled = false;
    disconnectBtn.disabled = true;
    micBtn.disabled = true;
    sendTextBtn.disabled = true;
    ws = null;

    stopMic(); // ensure cleanup
    clearPlayback();
  };
}

function disconnect() {
  if (!ws) return;
  try { sendJson({ type: "close" }); } catch {}
  try { ws.close(); } catch {}
}

// --- Mic capture ---
async function startMic() {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;

  // Must be in a user gesture on most browsers
  micStream = await navigator.mediaDevices.getUserMedia({
    audio: {
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
    }
  });

  audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  await audioCtx.resume();

  sourceNode = audioCtx.createMediaStreamSource(micStream);

  // ScriptProcessor is deprecated but widely supported; simplest for a starter app.
  // bufferSize 4096 => ~85ms at 48kHz
  processorNode = audioCtx.createScriptProcessor(4096, 1, 1);

  processorNode.onaudioprocess = (e) => {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;

    const input = e.inputBuffer.getChannelData(0);
    const down = downsampleBuffer(input, audioCtx.sampleRate, 16000);
    const pcm = floatTo16BitPCM(down);
    ws.send(pcm);
  };

  // Connect nodes (processorNode must be connected to destination in many browsers)
  sourceNode.connect(processorNode);
  processorNode.connect(audioCtx.destination);

  micOn = true;
  micLabel.textContent = "Stop mic";
  setStatus("pill-on", "Listening");
}

function stopMic() {
  if (!micOn) return;

  try { sendJson({ type: "stop" }); } catch {}

  try {
    if (processorNode) {
      processorNode.disconnect();
      processorNode.onaudioprocess = null;
    }
    if (sourceNode) sourceNode.disconnect();
  } catch {}

  processorNode = null;
  sourceNode = null;

  try {
    if (micStream) {
      micStream.getTracks().forEach(t => t.stop());
    }
  } catch {}
  micStream = null;

  try {
    if (audioCtx) audioCtx.close();
  } catch {}
  audioCtx = null;

  micOn = false;
  micLabel.textContent = "Start mic";
  if (ws && ws.readyState === WebSocket.OPEN) setStatus("pill-on", "Connected");
}

// --- UI wiring ---
connectBtn.onclick = () => connect();
disconnectBtn.onclick = () => disconnect();

micBtn.onclick = async () => {
  try {
    if (!micOn) await startMic();
    else stopMic();
  } catch (e) {
    addBubble("ai", `Mic error: ${e.message || e}`);
  }
};

sendTextBtn.onclick = () => {
  const text = (textInput.value || "").trim();
  if (!text) return;
  sendJson({ type: "text", text });
  addBubble("user", text);
  textInput.value = "";
};

// If user changes v1alpha toggles/voice mid-session, the server-side config can't be updated
// for an existing Live session; reconnect for changes.
[voiceNameEl, systemInstructionEl, affectiveEl, proactiveEl].forEach((el) => {
  el.addEventListener("change", () => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      addBubble("ai", "Config changed. Disconnect + Connect to apply voice/instruction/toggles.");
    }
  });
});
