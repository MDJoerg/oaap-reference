"""RFC-0058: shared folders and trainer seats -- the template's rules
(pure) and the mount arguments the runtime builds (docker faked)."""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))

import cohort  # noqa: E402

FAILS = []


def check(label, ok, extra=""):
    print(("PASS  " if ok else "FAIL  ") + label + (f"  [{extra}]" if extra and not ok else ""))
    if not ok:
        FAILS.append(label)


def make(extra, files=("material/LIESMICH.txt",)):
    d = tempfile.mkdtemp()
    for f in files:
        p = os.path.join(d, f)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w").write("x")
    body = ('oaap_cohort: "0.1"\nname: kurs\nseats: 2\n'
            'apps:\n  - id: webtop\n    name: desk\n' + extra)
    open(os.path.join(d, "cohort.yaml"), "w").write(body)
    return d


def problems(extra, **kw):
    try:
        cohort.load(make(extra, **kw))
        return []
    except cohort.TemplateError as e:
        return e.problems


ok = '''shared_storage:
  - name: gemeinsam
    mount: /gemeinsam
    access: rw
  - name: schulung
    mount: /schulung
    access: ro
    source: material/
trainers: 2
'''
tpl = cohort.load(make(ok))
check("Vorlage mit shared_storage und trainers wird gelesen",
      [x["name"] for x in tpl["shared_storage"]] == ["gemeinsam", "schulung"])
check("Trainer-Plaetze stehen hinter den Teilnehmern",
      tpl["seat_ids"] == ["01", "02", "t01", "t02"], tpl["seat_ids"])
check("Trainer: eigener Benutzername und Gruppe",
      cohort.user_name(tpl, "t01") == "kurs-t01"
      and cohort.trainer_group(tpl) == "kurs-trainer"
      and cohort.is_trainer(tpl, "t01") and not cohort.is_trainer(tpl, "01"))
base = "/data/cohorts/t/kurs/shared"
part = {m["name"]: m for m in cohort.shared_mounts(tpl, "01", base)}
train = {m["name"]: m for m in cohort.shared_mounts(tpl, "t01", base)}
check("Teilnehmer: schulung nur lesbar, gemeinsam beschreibbar",
      part["schulung"]["ro"] and not part["gemeinsam"]["ro"])
check("Trainer: beides beschreibbar",
      not train["schulung"]["ro"] and not train["gemeinsam"]["ro"])
check("Quelle liegt ausserhalb von tenants/ (Ordner der Kohorte)",
      part["schulung"]["host"] == base + "/schulung" or
      part["schulung"]["host"].replace("\\", "/") == base + "/schulung")
check("ohne shared_storage/trainers: wie bisher",
      cohort.load(make(""))["shared_storage"] == []
      and cohort.load(make(""))["seat_ids"] == ["01", "02"])

for label, extra in (
        ("mount /config wird abgelehnt",
         "shared_storage:\n  - {name: a, mount: /config, access: rw}\n"),
        ("mount mit .. wird abgelehnt",
         "shared_storage:\n  - {name: a, mount: /x/../etc, access: rw}\n"),
        ("access muss rw oder ro sein",
         "shared_storage:\n  - {name: a, mount: /a, access: x}\n"),
        ("doppelter Mount",
         "shared_storage:\n  - {name: a, mount: /a}\n  - {name: b, mount: /a}\n"),
        ("fehlende Quelle",
         "shared_storage:\n  - {name: a, mount: /a, source: nix/}\n"),
        ("trainers ausser Bereich", "trainers: 30\n"),
):
    check(label, bool(problems(extra)), problems(extra))

# ---- the runtime: what recreate_instance_containers hands to docker
import appctl as a  # noqa: E402

calls = []


class Done:
    stderr = ""
    returncode = 0
    stdout = ""


a.ensure_app_network = lambda n: "net"
a.connect_gateway = lambda n: None
a.restore_links = lambda n: None
a.load_registry = lambda: {"instances": {}}
a.image_uid = lambda i: None
a.env_path = lambda n, i=None: "/e"
a.instance_dir = lambda n, i=None: os.path.join(tempfile.gettempdir(), "oaap-sh-" + n)
a.run = lambda cmd, *x, **k: (calls.append(cmd), Done())[1]
a.subprocess.run = lambda *x, **k: Done()

svc = [{"service": "", "container": "c1", "image": "img"}]
stor = [{"name": "home", "mount": "/config"},
        {"name": "material", "mount": "/schulung"}]
host = os.path.join(tempfile.mkdtemp(), "schulung")
inst = {"shared_mounts": [{"name": "schulung", "mount": "/schulung",
                           "host": host, "ro": True}]}
a.recreate_instance_containers("kurs-desk-01", svc, stor, inst=inst)
cmd = calls[-1]
vs = [cmd[i + 1] for i, c in enumerate(cmd) if c == "-v"]
check("geteilter Ordner wird nur lesbar eingehaengt", f"{host}:/schulung:ro" in vs, vs)
check("eigener Speicher auf demselben Pfad entfaellt",
      not any(v.endswith(":/schulung") for v in vs)
      and any(v.endswith(":/config") for v in vs), vs)
check("der Ordner wird angelegt", os.path.isdir(host))

calls.clear()
a.recreate_instance_containers("kurs-desk-01", svc, stor, inst={})
vs = [calls[-1][i + 1] for i, c in enumerate(calls[-1]) if c == "-v"]
check("ohne Eintrag: eigenes Material wie bisher",
      any(v.endswith(":/schulung") and "storage" in v for v in vs), vs)

print()
print("OK" if not FAILS else "FAILED: " + "; ".join(FAILS))
sys.exit(1 if FAILS else 0)
