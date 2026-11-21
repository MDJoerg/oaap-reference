#!/usr/bin/env python3
"""Das Rezept „SAP SSO einrichten“ (RFC-0109 §12, Stufe 3) auf einem Papier-Knoten.

Die Sätze, die diese Datei verteidigt:

    Das Rezept aus oaap-docs ist gültig, und ein tenant_admin darf es nach
    der Freigabe selbst ausführen (Attribut im eigenen Realm, Destination
    ohne Geheimnis, Zuordnung im eigenen Mandanten).
    Gepinnt wird genau der Fingerabdruck, den ein Mensch verglichen und
    bestätigt hat -- nie einer aus dem Rezept; ein falscher wird abgelehnt,
    ein inzwischen anderes Zertifikat ist ein Konflikt.
    Der Kettentest hält den Lauf an, bis die Kette hält, und sagt, welches
    Glied bricht.
    Die Person wird erst NACH dem Kettentest durchgereicht, als eigener
    Schritt; Rückgängig nimmt nur das Durchreichen zurück, die Zuordnung
    gehört dem Schritt der Destination.
    Ein zweiter Durchgang findet alles vorhanden.
    Im Anmeldedienst wird nie etwas gelöscht; ein Attribut, das die Person
    selbst ändern darf, ist ein Konflikt.

Aufruf: python3 test/test_recipe_sap_sso.py
"""
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["OAAP_DATA_DIR"] = tempfile.mkdtemp(prefix="oaap-sap-sso-")
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))

import appctl as a                                            # noqa: E402
# RFC-0109 semantics: without the templates the platform ships (RFC-0117 D4
# makes a shipped template free; test_assistant_templates.py measures that)
a.SHIPPED_RECIPE_DIR = tempfile.mkdtemp(prefix="oaap-no-shipped-")
import tenant_build as tb                                     # noqa: E402
import tenant_recipe as tr                                    # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:900]}")


def refusal(fn, *args, **kw):
    try:
        fn(*args, **kw)
    except (tb.Refusal, a.DestinationRefused) as exc:
        return str(exc)
    return ""


DOCS = os.path.join(HERE, "..", "..", "oaap-docs", "szenarien", "rezepte", "sap-sso.json")
with open(DOCS, encoding="utf-8") as f:
    SAP = json.load(f)
ok("sap-sso aus oaap-docs ist ein gültiges Rezept", tr.recipe_problems(SAP) == [],
   tr.recipe_problems(SAP))

# ------------------------------------------------------------ the paper node
REG = {"instances": {
    "kunde-werkzeuge": {"tenant": "t-kunde", "app_id": "mcp-gateway", "name": "werkzeuge"},
    "kunde-chat": {"tenant": "t-kunde", "app_id": "chat", "name": "chat"},
    "kunde-modelle": {"tenant": "t-kunde", "app_id": "ai-gateway", "name": "modelle"},
}}
a.load_registry = lambda: REG
a.save_registry = lambda reg: None
a.resolve_tenant = lambda t: t
a.is_rehearsal = lambda inst: False
a.refresh_destinations = lambda reg=None: None
a.sync_destination_env = lambda reg, only=None, recreate=True: None
a.ensure_assertion_key = lambda: None
a.egress_destination_refusal = lambda inst, name, d: ""
a.declared_dest_needs = lambda inst: {}
TENANTS = {"t-kunde": {"label": "kunde", "idp": {
    "issuer": "https://auth.example/realms/kunde", "client_id": "oaap-kunde",
    "connector": "auth", "space": "kunde"}}, "t-default": {"label": "node"}}
AUDIT = []
a.load_tenants = lambda: TENANTS
a.save_tenants = lambda t: TENANTS.update(t)
a.default_tenant_id = lambda: "t-default"
a.audit_tenant = lambda action, tenant, **kw: AUDIT.append((action, tenant, kw.get("subject"),
                                                            kw.get("detail", "")))
a.tenant_recipe_view_write = lambda: True


def _by_label(label, include_former=False):
    for tid, t in TENANTS.items():
        if t.get("label") == label:
            return tid, t
    return None, None


a.tenant_by_label = _by_label
a.instance_key = lambda tid, loc: tid[2:] + "-" + loc

