"""Managing a provider from OAAP: the connector contract, and Keycloak
as its first connector.

RFC-0041 K3, decided AGAINST the recommendation: OAAP creates the realm
and the client itself instead of only consuming one somebody made by
hand. The objection that was overruled does not disappear by being
overruled -- it becomes this file's job. An integration ages against
somebody else's product, and that kind of ageing is invisible until it
breaks.

So this is deliberately NOT "the Keycloak integration". It is a
CONTRACT with one implementation, because Joerg named the shape on
2026-09-23: Keycloak is the first connector *with an API*, and when a
second SSO product arrives -- one whose settings we also want to make
and then write into our own configuration -- it should be a file and a
row in a table, not a redesign.

A connector is four verbs and nothing else:

    version   what the product says it is, so the pinned number is
              CHECKED rather than merely written down
    space     the place one tenant's people live in (Keycloak: a realm)
    client    the confidential OIDC client OAAP presents itself as
    issuer    the URL that space writes into its tokens

`settings` is declared and NOT implemented: turning self-registration
and a second factor on inside the space is RFC-0041 step 6. A verb that
is named and absent is a smaller lie than one that is silently missing,
and `missing_verbs()` says so out loud.

Three rules here are not settings and never become settings:

* **Nothing deletes** (`method_refusal`). K3.3: managing is not owning.
  A realm OAAP found stays usable by hand, and a realm OAAP made still
  holds the club's identities -- deleting a tenant must never delete
  the people in it. The rule sits on the path every call takes, not in
  the absence of a line somebody could add later.
* **The version is checked before anything is created**
  (`plan_refusal`). K3.2 says OAAP never half-creates a realm; the
  cheapest way to keep that is to find out first that this is not the
  server we were measured against.
* **The admin address obeys the channel rule** (`idp.channel_refusal`),
  for a stronger reason than an issuer does: a credential that can
  create realms travels it.

Pure functions, plus ONE class at the bottom that speaks HTTP -- and
that class is the only thing in this file that does. Everything worth
judging is above it and needs no server: the plan, its order, the
refusals, and what an answer means.
"""

import json
import re
import urllib.error
import urllib.parse
import urllib.request

import idp

# ---------------------------------------------------------------------------
# The contract
#
# One row per product. Nearly everything the code below needs comes
# out of this table: the addresses, where the version hides in the
# answer, how an issuer is spelled, which credential shapes exist.
#
# What does NOT fit in a table is the shape of the two requests that
# create something -- a realm and a client are Keycloak's own words for
# Keycloak's own fields. Those live in functions registered in `_BODIES`
# below, so a second product is still a file and two rows and never an
# `if` in the middle of the plan.

CONNECTOR_KINDS = {
    "keycloak": {
        "product": "Keycloak",
        "pinned": idp.KEYCLOAK_PINNED,
        "measured": "2026-09-22",
        # What a tenant gets at this product, in this product's word.
        "space_word": "realm",
        "reserved": ("master", "admin", "account"),
        # Where the server states its own version, and where in the
        # answer. This is what makes the pinned number checkable.
        "version_path": "/admin/serverinfo",
        "version_field": ("systemInfo", "version"),
        # How the issuer of a space is spelled.
        "issuer_template": "{base}/realms/{space}",
        # Every address OAAP uses at this product, by the name the code
        # asks for. `provision_plan` and `Admin` both read them here,
        # so what a plan PRINTS and what a run CALLS cannot drift.
        "paths": {
            "spaces": "/admin/realms",
            "space": "/admin/realms/{space}",
            "clients": "/admin/realms/{space}/clients",
            "client": "/admin/realms/{space}/clients/{uuid}",
            "client_secret":
                "/admin/realms/{space}/clients/{uuid}/client-secret",
        },
        # Where a credential is redeemed for an admin token. `master`
        # is Keycloak's server realm; see `credential_note`.
        "auth_realm": "master",
        "token_template": "{base}/realms/{realm}/protocol/openid-connect/token",
        "auth_kinds": ("client", "password"),
        "verbs": ("version", "space", "client", "issuer"),
        "later": ("settings",),
        "provider_kind": "oidc",
    },
}

