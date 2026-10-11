"""Pretend to be Exotel's voicebot applet, so the phone leg can be tested
without spending a call.

Speaks the real AgentStream protocol: connected -> start -> media... -> stop,
with base64 PCM16 inside JSON. Asserts the rules Exotel actually enforces --
outbound chunks a multiple of 320 bytes, stream_sid echoed on every message.

    python scripts/exotel_sim.py            # 8k, one turn
    python scripts/exotel_sim.py 16000      # 16k, one turn
    python scripts/exotel_sim.py 8000 barge # interrupt the agent mid-reply
"""

import asyncio, array, base64, json, os, pathlib, sys, time, wave

import websockets

FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "fixtures"
URL = os.environ.get("WS_URL") or f"ws://localhost:{os.environ.get('PORT', 8078)}/ws/exotel"
SID = "MZtest0000000000000000000000"


def load(path, rate):
    with wave.open(str(path)) as w:
        pcm = w.readframes(w.getnframes())
        src = w.getframerate()
    if src == rate:
        return pcm
    if src == 2 * rate:  # 16k -> 8k, average pairs as a crude anti-alias
        a = array.array("h", pcm)
        return array.array("h", [(a[i] + a[i + 1]) // 2 for i in range(0, len(a) - 1, 2)]).tobytes()
    raise SystemExit(f"cannot convert {src} -> {rate}")


async def main(rate: int, mode: str) -> None:
    question = load(FIXTURES / "question.wav", rate)
    interrupt = load(FIXTURES / "interrupt.wav", rate)
    frame = int(rate * 0.02) * 2  # 20ms, as Exotel sends

    got = bytearray()
    events, violations = [], []
    cleared = asyncio.Event()
    t0 = time.perf_counter()
    audio_after_clear = 0

    async with websockets.connect(URL, max_size=None) as ws:
        async def send(obj): await ws.send(json.dumps(obj))

        async def reader():
            nonlocal audio_after_clear
            async for raw in ws:
                m = json.loads(raw)
                el = (time.perf_counter() - t0) * 1000
                kind = m.get("event")
                if m.get("stream_sid") != SID:
                    violations.append(f"{kind}: wrong stream_sid {m.get('stream_sid')!r}")
                if kind == "media":
                    chunk = base64.b64decode(m["media"]["payload"])
                    if len(chunk) % 320:
                        violations.append(f"chunk {len(chunk)}B not a multiple of 320")
                    got.extend(chunk)
                    if cleared.is_set():
                        audio_after_clear += len(chunk)
                    if not any(e[0] == "first media" for e in events):
                        events.append(("first media", el))
                elif kind == "mark":
                    # Exotel echoes marks back once the audio ahead of them has
                    # played. Mirror that so the mark path is exercised.
                    events.append((f"mark:{m['mark']['name']}", el))
                    await ws.send(json.dumps({"event": "mark", "stream_sid": SID,
                                              "mark": m["mark"]}))
                elif kind == "clear":
                    # Reset: we want trailing audio after the LATEST clear, not
                    # after the spurious one the caller's first words trigger.
                    events.append(("CLEAR", el)); cleared.set(); audio_after_clear = 0
                else:
                    events.append((kind, el))

        r = asyncio.create_task(reader())
        await send({"event": "connected"})
        await send({"event": "start", "sequence_number": "1", "stream_sid": SID,
                    "start": {"stream_sid": SID, "call_sid": "CAtest", "account_sid": "test-account",
                              "from": "+919999999999", "to": "08000000000",
                              "media_format": {"encoding": "audio/x-raw",
                                               "sample_rate": str(rate), "bit_rate": "16"}}})

        async def stream(pcm):
            for i in range(0, len(pcm), frame):
                await send({"event": "media", "stream_sid": SID,
                            "media": {"chunk": "1", "timestamp": "0",
                                      "payload": base64.b64encode(pcm[i:i+frame]).decode()}})
                await asyncio.sleep(0.02)

        await stream(question)
        await stream(b"\x00" * frame * 30)  # trailing silence -> END_SPEECH

        if mode == "barge":
            for _ in range(200):
                if any(e[0] == "first media" for e in events): break
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.4)
            events.append(("<<INTERRUPT>>", (time.perf_counter()-t0)*1000))
            await stream(interrupt)
            await stream(b"\x00" * frame * 30)
        else:
            await asyncio.sleep(14)

        await send({"event": "stop", "stream_sid": SID,
                    "stop": {"call_sid": "CAtest", "reason": "callended"}})
        await asyncio.sleep(0.5)
        r.cancel()

    print(f"\n=== exotel sim: {rate}Hz {mode} ===")
    for e in events:
        print(f"  [{e[1]:7.0f}ms] {e[0]}")
    print(f"agent audio: {len(got)} bytes = {len(got)/2/rate:.2f}s @{rate}")
    if mode == "barge":
        print(f"clear received: {cleared.is_set()}")
        print(f"audio after clear: {audio_after_clear} bytes ({audio_after_clear/2/rate:.2f}s)")
    print("PROTOCOL VIOLATIONS:", violations or "none")
    with wave.open(str(FIXTURES / f"exotel_{rate}_{mode}.wav"), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(bytes(got))


rate = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
mode = sys.argv[2] if len(sys.argv) > 2 else "normal"
asyncio.run(main(rate, mode))
