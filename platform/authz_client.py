"""Reference client for oaap.core.authorization 0.1 (spec 2.4).

Standard library only, so an app can copy this single file. It asks the
platform what a person may do inside THIS app and answers `may(...)`.

    from authz_client import Authz
    az = Authz()                       # OAAP_AUTHZ_URL / OAAP_AUTHZ_KEY
    if az.may(user_id, "team.edit_lineup", team=team_id):
        ...

It FAILS CLOSED, in every way it can fail: the service cannot be reached,
the answer is not what was asked for, the answer is about another app,
an object or activity is unknown -- all of them are `False`. A client
that answered "yes" when it could not find out would be a door that opens
when the lock is broken.

It also fails closed on the question itself: a grant that is restricted
to a context (a team) only matches when the caller NAMES that context.
Asking "may he edit the line-up?" without saying which team is a question
with no safe answer; use `may_any` when "in some context" is what is meant
(a menu entry, never a door).

Answers are cached for at most `fresh_for` seconds, never more than 30
(RFC-0045 A4): a revocation is effective within that bound.
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

MAX_CACHE = 30


def may_in(grants, permission, **fields):
    """Pure: whether already-resolved grants allow `object.activity`.

    Fails closed in both directions (see the module docstring)."""
    ob, _, act = (permission or "").partition(".")
    for g in grants or ():
        if not isinstance(g, dict) or g.get("object") != ob \
                or act not in (g.get("activities") or ()):
            continue
        fl = g.get("fields") or {}
        if all(fk in fields and fields[fk] in have for fk, have in fl.items()) \
                and all(fl[fk] is None or fields[fk] in fl[fk]
                        for fk in fields if fk in fl):
            return True
    return False


def may_any_in(grants, permission):
    ob, _, act = (permission or "").partition(".")
    return any(isinstance(g, dict) and g.get("object") == ob
               and act in (g.get("activities") or ())
               and all(isinstance(v, list) and v
                       for v in (g.get("fields") or {}).values())
               for g in grants or ())


class Authz:
    def __init__(self, url=None, key=None, app=None, timeout=5, clock=time.time):
        self.url = (url or os.environ.get("OAAP_AUTHZ_URL") or "").rstrip("/")
        self.key = key or os.environ.get("OAAP_AUTHZ_KEY") or ""
        self.app = app          # when set, an answer about another app is refused
        self.timeout = timeout
        self._clock = clock
        self._cache = {}

    def grants(self, user_id):
        """The person's grants in this app, or None if it cannot be known."""
        if not (self.url and self.key and user_id):
            return None
        hit = self._cache.get(user_id)
        if hit and hit[0] > self._clock():
            return hit[1]
        req = urllib.request.Request(
            self.url + "/effective?user=" + urllib.parse.quote(str(user_id)),
            headers={"Authorization": "Bearer " + self.key,
                     "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                doc = json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError):
            return None
        if not isinstance(doc, dict) or doc.get("user") != user_id \
                or not isinstance(doc.get("grants"), list):
            return None
        if self.app and doc.get("app") != self.app:
            return None
        try:
            fresh = min(MAX_CACHE, max(0, int(doc.get("fresh_for", 0))))
        except (TypeError, ValueError):
            fresh = 0
        self._cache[user_id] = (self._clock() + fresh, doc["grants"])
        return doc["grants"]

    def may(self, user_id, permission, **fields):
        g = self.grants(user_id)
        return bool(g) and may_in(g, permission, **fields)

    def may_any(self, user_id, permission):
        g = self.grants(user_id)
        return bool(g) and may_any_in(g, permission)