# What every connector must be able to do to be usable at all.
REQUIRED_VERBS = ("version", "space", "client", "issuer")

# The methods OAAP uses against somebody else's identity server.
# DELETE is absent and stays absent -- see `method_refusal`. PATCH is
# absent for a duller reason: nothing here needs it, and a method
# nobody needs is a method nobody has reviewed.
SAFE_METHODS = ("GET", "POST", "PUT")

SPACE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,35}$")
CONNECTOR_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")


def connector_of(kind):
    """The declaration for a connector kind, or {}."""
    return dict(CONNECTOR_KINDS.get((kind or "").strip().lower()) or {})


def connector_kinds():
    """Every kind this build speaks, in a stable order."""
    return tuple(sorted(CONNECTOR_KINDS))


def path_of(kind, name, space="", uuid="", query=None):
    """One of this connector's addresses, filled in.

    Both the plan and the run come through here. A plan that printed
    one address while the run called another would be a dry run that
    reassures about something else.
    """
    c = connector_of(kind)
    tpl = ((c.get("paths") or {}).get(name) or "")
    if not tpl:
        return ""
    path = tpl.format(space=space, uuid=uuid)
    if query:
        path += "?" + urllib.parse.urlencode(query)
    return path


def missing_verbs(kind):
    """Which required verbs this connector does not declare.

    Empty for every kind in the table today, and that is the point: a
    connector added later fails this before it fails an operator.
    """
    have = set(connector_of(kind).get("verbs") or ())
    return tuple(v for v in REQUIRED_VERBS if v not in have)


def method_refusal(method):
    """Why this HTTP method may not be used against a provider.

    K3.3 as something that RUNS rather than something that is promised
    in a README. The day somebody writes a tidy-up that removes the
    realm of a tenant being deleted, this is the line that stops it --
    and it stops it whether the call was written here or somewhere that
    imported this class.
    """
    m = (method or "").strip().upper()
    if not m:
        return "a call needs a method"
    if m not in SAFE_METHODS:
        return (f"{m} is not a method OAAP uses against an identity "
                "provider: OAAP manages a space, it does not own it, and "
                "the people in it are the tenant's, not ours "
                "(RFC-0041 K3)")
    return ""


def kind_refusal(kind):
    """Why this connector kind cannot be used."""
    kind = (kind or "").strip().lower()
    if kind not in CONNECTOR_KINDS:
        return (f"unknown connector kind '{kind}' -- this build speaks "
                f"{', '.join(connector_kinds())} (RFC-0041 K3)")
    absent = missing_verbs(kind)
    if absent:
        return (f"the connector '{kind}' does not declare "
                f"{', '.join(absent)} and cannot manage anything")
    return ""


def connector_refusal(kind, name, base_url, auth, admin_id, admin_secret):
    """The ONE sentence that refuses a connector on this node.

    Same posture as `idp.provider_refusal`: one place, because there is
    already a second door coming (the portal wizard of RFC-0041 6) and
    a refusal that can only be phrased here is a refusal a third door
    cannot soften.
    """
    bad = kind_refusal(kind)
    if bad:
        return bad
    c = connector_of(kind)
    if not CONNECTOR_NAME_RE.match((name or "").strip()):
        return ("a connector needs a short name of its own -- lower case, "
                "digits and hyphens")
    bad = idp.channel_refusal(
        base_url, "the admin address",
        carries="a credential that can create " + c["space_word"] + "s travels")
    if bad:
        return bad
    auth = (auth or "").strip().lower()
    if auth not in c["auth_kinds"]:
        return (f"'{auth}' is not a credential shape {c['product']} takes "
                f"-- one of {', '.join(c['auth_kinds'])}")
    if not (admin_id or "").strip():
        return ("the admin credential needs a name (a client id, or a "
                "user name)")
    if len((admin_secret or "").strip()) < idp.SECRET_MIN:
        return (f"the admin credential is missing or shorter than "
                f"{idp.SECRET_MIN} characters")
    return ""


