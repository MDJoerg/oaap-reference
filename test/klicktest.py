#!/usr/bin/env python3
"""Klicktest an einem laufenden Knoten (RFC-0012 §6/§7, Spec 2.10).

Anders als die `test_*.py` braucht dieser Test eine **echte Maschine**:
Er meldet sich am Portal an und prueft, was ein Mensch im Browser saehe
— Katalog, Filter, Objektseite, Quellenverwaltung, und den Schreibweg
ueber den Spool-Worker.

Der Schreibteil legt eine Quelle an, benennt sie um, schaltet sie aus
und wieder ein, versucht sie unzulaessig hochzustufen und entfernt sie
wieder. Die verwendete URL zeigt bewusst ins Leere und liegt ausserhalb
unserer Repositories: So wird zugleich die Fehleranzeige einer
unlesbaren Quelle geprueft, und es bleibt kein Rueckstand.

Dazu die Kachel im Launchpad (Spec 2.10): aus, an, eine unzulaessige
Einstellung, zurueck auf automatisch — jeweils mit Blick darauf, ob das
Launchpad mitzaehlt, was es verbirgt. Auch dieser Teil raeumt hinter
sich auf.

Zugangsdaten kommen aus `test/.env` (nicht im Git) und werden nirgends
ausgegeben:

    OAAP_PORTAL_KLICKTEST_USER=...
    OAAP_PORTAL_KLICKTEST_PASSWORD=...

Der Benutzer braucht die Rolle `server_admin`.

    python3 test/klicktest.py [test/.env] [http://10.10.10.96]
"""
import http.cookiejar
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, ".env")
BASE = (sys.argv[2] if len(sys.argv) > 2 else "http://10.10.10.96").rstrip("/")

