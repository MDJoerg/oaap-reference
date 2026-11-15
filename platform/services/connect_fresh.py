"""Is the connector service's state.json still its word? (oaap.net.connector 2.7)

The connect service writes state.json when something changes and, since
0.1.286, at least every `beat` seconds (it says so in the file). A file
that is much older than its beat was left behind by a service that is no
longer running: its "connected" is a memory, not a fact. Read so, a
stopped service looked connected on the portal, in the status document
and so at the hub (found 2026-11-15 measuring RFC-0087 stage 2).

One rule, one function: the portal (page, attention items, fleet links)
and the host's CLI both read state.json through `judge`.

A file without `beat` comes from a service before 0.1.286, which wrote
only on changes; its age says nothing and it is taken as it is.
"""
import calendar
import time

STALE_BEATS = 4        # four missed beats ...
STALE_MIN = 120        # ... and never less than two minutes


def _epoch(text):
    try:
        return calendar.timegm(time.strptime(str(text or ""), "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return None


def stale_since(state, now=None):
    """The state's own time (ISO) if it is too old to be believed, else ""."""
    if not isinstance(state, dict):
        return ""
    try:
        beat = float(state.get("beat") or 0)
    except (TypeError, ValueError):
        beat = 0
    if beat <= 0:
        return ""
    at = _epoch(state.get("time"))
    if at is None:
        return str(state.get("time") or "?")
    now = time.time() if now is None else now
    return state.get("time") if now - at > max(STALE_BEATS * beat, STALE_MIN) else ""


def disconnected(state):
    """A stale state with every tunnel and connector set to not connected
    and nothing exposed -- but its NAMES kept. The fleet's half-edges need
    the `tunnel_id`, which only the connect service can compute (it alone
    reads the key) and which does not age: without it the inner half
    vanished, the hub saw one half only (`half`, no fault) and stayed
    silent (measured 2026-11-15, the first live run of this rule)."""
    out = {k: v for k, v in (state or {}).items() if k not in ("tunnels", "connectors", "exposures")}
    for part in ("tunnels", "connectors"):
        out[part] = {}
        for label, rec in ((state or {}).get(part) or {}).items():
            rec = dict(rec) if isinstance(rec, dict) else {}
            rec["connected"] = False
            rec.pop("since", None)
            rec.pop("remote", None)
            out[part][label] = rec
    out["exposures"] = {}
    return out


def judge(state, now=None):
    """(state or None, stale_since): None when nothing usable was written
    or what was written is stale -- then nothing is connected."""
    since = stale_since(state, now)
    if since or not isinstance(state, dict):
        return None, since
    return state, ""
