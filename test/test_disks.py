"""RFC-0063: disks and archives as node resources -- the host rules
(services/disks.py, host commands faked) and the container the runtime
builds for an instance with grants (docker faked).

What is held here are the rules the measurements of 2026-10-22 produced
(CURRENT_STATE 347, program/messungen/datentraeger-messung-*.sh), not
today's code: a read-only grant is ONE mount call (M13), an app gets its
grant base and never a disk or a FUSE mount (M5b), the base is sealed
(M12), a raw disk is mounted so that nothing writes it -- not even its
own journal --, a rehearsal gets no grant, and a missing disk stops the
start BEFORE the old container is removed.

Braucht nichts: kein Docker, keine Platte, kein root.
Aufruf: python3 test/test_disks.py
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp()
os.environ["OAAP_DISKS_ROOT"] = os.path.join(TMP, "srv")
os.environ["OAAP_DATA_DIR"] = os.path.join(TMP, "data")
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))

import disks  # noqa: E402

FAILS = []


def check(label, ok, extra=""):
    print(("PASS  " if ok else "FAIL  ") + label + (f"  [{extra}]" if extra and not ok else ""))
    if not ok:
        FAILS.append(label)


class Done:
    def __init__(self, out="", rc=0, err=""):
        self.stdout, self.returncode, self.stderr = out, rc, err


SH = []
MOUNTED = set()


def fake_sh(cmd, check=True):
    SH.append(list(cmd))
    if cmd[:2] == ["mount", "-t"] or cmd[:3] == ["mount", "-o", "bind"] \
            or cmd[:3] == ["mount", "-o", "bind,ro"]:
        MOUNTED.add(cmd[-1])
    if cmd[0] == "umount":
        MOUNTED.discard(cmd[-1])
    return Done()


disks.sh = fake_sh
disks.ismount = lambda p: p in MOUNTED

# ------------------------------------------------------------- rules
o = disks.mount_options("raw", "ext4").split(",")
check("raw ext4: ro, noexec, und noload (sonst spielt ro das Journal ab = Schreiben)",
      {"ro", "noexec", "noload", "nodev", "nosuid"} <= set(o), o)
o = disks.mount_options("raw", "exfat").split(",")
check("raw exFAT: ro und fuer alle lesbar, fuer niemanden schreibbar",
      {"ro", "uid=0", "gid=0", "fmask=0222", "dmask=0222"} <= set(o), o)
check("work ext4: beschreibbar, nodev/nosuid",
      disks.mount_options("work", "ext4") == "noatime,nodev,nosuid")
try:
    disks.mount_options("work", "exfat")
    check("work exFAT wird abgelehnt (keine Besitzer, keine Rechte)", False)
except disks.DiskRefused:
    check("work exFAT wird abgelehnt (keine Besitzer, keine Rechte)", True)

raw, work = {"name": "roh", "role": "raw"}, {"name": "arbeit", "role": "work"}
arch, path = {"kind": "archive"}, {"kind": "path"}
check("ro geht immer", disks.grant_refusal(raw, path, False) == "")
check("rw auf ein Archiv: abgelehnt", "read-only" in disks.grant_refusal(work, arch, True))
check("rw auf eine Rohplatte: abgelehnt (D5)", "raw" in disks.grant_refusal(raw, path, True))
check("rw auf einen Pfad der Arbeitsplatte: erlaubt", disks.grant_refusal(work, path, True) == "")
for bad in ("", "A", "1x", "a/b", "a" * 41, "../x"):
    try:
        disks.check_name("x", bad)
        check(f"Name {bad!r} abgelehnt", False)
    except disks.DiskRefused:
        pass
check("gueltige Namen", disks.check_name("x", "rohdaten-1") == "rohdaten-1")

# formats from header bytes, never from `file`
fd = os.path.join(TMP, "fmt")
os.makedirs(fd)
samples = {"t.tar": b"\0" * 257 + b"ustar" + b"\0" * 250, "a.gz": b"\x1f\x8b\x08",
           "b.zst": b"\x28\xb5\x2f\xfd", "c.zip": b"PK\x03\x04", "d.7z": b"7z\xbc\xaf\x27\x1c",
           "e.txt": b"hallo"}
for n, b in samples.items():
    open(os.path.join(fd, n), "wb").write(b)
got = {n: disks.detect_format(os.path.join(fd, n)) for n in samples}
check("Formate am Kopf erkannt", got == {"t.tar": "tar", "a.gz": "gzip", "b.zst": "zstd",
                                         "c.zip": "zip", "d.7z": "7z", "e.txt": "other"}, got)
check("7z ist nicht unter den unterstuetzten Formaten (M11)", "7z" not in disks.ARCHIVE_FORMATS)

# scan: fixed order, independent of the filesystem's own
sd = os.path.join(TMP, "scan")
for rel in ("b/z.tar", "a/y.tar", "a/x.tar", "c.tar"):
    p = os.path.join(sd, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "wb").write(samples["t.tar"])
order = [x["rel"].replace(os.sep, "/") for x in disks.scan(sd)]
check("scan nummeriert sortiert (Ordner und Dateien)",
      order == ["c.tar", "a/x.tar", "a/y.tar", "b/z.tar"], order)

mi = ("36 35 98:0 / /oaap/disks rw,relatime master:1 - tmpfs oaap-grants ro\n"
      "37 36 98:1 / /oaap/disks/roh ro,relatime master:2 - fuse.ratarmount r rw\n"
      "38 36 98:2 / /oaap/disks/arbeit rw,relatime master:3 - ext4 /dev/x rw\n")
seen = disks.parse_mountinfo(mi)
check("mountinfo: Flag je Einhaengung (nicht das des Dateisystems)",
      "ro" in seen["/oaap/disks/roh"] and "ro" not in seen["/oaap/disks/arbeit"], seen)

# ------------------------------------------------------ grant base
dreg = {"disks": {"roh": {"name": "roh", "role": "raw"},
                  "arbeit": {"name": "arbeit", "role": "work"}},
        "sources": {"leak": {"kind": "archive", "disk": "roh", "path": "x.tar"},
                    "projekt": {"kind": "path", "disk": "arbeit", "path": "projekt"}}}
MOUNTED.update({disks.archive_mount("leak"), disks.disk_mount("arbeit")})
os.makedirs(disks.source_path(dreg, "projekt"), exist_ok=True)
grants = [{"grant": "rohdaten", "source": "leak", "rw": False},
          {"grant": "arbeit", "source": "projekt", "rw": True}]
SH.clear()
rep = dict(disks.open_grants(dreg, "iid-1", grants))
base = disks.grant_base("iid-1")
check("Basisordner versiegelt (chattr +i) vor dem ersten tmpfs",
      ["chattr", "+i", base] in SH and SH.index(["chattr", "+i", base])
      < next(i for i, c in enumerate(SH) if c[:2] == ["mount", "-t"]), SH[:4])
check("tmpfs geteilt (rslave-Container sehen spaetere Freigaben)",
      ["mount", "--make-shared", base] in SH)
ro_calls = [c for c in SH if c[:3] == ["mount", "-o", "bind,ro"]]
check("ro-Freigabe in EINEM Aufruf: mount -o bind,ro (M13)",
      ro_calls == [["mount", "-o", "bind,ro", disks.archive_mount("leak"),
                    os.path.join(base, "rohdaten")]], ro_calls)
check("niemals bind und danach remount,bind,ro (M13 A)",
      not any("remount,bind,ro" in " ".join(c) for c in SH))
check("rw-Freigabe nur als bind", ["mount", "-o", "bind", disks.source_path(dreg, "projekt"),
                                   os.path.join(base, "arbeit")] in SH)
check("Platzhalter: tmpfs danach wieder ro",
      [c for c in SH if c[:3] == ["mount", "-o", "remount,ro"]] != [])
check("beide gemeldet", rep == {"rohdaten": "mounted", "arbeit": "mounted"}, rep)

SH.clear()
rep = dict(disks.open_grants(dreg, "iid-1", grants))
check("zweiter Lauf: nichts neu, nichts doppelt", rep == {"rohdaten": "kept", "arbeit": "kept"}
      and not any(c[:2] == ["mount", "-o"] and "bind" in c[2] for c in SH), SH)

# the check from inside: the container saw rw for a ro grant -> revoked
pid_dir = os.path.join(TMP, "proc")
os.makedirs(pid_dir)
real_open = open


def fake_open(p, *a, **k):
    if p == "/proc/4711/mountinfo":
        return real_open(os.path.join(pid_dir, "mi"), *a, **k)
    return real_open(p, *a, **k)


real_open(os.path.join(pid_dir, "mi"), "w").write(
    "37 36 98:1 / /oaap/disks/rohdaten rw,relatime - fuse r rw\n"
    "38 36 98:2 / /oaap/disks/arbeit rw,relatime - ext4 /dev/x rw\n")
disks.open = fake_open
SH.clear()
rep = dict(disks.verify_inside(base, {g["grant"]: g for g in grants}, 4711,
                               [("rohdaten", "kept"), ("arbeit", "kept")]))
del disks.open
check("Container sieht rw bei einer ro-Freigabe: Freigabe entzogen",
      rep["rohdaten"].startswith("revoked") and ["umount", os.path.join(base, "rohdaten")] in SH, rep)
check("rw-Freigabe mit rw im Container bleibt", rep["arbeit"] == "kept", rep)

# revoke one: placeholder goes, the other stays
MOUNTED.add(os.path.join(base, "rohdaten"))
SH.clear()
disks.close_grants("iid-1", only={"rohdaten"})
disks.open_grants(dreg, "iid-1", grants[1:])
check("Entziehen: Freigabe ausgehaengt, Platzhalter weg, andere bleibt",
      not os.path.isdir(os.path.join(base, "rohdaten")) and os.path.isdir(os.path.join(base, "arbeit")))

check("fehlende Pflicht-Freigabe wird gemeldet, optionale nicht",
      disks.missing(dreg, [{"grant": "g", "source": "weg"},
                           {"grant": "h", "source": "weg2", "optional": True}]) == [("g", "weg", "?")])

argv = disks.archive_argv(dict(dreg, sources=dict(dreg["sources"], leak=dict(
    dreg["sources"]["leak"], index_on="arbeit"))), "leak")
check("ratarmount: Vordergrund, allow_other, Index auf der Arbeitsplatte, nie neben dem Archiv",
      argv[1:4] == ["-f", "-o", "allow_other"]
      and argv[argv.index("--index-file") + 1].startswith(disks.disk_mount("arbeit"))
      and argv[-2].startswith(disks.disk_mount("roh")), argv)

# devices(): what lsblk says, flattened (fake lsblk)
LSBLK = {"blockdevices": [
    {"name": "sda", "kname": "sda", "path": "/dev/sda", "size": 500, "type": "disk",
     "tran": "sata", "model": "SSD", "children": [
         {"name": "sda1", "path": "/dev/sda1", "size": 400, "type": "part", "fstype": "ext4",
          "uuid": "sys-1", "mountpoints": ["/"]}]},
    {"name": "sdb", "kname": "sdb", "path": "/dev/sdb", "size": 466, "type": "disk",
     "tran": "usb", "model": "HDD", "children": [
         {"name": "sdb1", "path": "/dev/sdb1", "size": 465, "type": "part", "fstype": "crypto_LUKS",
          "uuid": "luks-part", "children": [
              {"name": "oaap-disk-w", "type": "crypt", "fstype": "ext4",
               "mountpoints": ["/srv/oaap/disks/w"]}]}]},
    {"name": "loop1", "kname": "loop1", "path": "/dev/loop1", "size": 300, "type": "loop",
     "fstype": "crypto_LUKS", "uuid": "luks-whole", "children": [
         {"name": "oaap-disk-x", "type": "crypt", "fstype": "ext4", "mountpoints": ["/srv/x"]}]},
]}
SH.clear()
_sh = disks.sh
disks.sh = lambda cmd, check=True: Done(json.dumps(LSBLK)) if cmd[0] == "lsblk" else _sh(cmd, check)
devs = {d["uuid"]: d for d in disks.devices()}
disks.sh = _sh
check("Systemplatte erkannt (traegt /)", devs["sys-1"]["system"])
check("LUKS-Partition gefunden, Inhalt ext4, eingehaengt", devs["luks-part"]["inner_fstype"] == "ext4"
      and devs["luks-part"]["mountpoints"] == ["/srv/oaap/disks/w"], devs.get("luks-part"))
check("LUKS ueber das ganze Geraet: das offene Kind verdeckt das Geraet nicht",
      "luks-whole" in devs and devs["luks-whole"]["path"] == "/dev/loop1", list(devs))

MOUNTED.add(disks.archive_mount("leak"))
SH.clear()
disks.stop_archive("leak")
check("Archiv stoppen: erst aushaengen, dann die Unit (sonst endet sie als failed)",
      SH[0] == ["umount", disks.archive_mount("leak")] and SH[1][:2] == ["systemctl", "stop"], SH)
MOUNTED.add(disks.archive_mount("leak"))

# --------------------------------------------- the runtime (appctl)
import appctl as a  # noqa: E402

a.disks.sh = fake_sh
a.disks.ismount = disks.ismount
a.DISKS_FILE = os.path.join(TMP, "disks.json")
json.dump(dreg, open(a.DISKS_FILE, "w"))
calls, sub = [], []
a.ensure_app_network = lambda n: "net"
a.connect_gateway = lambda n: None
a.restore_links = lambda n: None
a.image_uid = lambda i: None
a.env_path = lambda n, i=None: "/e"
a.instance_dir = lambda n, i=None: os.path.join(TMP, "inst-" + n)
a.run = lambda cmd, *x, **k: (calls.append(cmd), Done())[1]
a.subprocess.run = lambda cmd, *x, **k: (sub.append(cmd), Done())[1]
REC = {"id": "iid-2", "disk_grants": [{"grant": "rohdaten", "source": "leak"}]}
a.load_registry = lambda: {"instances": {"desk": REC}}
svc = [{"service": "", "container": "c1", "image": "img"}]

a.recreate_instance_containers("desk", svc, [], inst=REC)
cmd = calls[-1]
mnts = [cmd[i + 1] for i, c in enumerate(cmd) if c == "--mount"]
check("Instanz mit Freigabe: genau ihr Basisordner, rslave, nach /oaap/disks",
      mnts == [f"type=bind,src={disks.grant_base('iid-2')},dst=/oaap/disks,"
               "bind-propagation=rslave"], mnts)
check("kein Archiv und keine Platte direkt im Container (M5b)",
      not any(disks.archive_mount("leak") in x or disks.disk_mount("roh") in x
              for x in cmd), cmd)
check("nach dem Start: Freigabe in den Basisordner gehaengt",
      ["mount", "-o", "bind,ro", disks.archive_mount("leak"),
       os.path.join(disks.grant_base("iid-2"), "rohdaten")] in SH)

MOUNTED.discard(disks.archive_mount("leak"))
calls.clear()
sub.clear()
try:
    a.recreate_instance_containers("desk", svc, [], inst=REC)
    check("Platte fehlt: Start verweigert", False)
except a.DiskAbsent as e:
    check("Platte fehlt: Start verweigert, mit Platte im Satz", "roh" in str(e), str(e))
check("... und zwar VOR dem docker rm (sonst steht die Instanz ohne Container da)",
      not any(c[:2] == ["docker", "rm"] for c in sub) and not calls, sub)
check("DiskAbsent ist ein DiagnoseRefused (jede Tuer antwortet in ihren Worten)",
      issubclass(a.DiskAbsent, a.DiagnoseRefused))

REC_OPT = {"id": "iid-3", "disk_grants": [{"grant": "rohdaten", "source": "leak", "optional": True}]}
a.load_registry = lambda: {"instances": {"desk": REC_OPT}}
calls.clear()
a.recreate_instance_containers("desk", svc, [], inst=REC_OPT)
check("optionale Freigabe fehlt: Instanz startet trotzdem, mit Basisordner",
      calls and any("iid-3" in x for x in calls[-1]))

REH = dict(REC, rehearsal={"of": "desk"})
a.load_registry = lambda: {"instances": {"desk-probe": REH}}
calls.clear()
a.recreate_instance_containers("desk-probe", svc, [], inst=REH)
check("Generalprobe: kein Basisordner, keine Freigabe (§4.9), auch bei fehlender Platte kein Abbruch",
      calls and "--mount" not in calls[-1])
check("disk_grants_of: ohne id keine Freigaben (der Basisordner haengt an der id)",
      a.disk_grants_of({"disk_grants": REC["disk_grants"]}) == ([], ""))

a.load_registry = lambda: {"instances": {"plain": {"id": "iid-4"}}}
calls.clear()
a.recreate_instance_containers("plain", svc, [], inst={"id": "iid-4"})
check("Instanz ohne Freigaben: nichts Neues im docker run", "--mount" not in calls[-1])

src = open(os.path.join(HERE, "..", "platform", "appctl.py"), encoding="utf-8").read()
check("Redeploy behaelt disk_grants, eine Generalprobe nie",
      'inst.get("disk_grants") and not rehearsal' in src)
check("wake_instance fragt die Startregel (docker start umgeht recreate)",
      "def wake_instance" in src and src.index("disk_absent_sentence(name, gone)",
                                                src.index("def wake_instance")) > 0)

print("ALL PASS" if not FAILS else f"{len(FAILS)} FAIL")
sys.exit(1 if FAILS else 0)