def credential_note(kind, auth, realm=""):
    """What this credential can do, said out loud rather than assumed.

    K3.4 asked for a credential "scoped to realm administration, never
    to the master realm", and building it found that the two halves of
    that sentence cannot both be true at once: at Keycloak, CREATING a
    realm is a master-realm act. A realm-scoped administrator can
    manage the realm it belongs to and cannot make a new one.

    So the decision's intent is kept where it can be kept -- the
    credential is not a human's console login and not `admin`. It is a
    service account in the server realm holding `create-realm` and
    nothing else, which can make realms and cannot read the people in
    anybody else's. The recipe in the keycloak app's README sets one up.

    And where the intent cannot be kept, it is NAMED. That is what this
    function is for: a password credential says what it costs, every
    time it is printed, instead of being quietly equivalent.
    """
    c = connector_of(kind)
    if not c:
        return ""
    realm = (realm or c.get("auth_realm") or "").strip()
    if (auth or "").lower() == "client":
        return (f"a service account in the '{realm}' realm -- grant it "
                "`create-realm` and nothing else; it can then make "
                f"{c['space_word']}s and administer the ones it made, and "
                "it can read nobody else's people. What it CANNOT do is "
                "read the server's version, so that number is stated "
                "once with --accept-version and printed as stated")
    return (f"a USER's login in the '{realm}' realm. It can do whatever "
            "that user can do -- on a shared node that is usually every "
            "club on this server. It can read the server's version, "
            "which the narrow credential cannot; that is the whole of "
            "what it buys, and a service account with `create-realm` is "
            "the credential this was designed for (RFC-0041 K3.4)")


# ---------------------------------------------------------------------------
# Naming: what a tenant's space and client are called
#
# Both are derived and then CHECKED, never guessed at the far end. The
# space keeps the tenant's label because K6's move exports a realm and
# imports it under its own name -- a derived name that changed with the
# node would break every binding the moment the club moved, which is
# the one thing the move must not do.

def space_for(tenant_label):
    """The space a tenant's people live in: its own label."""
    return (tenant_label or "").strip().lower()


def client_for(node_name):
    """The client OAAP presents itself as at this provider.

    Named after the NODE, not the tenant: one node is one relying
    party, and a realm belongs to one tenant anyway. After a move the
    new node makes its own client in the imported realm and the old
    one is left in place, disabled by the operator -- which is a
    two-line difference an operator can see, rather than a silent
    reuse of somebody else's secret.
    """
    node = re.sub(r"[^A-Za-z0-9._-]", "-", (node_name or "").strip().lower())
    return ("oaap-" + node.strip("-"))[:64] or "oaap"


def redirect_uris_for(tenant_label, host, schemes=("https", "http")):
    """Every address at which this node's login may come back.

    Both schemes on purpose, and this is not sloppiness -- it is the
    finding of 2026-09-23. The redirect URI must match to the
    character, and the scheme in it is the one the VISITOR'S BROWSER
    used, which on a node reached without TLS is `http`. A provider
    that holds only the https form rejects the login without naming a
    cause, and the operator has nothing to read.
    """
    label = (tenant_label or "").strip().lower()
    host = (host or "").strip().lower()
    if not label or not host:
        return []
    return [f"{s}://{label}.{host}/auth/oidc/callback" for s in schemes]


def space_refusal(kind, space):
    """Why this name may not be a tenant's space at this provider."""
    bad = kind_refusal(kind)
    if bad:
        return bad
    c = connector_of(kind)
    word = c["space_word"]
    space = (space or "").strip()
    if space in (c.get("reserved") or ()):
        return (f"'{space}' administers the server itself -- a tenant must "
                f"never be given it (RFC-0041 K3)")
    if not SPACE_RE.match(space):
        return (f"'{space}' is not a usable {word} name: lower case, digits "
                "and hyphens, two to thirty-six characters")
    return ""


def issuer_for(kind, base_url, space):
    """The URL this space will write into its tokens.

    Derived here and COMPARED later: `idp.endpoints_refusal` holds the
    discovery document's own `issuer` against this one, character for
    character. Deriving it is a convenience; the comparison is what
    makes it true.
    """
    c = connector_of(kind)
    if not c:
        return ""
    return c["issuer_template"].format(
        base=(base_url or "").rstrip("/"), space=(space or "").strip())


