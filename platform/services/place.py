"""The tenant as a place: which tenant a host names, and what its face is.

RFC-0042 T2/T3. **This module exists because three programs need the
same two answers**, and this project has paid for the alternative five
times in one week: the portal (every page it renders), the identity
service (the login page, which is the FIRST page a club member ever
sees) and `appctl` on the host (which validates what an operator sets
and projects the logo for the gateway).

If those three disagreed about which tenant `cls.example.org` names, or
about what counts as a colour, the result would not be a cosmetic bug.
A login page wearing one club's face in front of another club's portal
is the impersonation T3 forbids in so many words.

So: **pure functions, no file access, no framework.** Each caller reads
`tenants.json` its own way -- they mount it at different paths and
always have -- and then asks here. What is shared is the judgement, not
the plumbing.
"""

import re

# ---------------------------------------------------------------------------
# Which tenant a host names

# Node-wide roles, the same list the portal has always had. Named here
# because T3's first rule is about them: a caller who holds node-wide
# power must be able to SEE that they do, and a themed page is a page
# that looks like it belongs to a customer.
NODE_WIDE_ROLES = ("server_admin", "support")


def host_place(host, ext_host, tenants, default_tid, now_iso):
    """Which tenant this HOST names: (tenant_id_or_None, resolved).

    Three answers, and the third is the whole point:

    * the node's own apex, a LAN name, an operator's CNAME -- no tenant
      in the host, so the portal shows the node view it always did
      (None, True);
    * `<label>.<node>` naming a tenant this node has -- that tenant
      scopes the page for EVERYONE reached through this host, a
      server_admin included (tid, True);
    * `<label>.<node>` naming a tenant this node does NOT have -- serve
      nothing (None, False). Falling back to the operator's own view
      would be the exact substitution the resolution rules exist to
      prevent, and it is the same fail-closed direction
      `tenant_host_prefixes` already takes on the other side.

    A FORMER label resolves for as long as it is unexpired, the same
    grace the gateway gives it -- the gateway writes a site for it, so
    a reader that did not recognise it would send a just-renamed club
    to a page that refuses them (RFC-0026 3.3).

    `now_iso` is passed in rather than read: these are compared as ISO
    strings, which is sound only because every such timestamp is
    written by the host in UTC at the same precision. Anything else
    sorts as expired, which is the safe direction.
    """
    host = (host or "").split(":")[0].lower()
    ext = (ext_host or "").lower()
    if not ext or host == ext or not host.endswith("." + ext):
        return None, True
    label = host[: -len(ext) - 1]
    if "." in label:
        # `<instance>.<label>.<node>` has its own site pointing at the
        # app's container, so this is never reached in practice -- it
        # is answered rather than assumed away.
        return None, True
    tid = tenant_id_by_label(label, tenants, now_iso)
    if tid is None:
        return None, False
    # The default tenant's place IS the apex; its label never appears in
    # a host, so a host that spells it out is not its address.
    if default_tid and tid == default_tid:
        return None, False
    return tid, True


def tenant_id_by_label(label, tenants, now_iso):
    """The tenant answering to this label, current or former, or None."""
    label = (label or "").strip().lower()
    if not label:
        return None
    for tid, t in sorted((tenants or {}).items()):
        if (t.get("label") or "") == label:
            return tid
        for f in (t.get("former_labels") or []):
            if f.get("label") == label and str(f.get("until", "")) > now_iso:
                return tid
    return None


# ---------------------------------------------------------------------------
# The face (T3): what a tenant may set, and what it may never do

HEX_RE = re.compile(r"#[0-9a-f]{6}")
TITLE_MAX = 60

# What the platform looks like when nothing is themed. Kept here so the
# themed and unthemed pages are described in one vocabulary -- a page
# that "has no theme" is a page carrying THESE values, not a page that
# skips a step somewhere.
PLATFORM_PRIMARY = "#1e3a8a"
PLATFORM_ACCENT = "#2563eb"

# A logo is a picture, and it is served from the PLATFORM's own origin
# so that the login page can show it without a session. That rules out
# SVG: an SVG may carry script, and a script served from this origin
# runs beside the session cookie of every user of this node. The
# refusal is not about trusting the operator who uploads it -- it is
# about what the file becomes once it is a URL under our own name.
LOGO_TYPES = {
    "png": (b"\x89PNG\r\n\x1a\n",),
    "jpeg": (b"\xff\xd8\xff",),
    "webp": (b"RIFF",),          # plus "WEBP" at offset 8, checked below
    "gif": (b"GIF87a", b"GIF89a"),
}
LOGO_MAX_BYTES = 512 * 1024


