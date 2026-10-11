from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    sarvam_api_key: str
    anthropic_api_key: str | None = None  # SDK also reads ANTHROPIC_API_KEY itself
    # User-scoped keys (sk-ant-usr-...) belong to no workspace, so every request
    # must name one. Workspace-scoped keys do not need this.
    anthropic_workspace_id: str | None = None

    # --- Speech in -------------------------------------------------------
    stt_model: str = "saaras:v3"
    language_code: str = "en-IN"
    mic_sample_rate: int = 16000
    # Sustained speech required before an interruption counts, so the agent's
    # own voice leaking into the mic does not cut it off mid-sentence.
    barge_in_min_speech_frames: str = "10"

    # --- Speech out ------------------------------------------------------
    tts_model: str = "bulbul:v3"
    tts_speaker: str = "ritu"  # bulbul:v3 voice; v2 voices (anushka, meera...) are rejected
    # 24k divides evenly into the 48k most devices run at, so the browser can
    # play it without forcing its AudioContext onto an odd rate -- which is
    # what degrades echo cancellation. Sarvam allows 8k/16k/22.05k/24k only.
    tts_sample_rate: int = 24000

    # --- Brain -----------------------------------------------------------
    # Haiku answers ~1.1s faster than Opus 5 on this prompt (841ms vs 1948ms
    # time-to-first-token, measured), and this agent's job -- name a course,
    # refuse to invent details, capture an email -- does not need more. Switch
    # to "claude-opus-5" if the script grows tools or real reasoning; you pay
    # for it directly in dead air.
    llm_model: str = "claude-haiku-4-5"
    llm_effort: str = "low"  # only applies to Opus/Fable-class models
    max_tokens: int = 1024

    # --- CRM -------------------------------------------------------------
    # Zoho's MCP server URL carries its API key in the path, so the URL itself
    # is the credential -- it belongs in .env, never in source. Regenerate it
    # from the Zoho MCP dashboard (Connect tab) if it ever leaks.
    zoho_mcp_url: str | None = None

    # --- Course knowledge (Pinecone) -------------------------------------
    # When a key is set, each turn retrieves the most relevant course chunks and
    # the hardcoded `courses` list below is ignored. Built by
    # scripts/build_course_index.py, which leaves pricing out of the index.
    pinecone_api_key: str | None = None
    pinecone_index: str = "llama-edyoda-course-search"  # read-only: this app never writes to it
    pinecone_namespace: str = "courses"
    pinecone_cloud: str = "aws"
    pinecone_region: str = "us-east-1"
    pinecone_embed_model: str = "llama-text-embed-v2"
    rag_top_k: int = 3
    rag_min_score: float = 0.25  # below this a hit is noise, not an answer
    rag_timeout: float = 0.8  # seconds; past this the cached local copy answers instead

    # --- Exotel ----------------------------------------------------------
    exotel_api_key: str | None = None
    exotel_api_token: str | None = None
    exotel_sid: str | None = None
    exotel_subdomain: str = "api.exotel.com"  # api.in.exotel.com for Mumbai
    exotel_caller_id: str | None = None  # your ExoPhone
    exotel_app_id: str | None = None     # App Bazaar flow holding the Voicebot applet
    # A public endpoint is reachable by anyone who learns the URL, and every
    # stray session burns Sarvam + Claude quota. Exotel names the account on the
    # `start` event, so we can hang up on anyone else. Empty = allow all.
    exotel_allowed_accounts: str = ""
    # `mark` is a diagnostic, not a requirement. It goes out mid-playback (as
    # soon as Sarvam finishes generating) and is a prime suspect for Exotel
    # closing the stream, so it stays off unless explicitly enabled.
    exotel_send_marks: bool = False

    # Spoken the moment the call connects. An outbound call that opens with
    # silence gets hung up on. This is also the natural place to disclose that
    # the caller reached a bot -- several jurisdictions require it.
    # Spoken the instant the call connects. On an outbound call the callee did
    # not ask to be called, so this has ~4 seconds to say who is calling, why,
    # and hand them the conversation. It also carries the AI disclosure, which
    # several jurisdictions require and which costs nothing here.
    greeting: str = (
        "Hi, this is the EdYoda assistant, an AI helper calling about our courses. "
        "Which course would you like to know about?"
    )

    # The agent may only discuss what is listed here. Left empty it will not
    # name any course at all -- inventing a syllabus or a price on a sales call
    # to a real customer is far worse than admitting it needs to check.
    courses: str = (
        "AI Agent Development\n"
        "Drone Engineering"
    )

    system_prompt: str = (
        "You are an outbound phone agent for EdYoda, an online learning company. "
        "You called this person to find out which course they are interested in "
        "and to answer questions about it.\n"
        "You are speaking out loud. Keep every reply to one or two short "
        "sentences of plain conversational language. Never use markdown, bullet "
        "points, emoji, or URLs. Write numbers, dates and prices the way they "
        "are spoken.\n"
        "Your goal for the call: find out which course they want to know about, "
        "answer what you can, and capture the best email or phone number for "
        "follow-up. Ask exactly one question at a time, then stop and listen.\n"
        "Only discuss courses listed below. Never invent a course, price, "
        "duration, start date, or discount -- if you do not know, say you will "
        "have someone confirm and ask for their email. If they are not "
        "interested or ask you to stop calling, apologise briefly, say you will "
        "remove them from the list, and end the conversation warmly."
    )

    @property
    def full_system_prompt(self) -> str:
        if self.pinecone_api_key:
            # Course facts arrive per turn (app/rag.py), so no catalogue here.
            return (self.system_prompt + "\n\nCourse facts for the caller's latest "
                    "question are attached below when we have them. State only what "
                    "they say; if the answer is not there, say you will have someone "
                    "confirm. When course facts are attached, ANSWER the question from "
                    "the best match first: name two or three specific topics or "
                    "skills in one or two sentences. Never reply with only a "
                    "clarifying question. If several courses fit, answer for the "
                    "best match, then briefly offer one alternative by name. "
                    "You have no pricing, fee, EMI, discount or salary "
                    "information: never quote or estimate any, say the team will "
                    "share it.")
        if not self.courses.strip():
            return (self.system_prompt + "\n\nNo course catalogue is loaded, so "
                    "you cannot name or describe any specific course. Find out "
                    "what they are interested in, take their email, and promise "
                    "a follow-up with details.")
        return f"{self.system_prompt}\n\nCourses you may discuss:\n{self.courses}"


settings = Settings()
