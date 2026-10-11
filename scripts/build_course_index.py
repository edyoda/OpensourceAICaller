"""Load the course catalogue CSV into Pinecone, minus anything about money.

    python scripts/build_course_index.py CSV              # dry run: stats + samples
    python scripts/build_course_index.py CSV --apply      # create index, upsert

The agent talks to prospects on the phone, so the index must never be able to
answer a pricing question -- it should have nothing to retrieve. That is done
by omission, not by a prompt instruction:

  * pricing columns are never read (total_cost, cost_div, discount_*, USP_data,
    schema_jsonld which carries offers, meta_*, thumbnails, internal id lists);
  * hidden courses (visibility_status = no) are left out;
  * every remaining free-text item (FAQ, bullet, sentence) that talks about fees,
    EMI, discounts, scholarships, refunds etc. is dropped. Words like "cost
    optimisation" or "house price prediction" are syllabus topics, not pricing,
    and are kept.

Embedding happens inside Pinecone (integrated inference), so no separate
embedding-provider key is needed.
"""

import csv
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import settings  # noqa: E402

# Money. Deliberately commercial-only: topic words ("cost optimisation",
# "price prediction", "Pricing Models" as a chapter) must survive.
MONEY = re.compile(
    r"(₹|\bRs\.?\s?\d|\bINR\b|\bUSD\b|\$\s?\d"
    r"|\bfees?\b|\btuition\b|\bdiscount|\bscholarship|\bcoupon|\brefund"
    r"|\binstal+ments?\b|\bno[- ]cost\b"
    # EMI is also electromagnetic interference in PCB courses; only payment phrasing counts.
    r"|\b(easy|monthly|low|zero|no[- ]cost|flexible) EMIs?\b|\bEMIs? (option|plan|facilit|available|payment|starting|from|of \d)"
    r"|\bpayment (plan|option|method|mode|gateway|terms|schedule|structure)"
    r"|\bpay (in|monthly|later|after|upfront)\b|\bfinanc(e|ing) (option|plan)"
    r"|\bper month\b|/\s?month\b|\baffordable\b|\bcheap\b|\bbudget[- ]friendly\b"
    r"|\bhow much (does|is|do|will|would)\b[^?]{0,40}\b(cost|course|program|track|fee|charge)"
    r"|\b(cost|price|pricing|charges?) (of|for) (the |this |a |an )?(course|program|programme|track|training|certification|bootcamp|batch)"
    r"|\bwhat('s| is| are) the (price|pricing|cost|charges?|fees?)\b"
    r"|\bis there (a|any) (cost|charge|fee)\b|\bfree of cost\b|\bwaiver\b)",
    re.I,
)
# FAQ questions are held to a stricter standard: any cost/price word at all.
MONEY_Q = re.compile(r"\b(cost|costs|price|prices|pricing|charges?|fees?|pay|paid|payment|emi)\b", re.I)

MAX_CHUNK = 1400  # characters; well under the embedding model's limit


def jload(s: str | None):
    try:
        return json.loads(s) if s and s.strip() else None
    except ValueError:
        return None


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(s))).strip()


def sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+|\n+", clean(text)) if s]


def safe(text: str) -> bool:
    return not MONEY.search(text)


def flat(node) -> list[str]:
    """Every string leaf of a JSON value, in order."""
    if node is None:
        return []
    if isinstance(node, str):
        return [clean(node)] if node.strip() else []
    if isinstance(node, dict):
        return [t for v in node.values() for t in flat(v)]
    if isinstance(node, (list, tuple)):
        return [t for v in node for t in flat(v)]
    return [clean(str(node))]


class Stats(Counter):
    pass


def keep_items(items: list[str], stats: Stats, label: str) -> list[str]:
    out = []
    for it in items:
        if safe(it):
            out.append(it)
        else:
            stats[f"dropped {label}"] += 1
    return out


