#!/usr/bin/env python3
"""Plattenplatz freigeben (RFC-0128 Stufen 1 und 4).

Festgehalten wird, was der Lauf verspricht -- jede Regel mit Gegenprobe:

- Klasse B: ältere Versionen eines Images, das die Plattform baut und eine
  Instanz noch benutzt; es bleiben das laufende Image, das `-prev` eines
  Neubaus und die vorige Version (D2);
- Klasse C: Images ohne Instanz erst nach 7 Tagen, gezählt ab dem ersten Lauf,
  der sie so sah -- nicht ab dem Bau (D6);
- Klasse D: fremde Images nie automatisch, nur einzeln von einer Person; ein
  Image der Plattform oder eines in Benutzung lehnt `rmi` mit Grund ab (D3);
- nie: was ein Container benutzt (auch ein schlafender = gestoppter), was eine
  registrierte Instanz zum Start braucht (auch eine Generalprobe), was jünger
  als ein Tag ist, die eigenen Images der Plattform;
- der Lauf entfernt mit `docker rmi` ohne -f und `prune`, nie ein Volume;
- der nächtliche Lauf: einmal am Tag, nach der Sicherung, nie während einer;
- der Wochenbericht: eine Zeile „Platz freigegeben“ statt sieben Ereignisse;
- appctl: `oaap node care space [--dry-run] [--class] | rmi`, Protokollzeile;
- migrate.sh: journald auf 2 GB in einer eigenen Drop-in-Datei (D4).

Kein Docker nötig. Aufruf: python3 test/test_space.py
"""
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLATFORM = os.path.join(HERE, "..", "platform")
SERVICES = os.path.join(PLATFORM, "services")
os.environ.setdefault("OAAP_DATA_DIR", tempfile.mkdtemp(prefix="oaap-space-"))
sys.path.insert(0, PLATFORM)
sys.path.insert(0, SERVICES)

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:900]}")


import space  # noqa: E402
import report  # noqa: E402

DAY = 86400
NOW = 1_800_000_000.0

print("=== Regeln ===")
ok("Größen von Docker: 13.2GB, 512kB, 0B, 1.5GiB",
   space.parse_size("13.2GB") == 13_200_000_000 and space.parse_size("512kB") == 512_000
   and space.parse_size("0B") == 0 and space.parse_size("1.5GiB") == int(1.5 * 1024 ** 3)
   and space.parse_size("viel") is None)
ok("Docker-Zeit mit Nanosekunden und Zone",
   space.parse_iso("2026-11-24T10:00:00.123456789Z") == space.parse_iso("2026-11-24T11:00:00+01:00")
   and space.parse_iso("") is None)
ok("Repository eines Bezugs, auch mit Registry-Port",
   space.repo_of("oaap-inst/a-web:1.2") == "oaap-inst/a-web"
   and space.repo_of("reg:5000/x/y:2") == "reg:5000/x/y" and space.repo_of("reg:5000/x/y") == "reg:5000/x/y")
ok("Images der Plattform: compose baut oaap-<dienst>, die compose-Datei nennt caddy:2",
   space.is_platform("oaap-portal:latest") and space.is_platform("caddy:2", {"caddy:2"})
   and not space.is_platform("oaap-inst/a:1") and not space.is_platform("ollama/ollama:latest"))


def img(ref, iid, age_days, size=1_000_000_000):
    return {"ref": ref, "id": iid, "created": NOW - age_days * DAY, "size": size}


