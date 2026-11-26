"""Hand the gates' events to the bus (RFC-0135 stage 2b, RFC-0127).

The host counts the gates from the gateway's access log (gate_stats.py)
and keeps one outbox; every minute with something in it, it runs this in
the twin's container:

    docker exec -i oaap-twin-1 python gate_events.py < outbox.json

Each item goes to this container's own /internal/bus/platform (the same
door identity uses), oldest first; the first one the bus cannot take now
stops the run, so the order holds. One JSON line on stdout: {"taken": n}
-- how many from the front are done (stored, or refused for good).
"""
import json
import os
import sys
import urllib.error
import urllib.request

URL = "http://localhost:8000/internal/bus/platform"
KEY = os.environ.get("INTERNAL_API_KEY", "")


def send(item):
    """'sent', 'refused' or 'later' -- identity's _bus_send, same rule."""
    body = json.dumps({k: v for k, v in item.items() if k != "at"}).encode()
    req = urllib.request.Request(URL, data=body, method="POST", headers={
        "Content-Type": "application/json", "X-OAAP-Internal-Key": KEY})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            r.read()
            return "sent"
    except urllib.error.HTTPError as e:
        if 400 <= e.code < 500 and e.code not in (401, 408, 429):
            print(f"bus: {item.get('type')} refused {e.code}: {e.read()[:200]!r}",
                  file=sys.stderr)
            return "refused"
        return "later"
    except (OSError, ValueError):
        return "later"


def main():
    try:
        items = json.load(sys.stdin)
    except ValueError:
        items = []
    taken = 0
    for it in items if isinstance(items, list) else []:
        if isinstance(it, dict) and send(it) == "later":
            break
        taken += 1
    print(json.dumps({"taken": taken}))


if __name__ == "__main__":
    main()
