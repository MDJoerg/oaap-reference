#!/usr/bin/env python3
"""Ein hochgeladenes Paket direkt in Produktion (RFC-0037).

Sideloading ist die Schwester der Uebernahme (RFC-0020) -- derselbe Akt
nach Produktiv, nur kommen die Bytes aus einem Browser statt aus einem
Paket, das dieser Knoten schon angenommen hat. Damit fehlt genau die
eine Zusage, die die Uebernahme gibt: dass es die getesteten Bytes sind.

Vorbild ist Androids Sideloading, samt seiner Sicherungen: ab Werk aus
(Profil `sideload`), Rechte VOR dem Installieren sichtbar (der Rahmen,
vollstaendig aufgezaehlt), und eine Herkunft, die die Instanz behaelt.
Androids Signaturregel koennen wir nicht nachbauen -- OAAP-Pakete sind
nicht signiert. Ihr Ersatz ist D3: eine Instanz hat EINE Antwort auf
die Frage, woher ihre Aktualisierungen kommen.

Geprueft werden die Regeln, nicht Docker: das Installieren selbst wird
abgefangen, sobald feststeht, dass es haette laufen duerfen.

Aufruf: python3 test/test_sideload.py
"""
import argparse
import contextlib
import io
import os
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="oaap-sideload-test-")
os.environ["OAAP_DATA_DIR"] = DATA
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import appctl as m                                            # noqa: E402

m.reload_gateway = lambda: None

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:500]}")


def manifest(version="1.0.0", app_id="demo", public=False, storage=False,
             endpoint=False):
    lines = ['oaap_manifest: "0.1"', "app:", f"  id: {app_id}",
             "  name: Demo", f"  version: {version}", "  type: native",
             "services:", "  web:", "    build: .", "    port: 80",
             "routes:", "  - path: /", "    roles: [user]"]
    if public:
        lines += ["  - path: /anzeige", "    roles: [public]"]
    if storage:
        lines += ["storage:", "  - name: ablage", "    mount: /data"]
    if endpoint:
        lines += ["endpoints:", "  - name: mqtt", "    protocol: tcp",
                  "    container_port: 1883",
                  "    reason: Geraete sprechen MQTT ohne HTTP"]
    lines += ["health:", "  path: /healthz", ""]
    return "\n".join(lines)


PKG = tempfile.mkdtemp(prefix="oaap-sideload-pkg-")


def package(name, text):
    """Ein echtes ZIP -- die Pruefung packt es selbst aus, und genau das
    ist der Teil, der dem Knoten gehoert und nicht dem Portal."""
    path = os.path.join(PKG, f"{name}.zip")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("oaap-app.yaml", text)
    return path


def review(zip_path, name, tenant=None):
    reg = m.load_registry()
    try:
        return m.sideload_review(reg, zip_path, tenant or DEFAULT, name), ""
    except m.SideloadRefused as e:
        return None, str(e)


DEFAULT = m.ensure_default_tenant()
plain = package("plain", manifest())

print("Ab Werk aus -- wie Androids Schalter")
res, why = review(plain, "neu")
ok("ohne das Profil wird gar nichts geprueft", res is None, res)
ok("und die Ablehnung nennt den Befehl, der es einschaltet",
   "add-profile sideload" in why, why)

m.save_profiles(["sideload"])
ok("das Profil steht in der Tabelle der Knotenprofile",
   "sideload" in m.PROFILES)

print("")
print("Eine NEUE Instanz: der ganze Rahmen, nicht seine Anzahl")

wide = package("wide", manifest(public=True, storage=True, endpoint=True))
res, why = review(wide, "neu")
ok("sie wird angenommen", res is not None, why)
mf, notes, key, existing, sha, size = res
ok("und es ist wirklich eine neue", existing is None)
text = " | ".join(notes)
ok("die oeffentliche Route steht da, mit ihrem Pfad",
   "/anzeige" in text, text)
ok("der Speicher steht da, mit seinem Namen", "ablage" in text, text)
ok("der Port am Gateway vorbei steht da -- samt noetigem Profil",
   "mqtt" in text and "exposed" in text, text)