REG = {
    # webtop: läuft in 0.2.0; 0.1.9 die vorige, 0.1.8 / 0.1.7 älter
    "wt": {"tenant": "t1", "services": [{"service": "", "image": "oaap-inst/wt:0.2.0"}]},
    # zwei Dienste, einer mit -prev nach einem Neubau
    "shop": {"tenant": "t1", "services": [{"service": "web", "image": "oaap-inst/shop-web:3"},
                                          {"service": "db", "image": "oaap-inst/shop-db:3"}]},
    # schlafend (gestoppter Container) und eine Generalprobe: beide registriert
    "sleepy": {"tenant": "t2", "services": [{"service": "", "image": "oaap-inst/sleepy:1"}]},
    "shop-qas": {"tenant": "t1", "rehearsal": True,
                 "services": [{"service": "web", "image": "oaap-inst/shop-qas-web:3"}]},
    # noch auf dem alten geteilten Namen (vor RFC-0130)
    "old": {"tenant": "t2", "services": [{"service": "", "image": "oaap-app/legacy:2",
                                         "base": {"ref": "python:3.13-slim"}}]},
}
IMAGES = [
    img("oaap-inst/wt:0.2.0", "sha256:w20", 3, 13_000_000_000),
    img("oaap-inst/wt:0.1.9", "sha256:w19", 10, 13_000_000_000),
    img("oaap-inst/wt:0.1.8", "sha256:w18", 20, 13_000_000_000),
    img("oaap-inst/wt:0.1.7", "sha256:w17", 30, 12_000_000_000),
    img("oaap-inst/shop-web:3", "sha256:s3", 5),
    img("oaap-inst/shop-web:2", "sha256:s2", 9),
    img("oaap-inst/shop-db:3", "sha256:d3", 5),
    img("oaap-prev/shop-web:prev", "sha256:s2", 9),        # dasselbe Image wie :2
    img("oaap-inst/sleepy:1", "sha256:z1", 40),
    img("oaap-inst/sleepy:0", "sha256:z0", 60),
    img("oaap-inst/shop-qas-web:3", "sha256:q3", 2),
    img("oaap-app/legacy:2", "sha256:l2", 50),
    img("oaap-app/legacy:1", "sha256:l1", 80),
    img("oaap-app/legacy:0", "sha256:l0", 90),
    img("oaap-inst/gone:5", "sha256:g5", 100),            # Instanz entfernt
    img("oaap-prev/gone:prev", "sha256:g4", 100),
    img("oaap-inst/wt:0.2.1", "sha256:w21", 0.2),          # gerade gebaut (Deploy läuft)
    img("oaap-inst/fresh-try:1", "sha256:f1", 0.5),        # jung, ohne Instanz
    img("oaap-portal:latest", "sha256:p1", 70),
    img("caddy:2", "sha256:c2", 70),
    img("sapse/abap-cloud-developer-trial:2023", "sha256:sap", 200, 85_000_000_000),
    img("ollama/ollama:latest", "sha256:oll", 30, 3_000_000_000),
    img("open-webui:main", "sha256:ow", 30),               # läuft in einem fremden Container
    img("python:3.13-slim", "sha256:py", 30),              # Grundlage eines Baus
    img("", "sha256:dangling", 50),
]
USED = {"sha256:w20", "sha256:s3", "sha256:d3", "sha256:z1", "sha256:q3", "sha256:l2",
        "sha256:p1", "sha256:c2", "sha256:ow"}
rows, seen = space.plan(IMAGES, USED, REG, platform_refs={"caddy:2"}, seen={}, now=NOW)
cls = {r["ref"]: r["cls"] for r in rows}
why = {r["ref"]: r["why"] for r in rows}

print("=== Klasse B: ältere Versionen (D2) ===")
ok("webtop: 0.2.0 läuft und bleibt", cls["oaap-inst/wt:0.2.0"] == "keep")
ok("webtop: die gerade gebaute 0.2.1 (jünger als ein Tag) bleibt -- ein Deploy schreibt sie",
   cls["oaap-inst/wt:0.2.1"] == "keep" and "Tag" in why["oaap-inst/wt:0.2.1"], why["oaap-inst/wt:0.2.1"])
ok("webtop: die jüngste andere Version bleibt als schneller Rückweg",
   cls["oaap-inst/wt:0.1.9"] == "keep" and "vorige" in why["oaap-inst/wt:0.1.9"], why["oaap-inst/wt:0.1.9"])
ok("webtop: 0.1.8 und 0.1.7 gehen (B)",
   cls["oaap-inst/wt:0.1.8"] == "B" and cls["oaap-inst/wt:0.1.7"] == "B", cls)