def build(row: dict, stats: Stats) -> list[dict]:
    name = clean(row["name"])
    base = {
        "course_name": name,
        "course_url": row["course_url"].strip(),
        "course_type": row["course_type"].strip(),
        "duration": (row["duration"] or "").strip(),
        "batch_time": clean(row["batch_time"] or ""),
    }
    sections: list[tuple[str, list[str]]] = []

    # --- overview ---------------------------------------------------------
    overview = [f"Course: {name}."]
    journey = clean(row["journey_name"] or "")
    if journey and safe(journey):
        overview.append(f"Learning journey: {journey}.")
    elif journey:
        stats["dropped journey_name"] += 1
    # Micro-degree durations are weeks (the text says "24-week journey",
    # "4-week program"). AI-lab values (45-180) carry no unit, so none is stated.
    if base["duration"].isdigit() and base["course_type"] == "micro_degree":
        n = int(base["duration"])
        overview.append(f"Duration: {n} week{'s' if n != 1 else ''}.")
    if base["batch_time"]:
        overview.append(f"Live batch timings: {base['batch_time']}.")
    for field in ("curriculum_subheading", "description"):
        overview += keep_items(sentences(row[field] or ""), stats, field)
    pre = keep_items(flat(jload(row["prerequisites"])), stats, "prerequisites")
    if pre:
        overview.append("Prerequisites: " + "; ".join(pre))
    aud = keep_items(flat(jload(row["target_audiance"])), stats, "audience")
    if aud:
        overview.append("Who it is for: " + ", ".join(aud) + ".")
    car = keep_items(flat(jload(row["career_pathways"])), stats, "career")
    if car:
        overview.append("Career pathways: " + ", ".join(car) + ".")
    tools = keep_items(flat(jload(row["tools"])), stats, "tools")
    if tools:
        overview.append("Tools covered: " + ", ".join(tools) + ".")
    sections.append(("overview", overview))

    # --- what you learn / syllabus / projects -------------------------------
    skills = keep_items(flat(jload(row["curriculum_skills"])), stats, "skills")
    if skills:
        sections.append(("skills", [f"{name} - skills you will gain:"] + skills))

    curriculum = keep_items(flat(jload(row["curriculum_data"])), stats, "curriculum")
    if curriculum:
        sections.append(("curriculum", [f"{name} - curriculum topics:"] + curriculum))

    projects = []
    for p in jload(row["projects"]) or []:
        text = ": ".join(flat(p)[:2]) if isinstance(p, dict) else " ".join(flat(p))
        if text and safe(text):
            projects.append(text)
        elif text:
            stats["dropped projects"] += 1
    if projects:
        sections.append(("projects", [f"{name} - hands-on projects:"] + projects))

    # --- FAQs: whole Q&A pairs, dropped together ---------------------------
    faqs = []
    for qa in jload(row["faqs"]) or []:
        if not isinstance(qa, dict):
            continue
        q, a = clean(qa.get("question", "")), clean(qa.get("answer", ""))
        if not q or not a:
            continue
        if MONEY_Q.search(q) or MONEY.search(a) or MONEY.search(q):
            stats["dropped faq"] += 1
            continue
        faqs.append(f"Q: {q} A: {a}")
    for i in range(0, len(faqs), 1):
        sections.append(("faq", [f"{name} - FAQ.", faqs[i]]))

    # --- pack into records ---------------------------------------------------
    records = []
    for section, parts in sections:
        buf, n = "", 0
        header = parts[0] if section in ("skills", "curriculum", "projects") else ""
        for part in parts:
            if buf and len(buf) + len(part) + 1 > MAX_CHUNK:
                records.append((section, buf)); buf = header
            buf = f"{buf} {part}".strip()
        if buf and buf != header:
            records.append((section, buf))
    out = []
    for i, (section, text) in enumerate(records):
        out.append({"_id": f"{row['course_id']}-{section}-{i}", "chunk_text": text[:MAX_CHUNK + 400],
                    "section": section, **base})
    return out


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        sys.exit("usage: build_course_index.py CSV [--apply]")
    csv.field_size_limit(10**9)
    rows = list(csv.DictReader(open(args[0], encoding="utf-8-sig")))

    stats = Stats(total_rows=len(rows))
    visible = [r for r in rows if (r["visibility_status"] or "").strip().lower() == "yes"]
    stats["hidden courses skipped"] = len(rows) - len(visible)
    best: dict[str, dict] = {}
    for r in visible:  # a course_id may repeat; keep the fuller row
        cur = best.get(r["course_id"])
        if not cur or sum(bool(v) for v in r.values()) > sum(bool(v) for v in cur.values()):
            best[r["course_id"]] = r
    stats["duplicate course_id skipped"] = len(visible) - len(best)

    records = []
    for r in best.values():
        records += build(r, stats)
    stats["courses indexed"] = len(best)
    stats["records"] = len(records)
    stats["by section: " + "/".join(sorted({x['section'] for x in records}))] = 0

    print("== stats")
    for k, v in stats.items():
        if v or not k.startswith("by section"):
            print(f"  {k:32} {v}")
    print("  section counts:", dict(Counter(x["section"] for x in records)))
    sizes = [len(x["chunk_text"]) for x in records]
    print(f"  chunk chars: avg {sum(sizes)//len(sizes)}, max {max(sizes)}")

    leak = [x for x in records if MONEY.search(x["chunk_text"])]
    print(f"  records matching the money filter after build: {len(leak)}")
    cols = ("total_cost", "cost_div", "discount_percentage", "discount_cycle_start")
    print(f"  pricing columns read: none ({', '.join(cols)} are never touched)")

    print("\n== sample records")
    seen = set()
    for x in records:
        if x["section"] not in seen:
            seen.add(x["section"])
            print(f"\n[{x['_id']}] {x['course_name']} | {x['section']} | {len(x['chunk_text'])} chars")
            print("  " + x["chunk_text"][:520])

    if "--apply" not in sys.argv:
        print("\nDRY RUN -- nothing sent to Pinecone. Re-run with --apply.")
        return

    from pinecone import Pinecone

    if not settings.pinecone_api_key:
        sys.exit("missing in .env: PINECONE_API_KEY")
    pc = Pinecone(api_key=settings.pinecone_api_key)
    name = settings.pinecone_index
    if not pc.has_index(name):
        print(f"\ncreating index {name!r} ({settings.pinecone_cloud}/{settings.pinecone_region}, "
              f"{settings.pinecone_embed_model})")
        pc.create_index_for_model(
            name=name, cloud=settings.pinecone_cloud, region=settings.pinecone_region,
            embed={"model": settings.pinecone_embed_model, "field_map": {"text": "chunk_text"}},
        )
    index = pc.Index(name)
    # Rebuild from scratch so a course removed from the CSV is removed here too.
    try:
        index.delete(delete_all=True, namespace=settings.pinecone_namespace)
    except Exception as exc:  # empty/new namespace 404s on some versions
        print(f"  (namespace clear skipped: {type(exc).__name__})")
    for i in range(0, len(records), 90):  # integrated-inference batch limit is 96
        index.upsert_records(records=records[i:i + 90], namespace=settings.pinecone_namespace)
        print(f"  upserted {min(i + 90, len(records))}/{len(records)}")
    print("done. Pinecone indexes asynchronously; give it ~10-20s before querying.")


if __name__ == "__main__":
    main()