FP = "ab" * 32
CERT = {"fp": FP}
a.fetch_server_cert = lambda host, port: ("PEM", CERT["fp"])
a.propagation_claim_check = lambda tid, claims: ({c: "admin" for c in claims}, "papier")
CHAIN = {"ok": False}


def fake_check(tid, name, who="root", role="root"):
    if CHAIN["ok"]:
        return {"ok": True, "links": [["target", "held", ""], ["mapping", "held", ""],
                                      ["user", "held", "OAAP_CHECK"]]}
    return {"ok": False, "links": [["target", "held", ""], ["ca", "held", ""],
                                   ["mapping", "broke", "401"]]}


a.propagation_check = fake_check

# the realm, through a paper connector
REALM = {"attribute": "", "scope": False, "default": False, "calls": []}


class PaperAdmin:
    def person_claim(self, space, client_id, claim, scope, dry=True):
        REALM["calls"].append(("dry" if dry else "do", space, client_id, claim, scope))
        parts = []
        if REALM["attribute"] == "self":
            parts.append(("attribute", "conflict", f"'{claim}' editable by the person"))
        elif REALM["attribute"] == "admin":
            parts.append(("attribute", "present", "edited by admins only"))
        elif dry:
            parts.append(("attribute", "absent", "would be added"))
        else:
            REALM["attribute"] = "admin"
            parts.append(("attribute", "made", "added"))
        for part in ("scope", "default"):
            if REALM[part]:
                parts.append((part, "present", "there"))
            elif dry:
                parts.append((part, "absent", "would be added"))
            else:
                REALM[part] = True
                parts.append((part, "made", "added"))
        return parts, ""


a._BuildDrivers._idp_admin = lambda self: (PaperAdmin(), {"space": "kunde",
                                                          "client_id": "oaap-kunde"}, "")

# the MCP gateway's and AI gateway's doors, on paper
DOOR = {}          # (key, kind, id) -> wish
READY = {"catalog": False}


def fake_setup_call(key, inst, method, body=None, query="", actor="root", recipe=""):
    if method == "GET":
        q = dict(x.split("=", 1) for x in query.split("&") if "=" in x)
        if q.get("kind") == "alias" and "id" not in q:
            return {"v": 1, "kind": "alias", "items": [
                {"id": "chat-default", "title": "openrouter:x", "light": "red"}]}, ""
        if q.get("kind") == "server":
            return {"v": 1, "ready": READY["catalog"] and (key, "server", q.get("id")) in DOOR,
                    "note": "Katalog"}, ""
        return {}, "unbekannt"
    results = []
    for it in body.get("items") or []:
        k = (key, it["kind"], it["id"])
        wish = {x: y for x, y in it.items() if x not in ("kind", "id")}
        if it["kind"] == "view" and (key, "server", it["tools"]["server"]) not in DOOR:
            # like the real gateway (measured on oaap-test): no server, no view
            results.append({"kind": "view", "id": it["id"], "result": "refused",
                            "note": "Einen Server gibt es nicht (erst refresh)."})
        elif k in DOOR:
            results.append({"kind": it["kind"], "id": it["id"],
                            "result": "present" if DOOR[k] == wish else "conflict"})
        elif body.get("dry"):
            results.append({"kind": it["kind"], "id": it["id"], "result": "would_make"})
        else:
            DOOR[k] = wish
            results.append({"kind": it["kind"], "id": it["id"], "result": "made"})
    for it in body.get("remove") or []:
        DOOR.pop((key, it["kind"], it["id"]), None)
        results.append({"kind": it["kind"], "id": it["id"], "result": "removed"})
    return {"v": 1, "results": results}, ""


a.setup_call = fake_setup_call
a.setup_door = lambda inst: {"path": "/oaap/setup"}
os.makedirs(a.RECIPE_DIR, exist_ok=True)
shutil.copy(DOCS, a.tenant_recipe_path("sap-sso"))
P = {"sap_url": "https://a4h.example:44300", "mcp_path": "zmcp2/bc/access-support",
     "model": "chat-default"}

