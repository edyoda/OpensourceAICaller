# OpenSource AI Caller

Speak into the browser mic → **Sarvam STT** transcribes → **Claude** answers →
**Sarvam TTS** speaks the answer back, all streamed over one WebSocket.

```
browser mic ──PCM16 16k──▶ /ws/call ──b64──▶ Sarvam STT ws (saaras:v3, VAD on)
                              │                    │ transcript
                              │                    ▼
                              │              Claude (streaming)
                              │                    │ sentence chunks
                              │                    ▼
browser speaker ◀─PCM16 24k── │ ◀──audio──── Sarvam TTS ws (bulbul:v3, linear16)
```

A voice agent you can talk to in the browser or reach over a phone line
(Exotel). Bring your own Sarvam and Anthropic keys.

## What you get

- **Browser client with push-to-talk** (hold the button or the space bar), an
  open-mic mode, live transcript and a level ring. Good for demos over a
  screen share.
- **Barge-in**: talk over the agent and it stops mid-word.
- **Phone calls** through Exotel's voicebot applet (`/ws/exotel`).
- **Optional course knowledge (RAG)** from a Pinecone index, with a local
  in-memory fallback so a slow search never leaves the agent without facts.
- **Optional Zoho CRM client** that calls MCP tools directly, without a model
  in the loop.

