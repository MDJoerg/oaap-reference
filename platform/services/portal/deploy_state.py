"""What a deployment is doing right now (RFC-0024).

A deployment is `queued`, `running` or `done`. Until this existed the
platform could only report outcomes after the fact — which is why a
caller asking about ITS deployment could be handed the previous one's
success, and why a build in progress was invisible everywhere except in
`systemctl` on the machine.

The state is derived from the spool every time it is asked for, never
stored a second time: a second store is a second thing that can be
wrong. Two directories carry it —

    queue/    a request nobody has taken up yet
    claims/   a request the worker has taken up and not yet answered

— and the worker moves a file from the first to the second in one
atomic step. That single move is what makes both "läuft seit" and
"Abbrechen" honest: whoever moves the file first wins, so withdrawing a
request can never race a build that has already begun.

Kept out of app.py, like store_view and instance_view, so the rules can
be read and tested without Flask around them.
"""
import json
import os
import time

# Mirrors appctl.DEPLOY_MAX_SECONDS. Only ever quoted back to a caller
# and used to recognise a claim whose worker died — the limit itself is
# enforced by the worker, on the commands it issues.
DEPLOY_MAX_MINUTES = 20
TIMED_OUT = (f"aborted after the {DEPLOY_MAX_MINUTES} minute time limit "
             "— the build was stopped, nothing was left running")
# A claim may legitimately be as old as the limit; past that, plus a
# minute of slack, the run it belongs to is gone.
STALE_AFTER = DEPLOY_MAX_MINUTES * 60 + 60

# Which spool actions are a deployment. A visibility change or a token
# issue also passes through the spool, and announcing those as
# "Deployment läuft" would trade one misleading word for another.
DEPLOY_ACTIONS = {"redeploy", "install", "artifact", "announce", "rollback",
                  "promote", "create"}

# What each action is called on a page a person reads. The worker's
# vocabulary is fine in a log; "artifact" is not an answer to "was
# passiert hier gerade".
ACTION_LABEL = {
    "redeploy": "aus der hinterlegten Quelle",
    "install": "Installation aus dem Store",
    "announce": "Anmeldung eines Pakets",
    "artifact": "hochgeladenes Paket",
    "rollback": "aus einem aufgehobenen Paket",
    "promote": "Übernahme nach Produktiv",
    "create": "Anlage der Instanz",
}


def ago(seconds):
    """'seit …' in words. Minutes are what an operator waits in."""
    if seconds < 90:
        return f"{max(1, int(seconds))} Sekunden"
    return f"{int(seconds // 60)} Minuten"


def _read(directory):
    """Requests lying in one spool directory, newest first."""
    out = []
    try:
        names = os.listdir(directory)
    except OSError:
        return out
    for fn in names:
        if not fn.endswith(".json"):
            continue
        p = os.path.join(directory, fn)
        try:
            with open(p, encoding="utf-8") as f:
                req = json.load(f)
            req["_since"] = os.path.getmtime(p)
        except (OSError, ValueError):
            continue
        if not isinstance(req, dict):
            continue
        req.setdefault("id", fn[:-5])
        out.append(req)
    out.sort(key=lambda r: r["_since"], reverse=True)
    return out


def in_flight(queue_dir, claims_dir, name="", now=None):
    """Everything queued or running, optionally for one instance.

    Each entry carries `state`, `id`, `instance`, `action` and `since`
    (seconds). `state` is 'queued', 'running' — or 'stale' for a claim
    older than the worker's own limit, which belongs to a run that died:
    the limit is enforced on every command the worker issues, so a live
    run cannot outlast it. Calling that one "läuft" would be the same
    lie in a new place.
    """
    now = time.time() if now is None else now
    out = []
    for state, d in (("running", claims_dir), ("queued", queue_dir)):
        for req in _read(d):
            if name and req.get("instance") != name:
                continue
            since = max(0, int(now - req["_since"]))
            stale = state == "running" and since > STALE_AFTER
            out.append({"state": "stale" if stale else state,
                        "id": req.get("id", ""),
                        "instance": req.get("instance", ""),
                        "action": req.get("action", "redeploy"),
                        "since": since})
    return out


def deployment(entries, rid=""):
    """The one entry that answers "what is happening to this instance".

    With an id, that exact request — whatever it is. Without one, the
    first entry that is actually a deployment.
    """
    for e in entries:
        if rid:
            if e["id"] == rid:
                return e
        elif e["action"] in DEPLOY_ACTIONS:
            return e
    return None


def for_page(entries):
    """The in-flight deployment, dressed for a page a person reads."""
    e = deployment(entries)
    if not e:
        return None
    return {**e, "ago": ago(e["since"]),
            "label": ACTION_LABEL.get(e["action"], e["action"])}


def withdraw(queue_dir, uploads_dir, name, rid, entries=()):
    """Remove a queued request. Returns (done, reason, started).

    `started` says whether the refusal is "the worker already has it" —
    the one case that is not an error but a decision: a build under way
    is not killed, because a half-built state is worse than waiting
    (RFC-0024 decision 4).

    The race against the worker resolves itself. It claims a request by
    moving the file out of the queue, so exactly one of the two
    operations finds it there.
    """
    p = os.path.join(queue_dir, f"{rid}.json")
    try:
        with open(p, encoding="utf-8") as f:
            if json.load(f).get("instance") != name:
                return False, "that deployment belongs to another instance", False
        os.remove(p)
    except (OSError, ValueError):
        started = any(e["id"] == rid and e["state"] in ("running", "stale")
                      for e in entries)
        if started:
            return False, (f"deployment {rid} has already started and is not "
                           "killed — a half-built state is worse than waiting. "
                           f"It ends after {DEPLOY_MAX_MINUTES} minutes at the "
                           "latest."), True
        return False, f"no queued deployment {rid} for this instance", False
    # the package that came with it has no request left to belong to
    try:
        os.remove(os.path.join(uploads_dir, f"{rid}.zip"))
    except OSError:
        pass
    return True, f"deployment {rid} withdrawn before it started", False
