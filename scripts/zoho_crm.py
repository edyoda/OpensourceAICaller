"""Talk to the EdYoda Zoho CRM in plain English.

    python scripts/zoho_crm.py "which custom fields does Leads have?"
    python scripts/zoho_crm.py "leads created this week with no Lead Source"

Claude reaches Zoho through its MCP connector: Anthropic opens the connection
to the `crm-data-metadata` server itself, so there is no tool loop to drive
here -- one request goes out and a finished answer comes back, with any Zoho
calls made in between.

The MCP URL has the API key baked into its path, which makes the URL a
password. It lives in .env as ZOHO_MCP_URL.
"""

import json
import sys
import time
from pathlib import Path

import anthropic
import httpx

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
from app.config import settings  # noqa: E402

SERVER_NAME = "zoho-crm"
TOKEN_FILE = Path(__file__).resolve().parent.parent / ".zoho_mcp_token.json"

# The website's web-to-lead forms post field IDs (LEADCF5, LEADCF35), which are
# not what the REST/MCP layer calls those fields. Left to guess, the model will
# write to a field that does not exist and Zoho will accept the call anyway.
SYSTEM = (
    "You operate the EdYoda Zoho CRM through the connected MCP tools.\n"
    "Field IDs used by the website's web-to-lead forms (LEADCF5, LEADCF35, ...) "
    "are NOT valid API names. Look a field up with the metadata tools before "
    "reading or writing it -- never guess.\n"
    "Before any call that modifies records, say what you are about to change "
    "and how many records it touches."
)


def bearer() -> str:
    """Return a live access token, refreshing it if this one has expired.

    Zoho's access tokens are short-lived; the refresh token is what makes this
    a one-time browser handshake instead of a daily one.
    """
    if not TOKEN_FILE.exists():
        sys.exit("no token - run: python scripts/zoho_mcp_auth.py")

    tok = json.loads(TOKEN_FILE.read_text())
    if time.time() < tok["expires_at"] - 60:
        return tok["access_token"]

    if not tok.get("refresh_token"):
        sys.exit("token expired and no refresh token - re-run zoho_mcp_auth.py")

    resp = httpx.post(tok["token_endpoint"], timeout=30, data={
        "grant_type": "refresh_token",
        "refresh_token": tok["refresh_token"],
        "client_id": tok["client_id"],
        "client_secret": tok["client_secret"],
    })
    resp.raise_for_status()
    fresh = resp.json()

    tok["access_token"] = fresh["access_token"]
    tok["expires_at"] = time.time() + fresh.get("expires_in", 3600)
    tok["refresh_token"] = fresh.get("refresh_token", tok["refresh_token"])
    TOKEN_FILE.write_text(json.dumps(tok, indent=2))
    return tok["access_token"]


def ask(instruction: str) -> str:
    # .env is loaded into settings, not os.environ, so pass the key through
    # explicitly -- same as app/main.py does for the call agent.
    client = anthropic.Anthropic(
        api_key=settings.anthropic_api_key,
        default_headers=({"anthropic-workspace-id": settings.anthropic_workspace_id}
                         if settings.anthropic_workspace_id else None))
    response = client.beta.messages.create(
        model="claude-opus-5",
        max_tokens=16000,
        # mcp-client: the connector itself. server-side-fallback: a safety
        # classifier declining mid-task would otherwise just stop the run.
        betas=["mcp-client-2025-11-20", "server-side-fallback-2026-07-01"],
        fallbacks="default",
        thinking={"type": "adaptive"},
        system=SYSTEM,
        mcp_servers=[
            {
                "type": "url",
                "url": settings.zoho_mcp_url,
                "name": SERVER_NAME,
                # The key in the URL names the server; this proves who we are.
                "authorization_token": bearer(),
            },
        ],
        # Both halves are required -- mcp_servers alone is a validation error.
        tools=[{"type": "mcp_toolset", "mcp_server_name": SERVER_NAME}],
        messages=[{"role": "user", "content": instruction}],
    )

    if response.stop_reason == "refusal":
        return f"[refused] {getattr(response.stop_details, 'explanation', '')}"

    return "\n".join(b.text for b in response.content if b.type == "text")


def main() -> None:
    if not settings.zoho_mcp_url:
        sys.exit("missing in .env: ZOHO_MCP_URL")
    if len(sys.argv) < 2:
        sys.exit('usage: zoho_crm.py "<what you want to know or change>"')
    print(ask(" ".join(sys.argv[1:])))


if __name__ == "__main__":
    main()