## Run it

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env      # fill in SARVAM_API_KEY and ANTHROPIC_API_KEY
.venv/bin/uvicorn app.main:app --reload
```

Open http://localhost:8000 and hit **Start call**. Mic access needs `localhost` or HTTPS.

**Wear headphones.** Browser echo cancellation is on, but on speakers the agent
still hears itself and barges in on its own reply.

## The four files that matter

| File | Job |
|---|---|
| `app/session.py` | The call. Owns four concurrent legs (mic pump, STT reader, TTS reader, keepalive) and the barge-in logic. |
| `app/llm.py` | Claude turn + the sentence chunker that decides when a fragment is worth speaking. |
| `app/tts.py` | One long-lived Sarvam TTS socket, with per-turn boundaries so a cancelled reply can be dropped. |
| `static/index.html` | Mic capture, jitter-buffered playback, transcript UI. |

## Four things that make or break the feel

**1. Chunk the LLM output, don't wait for it.** `llm.py` splits Claude's token
stream at sentence boundaries (`.!?` and the Devanagari `।`) with a 15-char
minimum (20 for the first chunk, which may also break at a clause), and forwards each chunk to TTS immediately. Time-to-first-audio
becomes "Claude's first sentence" instead of "Claude's whole answer". The
minimum length is what stops `"Sure."` from becoming its own clipped
utterance.

**2. Barge-in has to stop three things, not one.** The LLM task finishes
generating seconds before the caller stops *hearing* the reply, so "is the
agent talking?" cannot be answered by looking at that task — it has to check
whether TTS is still voicing a turn. Getting this wrong is silent: the agent
talks straight through the interruption. On `START_SPEECH` we cancel the LLM,
cancel the TTS turn, and flush the browser's buffer.

**3. Cancelling TTS needs a boundary marker.** Sarvam has no "cancel current
synthesis" call, and reopening the socket per turn costs a handshake every
reply. So the socket stays open and turns are delimited: a run of `convert()`
calls closed by `flush()`, which Sarvam answers with a `final` event. On
interruption `tts.py` marks the turn dead and drops its audio until `final`
arrives — the socket stays warm for the next turn. Meanwhile the browser gets
`{"type":"interrupt"}` and flushes its jitter buffer, so the voice stops
mid-word instead of draining a second of buffered speech.

**4. Interrupted turns must be rewritten in history.** `commit_interruption()`
truncates the assistant message to what the caller actually heard. Skip this
and Claude's next turn confidently references three sentences nobody heard.
(Caveat: this is exact only when the interruption lands mid-generation. If
Claude already finished, we truncate to what was synthesized, which slightly
over-counts what the caller actually heard.)

## Room tone

`static/ambience.js` lays a quiet office bed under the call: a pink-noise floor
plus a speech-band layer that swells slowly, so it reads as voices somewhere
behind the agent rather than as hiss. Silence between turns sounds like a
dropped line, and the bed also masks how long the model takes to answer.

It runs on its own audio graph, not through the playback worklet, so a barge-in
flush cuts the agent's voice without punching a hole in the ambience. Levels
(`ROOM_LEVEL`, `MURMUR_LEVEL`) were set by rendering the same DSP offline and
measuring: ~26 dB under the speech, where real call ambience sits. Toggle it
with the **room tone** checkbox.

One thing to decide before this ships to real callers: ambience plus a natural
voice means many people will assume they reached a human. Several jurisdictions
(California's bot-disclosure law among them) require a bot to say so. Cheapest
fix is one line in the greeting — it costs nothing and settles the question.

## Wire protocol (`/ws/call`)

| Direction | Payload |
|---|---|
| → server | binary: PCM16 mono 16 kHz, 40 ms frames |
| ← client | binary: PCM16 mono 24 kHz (agent voice) |
| ← client | `{"type":"ready","sample_rate":24000}` |
| ← client | `{"type":"transcript","role":"user","text":…}` |
| ← client | `{"type":"reply_text","text":…}` — one spoken chunk |
| ← client | `{"type":"speech_start"}` / `{"type":"speech_end"}` |
| ← client | `{"type":"interrupt"}` — flush playback now |

## Course knowledge (optional)

Set `PINECONE_API_KEY` and point `PINECONE_INDEX` / `PINECONE_NAMESPACE` at an
index that uses integrated embeddings with a `chunk_text` field. Each turn the
caller's sentence is searched and the top matches are added to the prompt for
that turn only. The app only **reads** from the index. Without a key, the
agent falls back to the `courses` list in `app/config.py`.

`scripts/build_course_index.py` shows one way to build such an index from a CSV,
and how to keep pricing out of it by omission.

## Responsible use

This software places and answers phone calls and speaks as an AI. You are
responsible for complying with the laws that apply to you, including consent,
do-not-call registries (for example TRAI/DND in India, TCPA in the US),
call-recording and AI-disclosure rules. The default greeting discloses that the
caller is an AI; keep it. Do not use it to impersonate people or to call anyone
who has not agreed to be contacted.

## Security notes

- `/ws/call` and `/ws/exotel` are unauthenticated by default. Put them behind
  your own auth or a secret path before exposing them: every open session spends
  your Sarvam and Anthropic quota.
- Keep API keys in `.env` or a secret manager. `.env`, token files and CSV
  exports are in `.gitignore`.

## License

MIT, see `LICENSE`.

## Tuning

Everything is in `app/config.py`.

- `language_code` — `hi-IN`, `ta-IN`, `bn-IN`, … Sarvam covers 22 Indian languages.
  Set `"unknown"` on STT for auto-detect; TTS still needs a concrete language.
- `llm_effort` — `"low"` keeps voice turns snappy. Raise to `"medium"`/`"high"`
  if the agent has to reason over policies or tool results; you pay for it in
  time-to-first-word.
- `tts_speaker` — **must be a v3 voice.** The SDK's own default (`anushka`) is a
  bulbul:v2 voice and the socket rejects it with a 422. Valid v3 voices:
  `aditya, ritu, ashutosh, priya, neha, rahul, pooja, rohan, simran, kavya, amit,
  dev, ishita, shreya, ratan, varun, manan, sumit, roopa, kabir, aayan, shubh,
  advait, anand, tanya, tarun, sunny, mani, gokul, vijay, shruti, suhani, mohit,
  kavitha, rehan, soham, rupali, niharika`.
- `system_prompt` — written for speech: no markdown, no URLs, one question at a
  time. This matters more than the model choice for how the call sounds.

## Phone calls (Exotel)

`/ws/exotel` speaks Exotel's AgentStream voicebot protocol. `CallSession` is
transport-agnostic (`app/transport.py`), so the phone leg reuses the same
pipeline as the browser — no forked logic.

**No resampling anywhere.** Exotel does raw PCM16 at 8k/16k/24k; Sarvam STT
takes 8k/16k and its TTS emits 8k/16k/22.05k/24k. The rate is read from the
`start` event and Sarvam is configured to match, so the audio is never touched.
Prefer 16k in the applet — at 8k the STT tail measured 2.5× longer.

`clear` is Exotel's playout flush, the exact analogue of the browser's
`interrupt`, so barge-in maps over unchanged. Outbound audio is repacketized to
multiples of 320 bytes, which Exotel enforces and Sarvam's chunks don't respect.

### Setup

1. In App Bazaar, build a flow with a **Voicebot applet** (bidirectional) whose
   URL is `wss://<your-host>/ws/exotel`. Exotel rejects self-signed certs, so
   use a real cert or a tunnel (`ngrok http 8001`).
2. Put the flow's id in `.env` as `EXOTEL_APP_ID`.
3. Dial:

