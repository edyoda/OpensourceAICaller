"""Direct Zoho CRM access -- no LLM in the loop.

Zoho's MCP endpoint is plain JSON-RPC over HTTPS. Handing those tools to a
model costs ~51k tokens per request just to describe them, which is absurd for
a write whose shape you already know. This calls the same tools directly, for
nothing.

Rule of thumb: if you know the module, the record and the values, use this. Use
scripts/zoho_crm.py only for questions you cannot express as a query yourself.

    from app.crm import ZohoCRM

    crm = ZohoCRM()
    crm.coql("select Last_Name, Email from Leads limit 5")
    crm.log_call("4321335000309199002", "no answer")
    crm.update("Leads", [{"id": "...", "Lead_Status": "Contacted"}, ...])
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from .config import settings

# Zoho caps a write batch at 100 records; the API rejects 101 outright.
MAX_BATCH = 100


class ZohoError(RuntimeError):
    pass


class ZohoCRM:
    """One MCP session, reused across calls."""

    def __init__(self, token: str | None = None, timeout: float = 60.0) -> None:
        if not settings.zoho_mcp_url:
            raise ZohoError("ZOHO_MCP_URL is not set")
        self._url = settings.zoho_mcp_url
        self._client = httpx.Client(timeout=timeout)
        self._id = 0
        self._headers = {
            "Authorization": f"Bearer {token or _bearer()}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        self._handshake()

    # --- plumbing --------------------------------------------------------

    def _handshake(self) -> None:
        r = self._post({"jsonrpc": "2.0", "id": self._next(), "method": "initialize",
                        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                   "clientInfo": {"name": "edyoda-call-agent",
                                                  "version": "1"}}})
        # The session id only appears on the initialize response; every later
        # call has to carry it or the server treats us as a new client.
        if sid := r.headers.get("mcp-session-id"):
            self._headers["mcp-session-id"] = sid
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _next(self) -> int:
        self._id += 1
        return self._id

    def _post(self, payload: dict) -> httpx.Response:
        r = self._client.post(self._url, headers=self._headers, json=payload)
        r.raise_for_status()
        return r

    def call(self, tool: str, arguments: dict) -> Any:
        """Invoke one MCP tool and return the Zoho payload inside it."""
        r = self._post({"jsonrpc": "2.0", "id": self._next(), "method": "tools/call",
                        "params": {"name": tool, "arguments": arguments}})

        body = r.text
        if body.startswith("event:"):  # server may answer as SSE
            body = [l[6:] for l in body.splitlines() if l.startswith("data: ")][-1]
        envelope = json.loads(body)

        if "error" in envelope:
            raise ZohoError(f"{tool}: {envelope['error']}")
        result = envelope["result"]
        if result.get("isError"):
            raise ZohoError(f"{tool}: {result.get('content')}")

        structured = result.get("structuredContent")
        payload = (structured.get("data", structured) if structured is not None
                   else json.loads(result["content"][0]["text"]))

        # Zoho answers with a bare {"message": ...} in two opposite cases: a
        # rejected argument (isError stays unset, so it would otherwise look
        # like an empty success) and an empty related list, which really is a
        # success. The wording is the only thing separating them.
        if isinstance(payload, dict) and set(payload) == {"message"}:
            if "success" in payload["message"].lower():
                return {}
            raise ZohoError(f"{tool}: {payload['message']}")
        return payload

    # --- reads -----------------------------------------------------------

    def coql(self, select_query: str) -> list[dict]:
        """Run a COQL SELECT. Cheapest way to pull exactly the fields you want."""
        out = self.call("ZohoCRM_executeCOQLQuery", {"body": {"select_query": select_query}})
        return (out or {}).get("data", [])

    def get_records(self, module: str, fields: list[str], per_page: int = 200,
                    page: int = 1) -> list[dict]:
        out = self.call("ZohoCRM_getRecords", {
            "path_variables": {"module": module},
            "query_params": {"fields": ",".join(fields),
                             "per_page": per_page, "page": page},
        })
        return (out or {}).get("data", [])

    def fields(self, module: str) -> list[dict]:
        """Field metadata -- the authoritative source for API names."""
        out = self.call("ZohoCRM_getFields",
                        {"query_params": {"module": module, "include": "all"}})
        return (out or {}).get("fields", [])

    # --- writes ----------------------------------------------------------

    def _write(self, tool: str, module: str, records: list[dict],
               extra_body: dict | None = None) -> list[dict]:
        """Batch a write, 100 records at a time, returning per-record results."""
        results: list[dict] = []
        for start in range(0, len(records), MAX_BATCH):
            body: dict[str, Any] = {"data": records[start:start + MAX_BATCH]}
            if extra_body:
                body.update(extra_body)
            out = self.call(tool, {"path_variables": {"module": module},
                                   "body": body})
            results.extend((out or {}).get("data", []))
        return results

    def create(self, module: str, records: list[dict]) -> list[dict]:
        return self._write("ZohoCRM_createRecords", module, records)

    def update(self, module: str, records: list[dict],
               trigger: list[str] | None = None) -> list[dict]:
        """Update records. Every record must carry its `id`.

        `trigger=[]` suppresses workflows, approvals and blueprint transitions --
        worth passing on a bulk edit, where a status-change rule would otherwise
        fire once per record.
        """
        missing = [i for i, r in enumerate(records) if not r.get("id")]
        if missing:
            raise ZohoError(f"records at {missing} have no 'id'")
        extra = {"trigger": trigger} if trigger is not None else None
        return self._write("ZohoCRM_updateRecords", module, records, extra)

    def upsert(self, module: str, records: list[dict],
               duplicate_check_fields: list[str] | None = None) -> list[dict]:
        extra = {"duplicate_check_fields": duplicate_check_fields} \
            if duplicate_check_fields else None
        return self._write("ZohoCRM_upsertRecords", module, records, extra)

    # --- convenience -----------------------------------------------------

    def log_call(self, lead_id: str, note: str, when: float | None = None) -> dict:
        """Append one timestamped line to a Lead's Description."""
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(when or time.time()))
        existing = self.coql(
            f"select Description from Leads where id = {lead_id}")
        prior = (existing[0].get("Description") or "").strip() if existing else ""
        line = f"[{stamp}] {note}"
        text = f"{prior}\n{line}".strip()
        return self.update("Leads", [{"id": lead_id, "Description": text}])[0]

    # --- notes -----------------------------------------------------------

    NOTE_FIELDS = "Note_Title,Note_Content,Created_Time,Modified_Time,Owner,Parent_Id"

    def notes(self, lead_id: str, module: str = "Leads") -> list[dict]:
        """Every note attached to one record, newest first."""
        out = self.call("ZohoCRM_getRelatedRecords", {
            "path_variables": {"parentRecordModule": module,
                               "parentRecord": str(lead_id),
                               "relatedList": "Notes"},
            "query_params": {"fields": self.NOTE_FIELDS, "per_page": 200},
        })
        rows = (out or {}).get("data", []) or []
        return sorted(rows, key=lambda n: n.get("Created_Time") or "", reverse=True)

    def add_note(self, lead_id: str, content: str, title: str = "Call log",
                 module: str = "Leads") -> dict:
        """Attach a note. Parent_Id is a jsonobject -- a bare id string is rejected."""
        out = self.call("ZohoCRM_createRecords", {
            "path_variables": {"module": "Notes"},
            "body": {"data": [{
                "Note_Title": title,
                "Note_Content": content,
                "Parent_Id": {"id": str(lead_id), "module": {"api_name": module}},
            }]},
        })
        return (out or {}).get("data", [{}])[0]

    def person_leads(self, email: str | None = None,
                     mobile: str | None = None) -> list[dict]:
        """Every Lead belonging to one person, newest first.

        Matches on email OR phone because the same person arrives under both
        `+919876543210` and `919876543210`; the last 10 digits are the only
        part the forms agree on.
        """
        clauses = []
        if email:
            clauses.append(f"Email = '{email.strip().lower()}'")
        if mobile:
            tail = "".join(c for c in str(mobile) if c.isdigit())[-10:]
            if tail:
                clauses.append(f"Mobile like '%{tail}'")
        if not clauses:
            raise ZohoError("give an email or a mobile")

        rows = self.coql(
            "select id, First_Name, Last_Name, Email, Mobile, Lead_Status, "
            "Lead_Source, Created_Time from Leads "
            f"where ({' or '.join(clauses)}) order by Created_Time desc limit 200")
        return rows

    def person_notes(self, email: str | None = None, mobile: str | None = None,
                     limit: int | None = None) -> list[dict]:
        """Notes across ALL of a person's duplicate leads, newest first.

        A caller's history is scattered over however many records the forms
        created for them, so reading one lead's notes shows only part of it.
        """
        merged: list[dict] = []
        for lead in self.person_leads(email=email, mobile=mobile):
            for n in self.notes(lead["id"]):
                n = dict(n)
                n["_lead_id"] = lead["id"]
                n["_lead_source"] = lead.get("Lead_Source")
                merged.append(n)
        merged.sort(key=lambda n: n.get("Created_Time") or "", reverse=True)
        return merged[:limit] if limit else merged

    def latest_note(self, email: str | None = None,
                    mobile: str | None = None) -> dict | None:
        """The single most recent note for a person across all their leads."""
        found = self.person_notes(email=email, mobile=mobile, limit=1)
        return found[0] if found else None

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ZohoCRM":
        return self

    def __exit__(self, *_) -> None:
        self.close()


def _bearer() -> str:
    """Reuse the token the OAuth helper stored (refreshing it if stale)."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from zoho_crm import bearer  # noqa: PLC0415
    return bearer()
