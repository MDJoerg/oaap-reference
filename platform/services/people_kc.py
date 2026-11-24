"""People invited by an app (RFC-0126): identity's half at Keycloak.

identity holds, per tenant whose server_admin turned `people.invite` on, the
secret of ONE client in that tenant's realm (`oaap-people`, D12) whose service
account may manage and read users and find groups -- nothing that
administers the realm. This module is everything identity does with it:

    create   a user marked `oaap_invited_by = instance:<name>`, in the group
             `oaap-invited/<instance>`, with the required actions "verify
             address" and "set password" (D4)
    mail     Keycloak's execute-actions mail through the realm's mailbox
    read     one such user's state
    enable / disable  lock and unlock

and the one rule that keeps the narrowing narrow (security requirement 3):
**every call on an existing user first reads it and refuses unless its marker
names the calling instance.** No call changes a name, an address or a
password; none deletes (`never: users` holds for everything else).

The pure functions (paths, bodies, the marker check, the status) are tested
without a server; `Realm` is the only thing that speaks HTTP.
"""
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

MARKER = "oaap_invited_by"
GROUP = "oaap-invited"
ACTIONS = ["VERIFY_EMAIL", "UPDATE_PASSWORD"]
LINK_HOURS_DEFAULT = 72
EMAIL_RE = re.compile(r"^[^@\s<>()\[\],;:\"]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,63}$")
LANGS = ("de", "en")


def admin_base(issuer):
    """https://kc/realms/<r>  ->  https://kc/admin/realms/<r>"""
    m = re.match(r"^(https?://[^/]+(?:/[^/]+)*?)/realms/([^/]+)/?$", issuer or "")
    if not m:
        return ""
    return f"{m.group(1)}/admin/realms/{m.group(2)}"


def token_url(issuer):
    return (issuer or "").rstrip("/") + "/protocol/openid-connect/token"


def marker_of(instance):
    return f"instance:{instance}"


def user_body(email, instance, name="", language="de"):
    first, _, last = (name or "").strip().partition(" ")
    body = {"username": email.lower(), "email": email, "enabled": True,
            "emailVerified": False, "requiredActions": list(ACTIONS),
            "attributes": {MARKER: [marker_of(instance)],
                           "locale": [language if language in LANGS else "de"]}}
    if first:
        body["firstName"] = first[:100]
    if last:
        body["lastName"] = last[:100]
    return body


def owned_by(user_doc, instance):
    """Is this realm user one THIS instance invited? Only the marker counts."""
    vals = ((user_doc or {}).get("attributes") or {}).get(MARKER) or []
    return marker_of(instance) in vals


def status_of(user_doc, invited_at, link_hours, now=None):
    """invited · active · locked · expired, from the realm's own answer."""
    if not user_doc:
        return "gone"
    if user_doc.get("enabled") is False:
        return "locked"
    if not (user_doc.get("requiredActions") or []):
        return "active"
    t = time.time() if now is None else now
    if invited_at and t - invited_at > link_hours * 3600:
        return "expired"
    return "invited"


def email_error(email):
    if not isinstance(email, str) or len(email) > 254 or not EMAIL_RE.match(email.strip()):
        return "email: a single address, e.g. name@example.org"
    return ""


def _why(doc):
    """Keycloak's reasons, short: errorMessage, or the fields of `errors`."""
    if not isinstance(doc, dict):
        return ""
    if doc.get("errorMessage"):
        return f": {doc['errorMessage']}"
    errs = doc.get("errors") or []
    if errs:
        return ": " + "; ".join(f"{e.get('field')}: {e.get('errorMessage')}" for e in errs[:3]
                                if isinstance(e, dict))
    return ""


class RealmError(Exception):
    def __init__(self, status, sentence):
        super().__init__(sentence)
        self.status = status


