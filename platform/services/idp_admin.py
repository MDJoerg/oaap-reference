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

A connector is these verbs and nothing else:

    version   what the product says it is, so the pinned number is
              CHECKED rather than merely written down
    space     the place one tenant's people live in (Keycloak: a realm)
    client    the confidential OIDC client OAAP presents itself as
    issuer    the URL that space writes into its tokens
    settings  the two switches of K7 INSIDE that space -- who may
              register themselves, and whether a second factor is asked
              for (RFC-0041 step 6, built 2026-09-23)
    export    that space and its people in one file, for the move
              (RFC-0041 K6, step 7, built 2026-09-23)

The first four are required of any connector; `settings` and `export`
are optional and declared, so a product that cannot do one says so
instead of failing at an operator. `later` is empty now, and `users`
is in `never` rather than in `later`, because OAAP does not create
people in somebody's realm and is not going to start. A verb that is
named and absent is a smaller lie than one that is silently missing.

`export` is the verb that broke the shape of this file in a useful
way, and it is worth saying why. Every other verb is an HTTP call to
the product's admin API. This one is NOT -- not at Keycloak, and the
reason was measured rather than reasoned: the admin API's export
answers 200, carries the clients and the client secret, and contains
**not one person**. So a connector row now says which DOOR a verb
comes through, and `export` at Keycloak comes through the product's
own tool, run in a throwaway container beside the database the
serving one uses. That door is not a detail of Keycloak. It is the
first evidence that "a connector" is not the same thing as "an API
client", and the table had room for the difference.

`settings` carries the rule that this step is about, and it is the
second half of the sentence Joerg wrote the shape with -- *make the
settings that matter to us, and then write them into our own
configuration*. What is written into our configuration is what the
space ANSWERED afterwards, never what OAAP asked for. The same
distinction as `measured` against `asserted` one level up: a record
that says what somebody intended, while the realm says something else,
is worse than no record, because the operator reads it and stops
looking.

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
            "required_action":
                "/admin/realms/{space}/authentication/required-actions/"
                "{alias}",
        },
        # K7's two switches in this product's own words. `where` names
        # the document that holds the switch -- at Keycloak they live
        # in two different ones, which is exactly the sort of thing a
        # table is for and an `if` in the middle of a plan is not.
        "switches": {
            "self_registration": {
                "where": "space", "field": "registrationAllowed"},
            "second_factor": {
                "where": "required_action", "alias": "CONFIGURE_TOTP"},
        },
        # Where a credential is redeemed for an admin token. `master`
        # is Keycloak's server realm; see `credential_note`.
        "auth_realm": "master",
        "token_template": "{base}/realms/{realm}/protocol/openid-connect/token",
        "auth_kinds": ("client", "password"),
        # The move's export (K6, step 7), and the one verb here that
        # is not an HTTP call. `door` says so out loud. What the
        # measuring found, on 2026-09-23 against 26.7.4:
        #
        #   * POST /admin/realms/{r}/partial-export answers 200 and
        #     carries clients, roles, groups and the CLIENT SECRET --
        #     and no users at all. A file that looks complete.
        #   * `kc.sh export` run inside the SERVING container finds no
        #     KC_DB_* there (the entrypoint exports them into its own
        #     process, so `docker exec` never sees them), silently
        #     falls back to the built-in empty H2, and writes a
        #     flawless export of a realm nobody has ever used. Exit 0.
        #     It also re-persists that container's configuration, which
        #     is a second reason not to run it there.
        #   * The same tool in a THROWAWAY container, on the same
        #     private network and the serving container's database,
        #     writes the realm with its people and their password
        #     hashes.
        #
        # So: the third door, and `move.export_count_refusal` counts
        # the file afterwards -- because two of those three doors
        # produce a wrong file without failing, and a rule that counts
        # does not care how the next door is built.
        "export": {
            "door": "container",
            # Which OAAP app carries this product, so a connector can
            # be matched to the instance that serves it. Without an
            # instance on THIS node there is no container door, and
            # `oaap idp export` says that instead of guessing.
            "app_id": "keycloak",
            "service": "keycloak",
            "db_service": "db",
            "dir": "/tmp/oaap-export",
            "file": "/tmp/oaap-export/{space}-realm.json",
            # Where the product itself says how many people are in a
            # space. The counting rule needs a second opinion, and it
            # has to come from the provider, not from the file.
            "count_path": "/admin/realms/{space}/users/count",
            "people_key": "users",
        },
        "verbs": ("version", "space", "client", "issuer", "settings",
                  "export"),
        # Empty since step 7, and left here on purpose: a connector
        # that grows a verb should have somewhere to name it first.
        "later": (),
        # Named and never to be built: OAAP does not create, change or
        # remove people in somebody's realm -- that is the club's, and
        # K3.3 is the whole reason this file has no DELETE.
        "never": ("users",),
        "provider_kind": "oidc",
    },
}

