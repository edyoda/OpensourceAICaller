import contextlib
import logging
from contextlib import asynccontextmanager

from anthropic import AsyncAnthropic
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from sarvamai import AsyncSarvamAI

from .config import settings
from .rag import CourseSearch
from .session import CallSession
from .transport import BrowserTransport, Disconnected, ExotelTransport

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("callagent")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.sarvam = AsyncSarvamAI(api_subscription_key=settings.sarvam_api_key)
    # api_key=None falls through to the SDK's own ANTHROPIC_API_KEY / auth-profile
    # lookup; passing it explicitly is what makes a key in .env actually work.
    headers = ({"anthropic-workspace-id": settings.anthropic_workspace_id}
               if settings.anthropic_workspace_id else None)
    app.state.claude = AsyncAnthropic(api_key=settings.anthropic_api_key,
                                      default_headers=headers)
    app.state.courses = await CourseSearch.connect()
    yield


app = FastAPI(title="EdYoda AI Call Agent", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "stt": settings.stt_model, "tts": settings.tts_model, "llm": settings.llm_model}


async def _run(ws: WebSocket, transport) -> None:
    await transport.prepare()
    session = CallSession(transport, ws.app.state.sarvam, ws.app.state.claude,
                          ws.app.state.courses)
    try:
        await session.run()
    except (WebSocketDisconnect, Disconnected):
        log.info("caller hung up")
    except Exception:
        log.exception("call failed")
        with contextlib.suppress(RuntimeError):
            await ws.close(code=1011)


@app.websocket("/ws/call")
async def call(ws: WebSocket) -> None:
    """Browser client: binary PCM both ways."""
    await ws.accept()
    await _run(ws, BrowserTransport(ws, settings.mic_sample_rate, settings.tts_sample_rate))


@app.websocket("/ws/exotel")
async def exotel(ws: WebSocket) -> None:
    """Exotel AgentStream voicebot applet: base64 PCM inside JSON events."""
    await ws.accept()
    await _run(ws, ExotelTransport(ws))


app.mount("/", StaticFiles(directory="static", html=True), name="static")