ok("zwei Dienste: shop-web:2 ist das -prev desselben Images -- bleibt",
   cls["oaap-inst/shop-web:2"] == "keep" and cls["oaap-prev/shop-web:prev"] == "keep",
   (why["oaap-inst/shop-web:2"], why["oaap-prev/shop-web:prev"]))
ok("schlafende Instanz: ihr Image bleibt, die vorige Version auch",
   cls["oaap-inst/sleepy:1"] == "keep" and cls["oaap-inst/sleepy:0"] == "keep")
ok("Generalprobe: ihr Image bleibt", cls["oaap-inst/shop-qas-web:3"] == "keep")
ok("alter geteilter Name oaap-app/: laufend bleibt, vorige bleibt, ältere geht",
   cls["oaap-app/legacy:2"] == "keep" and cls["oaap-app/legacy:1"] == "keep"
   and cls["oaap-app/legacy:0"] == "B", (cls["oaap-app/legacy:1"], cls["oaap-app/legacy:0"]))

print("=== Klasse C: ohne Instanz, Frist 7 Tage ab dem ersten Sehen (D6) ===")
ok("eine entfernte Instanz, vor 100 Tagen gebaut: beim ersten Lauf noch nicht weg",
   cls["oaap-inst/gone:5"] == "keep" and "Frist noch 7" in why["oaap-inst/gone:5"]
   and cls["oaap-prev/gone:prev"] == "keep", why["oaap-inst/gone:5"])
ok("die Uhr läuft ab dem ersten Lauf", seen.get("oaap-inst/gone:5") == NOW
   and seen.get("oaap-prev/gone:prev") == NOW)
ok("ein junges Image ohne Instanz: jünger als ein Tag, nicht einmal in der Uhr",
   cls["oaap-inst/fresh-try:1"] == "keep" and "oaap-inst/fresh-try:1" not in seen)
w0 = space.waiting(rows)
ok("wartende Images werden gezählt, mit dem Tag, ab dem sie C sind (oaap-test 28.11.: "
   "118 Images, Summe sagte nur „C 0“)",
   w0["count"] >= 2 and w0["bytes"] > 0 and w0["from"] == NOW + space.GRACE_DAYS * DAY, w0)
rows6, seen6 = space.plan(IMAGES, USED, REG, platform_refs={"caddy:2"}, seen=seen, now=NOW + 6 * DAY)
c6 = {r["ref"]: r["cls"] for r in rows6}
ok("nach 6 Tagen: noch da", c6["oaap-inst/gone:5"] == "keep" and seen6["oaap-inst/gone:5"] == NOW)
rows8, seen8 = space.plan(IMAGES, USED, REG, platform_refs={"caddy:2"}, seen=seen6, now=NOW + 8 * DAY)
c8 = {r["ref"]: r["cls"] for r in rows8}
ok("nach 8 Tagen: C", c8["oaap-inst/gone:5"] == "C" and c8["oaap-prev/gone:prev"] == "C")
ok("nach 8 Tagen wartet nichts mehr aus dem ersten Lauf",
   not any(r["ref"] == "oaap-inst/gone:5" and r.get("grace") for r in rows8))
ok("nach 8 Tagen: die damals gebaute 0.2.1 (nie in Betrieb gegangen) ist neuer als die laufende "
   "-- kein Rückweg, B; die vorige 0.1.9 bleibt",
   c8["oaap-inst/wt:0.2.1"] == "B" and c8["oaap-inst/wt:0.1.9"] == "keep", c8)
reg2 = dict(REG, gone={"tenant": "t1", "services": [{"service": "", "image": "oaap-inst/gone:5"}]})
rows_b, seen_b = space.plan(IMAGES, USED, reg2, platform_refs={"caddy:2"}, seen=seen6, now=NOW + 8 * DAY)
ok("die Instanz kommt wieder: kein C mehr, die Uhr vergessen",
   {r["ref"]: r["cls"] for r in rows_b}["oaap-inst/gone:5"] == "keep"
   and "oaap-inst/gone:5" not in seen_b)

