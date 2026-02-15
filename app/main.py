import asyncio
import json
import os
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from google import genai
from google.genai import types

load_dotenv()

DEFAULT_MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"
MODEL = os.getenv("GEMINI_LIVE_MODEL", DEFAULT_MODEL)

# Live API expects:
# - input audio: PCM16 @ 16kHz mono
# - output audio: PCM16 @ 24kHz (we just forward whatever the model sends)
INPUT_MIME = "audio/pcm;rate=16000"

# ----------------------------
# Tool mounting (appliance_tools.py)
# ----------------------------
# Works whether your file is directly "appliance_tools.py" or inside "app/appliance_tools.py"
try:
    from appliance_tools import get_devices as tool_get_devices  # type: ignore
    from appliance_tools import control_device as tool_control_device  # type: ignore
except Exception:  # pragma: no cover
    from app.appliance_tools import get_devices as tool_get_devices  # type: ignore
    from app.appliance_tools import control_device as tool_control_device  # type: ignore


# --- Live API tool declarations (function calling) ---
GET_DEVICES_FN = {
    "name": "get_devices",
    "description": "Fetch the latest smart-home device list and on/off status from the appliance API.",
    "parameters": {"type": "object", "properties": {}},
}

CONTROL_DEVICE_FN = {
    "name": "control_device",
    "description": (
        "Turn an appliance on/off/toggle by specifying an action and a target string. "
        "The server will resolve the target against the latest device list, then call the appliance API."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["on", "off", "toggle"]},
            "target": {
                "type": "string",
                "description": "Examples: 'Living room TV', 'Bedroom bulb', 'Study PC', 'Bedroom AC', 'Bedroom fan'.",
            },
        },
        "required": ["action", "target"],
    },
}

TOOLS = [{"function_declarations": [GET_DEVICES_FN, CONTROL_DEVICE_FN]}]


def _bool(v) -> bool:
    return bool(v) and str(v).lower() not in ("0", "false", "no", "off", "")


app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.on_event("startup")
async def _startup():
    # Reuse one AsyncClient across requests (tools use this)
    app.state.http = httpx.AsyncClient(timeout=httpx.Timeout(10.0))


@app.on_event("shutdown")
async def _shutdown():
    try:
        await app.state.http.aclose()
    except Exception:
        pass


@app.get("/")
def index():
    return FileResponse("static/index.html")


@app.get("/api/devices")
async def proxy_devices():
    # Convenience endpoint (UI / debugging)
    return await tool_get_devices(app.state.http)


async def _safe_send_text(ws: WebSocket, payload: dict):
    try:
        await ws.send_text(json.dumps(payload))
    except Exception:
        pass


