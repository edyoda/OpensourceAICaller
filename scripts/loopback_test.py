"""Headless end-to-end test: synthesized speech -> /ws/call -> agent audio.

No browser and no microphone: fixture WAVs are fed in as 40ms PCM frames,
exactly the framing static/capture-worklet.js produces.

    uvicorn app.main:app --port 8078 &
    python scripts/loopback_test.py normal    # one full turn
    python scripts/loopback_test.py bargein   # talk over the agent mid-reply

`bargein` is the one that matters. It caught the agent speaking straight
through an interruption because the barge-in check looked at the LLM task,
which finishes long before the voice does. Expect `interrupt` within ~350ms of
the injection and well under a second of trailing audio.
"""
import asyncio, json, os, pathlib, sys, time, wave
import websockets

FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "fixtures"
URL = f"ws://localhost:{os.environ.get('PORT', 8078)}/ws/call"
FRAME = 640 * 2  # 40ms PCM16 @16k

def load(p):
    with wave.open(str(p)) as w: return w.readframes(w.getnframes())

async def feed(ws, pcm, tail_ms=1000):
    for i in range(0, len(pcm), FRAME):
        await ws.send(pcm[i:i+FRAME]); await asyncio.sleep(0.04)
    for _ in range(tail_ms // 40):
        await ws.send(b"\x00" * FRAME); await asyncio.sleep(0.04)

async def main(mode):
    q = load(FIXTURES / "question.wav")
    interrupt = load(FIXTURES / "interrupt.wav")
    audio = bytearray(); timeline = []; t0 = None
    barge_sent = False; audio_after_interrupt = 0; interrupt_at = None; heard_interrupt = False; got_clear = False; leaked = 0

    async with websockets.connect(URL, max_size=None) as ws:
        async def reader():
            nonlocal audio, t0, audio_after_interrupt, heard_interrupt, got_clear, leaked
            async for msg in ws:
                el = (time.perf_counter()-t0)*1000 if t0 else 0
                if isinstance(msg, bytes):
                    audio += msg
                    if interrupt_at is not None and not heard_interrupt:
                        audio_after_interrupt += len(msg)
                        if got_clear: leaked += len(msg)   # arrived after the flush: would be heard
                    if not any(k[0]=="first_audio" for k in timeline):
                        timeline.append(("first_audio", el))
                else:
                    m = json.loads(msg)
                    if m["type"] == "interrupt" and interrupt_at is not None:
                        got_clear = True
                    if m["type"] == "transcript" and interrupt_at is not None:
                        heard_interrupt = True   # what follows is the reply to the interruption
                    timeline.append((m["type"], el, m.get("text","")[:70]))

        async def driver():
            nonlocal t0, barge_sent, interrupt_at
            await asyncio.sleep(0.3)
            t0 = time.perf_counter()
            await feed(ws, q, tail_ms=600)
            if mode == "bargein":
                # wait until the agent is actually speaking, then talk over it
                for _ in range(200):
                    # the greeting also produces audio, so wait for the reply to the question
                    if sum(1 for t in timeline if t[0]=="reply_text") >= 2: break
                    await asyncio.sleep(0.05)
                await asyncio.sleep(0.4)
                interrupt_at = len(audio)
                barge_sent = True
                timeline.append(("<<INTERRUPT INJECTED>>", (time.perf_counter()-t0)*1000, ""))
                await feed(ws, interrupt, tail_ms=1500)
            else:
                await asyncio.sleep(6)

        r = asyncio.create_task(reader())
        await driver()
        await asyncio.sleep(1.5)
        r.cancel()

    print(f"\n=== {mode} timeline ===")
    for t in timeline:
        label, el = t[0], t[1]
        extra = f"  {t[2]!r}" if len(t) > 2 and t[2] else ""
        print(f"  [{el:7.0f}ms] {label}{extra}")
    print(f"agent audio: {len(audio)} bytes = {len(audio)/2/24000:.2f}s @24k")
    if mode == "bargein":
        print(f"stale audio arriving AFTER interrupt injected (before its transcript): {audio_after_interrupt} "
              f"({audio_after_interrupt/2/24000:.2f}s)")
        print(f"  of which arrived AFTER the interrupt/flush event (would be heard): "
              f"{leaked} ({leaked/2/24000:.2f}s)")
    with wave.open(str(FIXTURES / f"agent_{mode}.wav"),"wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000); w.writeframes(bytes(audio))

asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "normal"))