print("=== Klasse D und was nie geht ===")
ok("das SAP-Trial (85 GB) und ollama: fremd, D", cls["sapse/abap-cloud-developer-trial:2023"] == "D"
   and cls["ollama/ollama:latest"] == "D")
ok("ein fremdes Image in einem Container: bleibt", cls["open-webui:main"] == "keep")
ok("ein Basis-Image eines Baus: D, aber mit dem Hinweis, dass ein Neubau es neu lädt",
   cls["python:3.13-slim"] == "D" and why["python:3.13-slim"].startswith("Grundlage von 1"),
   why["python:3.13-slim"])


def fake_ls(cmd, timeout=600):
    if cmd[:3] == ["docker", "image", "ls"]:
        return "sha256:a\toaap-app/x\t1\t214MB\nsha256:a\t<none>\t<none>\t214MB\n"
    if cmd[:3] == ["docker", "image", "inspect"]:
        return "sha256:a\t2026-11-20T10:00:00.5Z\t53000000\n"


got = space.read_images(fake_ls)
ok("Größe aus docker image ls (ungepackt), nicht aus inspect (gepackt beim containerd-Speicher)",
   got and got[0]["size"] == 214_000_000 and got[0]["ref"] == "oaap-app/x:1"
   and got[1]["ref"] == "" and got[0]["created"] is not None, got)
ok("die Plattform: oaap-portal und caddy:2 bleiben",
   cls["oaap-portal:latest"] == "keep" and cls["caddy:2"] == "keep")
ok("verwaiste Schichten (ohne Namen) sind Klasse A, nicht hier gelistet",
   all(r["id"] != "sha256:dangling" for r in rows))
for ref, want in (("sapse/abap-cloud-developer-trial:2023", True), ("oaap-inst/wt:0.1.8", False),
                  ("caddy:2", False), ("oaap-inst/wt:0.2.0", False), ("nix:1", False)):
    good, txt = space.remove_one(ref, rows)
    ok(f"rmi von Hand: {ref} -> {'ja' if want else 'nein: ' + txt[:60]}", good == want, txt)

print("=== Summe und Lauf ===")
sm = space.summary(rows, {"count": 12, "bytes": 5_000_000_000})
ok("B zählt jedes Image einmal (25 GB), A aus docker system df, D 88 GB",
   sm["B"]["bytes"] == 26_000_000_000 and sm["B"]["count"] == 3 and sm["A"]["bytes"] == 5_000_000_000
   and sm["D"]["bytes"] == 88_000_000_000 + 1_000_000_000, sm)
calls = []


def fake(cmd, timeout=600):
    calls.append(cmd)
    return "" if cmd[:2] != ["docker", "rmi"] or cmd[2] != "oaap-app/legacy:0" else None


res = space.remove(rows8, ("A", "B", "C"), run=fake)
rmi = [c[2] for c in calls if c[:2] == ["docker", "rmi"]]
ok("rmi ohne -f, nur B und C", all("-f" not in c for c in calls if c[:2] == ["docker", "rmi"])
   and set(rmi) == {"oaap-inst/wt:0.2.1", "oaap-inst/wt:0.1.8", "oaap-inst/wt:0.1.7", "oaap-app/legacy:0",
                    "oaap-inst/gone:5", "oaap-prev/gone:prev"}, rmi)
ok("Docker lehnt eins ab: gezählt, nicht verschwiegen", res["B"]["refused"] == [
   ("oaap-app/legacy:0", "Docker hat abgelehnt")] and res["B"]["removed"] == 3 and res["C"]["removed"] == 2, res)
ok("A: image prune (nur verwaiste) und builder prune -f (ohne until: das räumte auf Docker 29 nichts)",
   ["docker", "image", "prune", "-f"] in calls
   and ["docker", "builder", "prune", "-f"] in calls
   and not any("until" in " ".join(c) for c in calls))