class Realm:
    """One tenant's realm, through its invitation client."""

    def __init__(self, issuer, client_id, secret, timeout=10):
        self.issuer, self.client_id, self.secret = issuer, client_id, secret
        self.base = admin_base(issuer)
        self.timeout = timeout
        self._token, self._until = "", 0.0

    def _http(self, method, url, body=None, form=None, auth=True):
        data, headers = None, {"Accept": "application/json"}
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if auth:
            headers["Authorization"] = "Bearer " + self.token()
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else None), r.headers.get("Location", "")
        except urllib.error.HTTPError as e:
            raw = e.read() or b""
            try:
                doc = json.loads(raw)
            except ValueError:
                doc = None
            return e.code, doc, ""
        except OSError as e:
            raise RealmError(502, f"the tenant's realm could not be reached ({e})")

    def token(self):
        if self._token and time.time() < self._until - 10:
            return self._token
        st, doc, _ = self._http("POST", token_url(self.issuer), auth=False, form={
            "grant_type": "client_credentials", "client_id": self.client_id,
            "client_secret": self.secret})
        if st != 200 or not isinstance(doc, dict) or not doc.get("access_token"):
            raise RealmError(503, "the realm refused the invitation client "
                                  f"({st}) -- `oaap tenant people <label> on` again")
        self._token = doc["access_token"]
        self._until = time.time() + int(doc.get("expires_in") or 60)
        return self._token

    def _group_id(self, instance):
        """The id of `oaap-invited/<instance>`, made when missing."""
        st, doc, _ = self._http("GET", f"{self.base}/groups?search={GROUP}&exact=true&briefRepresentation=true")
        top = next((g for g in (doc or []) if isinstance(g, dict) and g.get("name") == GROUP), None) \
            if st == 200 else None
        if not top:
            raise RealmError(503, f"the group '{GROUP}' is missing in the realm -- "
                                  "`oaap tenant people <label> on` again")
        st, kids, _ = self._http("GET", f"{self.base}/groups/{top['id']}/children?briefRepresentation=true&max=1000")
        kid = next((g for g in (kids or []) if isinstance(g, dict) and g.get("name") == instance), None) \
            if st == 200 else None
        if kid:
            return kid["id"]
        st, _d, loc = self._http("POST", f"{self.base}/groups/{top['id']}/children", body={"name": instance})
        if st not in (201, 204) or not loc:
            raise RealmError(502, f"making the group '{GROUP}/{instance}' answered {st}")
        return loc.rsplit("/", 1)[-1]

    def create(self, email, instance, name="", language="de"):
        """(sub) of the new realm user. 409 when the address has a user."""
        st, doc, loc = self._http("POST", f"{self.base}/users", body=user_body(email, instance, name, language))
        if st == 409:
            raise RealmError(409, "exists: this address already has a login in the tenant; "
                                  "the tenant admin links existing people (RFC-0126 D8)")
        if st != 201 or not loc:
            raise RealmError(502, f"creating the login answered {st}" + _why(doc))
        sub = loc.rsplit("/", 1)[-1]
        gid = self._group_id(instance)
        st, _d, _l = self._http("PUT", f"{self.base}/users/{sub}/groups/{gid}")
        if st not in (200, 204):
            raise RealmError(502, f"putting the login into its group answered {st}")
        try:
            self.read(sub, instance)            # the marker must have stayed (§1.5 K2)
        except RealmError as e:
            if e.status == 403:
                raise RealmError(503, "the realm dropped the marker 'oaap_invited_by' -- its "
                                      "user profile does not declare it; `oaap tenant people "
                                      "<label> on` again") from None
            raise
        return sub

    def read(self, sub, instance):
        st, doc, _ = self._http("GET", f"{self.base}/users/{urllib.parse.quote(sub)}")
        if st == 404:
            return None
        if st != 200 or not isinstance(doc, dict):
            raise RealmError(502, f"reading the login answered {st}")
        if not owned_by(doc, instance):
            raise RealmError(403, "not a person this app invited")
        return doc

    def mail(self, sub, instance, hours=LINK_HOURS_DEFAULT):
        self.read(sub, instance)
        st, doc, _ = self._http("PUT", f"{self.base}/users/{urllib.parse.quote(sub)}/execute-actions-email"
                                       f"?lifespan={int(hours * 3600)}", body=list(ACTIONS))
        if st not in (200, 204):
            raise RealmError(502, f"sending the invitation answered {st}"
                             + (f": {doc.get('errorMessage')}" if isinstance(doc, dict) and doc.get("errorMessage") else ""))

    def set_enabled(self, sub, instance, enabled):
        self.read(sub, instance)
        st, _d, _l = self._http("PUT", f"{self.base}/users/{urllib.parse.quote(sub)}",
                                body={"enabled": bool(enabled)})
        if st not in (200, 204):
            raise RealmError(502, f"{'unlocking' if enabled else 'locking'} the login answered {st}")