# What every connector must be able to do to be usable at all.
REQUIRED_VERBS = ("version", "space", "client", "issuer")

# What a connector MAY declare. A verb outside both lists is a typo
# that would otherwise sit in the table looking like a capability.
OPTIONAL_VERBS = ("settings", "export")
KNOWN_VERBS = REQUIRED_VERBS + OPTIONAL_VERBS

# The switches of K7, in OAAP's words rather than any product's. The
# values are OAAP's too: a product that spells them differently spells
# them differently in its own row, not here.
SWITCHES = ("self_registration", "second_factor")

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


def declares(kind, verb):
    """Whether this connector says it can do this verb at all.

    Asked before every optional verb is used. The four required ones
    are checked once, by `kind_refusal`; the optional ones have to be
    asked about here, because "this build cannot do it at this product"
    is an answer an operator can act on and a traceback is not.
    """
    return (verb or "") in (connector_of(kind).get("verbs") or ())


def verb_refusal(kind, verb):
    """Why this connector cannot be asked for this verb."""
    bad = kind_refusal(kind)
    if bad:
        return bad
    c = connector_of(kind)
    if declares(kind, verb):
        return ""
    if verb in (c.get("never") or ()):
        return (f"OAAP does not do '{verb}' at any provider, and this is "
                "not a gap: the people in a space are the tenant's, not "
                "OAAP's (RFC-0041 K3.3)")
    if verb in (c.get("later") or ()):
        return (f"'{verb}' is named in this connector and not built yet "
                f"-- {c['product']} can do it, OAAP cannot")
    return f"'{verb}' is not something a connector does"


def path_of(kind, name, space="", uuid="", alias="", query=None):
    """One of this connector's addresses, filled in.

    Both the plan and the run come through here. A plan that printed
    one address while the run called another would be a dry run that
    reassures about something else.
    """
    c = connector_of(kind)
    tpl = ((c.get("paths") or {}).get(name) or "")
    if not tpl:
        return ""
    path = tpl.format(space=space, uuid=uuid, alias=alias)
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


def incoherent_verbs(kind):
    """Verbs this connector says two things about, or no thing at all.

    A row that declares `users` and also lists it under `never` is not
    a small inconsistency -- it is a table saying two things about
    whether OAAP touches somebody's members. Caught here, at the
    contract, rather than at the door.

    `later` is read the same way, and that was a gap until step 7
    emptied it: a typo there named nothing, promised nothing and sat
    in the table looking like a considered position. `later` is a list
    of PROMISES, so every entry has to be a verb that could be kept.

    `never` deliberately is not held to that. What it names is not a
    verb this build has -- `users` is not in KNOWN_VERBS and must not
    be, because the whole point of the entry is that OAAP will not
    grow one. What `never` IS held to is not saying the opposite of
    another list.
    """
    c = connector_of(kind)
    have = set(c.get("verbs") or ())
    later = set(c.get("later") or ())
    never = set(c.get("never") or ())
    unknown = tuple(v for v in sorted(have | later)
                    if v not in KNOWN_VERBS)
    both = tuple(v for v in sorted((have & never) | (have & later)
                                   | (later & never)))
    return unknown + both


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
    muddled = incoherent_verbs(kind)
    if muddled:
        return (f"the connector '{kind}' is not coherent about "
                f"{', '.join(muddled)}: a verb is known and claimed, or "
                "it is sworn off, and never both")
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
        if step.get("writes") and i <= where:
            return (f"'{step.get('verb')}' would write before the version "
                    "has been checked (RFC-0041 K3.2)")
        if (step.get("door") or "api") == "container":
            # Not an HTTP call: the product's own tool, run beside its
            # database (step 7's export). Judged by the two rules that
            # actually apply -- it comes after the version check, and
            # it changes nothing at the provider -- and not by a method
            # it does not have. A container step that WRITES is refused
            # outright: K3.3 does not get an exception for arriving
            # through a different door.
            if step.get("writes"):
                return (f"'{step.get('verb')}' would change the provider "
                        "through its own tool rather than its API, and "
                        "that is not a door OAAP writes through "
                        "(RFC-0041 K3.3)")
            if not (step.get("run") or ""):
                return (f"'{step.get('verb')}' has no command at this "
                        "connector")
            continue
        bad = method_refusal(step.get("method"))
        if bad:
            return bad
        if not (step.get("path") or ""):
            return f"'{step.get('verb')}' has no address at this connector"
    return ""