def logo_type(data):
    """What these bytes actually are, by their content -- or "".

    By content and never by the file name: a name is what the uploader
    typed, and this decides what the platform will serve the world
    under its own hostname.
    """
    data = data or b""
    for kind, magics in LOGO_TYPES.items():
        for magic in magics:
            if data.startswith(magic):
                if kind == "webp" and data[8:12] != b"WEBP":
                    continue
                return kind
    return ""


def logo_refusal(data):
    """Why these bytes may not become a tenant's logo, or "".

    One sentence, one place -- the CLI, the portal's form and the host
    worker all ask here, so a refusal cannot depend on which door the
    file came through (the 0.1.115 lesson, applied before it can bite).
    """
    if not data:
        return "the file is empty"
    if len(data) > LOGO_MAX_BYTES:
        return (f"a logo may be at most {LOGO_MAX_BYTES // 1024} KB; this one "
                f"is {len(data) // 1024} KB")
    kind = logo_type(data)
    if not kind:
        if (data.lstrip()[:5].lower() == b"<?xml"
                or b"<svg" in data[:512].lower()):
            return ("an SVG cannot be a logo: it is served from this "
                    "platform's own address, where a picture that can carry "
                    "script sits beside every user's session -- use PNG, "
                    "JPEG, WebP or GIF")
        return ("this is not a picture the platform can serve (PNG, JPEG, "
                "WebP or GIF)")
    return ""


def color_refusal(value):
    """Why this is not a colour, or ""."""
    v = (value or "").strip().lower()
    if not v:
        return ""          # clearing a colour is allowed; see theme_of
    if not HEX_RE.fullmatch(v):
        return (f"'{value}' is not a colour -- write it as six hex digits "
                "with a leading '#', for example #1f4e79")
    return ""


def title_refusal(value):
    """Why this is not a public title, or ""."""
    v = (value or "").strip()
    if len(v) > TITLE_MAX:
        return f"the public title may be at most {TITLE_MAX} characters"
    return ""


def theme_refusal(title, primary, accent):
    """The one gate every door goes through."""
    return (title_refusal(title) or color_refusal(primary)
            or color_refusal(accent))


def face_target(role, own_tenant, wanted=""):
    """Whose place this caller may dress: (tenant_id, refusal).

    A tenant administers its own place and no other. Only the node's
    operator may name a different one, and a request that names one is
    never believed on its own -- the answer is computed from the
    caller's ROLE and their own tenant, here, where both doors ask.

    A function rather than a chain of `if`s inside the worker, for a
    reason this project learned the expensive way: a rule written into
    a branch can only be checked by reading the branch, and a test that
    reads source text goes green the moment somebody changes the
    condition and leaves the sentence.
    """
    if role not in ("server_admin", "tenant_admin"):
        return "", ("changing a tenant's face requires tenant_admin or "
                    "server_admin (oaap.core.tenant 2.3)")
    if wanted and role != "server_admin":
        return "", "a tenant administers only its own place"
    tid = wanted if (role == "server_admin" and wanted) else (own_tenant or "")
    if not tid:
        return "", "no tenant to change"
    return tid, ""


# ---------------------------------------------------------------------------
# Colour arithmetic -- the platform keeps the page readable, the tenant
# chooses the hue. This is the second half of "no stylesheet": four
# values cannot break a layout, but two of them CAN make text vanish
# into its own background, and that is the platform's problem, not the
# club's.

def _rgb(value):
    v = (value or "").strip().lstrip("#")
    return tuple(int(v[i:i + 2], 16) for i in (0, 2, 4))


def _hex(rgb):
    return "#" + "".join(f"{max(0, min(255, int(round(c)))):02x}" for c in rgb)


def mix(value, toward, amount):
    """Move a colour `amount` of the way toward another one."""
    a, b = _rgb(value), _rgb(toward)
    return _hex(tuple(a[i] + (b[i] - a[i]) * amount for i in range(3)))


def luminance(value):
    """Relative luminance, WCAG's formula. 0 is black, 1 is white."""
    def chan(c):
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (chan(c) for c in _rgb(value))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def readable_on(value):
    """Black or white -- whichever can actually be read on this colour."""
    return "#111827" if luminance(value) > 0.45 else "#ffffff"


def ink(value):
    """The same hue, dark enough to be read as TEXT on a white page.

    A tenant that picks a pale primary gets a pale header (with dark
    text on it, see `readable_on`) but not pale writing on white -- the
    same colour has two jobs in this stylesheet and only one of them
    can be satisfied by the value as given.
    """
    out = value
    for _ in range(12):
        if luminance(out) <= 0.2:
            break
        out = mix(out, "#000000", 0.15)
    return out