print("")
print("=== Vorschau ===")
pv = a.tenant_recipe_preview("sap-sso", "kunde", P, "root", "root")
rows = {r["id"]: r for r in pv["rows"]}
ok("vierzehn Schritte (SAP-Basis in fünf Teilen); das Attribut, die Destination, der Server werden angelegt",
   len(pv["rows"]) == 14 and rows["attribut"]["verdict"] == "absent"
   and rows["ziel"]["verdict"] == "absent" and rows["server"]["verdict"] == "absent", pv["rows"])
rows_first_note = rows["zertifikat"]["note"]
ok("der Fingerabdruck steht schon in der Vorschau zum Vergleichen",
   rows["zertifikat"]["verdict"] == "absent" and "AB:AB:AB" in rows["zertifikat"]["note"],
   rows["zertifikat"])
ok("die Sicht hinter dem Schritt eines Menschen: „erst danach prüfbar“, kein Halt "
   "(gemessen auf oaap-test: Server und Katalog entstehen erst im Lauf)",
   rows["sicht"]["verdict"] == "later" and "erst nach 'zertifikat'" in rows["sicht"]["note"]
   and not tr.preview_blockers(pv["rows"]), rows["sicht"])
ok("die Vorschau legt im Anmeldedienst nichts an (nur dry)",
   REALM["attribute"] == "" and all(c[0] == "dry" for c in REALM["calls"]), REALM)

print("")
print("=== tenant_admin nach Freigabe ===")
a._read_identity_users = lambda: [{"username": "kv", "display_name": "K V"}]
why = refusal(a.tenant_recipe_preview, "sap-sso", "kunde", P, "kv", "tenant_admin",
              actor_tid="t-kunde")
ok("ohne Freigabe: abgelehnt", "not released" in why, why)
TENANTS["t-kunde"]["tenant_admin_may"] = ["recipe:sap-sso@1"]
pv = a.tenant_recipe_preview("sap-sso", "kunde", P, "kv", "tenant_admin", actor_tid="t-kunde")
ok("freigegeben: der tenant_admin sieht die Vorschau (die Schritte liegen in seinem Bereich)",
   len(pv["rows"]) == 14, pv.get("problems"))

print("")
print("=== der Lauf ===")
st = a.tenant_recipe_run("sap-sso", "kunde", P, "kv", "tenant_admin", actor_tid="t-kunde")
steps = {r["id"]: r for r in st["steps"]}
ok("das Attribut ist angelegt (über den Konnektor), der Lauf wartet am Zertifikat",
   steps["attribut"]["state"] == "done" and REALM["attribute"] == "admin"
   and steps["zertifikat"]["state"] == "waiting" and st["state"] == "waiting", st["steps"][:3])
ok("…mit dem Fingerabdruck im Hinweis und als angebotener Wert",
   "AB:AB" in steps["zertifikat"]["note"] and steps["zertifikat"].get("offered_sha256") == FP,
   steps["zertifikat"])
ok("die Vorschau sagt es als Auskunft, der Lauf als Bitte mit dem Weg (STRUST oder Browser, Haken)",
   rows_first_note.startswith("wird im Lauf verglichen")
   and "STRUST" in steps["zertifikat"]["note"] and "Haken" in steps["zertifikat"]["note"],
   (rows_first_note, steps["zertifikat"]["note"]))
ok("im Anmeldedienst steht kein Gemachtes zum Rückbau (nie DELETE)",
   steps["attribut"].get("made") in (None, []), steps["attribut"])
rid = st["id"]
why = refusal(a.tenant_recipe_confirm, rid, "zertifikat", "kv", "tenant_admin",
              actor_tid="t-kunde")
ok("bestätigen ohne Fingerabdruck: abgelehnt", "fingerprint" in why, why)
why = refusal(a.tenant_recipe_confirm, rid, "zertifikat", "kv", "tenant_admin",
              actor_tid="t-kunde", value="cd" * 32)
ok("ein anderer Fingerabdruck: abgelehnt, nichts gepinnt",
   "not the fingerprint" in why and not a.load_destinations().get("t-kunde"), why)
st = a.tenant_recipe_confirm(rid, "zertifikat", "kv", "tenant_admin", actor_tid="t-kunde",
                             value=":".join(FP[i:i + 2] for i in range(0, 64, 2)).upper())