def plan_lines(plan):
    """The plan as an operator reads it."""
    out = []
    for step in plan:
        mark = {"absent": "if absent", "found": "if present",
                "different": "if it differs"}.get(step.get("when"), "")
        if (step.get("door") or "api") == "container":
            out.append(f"  {'RUN':<5}{step.get('run', '')}")
            out.append("        in a THROWAWAY container from this "
                       "instance's own image -- the serving one is not "
                       "touched")
        else:
            out.append(f"  {step['method']:<5}{step['path']}")
        out.append(f"        {mark + ': ' if mark else ''}{step['why']}")
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

    Born CLOSED: `registrationAllowed` is false, and it stays false
    until somebody decides otherwise in the open. Step 6 is what moves
    it, and it moves the tenant's record in the same act -- because K7
    says a `tenant_admin` must not read one number and live under
    another.

    Creation still sets it explicitly rather than leaving it to the
    product's default. A default is somebody else's decision that
    changes in somebody else's release.
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


def _keycloak_settings_read(docs):
    """Keycloak's two documents, in OAAP's two words.

    `registrationAllowed` is a field of the realm and reads straight
    across. The second factor is not a field at all: it is the required
    action CONFIGURE_TOTP, and "required" means `enabled` AND
    `defaultAction` together. The first half only makes the second
    factor possible; the second is what makes Keycloak ask for it.
    """
    out = {}
    realm = (docs or {}).get("space")
    if isinstance(realm, dict):
        out["self_registration"] = bool(realm.get("registrationAllowed"))
    action = (docs or {}).get("required_action")
    if isinstance(action, dict):
        out["second_factor"] = (
            "required" if (action.get("enabled")
                           and action.get("defaultAction")) else "off")
    return out


def _keycloak_settings_write(switch, value, doc, space):
    """The ONE call that moves one switch: (where, method, body).

    The realm is written PARTIALLY -- its name and the single field --
    and not as a whole document read back and sent again. K3.3 again: a
    realm OAAP manages may hold settings nobody in this build has heard
    of, and returning a whole representation is how those quietly
    become whatever this build happens to think they are. Keycloak
    applies the fields a request names and leaves the rest alone.

    The required action is written whole, because there the document IS
    the switch: a handful of fields, all of them about this one thing.

    "off" leaves the action ENABLED and only clears `defaultAction`,
    which is Keycloak's own resting state: members who have already set
    up a second factor keep being asked for it. Disabling the action
    outright would take something away from people who chose it, and
    taking things away from a club's members is not what this switch
    is for.
    """
    if switch == "self_registration":
        return "space", "PUT", {"realm": space,
                                "registrationAllowed": bool(value)}
    if switch == "second_factor":
        body = dict(doc or {})
        body["enabled"] = True
        body["defaultAction"] = (value == "required")
        return "required_action", "PUT", body
    return "", "", {}


def _keycloak_export_shell(space, where):
    """Keycloak's own export, as one line for a throwaway container.

    `--users realm_file` puts the people INTO the realm file rather
    than beside it, so there is one file to move and one file to
    count. The log goes to stderr and the file to stdout, and the
    caller catches stdout into a 0600 file on the node: the club's
    credentials never touch a directory two processes can see. The
    same reason step 4 piped a client secret straight into the command
    that needed it.
    """
    entry = "/opt/keycloak/bin/kc.sh"
    return (f"{entry} export --dir {where['dir']} --realm {space} "
            f"--users realm_file >&2 && "
            f"cat {where['file'].format(space=space)}")


