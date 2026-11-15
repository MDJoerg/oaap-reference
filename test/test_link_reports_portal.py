#!/usr/bin/env python3
"""The portal asks the tunnel apps (RFC-0087 §8.1, oaap.fleet.status 0.11).

`_link_reports` asks every instance with a `link_state` door through the
gateway's internal endpoint, and `_fleet_links` puts the answer into the
`through` row. The gateway is replaced by a fake `requests.get`, so what
is checked is the portal's own rule:

- who is asked (only instances with the door), where (/l/<name>), how
  long (2 s);
- a sleeping instance or one in maintenance is not asked -> `paused`;
- no answer, a 503, a body that is no JSON, a wrong version -> `unknown`;
- the answer reaches the status document's `through` row as one word,
  and nothing the app added (an address) leaves.

Needs Flask and requests (the portal's image):
    docker run --rm -v <ref>:/w -w /w --entrypoint python3 oaap-portal test/test_link_reports_portal.py
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["OAAP_DATA_DIR"] = tempfile.mkdtemp(prefix="oaap-link-reports-")
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "portal"))

import requests                                               # noqa: E402
import app as portal                                          # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:800]}")


class Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        if isinstance(self._body, str):
            return json.loads(self._body)
        return self._body


ASKED = []
ANSWERS = {}


def fake_get(url, timeout=None, allow_redirects=True):
    ASKED.append((url, timeout))
    a = ANSWERS.get(url.rsplit("/", 1)[-1])
    if isinstance(a, Exception):
        raise a
    return a or Resp(404, "")


portal.requests.get = fake_get
DOOR = {"path": "/oaap/links"}
GOOD = {"v": 1, "tunnel": {"state": "ok"},
        "targets": {"gamingpc": {"state": "down", "addr": "10.8.0.5:11434"}}}

print("=== wer gefragt wird ===")
insts = [("ais-ovc", {"link_state": DOOR, "tenant": "T-ais", "name": "ovc"}),
         ("ais-mg", {"links": ["ais-ovc"], "tenant": "T-ais"}),
         ("schlaeft", {"link_state": DOOR, "state": "standby", "standby": {"since": "x"}}),
         ("wartung", {"link_state": DOOR, "maintenance": {"since": "x"}}),
         ("stumm", {"link_state": DOOR}), ("kaputt", {"link_state": DOOR}),
         ("kein-json", {"link_state": DOOR}), ("v2", {"link_state": DOOR})]
ANSWERS.update({"ais-ovc": Resp(200, GOOD), "stumm": requests.ConnectTimeout("t"),
                "kaputt": Resp(503, GOOD), "kein-json": Resp(200, "<html>"),
                "v2": Resp(200, {"v": 2})})
rep = portal._link_reports(insts)
urls = sorted(u for u, _t in ASKED)
ok("gefragt: nur Instanzen mit Tür und wach, über /l/<name>",
   urls == sorted(f"{portal.GATEWAY_HEALTH}/l/{n}" for n in ("ais-ovc", "stumm", "kaputt",
                                                             "kein-json", "v2")), urls)
ok("je 2 s", {t for _u, t in ASKED} == {2}, ASKED)
ok("eine Instanz ohne Tür (auch mit Partnerliste) kommt nicht vor", "ais-mg" not in rep, rep)
ok("Standby und Wartung: paused, nicht gefragt",
   rep["schlaeft"] == "paused" and rep["wartung"] == "paused", rep)
ok("keine Antwort, 503, kein JSON, falsche Version: unknown",
   all(rep[n] == "unknown" for n in ("stumm", "kaputt", "kein-json", "v2")), rep)
ok("eine gute Antwort, gelesen nach der Whitelist",
   rep["ais-ovc"] == {"tunnel": "ok", "targets": {"gamingpc": "down"}}, rep["ais-ovc"])

print("=== bis ins Statusdokument ===")
portal._connect_data = lambda: ({}, {})
portal._connect_stale_since = lambda: ""
portal.load_tenants = lambda: {"T-ais": {"label": "ais"}}
portal.resolve_tenant = lambda ref: ref or ""
portal.instance_auto_host = lambda name, inst: ""
portal._load_destinations_all = lambda: {"T-ais": {"gamingpc": {"target": {"through": "ovc/gamingpc"}}}}
portal._read_json = lambda path: ({"ceiling": "names", "salt": "s"}
                                  if path == portal.FLEET_DISCLOSE_FILE else {})
rows = [{"instance": "ais-ovc"}]
links, _block = portal._fleet_links(insts[:2], rows)
thr = [lk for lk in links or [] if "through" in lk]
ok("through-Zeile: ais-ovc, gamingpc, state down-target",
   thr and thr[0]["through"] == {"instance": "ais-ovc", "target": "gamingpc", "state": "down-target"},
   links)
ok("die Adresse, die die App dazuschrieb, geht nicht hinaus",
   "10.8.0.5" not in json.dumps(links) and "11434" not in json.dumps(links), links)
ANSWERS["ais-ovc"] = requests.ConnectionError("weg")
links, _block = portal._fleet_links(insts[:2], rows)
thr = [lk for lk in links or [] if "through" in lk]
ok("App antwortet nicht: state unknown (die Zentrale entscheidet damit nichts)",
   thr and thr[0]["through"]["state"] == "unknown", links)
links, _block = portal._fleet_links([("ais-ovc", {"tenant": "T-ais", "name": "ovc"})], rows)
thr = [lk for lk in links or [] if "through" in lk]
ok("App ohne Tür: through ohne state, wie vor 0.11", thr and "state" not in thr[0]["through"], links)

print("")
print("ALLE GRUEN" if not fails else f"{fails} FEHLER")
sys.exit(1 if fails else 0)