steps = {r["id"]: r for r in st["steps"]}
d = (a.load_destinations().get("t-kunde") or {}).get("sap") or {}
ok("bestätigt (auch mit Doppelpunkten): die Destination ist auf genau ihn gepinnt",
   steps["zertifikat"]["state"] == "done" and steps["ziel"]["state"] == "done"
   and (d.get("auth") or {}).get("server_sha256") == FP
   and (d.get("auth") or {}).get("type") == "principal_propagation", (steps["ziel"], d))
ok("…mit Claim sap_user, SAP-Namensregel und Prüfpfad mit sap-client",
   [r["name"] for r in d["auth"]["user"]] == ["sap_user"] and d["auth"]["name_check"] == "sap"
   and d["auth"]["check_path"] == "/sap/bc/ui2/start_up?sap-client=001", d["auth"])
inst = REG["instances"]["kunde-werkzeuge"]
ok("die Destination ist dem MCP-Gateway zugeordnet, trägt aber noch KEINE Person",
   inst.get("destinations", {}).get("sap") == "sap" and not inst.get("propagate"), inst)
ok("der Lauf wartet bei der SAP-Basis, Teil 1 (CA in STRUST)",
   steps["sap-ca"]["state"] == "waiting", steps["sap-ca"])
ok("…mit Zeilen statt eines Blocks und dem Download der CA am Schritt (0.1.329)",
   "\n1. " in steps["sap-ca"]["text"] and "\n2. " in steps["sap-ca"]["text"]
   and steps["sap-ca"].get("offer") == ["ca"], steps["sap-ca"])
for part in ("sap-ca", "sap-regel", "sap-parameter", "sap-dienst", "sap-pruefer"):
    st = a.tenant_recipe_confirm(rid, part, "kv", "tenant_admin", actor_tid="t-kunde")
    steps = {r["id"]: r for r in st["steps"]}
    if part == "sap-regel":
        pass
ok("CERTRULE bietet das Musterzertifikat an, SICF nennt den Dienst",
   steps["sap-regel"].get("offer") == ["sample"]
   and "/zmcp2/bc/access-support" in steps["sap-dienst"]["text"], steps["sap-dienst"]["text"])
steps = {r["id"]: r for r in st["steps"]}
ok("die Kette hält noch nicht: der Lauf wartet und sagt, welches Glied bricht",
   steps["kette"]["state"] == "waiting" and "mapping" in steps["kette"]["note"].lower()
   or "Zuordnung" in steps["kette"]["note"], steps["kette"])
ok("…und die Person wird noch nicht durchgereicht", not inst.get("propagate"))
CHAIN["ok"] = True
st = a.tenant_recipe_continue(rid, "kv", "tenant_admin", actor_tid="t-kunde")
steps = {r["id"]: r for r in st["steps"]}
ok("die Kette hält: weiter; jetzt reicht das MCP-Gateway die Person durch (eigener Schritt)",
   steps["kette"]["state"] == "done" and steps["person"]["state"] == "done"
   and inst.get("propagate") == ["sap"] and steps["person"]["made"] == ["propagate:kunde-werkzeuge:sap"],
   (steps["person"], inst))
ok("der Server ist im MCP-Gateway angelegt, hinter der Destination",
   DOOR.get(("kunde-werkzeuge", "server", "sap")) == {
       "address": "destination:sap/zmcp2/bc/access-support", "auth": "propagate",
       "light": "yellow"}, (steps["server"], DOOR))
# RFC-0114 Stufe 2 (sap-sso 1.2.0): derselbe Schritt trägt das Zielsystem in die
# Systemlandschaft ein -- mit dem Server, SAP-Mandant, ohne SID (nicht angegeben)
ok("…und das Zielsystem steht in der Systemlandschaft, mit dem Server",
   DOOR.get(("kunde-werkzeuge", "system", "sap")) == {
       "title": "SAP-System", "role": "other", "type": "sap", "facts": {"client": "001"},
       "servers": ["sap"]}, DOOR.get(("kunde-werkzeuge", "system", "sap")))
ok("der Lauf wartet, bis die Personen ihren SAP-Benutzer haben",
   steps["personen"]["state"] == "waiting", steps["personen"])