def _keycloak_export_env(space, where):
    """What the throwaway container needs to see the REAL database.

    The measured trap, in two lines of configuration. Keycloak's OAAP
    app exports KC_DB_* inside its entrypoint, so they live in the
    serving PROCESS and not in the container's environment -- and a
    tool started next to it therefore sees none of them and quietly
    uses its own empty one instead.

    `from_serving` names what has to be copied across from the serving
    container rather than written here, because it is a secret and
    belongs in exactly one place.
    """
    svc = where.get("db_service", "db")
    return ({"KC_DB": "postgres",
             "KC_DB_URL": f"jdbc:postgresql://{svc}:5432/postgres",
             "KC_DB_USERNAME": "postgres"},
            {"KC_DB_PASSWORD": "POSTGRES_PASSWORD"})


def _keycloak_export_people(doc):
    """The people in an export file, as this product writes them."""
    return list((doc or {}).get("users") or [])


# The one place where a product's own field names live. A second
# connector is a file with these functions and a row in
# CONNECTOR_KINDS.
_BODIES = {
    "keycloak": (_keycloak_space_body, _keycloak_client_body),
}

_SETTINGS = {
    "keycloak": (_keycloak_settings_read, _keycloak_settings_write),
}

_EXPORTS = {
    "keycloak": (_keycloak_export_shell, _keycloak_export_env,
                 _keycloak_export_people),
}


# ---------------------------------------------------------------------------
# The settings verb (RFC-0041 K7, step 6)
#
# Two switches, and the whole difficulty is that each of them exists in
# two places: in the space, where it decides what actually happens, and
# in OAAP's record, where a tenant_admin reads it. This section keeps
# them one thing by never letting OAAP write down what it merely asked
# for.

SWITCH_VALUES = {
    "self_registration": (True, False),
    "second_factor": ("required", "off"),
}


def switch_refusal(kind, switch, value):
    """Why this switch may not be moved to this value."""
    bad = verb_refusal(kind, "settings")
    if bad:
        return bad
    c = connector_of(kind)
    if switch not in SWITCHES:
        return (f"'{switch}' is not a switch OAAP knows -- "
                f"{', '.join(SWITCHES)} (RFC-0041 K7)")
    if switch not in (c.get("switches") or {}):
        return (f"{c['product']} has no {c['space_word']} switch for "
                f"'{switch}' in this build")
    if value not in SWITCH_VALUES[switch]:
        allowed = ", ".join(str(v).lower() for v in SWITCH_VALUES[switch])
        return f"'{value}' is not a value for '{switch}' -- {allowed}"
    return ""


def switch_where(kind, switch):
    """Which of this product's documents holds this switch."""
    c = connector_of(kind)
    return ((c.get("switches") or {}).get(switch) or {}).get("where", "")


def switch_path(kind, space, switch):
    """The address of the document that holds this switch."""
    c = connector_of(kind)
    sw = (c.get("switches") or {}).get(switch) or {}
    if not sw:
        return ""
    return path_of(kind, sw["where"], space=space, alias=sw.get("alias", ""))


def settings_of(kind, docs):
    """What the space's own documents say, in OAAP's words.

    `docs` is {where: document}, and a `where` that is missing simply
    yields no answer for its switch -- a switch OAAP did not read is
    absent from the result rather than defaulting to something
    comfortable.
    """
    reader = _SETTINGS.get((kind or "").strip().lower())
    return reader[0](docs or {}) if reader else {}


def settings_call(kind, switch, value, doc, space):
    """The one call that moves one switch: (where, method, body)."""
    writer = _SETTINGS.get((kind or "").strip().lower())
    if not writer:
        return "", "", {}
    return writer[1](switch, value, doc, space)


def settings_kinds():
    """Which kinds can actually read and write their switches.

    Held against the table by the test, the same way `body_kinds` is: a
    row that declares `settings` with nothing behind it is a connector
    that refuses at the last moment instead of at the first.
    """
    return tuple(sorted(_SETTINGS))