ok("nie ein Volume, nie system prune, nie rmi von D",
   not any("volume" in c or c[:3] == ["docker", "system", "prune"] for c in calls)
   and "sapse/abap-cloud-developer-trial:2023" not in rmi)
text, freed = space.result_line(res, {"used": 200e9, "percent": 58.0}, {"used": 150e9, "percent": 45.0},
                                {"A": {"bytes": 5e9}})
ok("der Satz: freigegeben, Platte vorher/nachher, Ablehnungen",
   freed == 50e9 and "50,0 GB freigegeben" in text and "58 % → 45 %" in text and "1 abgelehnt" in text, text)
calls.clear()
space.remove(rows8, ("B",), run=fake)
ok("--class B: kein prune", not any(c[1] in ("image", "builder") for c in calls))

print("=== nächtlicher Lauf (D1) ===")
H = 3600
ok("ohne Sicherung: ab 03:00, einmal am Tag", space.due(None, NOW, 3) and not space.due(None, NOW, 2)
   and not space.due(NOW - 5 * H, NOW, 4) and space.due(NOW - 24 * H, NOW, 4))
ok("mit Sicherung: nach ihrem Ende, nicht vorher",
   space.due(NOW - 24 * H, NOW, 1, backup_on=True, backup_finished=NOW - H)
   and not space.due(NOW - 24 * H, NOW, 1, backup_on=True, backup_finished=NOW - 26 * H)
   and not space.due(NOW - 2 * H, NOW, 1, backup_on=True, backup_finished=NOW - H))
ok("nie während einer Sicherung", not space.due(None, NOW, 4, backup_running=True))

print("=== Ansicht für das Portal ===")
v = space.view(rows8, {"count": 1, "bytes": 2}, {"percent": 50.0, "free": 1}, now=NOW)
ok("Klassen mit Titel, fremde nach Größe, Kandidaten mit Grund",
   v["classes"]["D"]["title"].startswith("Fremde") and v["foreign"][0]["ref"].startswith("sapse/")
   and all(c["cls"] in ("B", "C") for c in v["candidates"]) and v["schema"] == space.SCHEMA)

print("=== Wochenbericht ===")
ev = [{"when": "2026-11-23T03:10:00Z", "action": "space.freed", "subject": "nightly",
       "detail": "10 GB", "bytes": 10_000_000_000},
      {"when": "2026-11-24T03:10:00Z", "action": "space.freed", "subject": "nightly",
       "detail": "2 GB", "bytes": 2_000_000_000},
      {"when": "2026-11-24T09:00:00Z", "action": "space.foreign", "subject": "ollama/ollama:latest",
       "detail": "fremdes Image entfernt", "bytes": 3}]
s0, e0 = report.day_bounds("2026-11-23")
s1, e1 = report.day_bounds("2026-11-24")
d0, d1 = report.day_events(ev, s0, e0), report.day_events(ev, s1, e1)
ok("die Bytes kommen ins Tagesbuch", d0[0].get("b") == 10_000_000_000)
lines = [report.ledger_line("2026-11-23", "t", events=d0), report.ledger_line("2026-11-24", "t", events=d1)]
md = report.build(lines, ["2026-11-23", "2026-11-24"], "Woche")
ok("eine Zeile „Platz freigegeben: 12 GB in 2 Läufen“, die Läufe nicht als Ereignisse",
   "Platz freigegeben:" in md and "in 2 Läufen" in md and md.count("Platz freigegeben") == 1, md[:900])
ok("das fremde Image steht als Ereignis", "Fremdes Image entfernt" in md)

print("=== appctl und migrate.sh ===")
src = open(os.path.join(PLATFORM, "appctl.py"), encoding="utf-8").read()
ok("appctl: Verb space, --dry-run, --class, rmi, Protokoll space.freed/space.foreign",
   'if verb == "space":' in src and '"--dry-run", dest="space_dry"' in src
   and '"--class", dest="space_class"' in src and 'audit_tenant("space.freed"' in src
   and 'audit_tenant("space.foreign"' in src)