def token_url(kind, base_url, realm=""):
    """Where a credential is redeemed for an admin token."""
    c = connector_of(kind)
    if not c:
        return ""
    return c["token_template"].format(
        base=(base_url or "").rstrip("/"),
        realm=(realm or c["auth_realm"]).strip())


# ---------------------------------------------------------------------------
# The plan
#
# Data, not code, for two reasons. `--dry-run` can print it, so an
# operator sees what is about to happen to their identity server before
# it happens. And a test can assert the ORDER without a server: that
# the version check is first, and that nothing which writes comes
# before it has passed.

def provision_plan(kind, space, client_id):
    """Every call this provisioning may make, in order.

    `when` says whether a step runs always, only if the thing is
    absent, or only if it was found. A plan is therefore longer than
    any single run -- which is right: it is what MAY happen, and that
    is the thing an operator wants to read beforehand.
    """
    if kind_refusal(kind):
        return []
    c = connector_of(kind)
    word = c["space_word"]

    def p(name, **kw):
        return path_of(kind, name, space=space, **kw)

    return [
        {"verb": "version", "method": "GET", "when": "always", "writes": False,
         "path": c["version_path"],
         "why": f"which {c['product']} is this? checked against the pinned "
                f"{c['pinned']} BEFORE anything is created"},
        {"verb": "space", "method": "GET", "when": "always", "writes": False,
         "path": p("space"),
         "why": f"does this {word} already exist? one OAAP finds is used "
                "as it is, never replaced"},
        {"verb": "space", "method": "POST", "when": "absent", "writes": True,
         "path": p("spaces"),
         "why": f"create the {word}, enabled, with nobody in it yet"},
        {"verb": "client", "method": "GET", "when": "always", "writes": False,
         "path": p("clients", query={"clientId": client_id}),
         "why": f"does '{client_id}' already exist in this {word}?"},
        {"verb": "client", "method": "POST", "when": "absent", "writes": True,
         "path": p("clients"),
         "why": "create the confidential client this node signs in with"},
        {"verb": "client", "method": "PUT", "when": "found", "writes": True,
         "path": p("client", uuid="<id>"),
         "why": "add this node's redirect URI to a client that was already "
                "there -- and nothing else about it is touched"},
        {"verb": "client", "method": "GET", "when": "always", "writes": False,
         "path": p("client_secret", uuid="<id>"),
         "why": "fetch the client secret, the one value OAAP keeps"},
    ]


def plan_refusal(plan):
    """Why a plan may not be carried out at all.

    Three things, and each of them is a rule from K3 rather than a
    style check:

    * a method nothing here may use (`method_refusal`),
    * a write before the version has been checked,
    * no version check at all.
    """
    if not plan:
        return "there is no plan for this connector"
    where = next((i for i, st in enumerate(plan)
                  if st.get("verb") == "version"), -1)
    if where != 0:
        return ("a plan must ask the server which version it is before it "
                "does anything else (RFC-0041 K3.2)")
    for i, step in enumerate(plan):
        bad = method_refusal(step.get("method"))
        if bad:
            return bad
        if step.get("writes") and i <= where:
            return (f"'{step.get('verb')}' would write before the version "
                    "has been checked (RFC-0041 K3.2)")
        if not (step.get("path") or ""):
            return f"'{step.get('verb')}' has no address at this connector"
    return ""


def plan_lines(plan):
    """The plan as an operator reads it."""
    out = []
    for step in plan:
        mark = {"always": "   ", "absent": "if absent  ",
                "found": "if present "}.get(step.get("when"), "")
        out.append(f"  {step['method']:<5}{step['path']}")
        out.append(f"        {mark and mark.strip() + ': ' or ''}{step['why']}")
    return out


# ---------------------------------------------------------------------------
# The version, and what an answer means