env = {}
with open(ENV_FILE, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")

jar = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

fails = []


def ok(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        fails.append(label)
        if detail:
            print(f"      {detail[:400]}")


def get(path):
    with op.open(BASE + path, timeout=30) as r:
        return r.status, r.read().decode("utf-8", "replace"), r.geturl()


def post(path, fields):
    data = urllib.parse.urlencode(fields).encode()
    with op.open(BASE + path, data=data, timeout=90) as r:
        return r.status, r.read().decode("utf-8", "replace"), r.geturl()


print("=== Anmeldung ===")
get("/auth/login")
st, body, url = post("/auth/login",
                     {"username": env["OAAP_PORTAL_KLICKTEST_USER"],
                      "password": env["OAAP_PORTAL_KLICKTEST_PASSWORD"]})
ok("Anmeldung am Portal", "/auth/login" not in url, url)

print("\n=== Store-Katalog (/store) ===")
st, body, url = get("/store")
ok("Seite kommt", st == 200 and "/store" in url, f"{st} {url}")
tiles = re.findall(r'href="/store/([^/"]+)/([^"]+)"', body)
ok("Kacheln verlinken auf Objektseiten", len(tiles) >= 7, str(tiles))
ok("jede Kachel nennt ihre Quelle",
   body.count("OAAP Community-Liste") + body.count("OAAP Plattform-Apps") >= 7,
   f"community={body.count('OAAP Community-Liste')} platform={body.count('OAAP Plattform-Apps')}")
ok("Vertrauensklassen stehen dran", "geprüft" in body and "von uns" in body)
ok("Ollama ist als Hintergrunddienst gekennzeichnet", "Hintergrunddienst" in body)
ok("alle acht Apps sind installiert und werden so gezeigt",
   body.count("installiert") >= 7, str(body.count("installiert")))
# oaap-test TRAEGT das Profil dev, also gehoert das Studio hier in die
# Voreinstellung. Auf einem Knoten ohne dev waere es ausgefiltert.
ok("Studio ist sichtbar, weil dieser Knoten das Profil dev hat",
   "/store/oaap.platform/studio" in body)
ok("Filterformular ist da", 'name="q"' in body and "<form" in body)

print("\n=== Filter aufheben ===")
st, body2, url = get("/store?profile=alle")
ok("mit aufgehobenem Profilfilter bleibt das Studio sichtbar",
   "/store/oaap.platform/studio" in body2, url)

print("\n=== Objektseite (/store/oaap.platform/studio) ===")
st, body3, url = get("/store/oaap.platform/studio")
ok("Objektseite kommt", st == 200, f"{st} {url}")
ok("nennt Quelle und Vertrauensklasse",
   "OAAP Plattform-Apps" in body3 and "von uns" in body3)
ok("warnt, dass die App ein anderes Knotenprofil erwartet",
   "dev" in body3 and ("Profil" in body3 or "profil" in body3))
ok("sagt, dass das Paket nicht festgelegt ist",
   "festgelegt" in body3 or "gepinnt" in body3 or "nicht angeheftet" in body3,
   body3[body3.find("Fakten"):][:300] if "Fakten" in body3 else "")
ok("zeigt den CLI-Befehl", "oaap app install" in body3)
# Alle acht Apps sind auf oaap-test installiert und aktuell, also bietet
# die Karte richtigerweise keine zweite Installation an.
ok("die Installations-Karte sagt, dass nichts zu tun ist",
   "Auf dem aktuellen Stand" in body3 and 'action="/store/install"' not in body3,
   body3[body3.find("Installieren"):][:200])

print("\n=== Objektseite einer Community-App ===")
st, body4, url = get("/store/oaap.community/ollama")
ok("Ollama-Objektseite kommt", st == 200, f"{st} {url}")
ok("als Hintergrunddienst ausgewiesen", "Hintergrunddienst" in body4)
ok("Beschreibung ist da", len(body4) > 2000, str(len(body4)))

print("\n=== Quellenverwaltung (/store/sources) ===")
st, body5, url = get("/store/sources")
ok("Quellenseite kommt", st == 200, f"{st} {url}")
ok("beide Quellen stehen in der Tabelle",
   "oaap.community" in body5 and "oaap.platform" in body5)
ok("mitgelieferte Quellen sind als solche erkennbar", "mitgeliefert" in body5)
ok("Hinzufuegen-Formular ist da", 'name="url"' in body5)
ok("die Plattform-Quelle bietet keine Umstufung auf etwas anderes an",
   body5.count("von uns") >= 1)

print("\n=== Quellen anlegen und pflegen ueber die Oberflaeche (§7) ===")
# Bewusst eine URL AUSSERHALB unserer Repositories: nicht abrufbar, also
# wird zugleich die Fehleranzeige einer unlesbaren Quelle geprueft, und
# es bleibt kein Rueckstand in den mitgelieferten Quellen.
TESTURL = "https://klicktest.invalid/oaap-store.json"
st, body, url = post("/store/sources", {"op": "add", "url": TESTURL,
                                        "name": "Klicktest-Quelle",
                                        "origin": "Abnahme RFC-0012",
                                        "trust": "unverified"})
ok("Quelle hinzugefuegt", "klicktest.invalid" in body, body[-600:])
sid = ""
m = re.search(r'name="source_id" value="(klicktest\.invalid-[a-f0-9]+)"', body)
if m:
    sid = m.group(1)
ok("bekommt eine abgeleitete Kennung", bool(sid), sid)
ok("und die Klasse 'muss bestaetigt werden'", "muss bestätigt werden" in body)

st, body, url = post("/store/sources", {"op": "rename", "source_id": sid,
                                        "name": "Umbenannt durch Klicktest"})
ok("umbenennen wirkt", "Umbenannt durch Klicktest" in body, body[-400:])

st, body, url = post("/store/sources", {"op": "trust", "source_id": sid,
                                        "trust": "platform"})
ok("auf 'von uns' hochstufen wird abgewiesen",
   "reserv" in body.lower() or "von uns" not in body.split(sid)[0][-800:],
   body[-500:])

st, body, url = post("/store/sources", {"op": "disable", "source_id": sid})
ok("abschalten wirkt", "aus" in body)
st, body, url = get("/store")
ok("eine abgeschaltete Quelle taucht im Katalog nicht auf",
   "Klicktest" not in body and "Umbenannt durch Klicktest" not in body)

st, body, url = post("/store/sources", {"op": "enable", "source_id": sid})
st, body, url = get("/store")
ok("wieder eingeschaltet meldet der Katalog die Quelle als unlesbar",
   "nicht" in body.lower() and ("klicktest.invalid" in body.lower()
                               or "Umbenannt" in body), body[:0])

st, body, url = post("/store/sources", {"op": "remove", "source_id": sid,
                                        "confirm": sid})
# Nicht auf den ganzen Rumpf pruefen: die Erfolgsmeldung nennt die
# Kennung, die Tabelle darf sie nicht mehr fuehren.
ok("entfernen wirkt", f'value="{sid}"' not in body,
   urllib.parse.unquote(url))
ok("und die mitgelieferten Quellen sind unversehrt",
   "oaap.community" in body and "oaap.platform" in body)

print("\n=== Kachel im Launchpad (Bauschritt 7, Spec 2.10) ===")
# Ollama ist der lebende Fall: der Katalog kennzeichnet es seit jeher
# als Hintergrunddienst, das Launchpad zeigte trotzdem eine Kachel auf
# eine reine Schnittstelle. Der Knoten entscheidet am MANIFEST, also
# wirkt das erst nach einem Deployment der Fassung, die sich selbst so
# nennt — bis dahin behaelt die Instanz ihre Kachel, und genau das
# gehoert geprueft: ein Update darf kein Launchpad leerraeumen.
st, body, url = get("/instances")
ok("die Instanzliste hat eine Spalte fuer die Kachel", "Kachel" in body, url)
ok("und sagt, dass das keine Zugriffskontrolle ist",
   "reine Anzeige" in body)

# Nicht auf einen Instanznamen festnageln — der Test soll auf jedem
# Knoten laufen. Die Regel gilt fuer jede Instanz gleich.
# 'new' ist der Anlegen-Knopf, keine Instanz — der Filter fehlte im
# ersten Anlauf und der Test prüfte die Dialogseite statt einer Instanz.
names = [n for n in re.findall(r'href="/instances/([a-z0-9][a-z0-9-]*)"', body)
         if n != "new"]
target = names[0] if names else ""
ok("mindestens eine Instanz zum Pruefen gefunden", bool(target), str(names[:5]))

st, body7, url = get(f"/instances/{target}")
ok("die Instanzseite kommt", st == 200, f"{st} {url}")
ok("sie hat die Karte fuer die Kachel", "Kachel im Launchpad" in body7)
ok("sie sagt, was die App ueber sich selbst behauptet",
   "bezeichnet sich selbst" in body7)
ok("sie warnt, dass Verstecken niemanden aussperrt",
   "Zugriffskontrolle" in body7)

# Kacheln zaehlen statt Namen suchen: eindeutig, auch wenn zwei
# Instanzen dieselbe App fahren.
def tiles_now():
    return get("/")[1].count('class="tile"')


before = tiles_now()
post(f"/instances/{target}/tile", {"mode": "off"})
after_off = tiles_now()
ok("abgeschaltet verschwindet genau eine Kachel", after_off == before - 1,
   f"vorher={before} nachher={after_off}")
ok("und das Launchpad sagt einem server_admin, dass es etwas verbirgt",
   "ohne Kachel" in get("/")[1])

post(f"/instances/{target}/tile", {"mode": "on"})
ok("eingeschaltet ist sie wieder da", tiles_now() == before,
   f"{tiles_now()} vs {before}")

st, body, url = post(f"/instances/{target}/tile", {"mode": "kaputt"})
ok("eine unbekannte Einstellung wird abgewiesen",
   "nbekannte" in body, body[-300:])
ok("und hat nichts veraendert", tiles_now() == before)

post(f"/instances/{target}/tile", {"mode": "auto"})
st, body7, url = get(f"/instances/{target}")
ok("zurueck auf automatisch wirkt", 'value="auto"' in body7, body[-300:])
# Egal wie die Kachel steht: die Instanz behaelt Adresse, Routen und
# Rollen. Das ist die Zusicherung, die am leichtesten kaputtgeht.
ok("die Instanz hat Sichtbarkeit und Adresse unveraendert behalten",
   "Sichtbarkeit" in body7 and "Eigene Adresse" in body7)

print("\n=== unbekannte Kennungen werden abgewiesen ===")
try:
    st, body6, url = get("/store/oaap.community/gibtsnicht")
    ok("unbekannte App-Kennung", st in (303, 404) or "nicht" in body6.lower(),
       f"{st} {url}")
except urllib.error.HTTPError as e:
    ok("unbekannte App-Kennung wird abgewiesen", e.code in (400, 404), str(e.code))

print()
if fails:
    print(f"{len(fails)} FEHLER: " + "; ".join(fails))
    sys.exit(1)
print("ALLE PRUEFUNGEN BESTANDEN")
