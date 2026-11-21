"""Fremder Inhalt einer MCP-Quelle (RFC-0116 Stufe 2, Manifest 0.19, 0.1.337).

    python3 test/test_mcp_untrusted.py

Was hier gehalten wird:

A. Manifest: `mcp_content: {untrusted: true}` ist gueltig neben einer MCP-Tuer;
   ohne Tuer, mit unbekanntem Schluessel oder nicht-boolesch: ein Satz.
   Die Plattform kennt Manifest 0.19.
B. Ein Manifest, das `untrusted` unter `mcp:` setzt, bleibt abgelehnt (ein
   Knoten vor 0.19 tut das auch -- darum der eigene Block).
C. Die Quellenliste eines Gateways nennt `untrusted_content: true` nur fuer
   eine Quelle, deren Register das sagt; sonst fehlt das Feld (ein Gateway
   vor 0.36 sieht nichts Neues).
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-mcp-untrusted-test-")
os.environ["OAAP_DATA_DIR"] = os.path.join(DATA, "appctl")
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import appctl as a                                            # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:600]}")


def manifest(**extra):
    m = {"oaap_manifest": "0.19",
         "app": {"id": "recherche", "name": "Recherche", "version": "0.1.1", "type": "native"},
         "services": {"web": {"build": ".", "port": 8000}},
         "routes": [{"path": "/", "roles": ["user"]}],
         "health": {"path": "/healthz"},
         "mcp": {"path": "/mcp"}}
    m.update(extra)
    return {k: v for k, v in m.items() if v is not None}


def errors(m):
    """Die Saetze zu mcp*, die validate_manifest mit die() meldet ([] = gueltig)."""
    try:
        a.validate_manifest(m)
    except SystemExit:
        pass
    else:
        return []
    return [x for x in DIED[-1].splitlines() if "mcp" in x]


DIED = [""]
_die = a.die


def _capture(msg, *args, **kw):
    DIED.append(str(msg))
    raise SystemExit(1)


a.die = _capture


print("A. Manifest")
ok("die Plattform kennt Manifest 0.19", (a.MANIFEST_MAJOR, a.MANIFEST_MINOR) >= (0, 19))
m = manifest(mcp_content={"untrusted": True})
ok("mcp_content.untrusted neben einer Tuer: gueltig", errors(m) == [], errors(m))
ok("… und gewollt", a.mcp_untrusted_wanted(m) is True)
ok("ohne Block: nicht gewollt", a.mcp_untrusted_wanted(manifest()) is False)
ok("untrusted: false: gueltig, nicht gewollt",
   errors(manifest(mcp_content={"untrusted": False})) == []
   and a.mcp_untrusted_wanted(manifest(mcp_content={"untrusted": False})) is False)
e = errors(manifest(mcp=None, mcp_content={"untrusted": True}))
ok("ohne MCP-Tuer: ein Satz", any("only for an app that declares an MCP door" in x for x in e), e)
ok("… und nicht gewollt", a.mcp_untrusted_wanted(manifest(mcp=None, mcp_content={"untrusted": True})) is False)
e = errors(manifest(mcp_content={"untrusted": "yes"}))
ok("nicht boolesch: ein Satz", any("true or false" in x for x in e), e)
e = errors(manifest(mcp_content={"untrusted": True, "trusted_for": "x"}))
ok("unbekannter Schluessel: ein Satz", any("only 'untrusted'" in x for x in e), e)
e = errors(manifest(mcp_content=["untrusted"]))
ok("keine Abbildung: ein Satz", any("object expected" in x for x in e), e)

print("B. Nicht unter mcp:")
e = errors(manifest(mcp={"path": "/mcp", "untrusted_content": True}))
ok("untrusted unter mcp: bleibt abgelehnt", any("only 'path' is known" in x for x in e), e)

print("C. Quellenliste")


def record(name, app_id, port, mcp="", sources=False, **extra):
    r = {"app_id": app_id, "version": "1.0.0", "channel": "production", "port": port,
         "container": f"oaap-app-{name}", "tenant": "t1", "name": name,
         "mcp": {"path": mcp} if mcp else {}, "mcp_sources": sources}
    r.update(extra)
    return r


insts = {"gw": record("gw", "mcp-gateway", 8120, sources=True),
         "recherche": record("recherche", "recherche", 8121, mcp="/mcp", mcp_untrusted=True),
         "crm": record("crm", "crm", 8122, mcp="/mcp")}
srcs = {s["name"]: s for s in a.mcp_sources_of(insts, "gw")}
ok("die Quelle mit fremdem Inhalt traegt untrusted_content: true",
   srcs.get("recherche", {}).get("untrusted_content") is True, srcs)
ok("… eine andere Quelle nennt das Feld nicht", "untrusted_content" not in srcs.get("crm", {"x": 1}), srcs)
insts["recherche"]["mcp_untrusted"] = False
srcs = {s["name"]: s for s in a.mcp_sources_of(insts, "gw")}
ok("Register sagt false: kein Feld", "untrusted_content" not in srcs.get("recherche", {"x": 1}), srcs)

print(f"\n{'OK' if not fails else 'FEHLER'}: {fails} Fehler")
sys.exit(1 if fails else 0)
