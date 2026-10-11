"""Course knowledge for the call: one read-only Pinecone search per turn.

The index uses Pinecone's integrated embedding (llama-text-embed-v2), so the
caller's sentence goes in as plain text and no embedding provider is involved.
Only reads happen here -- nothing in this module writes to the index.

Voice calls pay for every millisecond as dead air, so this is built to fail
open: a slow or failing search returns None and the turn proceeds without
course facts (the prompt then tells Claude to say it will confirm), rather than
stalling the caller.
"""

import asyncio
import logging
import math
import re
import time
from collections import Counter

from .config import settings

log = logging.getLogger(__name__)

# Fields worth reading aloud. `chunk_text` (the whole record, concatenated) and
# `faqs` (questions without answers) are left out on purpose.
_FIELDS = ["name", "program_duration", "description", "summary_curriculum_data", "prerequisites",
           "curriculum_skills", "projects", "career_pathways", "target_audiance",
           "mentors", "course_type"]

# Whatever the index holds, a rupee amount is never read to a caller: course
# pricing is for the sales team, and salary promises are a claim we will not
# make on a cold call.
_MONEY = re.compile(r"(₹|\bRs\.?\s?\d|\bINR\b|\bUSD\b|\$\s?\d|\bfees?\b|\bEMIs?\b|\bdiscount(?!ed)|\bscholarship|\brefund)", re.I)

_MIN_WORDS = 2  # a lone "yes" / "okay" needs no lookup; "python course" does


_STOP = set("""a an the and or of to in on for with about from at by is are was be do does did can could
would will i me my we you your it its this that these those what which who how when where why please
tell want like need know learn learning course courses program programs training class classes
interested interest hi hello hey yes no ok okay thanks thank have has any some more also just get give
there their they them more much many sure good great one two three tomorrow today""".split())