def settings_plan(kind, space, wants):
    """Every call moving these switches may make, in order.

    Judged by the same `plan_refusal` as provisioning, and that is the
    point of it being a plan at all: the version comes first, nothing
    writes before it, nothing deletes.

    The READ-BACK is part of the plan rather than an afterthought. What
    OAAP records is what the space answers once the writing is done --
    so an operator reading `--dry-run` sees that too, and a test can
    assert that the last word belongs to the provider.
    """
    if verb_refusal(kind, "settings"):
        return []
    c = connector_of(kind)
    word = c["space_word"]
    wants = {k: v for k, v in (wants or {}).items() if k in SWITCHES}
    steps = [
        {"verb": "version", "method": "GET", "when": "always", "writes": False,
         "path": c["version_path"],
         "why": f"which {c['product']} is this? a switch read at the wrong "
                "version is read wrongly"},
    ]
    reads = []
    for switch in SWITCHES:
        where = switch_where(kind, switch)
        if where and where not in reads:
            reads.append(where)
            steps.append(
                {"verb": "settings", "method": "GET", "when": "always",
                 "writes": False, "path": switch_path(kind, space, switch),
                 "why": f"what does the {word} say about "
                        f"{switch.replace('_', ' ')} right now?"})
    for switch in SWITCHES:
        if switch not in wants:
            continue
        steps.append(
            {"verb": "settings", "method": "PUT", "when": "different",
             "writes": True, "path": switch_path(kind, space, switch),
             "why": f"set {switch.replace('_', ' ')} to "
                    f"'{switch_word(switch, wants[switch])}' -- and only "
                    "this field of that document"})
    for where in reads:
        switch = next(s for s in SWITCHES if switch_where(kind, s) == where)
        steps.append(
            {"verb": "settings", "method": "GET", "when": "always",
             "writes": False, "path": switch_path(kind, space, switch),
             "why": "read it back: what OAAP writes into its own "
                    f"configuration is what the {word} says afterwards, "
                    "never what OAAP asked for"})
    return steps


def switch_word(switch, value):
    """One switch's value as a person says it."""
    if switch == "self_registration":
        return "on" if value else "off"
    return str(value or "off")


def settings_words(kind, settings):
    """The switches as an operator reads them."""
    c = connector_of(kind)
    word = c.get("space_word", "space")
    out = []
    for switch in SWITCHES:
        if switch not in (settings or {}):
            continue
        said = switch_word(switch, settings[switch])
        if switch == "self_registration":
            out.append(f"self-registration in the {word}: {said}"
                       + ("  (anybody who can reach the page gets an "
                          "identity here)" if said == "on" else ""))
        else:
            out.append(f"second factor in the {word}: {said}"
                       + ("  (asked of everybody who joins from now on)"
                          if said == "required" else ""))
    return out


def settings_reach(kind, switch, value):
    """What a switch does NOT reach, said before anybody assumes it does.

    Measured at Keycloak 26.7.4 and true of the product rather than of
    this build: a required action marked as a default action is handed
    to people who arrive AFTER it was marked. The members already in
    the realm are not asked retroactively -- Keycloak would need the
    action written onto each of them, one person at a time, and OAAP
    does not write onto people (K3.3, and `never: users`).

    A switch whose reach is smaller than its name is the sort of thing
    an operator has to be told once, out loud, rather than discover
    when an audit asks who actually has a second factor.
    """
    c = connector_of(kind)
    if switch == "second_factor" and value == "required":
        return (f"This asks it of everybody who joins the "
                f"{c['space_word']} from now on. The members already in "
                "it are not asked retroactively -- that would mean "
                f"writing onto each person in the {c['space_word']}, and "
                "OAAP does not touch a club's people (RFC-0041 K3.3). "
                f"{c['product']} can do it per person in its own console.")
    if switch == "self_registration" and value:
        return ("Whoever reaches the registration page gets an identity "
                "in this tenant. What that identity MAY do is OAAP's "
                "first-login policy and nothing the provider asserts "
                "(RFC-0041 K4).")
    return ""


