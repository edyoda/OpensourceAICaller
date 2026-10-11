"""One-time OAuth handshake with the Zoho MCP server.

    python scripts/zoho_mcp_auth.py            # read-only scopes
    python scripts/zoho_mcp_auth.py --write    # also CREATE/UPDATE

Zoho's MCP endpoint is an OAuth-protected resource: the API key baked into
ZOHO_MCP_URL identifies the server, but it does not authenticate you. Calls
need a Bearer token, which is what the "Connect" button in Claude Desktop
quietly obtains. This does the same handshake from the terminal.

Everything is discovered from the server -- client registration, the authorize
and token endpoints, the scope list -- so nothing here is hardcoded to one org.
Tokens land in .zoho_mcp_token.json (gitignored); scripts/zoho_crm.py reads and
refreshes them from there.
"""

import base64
import hashlib
import http.server
import json
import secrets
import sys
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import settings  # noqa: E402

TOKEN_FILE = Path(__file__).resolve().parent.parent / ".zoho_mcp_token.json"
REDIRECT_PORT = 8765
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"

# Read scopes are the default. A CRM agent that can silently rewrite records is
# a much bigger blast radius than one that can only look, and most exploration
# never needs more. --write opts in.
_ALWAYS = ("ZohoMCP.tool.execute",)


def discover(mcp_url: str) -> dict:
    """Pull the OAuth server metadata the MCP endpoint advertises."""
    origin = "{0.scheme}://{0.netloc}".format(urllib.parse.urlparse(mcp_url))
    meta = httpx.get(f"{origin}/.well-known/oauth-authorization-server", timeout=30)
    meta.raise_for_status()
    return meta.json()


def pick_scopes(supported: list[str], write: bool) -> str:
    chosen = [s for s in supported if s.endswith(".READ")]
    if write:
        chosen += [s for s in supported if s.endswith((".CREATE", ".UPDATE"))]
    chosen += [s for s in _ALWAYS if s in supported]
    return " ".join(sorted(set(chosen)))


def register(endpoint: str) -> dict:
    """Dynamic client registration (RFC 7591) -- no pre-created Zoho app."""
    resp = httpx.post(endpoint, timeout=30, json={
        "client_name": "EdYoda AI Call Agent",
        "redirect_uris": [REDIRECT_URI],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "client_secret_post",
    })
    resp.raise_for_status()
    return resp.json()


def await_code(state: str) -> str:
    """Serve exactly one request: Zoho's redirect carrying the auth code."""
    box: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            box.update({k: v[0] for k, v in query.items()})
            ok = box.get("state") == state and "code" in box
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h2>Done - close this tab.</h2>" if ok
                             else b"<h2>Authorisation failed.</h2>")

        def log_message(self, *_):  # keep the console clean
            pass

    server = http.server.HTTPServer(("localhost", REDIRECT_PORT), Handler)
    threading.Thread(target=server.handle_request, daemon=True).start()

    for _ in range(300):  # 5 minutes
        if box:
            break
        time.sleep(1)
    server.server_close()

    if box.get("state") != state:
        sys.exit("state mismatch - aborting")
    if "code" not in box:
        sys.exit(f"no code returned: {box.get('error', 'timed out')}")
    return box["code"]


def main() -> None:
    if not settings.zoho_mcp_url:
        sys.exit("missing in .env: ZOHO_MCP_URL")

    meta = discover(settings.zoho_mcp_url)
    scopes = pick_scopes(meta.get("scopes_supported", []), "--write" in sys.argv)
    client = register(meta["registration_endpoint"])

    # PKCE: the verifier never leaves this process, so an intercepted code is
    # useless on its own.
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(16)

    authorize = meta["authorization_endpoint"] + "?" + urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client["client_id"],
        "redirect_uri": REDIRECT_URI,
        "scope": scopes,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        # RFC 8707 -- binds the token to this MCP server specifically.
        "resource": settings.zoho_mcp_url,
    })

    print(f"scopes: {'write' if '--write' in sys.argv else 'read-only'}")
    print("opening browser; sign in to Zoho and approve...\n" + authorize + "\n")
    webbrowser.open(authorize)
    code = await_code(state)

    resp = httpx.post(meta["token_endpoint"], timeout=30, data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": client["client_id"],
        "client_secret": client.get("client_secret", ""),
        "code_verifier": verifier,
        "resource": settings.zoho_mcp_url,
    })
    resp.raise_for_status()
    token = resp.json()

    TOKEN_FILE.write_text(json.dumps({
        "client_id": client["client_id"],
        "client_secret": client.get("client_secret", ""),
        "token_endpoint": meta["token_endpoint"],
        "access_token": token["access_token"],
        "refresh_token": token.get("refresh_token"),
        "expires_at": time.time() + token.get("expires_in", 3600),
        "scope": token.get("scope", scopes),
    }, indent=2))
    TOKEN_FILE.chmod(0o600)
    print(f"saved -> {TOKEN_FILE.name}")


if __name__ == "__main__":
    main()