def version_said(kind, doc):
    """What the server says it is, out of its own answer, or ''."""
    c = connector_of(kind)
    cur = doc
    for step in (c.get("version_field") or ()):
        if not isinstance(cur, dict):
            return ""
        cur = cur.get(step)
    return str(cur or "").strip()


MEASURED = "measured"
ASSERTED = "asserted"


def version_check(kind, said, accepted=""):
    """(how, refusal). How the version of this server became known.

    Three ends, not two, and the third one was found on the machine
    rather than in the design (2026-09-23, Keycloak 26.7.4):

    * `measured` -- the server stated it and it is the pinned one, or
      it is one the operator has explicitly accepted.
    * a refusal -- the server stated a version nobody measured against
      and nobody accepted. K3.2.
    * `asserted` -- the server would not state it AT ALL, and the
      operator has typed the number instead.

    The third case is not a loophole, it is the only thing left after a
    measurement that went against the design. K3.1 wants the pinned
    version checkable; K3.4 wants a credential that is not the server's
    administrator. At Keycloak those two cannot both be had: the
    version lives at /admin/serverinfo, and that document comes back
    TRIMMED -- `profileInfo` and nothing else -- for a credential that
    is not a full server administrator. Measured for `create-realm`
    alone and for `create-realm` + `view-realm`; the second one buys no
    version and costs the ability to enumerate every realm on the
    server, which is every club on it.

    So what is kept is the substance of K3.1: nothing is created
    against a version nobody has checked, and where OAAP cannot check
    it, a HUMAN states it and OAAP says everywhere that this number was
    stated and not read. An assertion never overrides a reading -- if
    the server does say a version, that is the one that counts, and a
    contradicting assertion is refused like any other mismatch.
    """
    bad = kind_refusal(kind)
    if bad:
        return "", bad
    c = connector_of(kind)
    said = (said or "").strip()
    accepted = (accepted or "").strip()
    if said:
        if said == c["pinned"] or said == accepted:
            return MEASURED, ""
        return "", (
            f"this build was measured against {c['product']} {c['pinned']} "
            f"(on {c['measured']}), and {c['version_path']} says {said}. "
            f"OAAP creates {c['space_word']}s through this server's admin "
            "API, and that integration ages invisibly -- so it refuses "
            f"instead of guessing. Test {said} on oaap-test, then say "
            f"--accept-version {said}.")
    if accepted:
        return ASSERTED, ""
    return "", (
        f"{c['version_path']} answered, but named no version. At "
        f"{c['product']} that document is trimmed for a credential that "
        "is not a full server administrator -- and the narrow one this "
        "was designed for deliberately is not (measured 2026-09-23). "
        "Read the version yourself and state it: `--accept-version "
        f"{c['pinned']}`. It is then recorded as STATED, not read, "
        "everywhere OAAP prints it (RFC-0041 K3.1/K3.4).")


def version_refusal(kind, said, accepted=""):
    """Why this server may not be managed. '' when it may."""
    return version_check(kind, said, accepted)[1]


def version_words(how, version, kind):
    """How an operator should read this number."""
    c = connector_of(kind)
    if how == MEASURED:
        return (f"{version} -- read from {c.get('version_path', 'the server')}"
                + ("" if version == c.get("pinned")
                   else ", and accepted by hand"))
    if how == ASSERTED:
        return (f"{version} -- STATED by the operator; this credential "
                "cannot read it")
    return "unknown"


def _keycloak_space_body(space, title=""):
    """The realm OAAP asks Keycloak for.

    Self-registration and the second factor are deliberately NOT set
    here. They are RFC-0041 step 6, and K7 says the tenant's policy and
    the realm's switches move together -- setting one of them now would
    leave a `tenant_admin` reading one number and living under another.
    """
    return {
        "realm": space,
        "enabled": True,
        "displayName": (title or "").strip() or space,
        "registrationAllowed": False,
        "loginWithEmailAllowed": True,
        "duplicateEmailsAllowed": False,
        "sslRequired": "external",
    }