st = a.tenant_recipe_confirm(rid, "personen", "kv", "tenant_admin", actor_tid="t-kunde")
steps = {r["id"]: r for r in st["steps"]}
ok("…dann, bis eine Person den Katalog gelesen hat", steps["katalog"]["state"] == "waiting",
   steps["katalog"])
READY["catalog"] = True
st = a.tenant_recipe_continue(rid, "kv", "tenant_admin", actor_tid="t-kunde")
steps = {r["id"]: r for r in st["steps"]}
ok("Katalog da: Sicht, Zugangsprofil und Assistent; der Lauf ist fertig",
   st["state"] == "done" and ("kunde-werkzeuge", "view", "sap") in DOOR
   and ("kunde-werkzeuge", "profile", "kunde-chat-sap") in DOOR
   and DOOR[("kunde-werkzeuge", "assistant", "sap")]["model"] == "chat-default", st["state"])
ok("das Protokoll nennt tenant.recipe.*, die Destination und das Durchreichen",
   any(x[0] == "destination.add" for x in AUDIT)
   and any(x[0] == "destination.bind" and "ACTS AS THE SIGNED-IN PERSON" in x[3] for x in AUDIT),
   [x[0] for x in AUDIT][-12:])

print("")
print("=== zweiter Durchgang ===")
pv = a.tenant_recipe_preview("sap-sso", "kunde", P, "kv", "tenant_admin", actor_tid="t-kunde")
bad = [r for r in pv["rows"] if r["verdict"] not in ("present", "skipped")]
ok("alles vorhanden (das Zertifikat: schon gepinnt; die Kette: hält)", not bad, bad)

print("")
print("=== Köder ===")
CERT["fp"] = "ef" * 32
pv = a.tenant_recipe_preview("sap-sso", "kunde", P, "root", "root")
rows = {r["id"]: r for r in pv["rows"]}
ok("das SAP-System zeigt ein anderes Zertifikat: die Destination ist kein „vorhanden“ mehr",
   rows["zertifikat"]["verdict"] != "present", rows["zertifikat"])
CERT["fp"] = FP
REALM["attribute"] = "self"
pv = a.tenant_recipe_preview("sap-sso", "kunde", P, "root", "root")
rows = {r["id"]: r for r in pv["rows"]}
ok("ein Attribut, das die Person selbst ändern darf: Konflikt, nichts wird geändert",
   rows["attribut"]["verdict"] == "conflict" and REALM["attribute"] == "self", rows["attribut"])
REALM["attribute"] = "admin"
d2 = dict(SAP, steps=[dict(s) for s in SAP["steps"]])
for s in d2["steps"]:
    if s["id"] == "ziel":
        s.pop("pin_step")
ok("ohne Pin-Schritt gibt es keine Destination mit Durchreichen",
   any("pin_step" in p for p in tr.recipe_problems(d2)), tr.recipe_problems(d2))
d3 = dict(SAP, steps=[dict(s) for s in SAP["steps"]])
for s in d3["steps"]:
    if s["id"] == "ziel":
        s["auth"] = "bearer"
        s.pop("pin_step"), s.pop("claim"), s.pop("name_check"), s.pop("check_path")
plan = [("ziel", "destination.ensure", {"auth": "bearer"})]
ok("eine Destination mit Zugangsdaten bleibt außerhalb des tenant_admin",
   tr.tenant_admin_scope_problems(plan) != [], tr.tenant_admin_scope_problems(plan))

print("")
print("=== Rückgängig ===")
st = a.tenant_recipe_rollback(rid, "kv", "tenant_admin", actor_tid="t-kunde")
ok("zurückgenommen: Sicht, Server, Durchreichen, Destination; der Realm bleibt",
   st["state"] == "rolled-back" and not inst.get("propagate")
   and not inst.get("destinations", {}).get("sap")
   and not (a.load_destinations().get("t-kunde") or {}).get("sap")
   and ("kunde-werkzeuge", "server", "sap") not in DOOR and REALM["attribute"] == "admin",
   (st["state"], inst, DOOR))

print("")
print("PASS: alles" if not fails else f"FAIL: {fails}")
sys.exit(1 if fails else 0)