```bash
python scripts/call.py +9198XXXXXXXX          # dry run, prints the request
python scripts/call.py +9198XXXXXXXX --yes    # actually dials
```

The wss URL lives in the applet, not in the API call — the call request only
names the flow.

## Testing

**Headless, no browser or mic needed.** Fixture WAVs are fed into `/ws/call` as
40 ms PCM frames, exactly the framing the mic worklet produces:

```bash
./scripts/e2e.sh            # browser leg, one full turn
./scripts/e2e.sh bargein    # browser leg, talk over the agent

# phone leg, without spending a call -- speaks the real Exotel protocol
PORT=8078 python scripts/exotel_sim.py 8000        # 8k, one turn
PORT=8078 python scripts/exotel_sim.py 16000       # 16k, one turn
PORT=8078 python scripts/exotel_sim.py 8000 barge  # interrupt mid-reply
```

`exotel_sim.py` asserts the rules Exotel enforces — outbound chunks a multiple
of 320 bytes, `stream_sid` echoed on every message — and prints
`PROTOCOL VIOLATIONS` if either is broken.

Run `bargein` after any change to turn or cancellation logic — a broken
barge-in is silent, and this is the test that caught it. The agent's reply is
written to `fixtures/agent_<mode>.wav` if you want to listen to it.

**In the browser** (the only part these tests can't cover — mic capture, the
AudioWorklets, playback):

```bash
.venv/bin/uvicorn app.main:app --reload
```

Open http://localhost:8000, click **Start call**, wear headphones.

## Verified against the live API

Tested headlessly end to end: Sarvam TTS synthesizes a question, the audio is
fed into `/ws/call` as 40 ms PCM frames exactly like the mic worklet produces,
and the agent's reply audio comes back out.

```
[   0ms] ready
[3133ms] transcript   'What time does my Python batch start tomorrow?'
[5210ms] reply_text   'I can check that for you — could you give me the email…'
[5447ms] first_audio
[5883ms] << caller talks over the agent >>
[6198ms] interrupt                       ← 315ms to stop speaking
[8842ms] transcript   'No wait, I meant the data science one.'
[11181ms] reply_text  "No problem, the data science batch it is — what's the…"
```

Both turns land, context carries across the interruption, and the TTS socket
stays warm throughout.

**Latency budget** (caller stops talking → first word heard): **~3.1 s**

| Stage | Cost |
|---|---|
| Sarvam VAD `END_SPEECH` → transcript | ~250 ms |
| **Claude first sentence** | **~2.1 s** |
| Sarvam TTS → first audio chunk | ~250–500 ms |

Claude is the bottleneck, and it's the one knob worth thinking about. Measured
time-to-first-token, 3 runs each:

| Setting | TTFT (min/avg/max) |
|---|---|
| `claude-opus-5`, adaptive thinking, effort `low` (**current default**) | 1938 / 2077 / 2255 ms |
| `claude-opus-5`, thinking disabled, effort `low` | 1295 / 1561 / 1744 ms |
| `claude-haiku-4-5` | 719 / 764 / 836 ms |

Fast mode (`speed="fast"`, up to 2.5× output throughput on Opus 5) is **not
enabled on this org** — it returns `429 rate limit of 0`. Worth asking Anthropic
to turn on if you stay on Opus.

If 3 s of dead air is too much, prefer swapping the model over disabling
thinking: with thinking off, Opus 5 occasionally writes a tool call into its
visible text instead of emitting a `tool_use` block, which will bite you the
moment you add tools. A cheap middle path is a filler line ("let me check
that") spoken the instant the transcript lands, which covers the gap without
touching the model.

**Not yet verified:** the browser half — mic capture, the AudioWorklets, and
playback. That needs a human to click Start call.

## Where to go next

- **Tools** — an agent that only talks is a demo. Add `tools=[...]` in
  `llm.py:respond` and switch to `client.beta.messages.tool_runner` so Claude
  can check a batch schedule or create a ticket mid-call. Speak a filler line
  ("let me check that") before a slow tool call.
- **Interim transcripts** — this uses the GA `saaras:v3` socket, which emits one
  transcript per utterance. Sarvam's `saaras:v3-realtime` endpoint
  (`speech_to_text_realtime_streaming`) streams partials, letting you start the
  LLM before the caller finishes.
- **Real phone calls** — swap the browser leg for an Exotel/Twilio media-stream
  WebSocket. `session.py` is transport-agnostic; you add 8 kHz μ-law ⇄ 16 kHz
  PCM resampling at the edges and keep everything else.