def theme_of(tenant, label="", ext_host=""):
    """The face of this tenant -- always a complete answer.

    There is no "unthemed" branch anywhere else in the platform: a
    tenant that has set nothing gets the platform's own values here, so
    every page renders through exactly one path. `themed` says whether
    the tenant chose anything, which is what the two T3 rules turn on.

    `title` is the tenant's PUBLIC name and is deliberately NOT the
    `name` on the tenant record. That one is the Klarname, and the page
    that asks for it promises in writing that it stays in the house --
    it may not appear on a login page anyone can open. Unset, the
    public title falls back to the LABEL, which is public by
    construction (it is in the hostname and therefore in the
    certificate transparency log).
    """
    t = tenant or {}
    theme = t.get("theme") or {}
    label = (label or t.get("label") or "").strip().lower()
    primary = (theme.get("color_primary") or "").strip().lower()
    accent = (theme.get("color_accent") or "").strip().lower()
    themed = bool(primary or accent or theme.get("logo")
                  or theme.get("title"))
    primary = primary if HEX_RE.fullmatch(primary or "") else PLATFORM_PRIMARY
    accent = accent if HEX_RE.fullmatch(accent or "") else PLATFORM_ACCENT
    logo = (theme.get("logo") or "").strip().lower()
    return {
        "themed": themed,
        "label": label,
        "title": (theme.get("title") or "").strip() or label,
        "address": f"{label}.{ext_host}" if (label and ext_host) else label,
        "color_primary": primary,
        "color_accent": accent,
        "logo": logo,
        "logo_url": logo_url(label, theme.get("logo_type") or "", logo),
    }


def logo_url(label, kind, sha=""):
    """Where the gateway serves this tenant's logo, or "".

    `/platform/*` is already a public route (RFC-0035 D2), which is
    exactly what the login page needs -- and the Caddyfile comment
    there has said since 0.1 that a per-tenant theme "changes what is
    served here, never this route".

    Named by the LABEL, not by the hash. A hash-named file would be one
    file for two tenants that happen to use the same picture, and this
    platform's byte store refuses cross-tenant deduplication on
    purpose (`oaap.data.files` 2.1). The label is public anyway; the
    hash rides along only so a browser fetches a replaced logo.
    """
    if not label or not kind:
        return ""
    url = f"/platform/place/{label}.{kind}"
    return f"{url}?v={sha[:12]}" if sha else url


def theme_vars(theme):
    """Every value a themed page needs, computed once.

    Two programs paint with these -- the portal's full page and the
    login card -- and they call their CSS variables different things.
    What must NOT differ is the arithmetic, so it lives here and the
    emitters below only spell the names.
    """
    p, a = theme["color_primary"], theme["color_accent"]
    return {
        "header_bg": p,
        "header_bg_deep": mix(p, "#000000", 0.3),
        "header_text": readable_on(p),
        "accent": a,
        "accent_deep": ink(a),
        "accent_pale": mix(a, "#ffffff", 0.86),
        "accent_text": readable_on(a),
        "ink": ink(p),
    }


def theme_style(theme):
    """The CSS a themed PORTAL page adds after the platform stylesheet.

    Nothing but variable assignments, and every value is either a
    validated `#rrggbb` or computed from one -- so a theme cannot
    introduce a rule, a selector or a URL. That is what "no stylesheet"
    (T3) means in practice.
    """
    if not theme or not theme.get("themed"):
        return ""
    v = theme_vars(theme)
    return (
        "<style>:root{"
        f"--oaap-blue-900:{v['header_bg']};"
        f"--oaap-blue-950:{v['header_bg_deep']};"
        f"--oaap-header-text:{v['header_text']};"
        f"--oaap-blue-600:{v['accent']};"
        f"--oaap-blue-700:{v['accent_deep']};"
        f"--oaap-blue-100:{v['accent_pale']};"
        f"--oaap-accent-text:{v['accent_text']};"
        f"--oaap-ink:{v['ink']};"
        "}</style>"
    )


def card_style(theme):
    """The same theme for the login card, which has no header bar.

    There the brand is the mark and the wordmark, so the PRIMARY colour
    becomes writing on white and has to be dark enough to read -- the
    one place where a pale brand colour is silently deepened rather
    than flipping the text around it.
    """
    if not theme or not theme.get("themed"):
        return ""
    v = theme_vars(theme)
    return (
        "<style>:root{"
        f"--brand:{v['ink']};"
        f"--blue-600:{v['accent']};"
        f"--blue-700:{v['accent_deep']};"
        f"--btn-text:{v['accent_text']};"
        "}</style>"
    )


def platform_chrome(roles):
    """May this caller's page wear a tenant's face? (T3, rule one)

    No, if they hold node-wide power. An operator must be able to tell
    by LOOKING that they are on a page where they can act on the whole
    node -- a themed node administration is a page that can be mistaken
    for a customer's own. The cost is that a server_admin never sees
    what a club sees; the tenant page shows them the values instead.

    Unauthenticated callers hold no role and therefore DO see the
    theme, which is the point of theming the login page at all.
    """
    return bool(set(roles or ()) & set(NODE_WIDE_ROLES))