async def _safe_send_bytes(ws: WebSocket, data: bytes):
    try:
        await ws.send_bytes(data)
    except Exception:
        pass


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()

    # Expect an init JSON message first
    try:
        init_raw = await ws.receive_text()
        init = json.loads(init_raw)
    except Exception:
        await _safe_send_text(ws, {"type": "error", "message": "Expected init JSON as first message."})
        await ws.close()
        return

    system_instruction = init.get("system_instruction") or "You are a helpful and friendly AI assistant."
    voice_name = (init.get("voice_name") or "").strip()  # e.g. "Kore"
    enable_affective_dialog = _bool(init.get("enable_affective_dialog"))
    enable_proactive_audio = _bool(init.get("enable_proactive_audio"))

    # Affective dialog + proactivity are v1alpha features in docs.
    # If you toggle them on, we switch the SDK client to v1alpha.
    http_options: Optional[types.HttpOptions] = None
    if enable_affective_dialog or enable_proactive_audio:
        http_options = types.HttpOptions(api_version="v1alpha")

    # SDK picks up GEMINI_API_KEY / GOOGLE_API_KEY from env automatically,
    # but we'll also accept GEMINI_API_KEY explicitly if present.
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        await _safe_send_text(ws, {"type": "error", "message": "Missing GEMINI_API_KEY (or GOOGLE_API_KEY) in environment."})
        await ws.close()
        return

    client = genai.Client(api_key=api_key, http_options=http_options) if http_options else genai.Client(api_key=api_key)

    # Live connect config
    config: dict = {
        "response_modalities": ["AUDIO"],
        "system_instruction": system_instruction,
        # Enable both input/output transcriptions for UI captions
        "input_audio_transcription": {},
        "output_audio_transcription": {},
        # IMPORTANT: enable function calling tools
        "tools": TOOLS,
    }

    # Optional voice selection
    if voice_name:
        config["speech_config"] = {
            "voice_config": {"prebuilt_voice_config": {"voice_name": voice_name}}
        }

    # Optional native-audio dialog enhancements (v1alpha)
    if enable_affective_dialog:
        config["enable_affective_dialog"] = True
    if enable_proactive_audio:
        config["proactivity"] = {"proactive_audio": True}

    audio_in_q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=20)

    async def execute_tool(name: str, args: dict):
        """Dispatch tool calls from Gemini to your mounted appliance tools."""
        try:
            if name == "get_devices":
                return await tool_get_devices(app.state.http)

            if name == "control_device":
                action = (args or {}).get("action") or "toggle"
                target = (args or {}).get("target") or ""
                return await tool_control_device(app.state.http, action=action, target=target)

            return {"ok": False, "result": "error", "message": f"Unknown tool '{name}'"}
        except Exception as e:
            return {"ok": False, "result": "error", "message": f"{type(e).__name__}: {e}"}

    async def browser_reader(session):
        """
        Reads from browser websocket.
        - bytes => PCM16@16kHz audio chunks to queue
        - text  => JSON control messages
        """
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                raise WebSocketDisconnect()

            if msg.get("bytes") is not None:
                chunk = msg["bytes"]
                # Keep real-time feel: drop oldest if queue full
                if audio_in_q.full():
                    try:
                        _ = audio_in_q.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                try:
                    audio_in_q.put_nowait(chunk)
                except asyncio.QueueFull:
                    pass
                continue

            if msg.get("text") is not None:
                try:
                    payload = json.loads(msg["text"])
                except Exception:
                    continue

                ptype = payload.get("type")
                if ptype == "stop":
                    # Signal end of audio stream to server-side VAD pipeline
                    try:
                        await session.send_realtime_input(audio_stream_end=True)
                    except Exception:
                        pass
                elif ptype == "text":
                    # Optional typed input
                    text = (payload.get("text") or "").strip()
                    if text:
                        await session.send_realtime_input(text=text)
                elif ptype == "ping":
                    await _safe_send_text(ws, {"type": "pong"})
                elif ptype == "close":
                    await ws.close()
                    return

    async def gemini_sender(session):
        """Sends queued audio chunks to Gemini Live session."""
        while True:
            chunk = await audio_in_q.get()
            # IMPORTANT: this is the WORKING format (fixes talk-back)
            await session.send_realtime_input(audio={"data": chunk, "mime_type": INPUT_MIME})

    async def gemini_receiver(session):
        """
        Receives Gemini messages and forwards:
        - tool calls => execute tool + send_tool_response
        - binary audio => ws bytes
        - transcriptions / status => ws text JSON
        """
        await _safe_send_text(ws, {"type": "status", "status": "connected", "model": MODEL})

        while True:
            # SDK pattern: receive a "turn" worth of responses
            turn = session.receive()
            async for resp in turn:
                # ----------------------------
                # TOOL CALL HANDLING
                # ----------------------------
                tool_call = getattr(resp, "tool_call", None)
                if tool_call and getattr(tool_call, "function_calls", None):
                    function_responses = []
                    for fc in tool_call.function_calls:
                        await _safe_send_text(ws, {"type": "tool_call", "name": fc.name, "args": fc.args})

                        result = await execute_tool(fc.name, fc.args or {})
                        function_responses.append(
                            types.FunctionResponse(
                                id=fc.id,
                                name=fc.name,
                                response=result,
                            )
                        )

                        await _safe_send_text(ws, {"type": "tool_result", "name": fc.name, "result": result})

                    # Send tool results back to Gemini so it can continue talking
                    await session.send_tool_response(function_responses=function_responses)
                    continue

                sc = getattr(resp, "server_content", None)

                # interruption (barge-in / cut-off)
                if sc and getattr(sc, "interrupted", False):
                    await _safe_send_text(ws, {"type": "interrupt"})
                    continue

                # input transcription
                if sc and getattr(sc, "input_transcription", None):
                    it = sc.input_transcription
                    await _safe_send_text(ws, {
                        "type": "transcript_in",
                        "text": getattr(it, "text", ""),
                        "final": bool(getattr(it, "finished", False)),
                    })

                # output transcription
                if sc and getattr(sc, "output_transcription", None):
                    ot = sc.output_transcription
                    await _safe_send_text(ws, {
                        "type": "transcript_out",
                        "text": getattr(ot, "text", ""),
                        "final": bool(getattr(ot, "finished", False)),
                    })

                # Some SDK responses expose audio bytes as resp.data
                data = getattr(resp, "data", None)
                if isinstance(data, (bytes, bytearray)) and data:
                    await _safe_send_bytes(ws, bytes(data))
                    continue

                # Otherwise read audio from model_turn parts
                if sc and getattr(sc, "model_turn", None):
                    mt = sc.model_turn
                    for part in getattr(mt, "parts", []) or []:
                        inline = getattr(part, "inline_data", None)
                        if inline and isinstance(getattr(inline, "data", None), (bytes, bytearray)):
                            await _safe_send_bytes(ws, bytes(inline.data))

            await _safe_send_text(ws, {"type": "turn_complete"})

    try:
        async with client.aio.live.connect(model=MODEL, config=config) as session:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(browser_reader(session))
                tg.create_task(gemini_sender(session))
                tg.create_task(gemini_receiver(session))

    except WebSocketDisconnect:
        pass
    except Exception as e:
        await _safe_send_text(ws, {"type": "error", "message": f"{type(e).__name__}: {e}"})
    finally:
        try:
            await ws.close()
        except Exception:
            pass
        try:
            await client.aio.aclose()
        except Exception:
            pass