_sm = src.split("def space_minute", 1)[1][:1800]
ok("appctl: der Minutenjob startet den nächtlichen Lauf in einer eigenen systemd-Unit "
   "(sonst tötet KillMode=control-group ihn mit dem Minutenjob)",
   "space_minute()" in src and '"systemd-run", f"--unit={SPACE_UNIT}"' in _sm
   and "--setenv=OAAP_DATA_DIR=" in _sm and 'SPACE_UNIT = "oaap-space-nightly"' in src
   and _sm.index("systemd-run") < _sm.index("start_new_session=True"))
ok("appctl: kein Lauf während einer Sicherung, einer zur Zeit",
   "backup_running_now()" in src.split("def space_run", 1)[1][:900]
   and "_space_lock()" in src.split("def space_run", 1)[1][:900])
ok("appctl: --class kennt nur A, B, C", "space.AUTO_CLASSES" in src.split("def cmd_care_space", 1)[1])
mig = open(os.path.join(PLATFORM, "migrate.sh"), encoding="utf-8").read()
blk = mig.split("RFC-0128 D4", 1)[1] if "RFC-0128 D4" in mig else ""
ok("migrate.sh: journald.conf.d/oaap.conf mit SystemMaxUse=2G, nur bei Änderung neu gestartet",
   "/etc/systemd/journald.conf.d/oaap.conf" in blk and "SystemMaxUse=2G" in blk
   and re.search(r'if \[ "\$\(cat "\$JD_FILE" 2>/dev/null\)" != "\$JD_WANT" \]', blk) is not None
   and "systemctl restart systemd-journald" in blk)
ok("space.py nennt kein Volume und kein -f", "volume" not in open(os.path.join(SERVICES, "space.py"),
   encoding="utf-8").read().split('"""', 2)[2].lower().replace("never a volume", "")
   and '"rmi", "-f"' not in open(os.path.join(SERVICES, "space.py"), encoding="utf-8").read())

print("=== appctl: der Auftrag aus dem Portal (space_job) ===")
import appctl as m  # noqa: E402
CALLS = []
m.space_measure = lambda save_seen=True: (rows8, {"count": 1, "bytes": 2}, {"percent": 50.0}, {})
m.space_run = lambda classes, who, role, cause: (CALLS.append((classes, who, role, cause))
                                                 or ("5 GB freigegeben", 5, {}))
m.space_rmi = lambda ref, who, role: (CALLS.append(("rmi", ref, who)) or f"{ref} entfernt")
ok("ein tenant_admin: abgelehnt, nichts getan",
   m.space_job({"op": "run"}, "tenant_admin", "anna")[0] is False and not CALLS)
good, txt = m.space_job({"op": "run"}, "server_admin", "betreiber")
ok("server_admin: der Lauf A-C, mit dem Handelnden, Anlass portal",
   good and CALLS == [(("A", "B", "C"), "betreiber", "server_admin", "portal")], CALLS)
CALLS.clear()
good, txt = m.space_job({"op": "rmi", "args": {"ref": "ollama/ollama:latest"}}, "server_admin", "b")
ok("rmi geht an space_rmi mit Name", good and CALLS == [("rmi", "ollama/ollama:latest", "b")])
CALLS.clear()
ok("rmi ohne Namen oder mit Leerzeichen: abgelehnt",
   not m.space_job({"op": "rmi", "args": {"ref": "a b"}}, "server_admin", "b")[0]
   and not m.space_job({"op": "rmi", "args": {}}, "server_admin", "b")[0] and not CALLS)
ok("unbekannter Auftrag: abgelehnt", not m.space_job({"op": "prune-all"}, "server_admin", "b")[0])
m.space_run = lambda classes, who, role, cause: ("", 0, {"skipped": "backup"})
ok("während einer Sicherung: abgelehnt mit Satz",
   m.space_job({"op": "run"}, "server_admin", "b")
   == (False, "Eine Sicherung läuft gerade -- später noch einmal."))