def _tokens(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9+#.]+", text.lower()) if w not in _STOP and len(w) > 1]


class CourseSearch:
    def __init__(self, index) -> None:
        self._index = index
        # In-memory copy of the index's course records (read once at startup).
        # Pinecone's embedding call from here has 1-1.6s spikes; a voice call
        # cannot wait for that, and answering with no facts is worse than a
        # cruder match. See _local().
        self._docs: list[dict] = []
        self._df: Counter = Counter()

    @classmethod
    async def connect(cls) -> "CourseSearch | None":
        if not settings.pinecone_api_key:
            return None
        try:
            search = await asyncio.to_thread(cls._open)
            await asyncio.to_thread(search._snapshot)
            # The first query on a cold connection costs >1s; pay it at startup.
            await search.context_for("course overview", warm=True)
            log.info("course search ready: index=%s namespace=%s",
                     settings.pinecone_index, settings.pinecone_namespace)
            return search
        except Exception:
            log.exception("course search unavailable; calls will run without it")
            return None

    @classmethod
    def _open(cls) -> "CourseSearch":
        from pinecone import Pinecone

        pc = Pinecone(api_key=settings.pinecone_api_key)
        host = pc.describe_index(settings.pinecone_index).host
        return cls(pc.Index(host=host))

    async def context_for(self, query: str, warm: bool = False) -> str | None:
        """Course facts relevant to what the caller just said, or None."""
        query = " ".join(query.split())
        if not warm and len(query.split()) < _MIN_WORDS:
            return None
        t0 = time.perf_counter()
        try:
            # The warm-up pays for DNS/TLS/model spin-up, so it gets longer.
            hits = await asyncio.wait_for(asyncio.to_thread(self._search, query),
                                          10.0 if warm else settings.rag_timeout)
        except (asyncio.TimeoutError, Exception) as exc:
            hits = self._local(query)
            log.warning("course search %s after %.0fms; used local copy instead: %d hits (%s)",
                        "timed out" if isinstance(exc, asyncio.TimeoutError) else f"failed ({type(exc).__name__})",
                        (time.perf_counter() - t0) * 1000, len(hits),
                        "; ".join(f"{h['name']} {h['score']:.1f}" for h in hits[:3]))
            return _render(hits) if hits else None
        if not warm:
            log.info("course search: %.0fms, %d hits (%s)",
                     (time.perf_counter() - t0) * 1000, len(hits),
                     "; ".join(f"{h['name']} {h['score']:.2f}" for h in hits[:3]))
        return _render(hits) if hits else None

    def _snapshot(self) -> None:
        """Read every course record once (read-only) for the local fallback."""
        ns = settings.pinecone_namespace
        ids = []
        for page in self._index.list(namespace=ns):
            ids += [x if isinstance(x, str) else x.id for x in page]
        docs = []
        for i in range(0, len(ids), 50):
            for v in self._index.fetch(ids=ids[i:i + 50], namespace=ns).vectors.values():
                m = dict(v.metadata or {})
                if str(m.get("visibility_status", "yes")).lower() != "yes":
                    continue
                doc = {k: m.get(k) for k in _FIELDS}
                name = _tokens(str(m.get("name", "")))
                rest = _tokens(" ".join(str(m.get(k, "")) for k in _FIELDS if k != "name"))
                doc["_name_tok"], doc["_tok"] = set(name), set(rest)
                docs.append(doc)
        self._docs = docs
        self._df = Counter(t for d in docs for t in d["_name_tok"] | d["_tok"])
        log.info("course snapshot: %d visible courses cached for fallback", len(docs))

    def _local(self, query: str) -> list[dict]:
        """Keyword match over the cached records; instant, no network."""
        q = set(_tokens(query))
        if not q or not self._docs:
            return []
        n = len(self._docs)
        scored = []
        for d in self._docs:
            s = 0.0
            for t in q:
                idf = math.log(1 + n / (1 + self._df.get(t, 0)))
                if t in d["_name_tok"]:
                    s += 3 * idf
                elif t in d["_tok"]:
                    s += idf
            if s > 0:
                scored.append((s, d))
        scored.sort(key=lambda x: -x[0])
        return [{**{k: d[k] for k in _FIELDS}, "score": s} for s, d in scored[:settings.rag_top_k]]

    async def keepalive(self) -> None:
        """Hold the connection open for the length of a call.

        Pinecone drops an idle connection after ~5s, and the next search then
        pays a fresh TLS handshake to us-east-1: measured 1.4s cold vs 0.35s
        warm. A call has long gaps (the caller thinks, the agent speaks), so
        without this most searches landed cold. Stats is a near-free read.
        """
        while True:
            try:
                await asyncio.to_thread(self._index.describe_index_stats)
            except Exception:
                log.debug("course search keepalive failed", exc_info=True)
            await asyncio.sleep(2.5)

    def _search(self, query: str) -> list[dict]:
        res = self._index.search(
            namespace=settings.pinecone_namespace,
            query={"inputs": {"text": query}, "top_k": settings.rag_top_k,
                   # Hidden courses exist in the index; never pitch them.
                   "filter": {"visibility_status": "yes"}},
            fields=_FIELDS,
        )
        out = []
        for h in res.result.hits:
            if h.score < settings.rag_min_score:
                continue
            out.append({**h.fields, "score": h.score})
        return out


def _clean(value, limit: int) -> str:
    """Join whole items up to `limit` chars; never cut an item in half."""
    items = value if isinstance(value, list) else [value]
    out, used = [], 0
    for i in items:
        s = str(i).strip()
        if not s or _MONEY.search(s):
            continue
        if out and used + len(s) + 2 > limit:
            break
        out.append(s)
        used += len(s) + 2
    return "; ".join(out)


def _sentences(text: str, limit: int) -> str:
    """Leading whole sentences up to `limit` chars, money-free."""
    out, used = [], 0
    for s in re.split(r"(?<=[.!?])\s+", str(text or "").strip()):
        if not s or _MONEY.search(s):
            continue
        if out and used + len(s) + 1 > limit:
            break
        out.append(s)
        used += len(s) + 1
    return " ".join(out)


def _render(hits: list[dict]) -> str:
    blocks = []
    for n, h in enumerate(hits):
        lines = [f"{'Best match' if n == 0 else 'Also relevant'}: {h.get('name', '?')}"]
        if h.get("program_duration"):
            lines.append(f"  Duration: {h['program_duration']}")
        fields = (
            ("What it covers", _sentences(h.get("summary_curriculum_data"), 520)),
            ("Skills you gain", _clean(h.get("curriculum_skills"), 650)),
            ("About", _sentences(h.get("description"), 300)),
            ("Prerequisites", _clean(h.get("prerequisites"), 300)),
            ("Projects", _clean(h.get("projects"), 220)),
            ("Careers", _clean(h.get("career_pathways"), 150)),
            ("Who it is for", _clean(h.get("target_audiance"), 150)),
        )
        # Detail for the best match; the others only need enough to be named.
        for label, text in fields[: (7 if n == 0 else 3)]:
            if text:
                lines.append(f"  {label}: {text}")
        blocks.append("\n".join(lines))
    return ("Course information for the caller's latest question.\n"
            "INSTRUCTION FOR THIS REPLY: answer the question now using the Best "
            "match below, in one or two sentences naming two or three specific "
            "topics. Do not begin by asking which course they mean; if another "
            "course also fits, mention it by name at the end as an option.\n\n"
            + "\n\n".join(blocks))
