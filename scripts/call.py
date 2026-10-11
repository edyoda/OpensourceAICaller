"""Place an outbound call that lands in the voicebot flow.

    python scripts/call.py +919876543210            # dry run, prints the request
    python scripts/call.py +919876543210 --yes      # actually dials

Exotel rings `To` from your ExoPhone and hands the answered call to the App
Bazaar flow named by `Url`. That flow's Voicebot applet is what opens the
WebSocket back to /ws/exotel -- the applet holds the wss URL, not this request.
"""

import sys

import httpx

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
from app.config import settings  # noqa: E402


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    live = "--yes" in sys.argv
    if not args:
        sys.exit("usage: call.py <number-in-E.164> [--yes]")
    to = args[0]

    missing = [n for n, v in [
        ("EXOTEL_API_KEY", settings.exotel_api_key),
        ("EXOTEL_API_TOKEN", settings.exotel_api_token),
        ("EXOTEL_SID", settings.exotel_sid),
        ("EXOTEL_CALLER_ID", settings.exotel_caller_id),
        ("EXOTEL_APP_ID", settings.exotel_app_id),
    ] if not v]
    if missing:
        sys.exit(f"missing in .env: {', '.join(missing)}")

    url = (f"https://{settings.exotel_subdomain}"
           f"/v1/Accounts/{settings.exotel_sid}/Calls/connect.json")
    flow = (f"http://my.exotel.com/{settings.exotel_sid}"
            f"/exoml/start_voice/{settings.exotel_app_id}")
    data = {
        "From": to,
        "CallerId": settings.exotel_caller_id,
        "Url": flow,
        "CallType": "trans",
        "TimeLimit": "300",
    }

    print(f"POST {url}")
    for k, v in data.items():
        print(f"  {k}: {v}")

    if not live:
        print("\nDRY RUN -- nothing dialled. Re-run with --yes to place the call.")
        return

    r = httpx.post(url, data=data,
                   auth=(settings.exotel_api_key, settings.exotel_api_token), timeout=30)
    print(f"\n[{r.status_code}] {r.text[:900]}")


if __name__ == "__main__":
    main()