src2 = open(os.path.join(PLATFORM, "appctl.py"), encoding="utf-8").read()
ok("der Worker kennt die Aktion space (Job-Ergebnis, Art portal)",
   'elif action == "space":' in src2 and '"disk", "space", "fleet-disclose"' in src2
   and '"space": "portal"' in src2)

print("=== Stufe 3: bei Druck vor der Mail (D5) ===")
import json as _js  # noqa: E402
_st = {}
m._space_state = lambda: dict(_st)
m._space_write = lambda path, data, mode: (_st.clear(), _st.update(data)) if path == m.SPACE_STATE else None
RUNS = []
m.space_run = lambda classes, who, role, cause: (RUNS.append(cause) or ("8,0 GB freigegeben; Platte 92 % → 84 %", 8, {}))
_pct = {"v": 84.0}
m.metrics.read_disk_percent = lambda d: _pct["v"]
_vf = os.path.join(tempfile.mkdtemp(), "space.json")
_js.dump({"foreign": [{"ref": "sapse/abap-cloud-developer-trial:2023", "size": 85_000_000_000}]},
         open(_vf, "w"))
m.SPACE_VIEW = _vf
note = m.space_pressure(80.0, now=NOW)
ok("der Lauf A-C mit Anlass pressure, der Satz sagt, was er brachte",
   RUNS == ["pressure"] and "aufgeräumt" in note and "92 % → 84 %" in note, note)
ok("noch über der Schwelle: die fremden Images nach Größe -- nur ein Mensch",
   "Nur ein Mensch" in note and "sapse/abap-cloud-developer-trial:2023 (85,0 GB)" in note, note)
ok("höchstens alle sechs Stunden", m.space_pressure(80.0, now=NOW + 3600) == "" and RUNS == ["pressure"])
_st.clear()
_pct["v"] = 70.0
note = m.space_pressure(80.0, now=NOW)
ok("darunter: keine Liste", "Nur ein Mensch" not in note and "aufgeräumt" in note, note)
_st.clear()
m.space_run = lambda classes, who, role, cause: ("", 0, {"skipped": "backup"})
ok("kein Lauf (Sicherung): kein Satz", m.space_pressure(80.0, now=NOW) == "")
nc = src2.split("def notify_conditions", 1)[1].split("\ndef ", 1)[0]
ok("notify_conditions: vor dem Öffnen von disk.full und einer neuen Vorhersage, dann neu gemessen",
   nc.index("space_pressure(") < nc.index('cur[dflt]["disk.full"] = {')
   and "if pct >= s[\"disk_warn\"] and not open_before:" in nc
   and "fresh = [k for k in trend if k not in open_now]" in nc
   and "pct = metrics.read_disk_percent(DATA_DIR) or pct" in nc)

print("=== die Seite „Platz“ ===")
try:
    sys.path.insert(0, os.path.join(SERVICES, "portal"))
    import json as _json  # noqa: E402
    import app as portal  # noqa: E402
except ImportError as e:
    print(f"NOT RUN  die Seite: {e} (Flask steht im Portal-Abbild)")
    print(f"{fails} FAIL" if fails else "PARTIAL PASS (Seite nicht geprüft)")
    sys.exit(1 if fails else 2)
TMP = tempfile.mkdtemp(prefix="oaap-space-page-")
VF = os.path.join(TMP, "space.json")
portal.SPACE_VIEW_FILE = VF
v["last"] = {"when": "2026-11-26T03:10:00Z", "cause": "nightly", "text": "12,0 GB freigegeben"}
v["waiting"] = {"count": 118, "bytes": 25e9, "from": "2026-12-03"}
v["disk"] = {"percent": 58.3, "free": 80e9, "total": 200e9}
_json.dump(v, open(VF, "w"))
mg = portal.management_api
mg.SPOOL_DIR = TMP
for d_ in ("queue", "jobs"):
    os.makedirs(os.path.join(TMP, d_), exist_ok=True)