def _keycloak_client_body(client_id, redirect_uris, name=""):
    """The client OAAP presents itself as at Keycloak.

    CONFIDENTIAL -- `publicClient: false`, standard flow and nothing
    else -- because that is the premise the whole login rests on: the
    code is exchanged for the token over a back channel that proves who
    answered, which is why `idp.jwt_claims` does not verify a
    signature. A public client would take that proof away and leave the
    signature check we do not do as the only thing standing.

    The password grant is off as well. OAAP never sees a member's
    password and has no use for one that would let it ask for a token
    with somebody's credentials.
    """
    return {
        "clientId": client_id,
        "name": (name or "").strip() or f"OAAP ({client_id})",
        "protocol": "openid-connect",
        "enabled": True,
        "publicClient": False,
        "standardFlowEnabled": True,
        "implicitFlowEnabled": False,
        "directAccessGrantsEnabled": False,
        "serviceAccountsEnabled": False,
        "redirectUris": list(redirect_uris or []),
        "webOrigins": [],
    }


# The one place where a product's own field names live. A second
# connector is a file with two of these and a row in CONNECTOR_KINDS.
_BODIES = {
    "keycloak": (_keycloak_space_body, _keycloak_client_body),
}


def space_body(kind, space, title=""):
    """The request that creates a tenant's space at this product."""
    maker = _BODIES.get((kind or "").strip().lower())
    return maker[0](space, title) if maker else {}


def client_body(kind, client_id, redirect_uris, name=""):
    """The request that creates OAAP's client at this product."""
    maker = _BODIES.get((kind or "").strip().lower())
    return maker[1](client_id, redirect_uris, name) if maker else {}


def body_kinds():
    """Which kinds can actually build their requests.

    Held against `CONNECTOR_KINDS` by the test: a row in the table with
    no bodies behind it is a connector that refuses at the last moment
    instead of at the first.
    """
    return tuple(sorted(_BODIES))


def client_uuid(doc, client_id):
    """The internal id of a client, out of a search by clientId.

    Exact match only. A provider that answers a search with something
    near enough is answering a different question.
    """
    if not isinstance(doc, list):
        return ""
    for c in doc:
        if isinstance(c, dict) and (c.get("clientId") or "") == client_id:
            return str(c.get("id") or "")
    return ""


def secret_from(doc):
    """The client secret out of the credential answer, or ''."""
    if not isinstance(doc, dict):
        return ""
    if (doc.get("type") or "secret") != "secret":
        return ""
    return str(doc.get("value") or "").strip()


def missing_redirects(client_doc, wanted):
    """Which of our redirect URIs this client does not hold yet."""
    have = set((client_doc or {}).get("redirectUris") or [])
    return [u for u in (wanted or []) if u not in have]