def settings_disagreement(kind, wanted, got):
    """Why a read-back may not be recorded as success. '' when it may.

    The sharpest sentence of step 6. OAAP asked the space to be one
    way, the space says it is another -- and the temptation is to
    record the intention, because that is what the operator typed and
    it reads better. Recording it would put a number in front of a
    tenant_admin that nothing in the world backs up.
    """
    c = connector_of(kind)
    for switch, value in (wanted or {}).items():
        if switch not in (got or {}):
            return (f"the {c['space_word']} was asked about "
                    f"'{switch.replace('_', ' ')}' afterwards and did not "
                    "answer -- so nothing is known, and nothing that is "
                    "not known is written down")
        if got[switch] != value:
            return (f"the {c['space_word']} was told to set "
                    f"'{switch.replace('_', ' ')}' to "
                    f"'{switch_word(switch, value)}' and says it is "
                    f"'{switch_word(switch, got[switch])}'. OAAP records "
                    "what the provider says, so the record now holds the "
                    "provider's answer and not the instruction "
                    "(RFC-0041 K7)")
    return ""


# ---------------------------------------------------------------------------
# The export verb (RFC-0041 K6, step 7)
#
# The move needs one file: the club's space, its people, and their
# credentials. Everything here is about WHERE that file may come from
# and how it is judged afterwards -- never about trusting the tool
# that wrote it.

def export_of(kind):
    """This connector's export declaration, or {}."""
    return dict(connector_of(kind).get("export") or {})


def export_refusal(kind):
    """Why this connector cannot be asked for an export. '' when it can."""
    bad = verb_refusal(kind, "export")
    if bad:
        return bad
    if not export_of(kind):
        return (f"'{kind}' declares the verb 'export' and says nothing "
                "about how to reach it -- that is a connector row to fix, "
                "not something an operator can work around")
    return ""


def export_door(kind):
    """'container', 'api', or '' -- how an export is reached here.

    Asked out loud because the answer decides what the operator needs
    on this node. A container door means the product has to BE here;
    an API door would work against somebody else's server.
    """
    return (export_of(kind).get("door") or "")


def export_app_id(kind):
    """Which OAAP app carries this product, for finding the instance."""
    return (export_of(kind).get("app_id") or "")


def export_file(kind, space):
    """Where the product writes the file, inside its own container."""
    tpl = export_of(kind).get("file") or ""
    return tpl.format(space=space) if tpl else ""


def export_count_path(kind, space):
    """Where the product says how many people are in a space.

    The second opinion the counting rule needs. It has to come from
    the provider rather than from the file, or the file would be
    checked against itself.
    """
    tpl = export_of(kind).get("count_path") or ""
    return tpl.format(space=space) if tpl else ""


def export_shell(kind, space):
    """The one command a throwaway container runs, or ''."""
    fn = _EXPORTS.get((kind or "").strip().lower())
    return fn[0](space, export_of(kind)) if fn else ""


def export_env(kind, space):
    """(env, from_serving) for the throwaway container.

    `from_serving` maps a variable the export needs to the variable of
    the SERVING container it must be copied from. Kept as a mapping
    rather than a value because the thing being copied is a secret and
    has exactly one home.
    """
    fn = _EXPORTS.get((kind or "").strip().lower())
    return fn[1](space, export_of(kind)) if fn else ({}, {})


def export_people(kind, doc):
    """The people an export file carries, as this product writes them."""
    fn = _EXPORTS.get((kind or "").strip().lower())
    return fn[2](doc) if fn else []