QUEUED = []
mg.CTX["queue"] = lambda rid, name, payload, wait: QUEUED.append(dict(payload, _rid=rid))
WHO = {"role": "server_admin", "roles": {"server_admin"}, "tenant": "", "host": None}
portal.caller_roles = lambda: WHO["roles"]
portal.caller_scope = lambda: (WHO["role"], WHO["tenant"])
portal.host_tenant_scope = lambda host: (WHO["host"], True)
c = portal.app.test_client()
H = {"X-OAAP-User": "betreiber", "X-OAAP-Roles": "server_admin"}
r = c.get("/platz", headers=H)
t = r.get_data(as_text=True)
ok("der Betreiber am Knoten: Platte, Klassen, fremde Images mit Knopf, letzter Lauf, Menüpunkt",
   r.status_code == 200 and "58 %" in t and "Ältere Versionen" in t
   and "sapse/abap-cloud-developer-trial:2023" in t and 'action="/platz/rmi"' in t
   and "nachts" in t and "12,0 GB freigegeben" in t and 'href="/platz"' in t, t[-1500:])
ok("wartende Images stehen auf der Seite, mit dem Tag",
   "118 Image(s) ohne Instanz" in t and "frühestens ab 2026-12-03" in t, t[-2500:])
ok("ein Image der Plattform oder in Benutzung hat keinen Knopf",
   'value="caddy:2"' not in t and 'value="open-webui:main"' not in t
   and 'value="oaap-inst/wt:0.2.0"' not in t)
for who, role, roles in (("Verwalter", "tenant_admin", {"tenant_admin"}), ("Mitglied", "", {"user"})):
    WHO.update(role=role, roles=roles, tenant="t-a")
    codes = [c.get("/platz", headers=H).status_code,
             c.post("/platz/run", headers=H).status_code,
             c.post("/platz/rmi", headers=H, data={"ref": "ollama/ollama:latest"}).status_code]
    ok(f"{who}: 403 an jeder Tür, nichts im Spool", codes == [403, 403, 403] and not QUEUED, codes)
WHO.update(role="server_admin", roles={"server_admin"}, tenant="", host="t-kunde")
codes = [c.get("/platz", headers=H).status_code, c.post("/platz/run", headers=H).status_code]
ok("am Ort eines Mandanten: 404", codes == [404, 404] and not QUEUED, codes)
WHO.update(host=None)
r = c.post("/platz/run", headers=H)
ok("Jetzt freigeben: ein Auftrag space/run, zurück mit Job",
   len(QUEUED) == 1 and QUEUED[0]["action"] == "space" and QUEUED[0]["op"] == "run"
   and "/platz?job=" in r.headers["Location"], QUEUED)
QUEUED.clear()
c.post("/platz/rmi", headers=H, data={"ref": "sapse/abap-cloud-developer-trial:2023"})
ok("Entfernen: ein Auftrag mit genau diesem Image",
   QUEUED and QUEUED[0]["op"] == "rmi"
   and QUEUED[0]["args"] == {"ref": "sapse/abap-cloud-developer-trial:2023"}, QUEUED)
QUEUED.clear()
r = c.post("/platz/rmi", headers=H, data={"ref": "caddy:2"})
ok("Köder: ein Image, das nicht in der Liste steht -- nichts im Spool",
   not QUEUED and "err=" in r.headers["Location"])
r = c.post("/platz/prune-all", headers=H)
ok("ein Auftrag, den es nicht gibt: nichts im Spool", not QUEUED and r.status_code == 302)
r = c.post("/platz/run", headers=dict(H, Origin="https://evil.example"))
ok("fremde Herkunft: 403", r.status_code == 403 and not QUEUED)
portal.SPACE_VIEW_FILE = os.path.join(TMP, "nix.json")
t = c.get("/platz", headers=H).get_data(as_text=True)
ok("ohne Ansicht: „Noch nicht gemessen“ mit Knopf", "Noch nicht gemessen" in t
   and 'action="/platz/measure"' in t)

print(f"\n{'OK' if not fails else f'{fails} FAIL'}")
sys.exit(1 if fails else 0)