# ---------------------------------------------------------------------------
# The one thing here that speaks to a network

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """An admin call is never followed anywhere.

    The same handler identity uses for the token exchange, and here for
    a sharper reason: this connection carries a credential that can
    create realms. A redirect is somebody else saying "ask over there",
    and over there is not where we decided to send it.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Admin:
    """A provider's admin API, held by this node.

    Speaks only the methods `method_refusal` allows, and asks it on
    every call rather than trusting the call sites. Never raises: every
    method answers with a sentence, because this runs in a CLI an
    operator reads and a traceback is not a sentence.
    """

    def __init__(self, kind, base_url, auth, admin_id, admin_secret,
                 auth_realm="", timeout=15):
        self.kind = (kind or "").strip().lower()
        self.decl = connector_of(self.kind)
        self.base = (base_url or "").rstrip("/")
        self.auth = (auth or "client").strip().lower()
        self.admin_id = (admin_id or "").strip()
        self.admin_secret = (admin_secret or "").strip()
        self.auth_realm = (auth_realm or "").strip() or (
            self.decl.get("auth_realm") or "")
        self.timeout = timeout
        self.token = ""
        self.opener = urllib.request.build_opener(_NoRedirect)
        self.trace = []

    # -- plumbing ---------------------------------------------------
    def _call(self, method, path, body=None, form=None, auth=True):
        """(status, doc, error). `doc` is parsed JSON or None."""
        bad = method_refusal(method)
        if bad:
            return 0, None, bad
        url = path if path.startswith("http") else self.base + path
        data, headers = None, {"Accept": "application/json"}
        if form is not None:
            data = urllib.parse.urlencode(form).encode("ascii")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if auth and self.token:
            headers["Authorization"] = "Bearer " + self.token
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method=method.upper())
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                raw = r.read()
                status = r.getcode()
        except urllib.error.HTTPError as e:
            raw, status = (e.read() or b""), e.code
        except Exception as e:                        # noqa: BLE001
            return 0, None, f"{self.base} could not be reached: {e}"
        doc = None
        if raw:
            try:
                doc = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                doc = None
        return status, doc, ""

    def _note(self, line):
        self.trace.append(line)

    # -- the verbs --------------------------------------------------
    def login(self):
        """(ok, sentence). Obtain an admin token."""
        bad = kind_refusal(self.kind)
        if bad:
            return False, bad
        if self.auth == "client":
            form = {"grant_type": "client_credentials",
                    "client_id": self.admin_id,
                    "client_secret": self.admin_secret}
        else:
            form = {"grant_type": "password", "client_id": "admin-cli",
                    "username": self.admin_id, "password": self.admin_secret}
        url = token_url(self.kind, self.base, self.auth_realm)
        status, doc, err = self._call("POST", url, form=form, auth=False)
        if err:
            return False, err
        if status != 200 or not isinstance(doc, dict):
            return False, (f"the admin credential was refused by "
                           f"{url} ({status}) -- check the name, the secret "
                           f"and the realm '{self.auth_realm}'")
        self.token = str(doc.get("access_token") or "").strip()
        if not self.token:
            return False, "the provider returned no admin token"
        return True, f"the credential was accepted by {self.base}"

    def version(self):
        """(said, error)."""
        status, doc, err = self._call("GET", self.decl["version_path"])
        if err:
            return "", err
        if status != 200:
            return "", (f"{self.decl['version_path']} answered {status} -- "
                        "this credential may not be allowed to ask, or this "
                        f"is not a {self.decl['product']}")
        return version_said(self.kind, doc), ""

    def _path(self, name, space="", uuid="", query=None):
        return path_of(self.kind, name, space=space, uuid=uuid, query=query)

    def find_space(self, space):
        """(exists, error).

        A 403 is NOT "absent". It means this space is there and this
        credential may not look at it -- which on a shared server is
        somebody else's club, and creating over it is the last thing
        anybody wants. Loudly, per K3.2.
        """
        word = self.decl["space_word"]
        status, _doc, err = self._call("GET", self._path("space", space=space))
        if err:
            return False, err
        if status == 200:
            return True, ""
        if status == 404:
            return False, ""
        if status == 403:
            return False, (f"this credential may not look at the {word} "
                           f"'{space}': either it belongs to somebody else "
                           "on this server, or the grant is missing")
        return False, f"asking for the {word} '{space}' answered {status}"

    def create_space(self, space, title=""):
        """(ok, sentence)."""
        status, doc, err = self._call("POST", self._path("spaces"),
                                      body=space_body(self.kind, space, title))
        if err:
            return False, err
        if status in (201, 204):
            self._note(f"created the {self.decl['space_word']} '{space}'")
            return True, ""
        if status == 409:
            return True, ""
        return False, (f"creating the {self.decl['space_word']} '{space}' "
                       f"answered {status}"
                       + (f": {doc.get('errorMessage')}"
                          if isinstance(doc, dict) and doc.get("errorMessage")
                          else ""))

    def find_client(self, space, client_id):
        """(uuid, doc, error)."""
        status, doc, err = self._call(
            "GET", self._path("clients", space=space,
                              query={"clientId": client_id}))
        if err:
            return "", {}, err
        if status != 200:
            return "", {}, f"looking for the client answered {status}"
        uuid = client_uuid(doc, client_id)
        found = {}
        if uuid and isinstance(doc, list):
            found = next((c for c in doc if c.get("id") == uuid), {})
        return uuid, found, ""

    def create_client(self, space, client_id, redirect_uris, name=""):
        """(ok, sentence)."""
        body = client_body(self.kind, client_id, redirect_uris, name)
        status, doc, err = self._call(
            "POST", self._path("clients", space=space), body=body)
        if err:
            return False, err
        if status in (201, 204):
            self._note(f"created the client '{client_id}'")
            return True, ""
        if status == 409:
            return True, ""
        return False, (f"creating the client '{client_id}' answered {status}"
                       + (f": {doc.get('errorMessage')}"
                          if isinstance(doc, dict) and doc.get("errorMessage")
                          else ""))

    def add_redirects(self, space, uuid, client_doc, wanted):
        """(ok, sentence). Add ours, keep theirs.

        The list is extended, never replaced. A client OAAP finds may
        belong to somebody else's arrangement, and K3.3 says managing
        is not owning: taking a redirect URI away is the sort of edit
        that breaks a login nobody was thinking about.
        """
        absent = missing_redirects(client_doc, wanted)
        if not absent:
            return True, ""
        body = dict(client_doc or {})
        body["redirectUris"] = list(
            (client_doc or {}).get("redirectUris") or []) + absent
        status, _doc, err = self._call(
            "PUT", self._path("client", space=space, uuid=uuid), body=body)
        if err:
            return False, err
        if status not in (200, 204):
            return False, f"adding the redirect URI answered {status}"
        self._note("added this node's redirect URI to the existing client: "
                   + ", ".join(absent))
        return True, ""

    def client_secret(self, space, uuid):
        """(secret, error)."""
        status, doc, err = self._call(
            "GET", self._path("client_secret", space=space, uuid=uuid))
        if err:
            return "", err
        if status != 200:
            return "", f"fetching the client secret answered {status}"
        secret = secret_from(doc)
        if not secret:
            return "", ("the provider returned no client secret -- is this "
                        "client confidential?")
        return secret, ""

    # -- the whole of it --------------------------------------------
    def provision(self, space, client_id, redirect_uris, title="",
                  accept_version=""):
        """Make a tenant's space and client. (ok, result, sentence).

        `result` carries what OAAP keeps: issuer, client id, secret and
        the version actually measured. Nothing is created before the
        version has been checked -- `plan_refusal` says that must be
        true of the plan, and this is the plan being followed.
        """
        plan = provision_plan(self.kind, space, client_id)
        bad = plan_refusal(plan)
        if bad:
            return False, {}, bad
        bad = space_refusal(self.kind, space)
        if bad:
            return False, {}, bad
        ok, msg = self.login()
        if not ok:
            return False, {}, msg
        said, err = self.version()
        if err:
            return False, {}, err
        how, bad = version_check(self.kind, said, accept_version)
        if bad:
            return False, {}, bad
        seen = said or (accept_version or "").strip()
        self._note(f"{self.decl['product']} "
                   + version_words(how, seen, self.kind))
        exists, err = self.find_space(space)
        if err:
            return False, {}, err
        if exists:
            self._note(f"the {self.decl['space_word']} '{space}' was already "
                       "there and is used as it is")
        else:
            ok, msg = self.create_space(space, title)
            if not ok:
                return False, {}, msg
        uuid, doc, err = self.find_client(space, client_id)
        if err:
            return False, {}, err
        if not uuid:
            ok, msg = self.create_client(space, client_id, redirect_uris,
                                         name=title)
            if not ok:
                return False, {}, msg
            uuid, doc, err = self.find_client(space, client_id)
            if err:
                return False, {}, err
            if not uuid:
                return False, {}, ("the client was created and cannot be "
                                   "found again -- refusing to guess")
        else:
            ok, msg = self.add_redirects(space, uuid, doc, redirect_uris)
            if not ok:
                return False, {}, msg
        secret, err = self.client_secret(space, uuid)
        if err:
            return False, {}, err
        return True, {
            "kind": self.decl["provider_kind"],
            "issuer": issuer_for(self.kind, self.base, space),
            "client_id": client_id,
            "client_secret": secret,
            "version": seen,
            "version_how": how,
            "space": space,
        }, f"'{space}' is ready at {self.base}"