def export_plan(kind, space):
    """Every step an export takes, in order.

    Judged by the SAME `plan_refusal` as provisioning and settings,
    which is the reason the container step had to be spelled out in
    the plan rather than done quietly beside it: a step that no rule
    reads is a step no rule applies to.

    The counting step is in the plan for the same reason the read-back
    was in step 6's -- it is not an afterthought, it is what makes the
    file mean anything, and an operator reading `--dry-run` should see
    that the file will be judged before they are given it.
    """
    if export_refusal(kind):
        return []
    c = connector_of(kind)
    word = c["space_word"]
    return [
        {"verb": "version", "method": "GET", "when": "always",
         "writes": False, "path": c["version_path"],
         "why": f"which {c['product']} is this? an export read at the "
                "wrong version is read wrongly"},
        {"verb": "space", "method": "GET", "when": "always", "writes": False,
         "path": path_of(kind, "space", space=space),
         "why": f"does this {word} exist? nothing is exported from a "
                "space OAAP cannot see"},
        {"verb": "export", "method": "GET", "when": "always", "writes": False,
         "path": export_count_path(kind, space),
         "why": f"how many people does the {word} say it has? asked "
                "BEFORE the file exists, so the number is not taken from "
                "the thing it is meant to check"},
        {"verb": "export", "door": "container", "when": "always",
         "writes": False, "run": export_shell(kind, space),
         "why": f"the {c['product']} export of this {word}, with its "
                "people and their credentials, straight into a 0600 file "
                "on this node"},
        {"verb": "export", "method": "GET", "when": "always", "writes": False,
         "path": export_count_path(kind, space),
         "why": "count the file against the provider. Two of the three "
                "doors to an export write a file with no people in it "
                "and do not fail (measured) -- so the file is counted, "
                "never trusted"},
    ]


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

    def _not_ours(self, space, doing):
        """The sentence for a 403, wherever it arrives.

        A 403 is NOT "absent". It says this space is there and this
        credential may not have it -- which on a shared server means
        somebody else's club.

        One sentence on every door, because of where the machine said
        it actually arrives (oaap-test, 2026-09-23). The careful wording
        was written for the REALM lookup, and that one answered 200: a
        `create-realm` credential may see that a realm exists. The
        refusal lands one call later, at the CLIENTS lookup -- and that
        one had the bare status and no sentence. A rule worth saying is
        worth saying at every door it can come through.
        """
        return (f"this credential may not {doing} in the "
                f"{self.decl['space_word']} '{space}'. It exists and it is "
                "not ours: on a shared server that is somebody else's club, "
                "and OAAP manages only what it is allowed to manage "
                "(RFC-0041 K3.3)")

    def find_space(self, space):
        """(exists, error). A 403 here is not "absent" -- see _not_ours."""
        word = self.decl["space_word"]
        status, _doc, err = self._call("GET", self._path("space", space=space))
        if err:
            return False, err
        if status == 200:
            return True, ""
        if status == 404:
            return False, ""
        if status == 403:
            return False, self._not_ours(space, "look")
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
        if status == 403:
            return False, (f"this credential may not create a "
                           f"{self.decl['space_word']} at all -- it needs "
                           "the one grant this was designed around "
                           "(RFC-0041 K3.4)")
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
        if status == 403:
            return "", {}, self._not_ours(space, "look at the clients")
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
        if status == 403:
            return False, self._not_ours(space, "create a client")
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
        if status == 403:
            return False, self._not_ours(space, "change a client")
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
        if status == 403:
            return "", self._not_ours(space, "read a client's secret")
        if status != 200:
            return "", f"fetching the client secret answered {status}"
        secret = secret_from(doc)
        if not secret:
            return "", ("the provider returned no client secret -- is this "
                        "client confidential?")
        return secret, ""

    def read_settings(self, space):
        """(settings, error). What the space says about K7's switches.

        Every document the switches live in, asked for by the table and
        not by a name written here. A `where` that answers 403 gets the
        same sentence as every other door -- a space that exists and is
        not ours is not a space whose switches we report on.
        """
        docs, seen = {}, []
        for switch in SWITCHES:
            where = switch_where(self.kind, switch)
            if not where or where in seen:
                continue
            seen.append(where)
            path = switch_path(self.kind, space, switch)
            status, doc, err = self._call("GET", path)
            if err:
                return {}, err
            if status == 403:
                return {}, self._not_ours(space, "read the sign-in rules")
            if status == 404:
                return {}, (f"{path} is not there at this "
                            f"{self.decl['product']} -- this build was "
                            f"measured against {self.decl['pinned']}")
            if status != 200:
                return {}, f"reading the sign-in rules answered {status}"
            docs[where] = doc
        return settings_of(self.kind, docs), ""

    def count_people(self, space):
        """(count, error). How many people the space says are in it.

        The second opinion `move.export_count_refusal` needs. It is a
        pure read and it is the ONLY thing this class does for the
        export verb -- the file itself comes out of a container, and
        this class speaks HTTP and nothing else.

        `None` on any doubt, never 0: an export checked against a
        number nobody answered would pass exactly when it matters.
        """
        path = export_count_path(self.kind, space)
        if not path:
            return None, (f"this connector does not say where "
                          f"{self.decl['product']} counts the people in a "
                          f"{self.decl['space_word']}")
        status, doc, err = self._call("GET", path)
        if err:
            return None, err
        if status == 403:
            return None, self._not_ours(space, "count the people")
        if status != 200:
            return None, (f"counting the people in "
                          f"'{space}' answered {status}")
        try:
            return int(doc), ""
        except (TypeError, ValueError):
            return None, (f"{self.decl['product']} answered something "
                          f"other than a number when asked how many "
                          f"people are in '{space}'")

    def write_switch(self, space, switch, value, doc):
        """(ok, sentence). Move one switch, and touch nothing else."""
        where, method, body = settings_call(self.kind, switch, value, doc,
                                            space)
        if not where:
            return False, f"this connector cannot move '{switch}'"
        path = switch_path(self.kind, space, switch)
        status, got, err = self._call(method, path, body=body)
        if err:
            return False, err
        if status == 403:
            return False, self._not_ours(space, "change the sign-in rules")
        if status not in (200, 204):
            return False, (f"setting '{switch.replace('_', ' ')}' answered "
                           f"{status}"
                           + (f": {got.get('errorMessage')}"
                              if isinstance(got, dict)
                              and got.get("errorMessage") else ""))
        self._note(f"set {switch.replace('_', ' ')} to "
                   f"'{switch_word(switch, value)}' in the "
                   f"{self.decl['space_word']} '{space}'")
        return True, ""

    def settings(self, space, wants=None, accept_version=""):
        """Read, and if asked move, K7's switches. (ok, settings, sentence).

        `settings` is ALWAYS what the space answered at the end, even
        when this returns not-ok. That is the contract the caller needs:
        the record can then be corrected to the truth in the same breath
        as the failure is reported, instead of being left holding an
        instruction that did not take.

        The version is checked even for a pure read. A document read at
        a product two releases along is read WRONGLY rather than not at
        all -- `defaultAction` meaning something else is exactly the
        kind of ageing K3.1 exists for.
        """
        wants = {k: v for k, v in (wants or {}).items() if k in SWITCHES}
        plan = settings_plan(self.kind, space, wants)
        bad = plan_refusal(plan)
        if bad:
            return False, {}, bad
        for switch, value in wants.items():
            bad = switch_refusal(self.kind, switch, value)
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
        exists, err = self.find_space(space)
        if err:
            return False, {}, err
        if not exists:
            return False, {}, (
                f"there is no {self.decl['space_word']} '{space}' at "
                f"{self.base}. These are switches INSIDE a space, and "
                "this step does not create one -- `oaap idp provision` "
                "does that, deliberately in its own act")
        docs = {}
        for switch in SWITCHES:
            where = switch_where(self.kind, switch)
            if where and where not in docs:
                status, doc, err = self._call(
                    "GET", switch_path(self.kind, space, switch))
                if err:
                    return False, {}, err
                if status == 403:
                    return False, {}, self._not_ours(
                        space, "read the sign-in rules")
                if status != 200:
                    return False, {}, (f"reading the sign-in rules answered "
                                       f"{status}")
                docs[where] = doc
        now = settings_of(self.kind, docs)
        for switch, value in wants.items():
            if now.get(switch) == value:
                self._note(f"{switch.replace('_', ' ')} was already "
                           f"'{switch_word(switch, value)}'")
                continue
            ok, msg = self.write_switch(
                space, switch, value, docs.get(switch_where(self.kind,
                                                            switch)))
            if not ok:
                after, _err = self.read_settings(space)
                return False, (after or now), msg
        after, err = self.read_settings(space)
        if err:
            return False, now, err
        bad = settings_disagreement(self.kind, wants, after)
        if bad:
            return False, after, bad
        seen = said or (accept_version or "").strip()
        self._note(f"{self.decl['product']} "
                   + version_words(how, seen, self.kind))
        return True, after, (f"the {self.decl['space_word']} '{space}' "
                             "answered, and that answer is what OAAP "
                             "writes down")

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