ok("die Pruefsumme ist die der Datei",
   sha == m._sha256_file(wide) and size == os.path.getsize(wide))

res, why = review(plain, "Gross")
ok("ein unmoeglicher Name wird abgelehnt", res is None and "lowercase" in why,
   why)

print("")
print("Eine bestehende Instanz: dieselben Regeln wie die Uebernahme")

reg = m.load_registry()
reg["instances"]["prod"] = {
    "app_id": "demo", "app_name": "Demo", "version": "1.0.0",
    "channel": "production", "port": 8801, "svc_port": 80,
    "container": "oaap-app-prod", "tenant": DEFAULT, "name": "prod",
    "id": m.new_instance_id(), "routes": [{"path": "/", "roles": ["user"]}],
    "source": {"kind": "artifact", "version": "1.0.0", "sha256": "a" * 64}}
reg["instances"]["ausgit"] = dict(reg["instances"]["prod"],
                                  name="ausgit", id=m.new_instance_id(),
                                  port=8802,
                                  source={"kind": "git", "url": "https://x"})
reg["instances"]["testinst"] = dict(reg["instances"]["prod"],
                                    name="testinst", id=m.new_instance_id(),
                                    port=8803, channel="test",
                                    source={"kind": "artifact",
                                            "sha256": m._sha256_file(plain)})
m.save_registry(reg)

res, why = review(package("same", manifest("1.0.0")), "prod")
ok("dieselbe Version wird abgelehnt", res is None, res)
ok("und der Weg zurueck wird beim Namen genannt",
   "higher version only" in why and "rollback" in why, why)

res, why = review(package("older", manifest("0.9.0")), "prod")
ok("eine aeltere auch", res is None and "higher version only" in why, why)

res, why = review(package("newer", manifest("1.1.0")), "prod")
ok("eine hoehere kommt durch", res is not None, why)
ok("und meldet keine Rahmenerweiterung, wo es keine gibt",
   res and res[1] == [], res)

res, why = review(package("widen", manifest("1.2.0", public=True)), "prod")
ok("eine neue oeffentliche Route wird als Erweiterung gemeldet",
   res and any("/anzeige" in n for n in res[1]), res)

res, why = review(package("other", manifest("2.0.0", app_id="andere")), "prod")
ok("ein anderes Paket in derselben Instanz wird abgelehnt",
   res is None and "belongs to one app" in why, why)

res, why = review(package("t", manifest("1.1.0")), "testinst")
ok("eine TEST-Instanz ist kein Ziel", res is None, res)
ok("und die Ablehnung sagt, warum", "not a production instance" in why, why)

print("")
print("D3 -- eine Instanz hat EINE Antwort, woher ihre Updates kommen")

res, why = review(package("git", manifest("1.1.0")), "ausgit")
ok("eine Instanz aus Git wird im Browser nicht umgestellt", res is None, res)
ok("die Ablehnung nennt die Quelle, der sie folgt",
   "Git repository" in why, why)
ok("und den Weg, der offen bleibt: an der Maschine",
   "oaap app install" in why and "machine" in why, why)

print("")
print("Der Hinweis auf die Uebernahme -- der bessere Weg zuerst")

reg = m.load_registry()
res, _ = review(plain, "neu")
hint = m._same_package_test_instance(reg, DEFAULT, "demo",
                                     m._sha256_file(plain))
ok("liegt dasselbe Paket schon in einer Test-Instanz, wird sie genannt",
   hint == "testinst", hint)
ok("verglichen wird die Pruefsumme, nicht die Versionsnummer",
   m._same_package_test_instance(reg, DEFAULT, "demo", "b" * 64) == "",
   "die Nummer ist genau das, wofuer ein hochgeladenes Paket nicht "
   "einstehen kann")

print("")
print("Bestaetigen heisst bestaetigen")

seen = {}


def _capture(name, zip_path, grant, channel="test", **kw):
    seen.update(name=name, channel=channel,
                sideloaded_by=kw.get("sideloaded_by", ""))
    return "1.2.0", "c" * 64


real_install = m.install_artifact
m.install_artifact = _capture

widen = package("widen2", manifest("1.2.0", public=True))
try:
    with contextlib.redirect_stdout(io.StringIO()):
        m.sideload_install(widen, DEFAULT, "prod", "joerg")
    refused = False
except m.SideloadRefused as e:
    refused, why = True, str(e)
ok("eine Erweiterung ohne Bestaetigung installiert nichts",
   refused and not seen, why if refused else seen)
ok("und die Ablehnung zaehlt auf, was zu bestaetigen waere",
   "/anzeige" in why, why)

seen.clear()
with contextlib.redirect_stdout(io.StringIO()):
    version, sha2, notes, key, created = m.sideload_install(
        widen, DEFAULT, "prod", "joerg", confirmed=True)
ok("mit Bestaetigung laeuft sie", seen.get("name") == "prod", seen)
ok("und zwar auf den Produktiv-Kanal", seen.get("channel") == "production")
ok("die Herkunft wird mitgeschrieben: wer es hochgeladen hat",
   seen.get("sideloaded_by") == "joerg", seen)
ok("es war eine Aktualisierung, keine Neuanlage", created is False)

log = m.read_tenant_log(DEFAULT, limit=50)
entry = next((e for e in log if e.get("action") == "instance.sideload"), None)
ok("der Mandant sieht es in SEINEM Protokoll", entry is not None, log[:3])
ok("mit Person, Fassung und Pruefsumme",
   entry and entry.get("who") == "joerg" and "1.2.0" in entry.get("detail", ""),
   entry)
ok("und mit der Erweiterung, die bestaetigt wurde",
   entry and "/anzeige" in entry.get("detail", ""), entry)

seen.clear()
try:
    with contextlib.redirect_stdout(io.StringIO()):
        m.sideload_install(package("x", manifest("1.3.0")), DEFAULT, "prod",
                           "joerg", sha="d" * 64)
    refused = False
except m.SideloadRefused as e:
    refused, why = True, str(e)
ok("eine andere Datei als die gepruefte installiert nichts",
   refused and not seen, why if refused else seen)
ok("und sagt, dass sich die Pruefsumme geaendert hat",
   "checksum changed" in why, why)

m.install_artifact = real_install

print("")
print("Die Kommandozeile prueft den Rahmen jetzt auch in Produktion")

# Der Nebenbefund, den RFC-0037 beim Schreiben selbst gefunden hat: die
# Rahmenpruefung lief nur fuer Test-Instanzen. Ein ZIP-Update einer
# PRODUKTIV-Instanz an der Maschine installierte eine Erweiterung, ohne
# sie zu nennen -- waehrend die Uebernahme, der andere Weg nach
# Produktiv, sie seit je anzeigt und --confirm verlangt.
m.install_artifact = _capture


def cli(zip_path, name, confirm=False, channel=None):
    seen.clear()
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            m.cmd_install(argparse.Namespace(
                package=zip_path, path="", ref="", name=name, channel=channel,
                store_source="", tenant="", key="", bind=[], confirm=confirm))
        return dict(seen), buf.getvalue()
    except SystemExit:
        return dict(seen), buf.getvalue()


got, said = cli(package("cliwide", manifest("1.4.0", public=True)), "prod")
ok("eine Erweiterung wird jetzt genannt", "NOTE" in said and "/anzeige" in said,
   said)
ok("und ohne --confirm installiert sie nichts", not got, (got, said))

got, said = cli(package("cliwide2", manifest("1.5.0", public=True)), "prod",
                confirm=True)
ok("mit --confirm laeuft sie", got.get("channel") == "production", (got, said))
ok("und die Herkunft sagt, dass es von dieser Maschine kam",
   got.get("sideloaded_by") == "cli", got)

got, said = cli(package("cliold", manifest("0.5.0")), "prod")
ok("eine niedrigere Version kommt auch an der Maschine nicht durch",
   not got and "higher version only" in said, (got, said))

m.install_artifact = real_install

print("")
print(f"{'FEHLER' if fails else 'Alles gruen'} - {fails} Fehlschlag(e)")
sys.exit(1 if fails else 0)
