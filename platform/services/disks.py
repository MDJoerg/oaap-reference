"""Disks and archives as node resources (RFC-0063, oaap.data.disks 0.1).

Three nouns: a DISK (one device, known by its UUID, role `raw` or
`work`), a SOURCE (a path on a disk, or an archive on a disk shown as a
read-only folder by ratarmount) and a GRANT (a source handed to an
instance under a name, `ro` or `rw`).

Everything that touches the host goes through `sh()`, so the tests can
replace it and count what would have run. The rules that the
measurements of 2026-10-22 produced live in functions, not in branches,
because a rule in a function can be executed by a test (CURRENT_STATE
347, M1-M13):

- a disk is found by UUID, never by /dev/sdX (M1);
- the kernel write lock sticks to the device NUMBER, so a raw disk gets
  it again at every open (M2b);
- an app never receives a disk or a FUSE mount directly -- a direct bind
  keeps the old ratarmount alive and the disk cannot be unmounted (M5b);
  it receives one grant base per instance, bound `rslave` (M4, M9);
- the grant base is immutable on the system disk and a read-only tmpfs at
  runtime, so an empty placeholder cannot be written, not even by root in
  the container (M12);
- a read-only grant is mounted read-only in ONE call: bind first and
  remount read-only later shows `ro` on the host while the running
  container writes (M13). The flag is then checked from INSIDE the
  container, because the host's word for it is exactly what M13 showed
  to be wrong.
"""
import json
import os
import re
import shutil
import subprocess
import time

ROOT = os.environ.get("OAAP_DISKS_ROOT", "/srv/oaap")
KEY_DIR = os.environ.get("OAAP_DISKS_KEYS", "/etc/oaap/disks")
CONTAINER_BASE = "/oaap/disks"          # D3: one fixed base in the container
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
ROLES = ("raw", "work")
# Filesystems a work disk may carry: it must keep owners and modes, or a
# container user cannot be given a folder of its own (exFAT has neither).
POSIX_FS = ("ext4", "xfs", "btrfs")
# Without owners on disk (exFAT, FAT, NTFS): ownership and modes are mount
# options, the same for the whole disk.
FOREIGN_FS = ("exfat", "vfat", "ntfs3", "ntfs")
# Measured through ratarmount 1.3.0 (M11). 7z failed, rar was not measured:
# refused until a backend is measured, not tried and hoped for.
ARCHIVE_FORMATS = ("tar", "gzip", "bzip2", "xz", "zstd", "zip")
_MAGIC = ((b"\x1f\x8b", "gzip"), (b"BZh", "bzip2"), (b"\xfd7zXZ", "xz"),
          (b"\x28\xb5\x2f\xfd", "zstd"), (b"PK\x03\x04", "zip"),
          (b"7z\xbc\xaf", "7z"), (b"Rar!", "rar"))


class DiskRefused(Exception):
    """A disk operation that must not happen, with the reason."""


# ---------------------------------------------------------------- host

def sh(cmd, check=True):
    """Run one host command. Replaced by the tests."""
    return subprocess.run(cmd, check=check, text=True, capture_output=True)


def ismount(path):
    return os.path.ismount(path)


# ------------------------------------------------------------ registry

def load(path):
    try:
        with open(path, encoding="utf-8") as f:
            reg = json.load(f)
    except FileNotFoundError:
        reg = {}
    reg.setdefault("disks", {})
    reg.setdefault("sources", {})
    return reg


def save(path, reg):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(reg, f, indent=2, ensure_ascii=False)
    # root only: a source's path is a file name on the disk, and a name may
    # be content (D7). The portal reads apps/ -- it gets a view without
    # names (stage 2), never this file.
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def check_name(kind, name):
    if not NAME_RE.match(name or ""):
        raise DiskRefused(f"{kind} name '{name}': lower-case letters, digits "
                          "and '-', starting with a letter, at most 40.")
    return name


# --------------------------------------------------------------- paths

def disk_mount(name):
    return os.path.join(ROOT, "disks", name)


def archive_mount(source):
    return os.path.join(ROOT, "archives", source)


def grant_base(iid):
    """Keyed by the instance ID, which never changes (RFC-0026): a rename
    must not move a mount."""
    return os.path.join(ROOT, "grants", iid)


def mapper_name(name):
    return f"oaap-disk-{name}"


def key_path(name):
    return os.path.join(KEY_DIR, f"{name}.key")


def archive_unit(source):
    return f"oaap-archive@{source}.service"


# ----------------------------------------------------------- inventory

def _usb_speed(kname):
    """Mbit/s of the USB link a block device hangs on, or None."""
    p = os.path.realpath(f"/sys/block/{kname}/device")
    while p not in ("/", "") and not os.path.isfile(os.path.join(p, "speed")):
        p = os.path.dirname(p)
    try:
        return int(open(os.path.join(p, "speed")).read().strip())
    except (OSError, ValueError):
        return None


def devices():
    """Every partition (or unpartitioned disk) with a filesystem or LUKS,
    flattened, each carrying its parent disk's transport, model and USB
    speed, and whether it belongs to the disk the system runs from."""
    out = sh(["lsblk", "-J", "-b", "-o",
              "NAME,KNAME,PATH,SIZE,TYPE,FSTYPE,LABEL,UUID,MOUNTPOINTS,TRAN,"
              "MODEL,RO"]).stdout
    tree = json.loads(out).get("blockdevices") or []
    found = []

    def mounts_of(node):
        ms = [m for m in (node.get("mountpoints") or []) if m]
        for c in node.get("children") or []:
            ms += mounts_of(c)
        return ms

    for disk in tree:
        # a loop device is a disk as far as this is concerned: an image
        # file a person attached on purpose (and what the tests use)
        if disk.get("type") not in ("disk", "loop"):
            continue
        system = any(m in ("/", "/boot", "/boot/efi", "[SWAP]", "/var",
                           "/var/lib/oaap", "/var/lib/docker")
                     for m in mounts_of(disk))
        speed = _usb_speed(disk.get("kname") or disk.get("name"))
        # partitions if there are any; else the device itself -- also when
        # its only child is an opened LUKS mapping (whole-device LUKS),
        # which is not a partition and must not hide the device
        parts = [c for c in disk.get("children") or [] if c.get("type") == "part"] or [disk]
        for p in parts:
            if p.get("type") not in ("part", "disk", "loop") or not p.get("fstype"):
                continue
            inner = [c for c in (p.get("children") or []) if c.get("type") == "crypt"]
            found.append({
                "path": p.get("path"), "disk": disk.get("path"),
                "size": int(p.get("size") or 0), "fstype": p.get("fstype"),
                "label": p.get("label") or "", "uuid": p.get("uuid") or "",
                "mountpoints": [m for m in (p.get("mountpoints") or []) if m]
                               + [m for c in inner for m in (c.get("mountpoints") or []) if m],
                "tran": disk.get("tran") or "", "model": (disk.get("model") or "").strip(),
                "usb_speed": speed, "system": system,
                "loop": disk.get("type") == "loop",
                "inner_fstype": (inner[0].get("fstype") if inner else None),
            })
    return found


def find_device(devs, uuid):
    for d in devs:
        if d["uuid"] and d["uuid"].lower() == (uuid or "").lower():
            return d
    return None


def state(rec, devs):
    """`absent`, `present` or `open` -- what the page and `list` show."""
    if not find_device(devs, rec["uuid"]):
        return "absent"
    return "open" if ismount(disk_mount(rec["name"])) else "present"


# --------------------------------------------------------- mount rules

def mount_options(role, fstype):
    """The mount options for a disk of this role and filesystem (§3.1).
    Raises DiskRefused for a work disk without owners and modes."""
    opts = ["noatime", "nodev", "nosuid"]
    if role == "raw":
        opts = ["ro", "noexec"] + opts
        if fstype == "ext4":
            # `ro` alone still replays the journal -- a write. A raw disk
            # is never written, not even by its own driver.
            opts.append("noload")
        if fstype in FOREIGN_FS:
            # no owners on disk: readable for every container user,
            # writable for none (the mount is ro anyway)
            opts += ["uid=0", "gid=0", "fmask=0222", "dmask=0222"]
        return ",".join(opts)
    if fstype not in POSIX_FS:
        raise DiskRefused(
            f"a work disk needs a filesystem with owners and modes "
            f"({', '.join(POSIX_FS)}); this one is {fstype}. Format it "
            "(oaap disk format <disk> --luks) or adopt it as raw.")
    return ",".join(opts)


def grant_refusal(disk_rec, source_rec, rw):
    """Why this grant may not be made, or '' (§3.3)."""
    if not rw:
        return ""
    if source_rec["kind"] == "archive":
        return "an archive is always read-only."
    if disk_rec["role"] == "raw":
        return (f"'{disk_rec['name']}' is a raw disk: its sources are "
                "granted read-only, never --rw (RFC-0063 D5).")
    return ""


def detect_format(path):
    """Archive format from the header bytes. Never `file`: it prints the
    original name a gzip header carries, and a name may be content."""
    with open(path, "rb") as f:
        h = f.read(512)
    if h[257:262] == b"ustar":
        return "tar"
    for magic, kind in _MAGIC:
        if h.startswith(magic):
            return kind
    return "other"


def scan(root, min_size=1):
    """Every file below `root`, numbered in a fixed order (directories
    and files sorted), with size and format. Names are in the result for
    the caller to show or not (D7)."""
    out, n = [], 0
    for d, ds, fs in os.walk(root):
        ds.sort()
        for f in sorted(fs):
            p = os.path.join(d, f)
            try:
                size = os.path.getsize(p)
            except OSError:
                continue
            if size < min_size:
                continue
            n += 1
            try:
                kind = detect_format(p)
            except OSError:
                kind = "unreadable"
            out.append({"nr": n, "rel": os.path.relpath(p, root),
                        "size": size, "format": kind})
    return out


# ------------------------------------------------------- open / close

def open_disk(rec, devs):
    """Unlock (LUKS), lock (raw) and mount one adopted disk. Returns the
    mount point. Idempotent: an open disk is left as it is, except that a
    raw disk's write lock is set again (it may have been replugged)."""
    dev = find_device(devs, rec["uuid"])
    if not dev:
        raise DiskRefused(f"disk '{rec['name']}' is not plugged in "
                          f"(UUID {rec['uuid']}).")
    if dev["system"]:
        raise DiskRefused(f"'{rec['name']}' is on the system disk; refused.")
    raw = rec["role"] == "raw"
    if raw:
        for p in {dev["path"], dev["disk"]}:
            sh(["blockdev", "--setro", p])
    src, fstype = dev["path"], dev["fstype"]
    if rec.get("encryption") == "luks":
        mapper = "/dev/mapper/" + mapper_name(rec["name"])
        if not os.path.exists(mapper):
            cmd = ["cryptsetup", "open", "--key-file", rec["key"]]
            if raw:
                cmd.append("--readonly")
            sh(cmd + [dev["path"], mapper_name(rec["name"])])
        src = mapper
        fstype = sh(["blkid", "-o", "value", "-s", "TYPE", mapper]).stdout.strip()
    mnt = disk_mount(rec["name"])
    if not ismount(mnt):
        os.makedirs(mnt, exist_ok=True)
        sh(["mount", "-t", fstype, "-o", mount_options(rec["role"], fstype), src, mnt])
    if raw:
        opts = sh(["findmnt", "-no", "OPTIONS", mnt]).stdout.strip().split(",")
        if "ro" not in opts:
            sh(["umount", mnt], check=False)
            raise DiskRefused(f"'{rec['name']}' came up writable; unmounted.")
    rec["fstype_inner"] = fstype
    return mnt


def holders(path):
    """Who keeps a mount busy, as `fuser -vm` names them (best effort)."""
    r = sh(["fuser", "-vm", path], check=False)
    lines = [ln.strip() for ln in (r.stderr or "").splitlines()[1:] if ln.strip()]
    return lines


def close_disk(rec):
    """Unmount and lock one disk. Never lazy: a lazy unmount reports
    success while the disk is still in use (§4.4)."""
    mnt = disk_mount(rec["name"])
    if ismount(mnt):
        r = sh(["umount", mnt], check=False)
        if r.returncode != 0:
            who = holders(mnt)
            raise DiskRefused(f"'{rec['name']}' is busy"
                              + (": " + "; ".join(who) if who else "") + ".")
    mapper = "/dev/mapper/" + mapper_name(rec["name"])
    if rec.get("encryption") == "luks" and os.path.exists(mapper):
        sh(["cryptsetup", "close", mapper_name(rec["name"])])


# ------------------------------------------------------------ sources

def source_path(reg, name):
    """Where a source appears on the host."""
    s = reg["sources"][name]
    if s["kind"] == "archive":
        return archive_mount(name)
    return os.path.normpath(os.path.join(disk_mount(s["disk"]), s["path"].lstrip("/")))


def source_available(reg, name):
    s = reg["sources"].get(name)
    if not s:
        return False
    if s["kind"] == "archive":
        return ismount(archive_mount(name))
    return ismount(disk_mount(s["disk"])) and os.path.isdir(source_path(reg, name))


def index_file(reg, name):
    s = reg["sources"][name]
    if s.get("index_on"):
        return os.path.join(disk_mount(s["index_on"]), "index", f"{name}.index.sqlite")
    return os.path.join(ROOT, "archive-index", f"{name}.index.sqlite")


def ratarmount_bin():
    return shutil.which("ratarmount") or "/usr/local/bin/ratarmount"


def archive_argv(reg, name):
    """The ratarmount command of one archive source (foreground, for its
    systemd unit). `allow_other`, or a container user gets `Permission
    denied` (M3). The index goes where the source says -- never next to
    the archive, which is ratarmount's default and would write to a raw
    disk."""
    s = reg["sources"][name]
    arch = os.path.join(disk_mount(s["disk"]), s["path"].lstrip("/"))
    # `-d 0`: at its default level ratarmount writes the archive's path
    # into the journal when it builds an index ("Creating offset
    # dictionary for <path>") -- a file name, and a name may be content
    # (D7). Measured 2026-10-22: level 0 is silent; an error still names
    # the file, which is when it is needed.
    argv = [ratarmount_bin(), "-f", "-d", "0", "-o", "allow_other",
            "--index-file", index_file(reg, name)]
    if s.get("recursive"):
        argv.append("--recursive")
    return argv + [arch, archive_mount(name)]


def start_archive(reg, name, wait=60.0):
    """Start the archive's unit and wait for the mount. Raises DiskRefused
    with the unit's last log lines when it does not come up."""
    os.makedirs(archive_mount(name), exist_ok=True)
    idx = index_file(reg, name)
    os.makedirs(os.path.dirname(idx), mode=0o700, exist_ok=True)
    if ismount(archive_mount(name)):
        return
    sh(["systemctl", "start", "--no-block", archive_unit(name)])
    end = time.time() + wait
    while time.time() < end:
        if ismount(archive_mount(name)):
            return
        time.sleep(0.5)
    log = sh(["journalctl", "-u", archive_unit(name), "-n", "5", "--no-pager"],
             check=False).stdout
    raise DiskRefused(f"archive '{name}' did not come up within {wait:.0f} s:\n{log}")


def stop_archive(name):
    """Unmount first: ratarmount then ends by itself, cleanly. Stopped by
    SIGTERM it exits 1 and leaves the unit `failed` (measured 2026-10-22);
    a success code for 1 in the unit would also hide a real failure at
    start."""
    if ismount(archive_mount(name)):
        sh(["umount", archive_mount(name)], check=False)
    sh(["systemctl", "stop", archive_unit(name)], check=False)
    sh(["systemctl", "reset-failed", archive_unit(name)], check=False)


# ------------------------------------------------------------- grants

def grant_mount_args(iid):
    """The one mount every instance with grants gets (D3, M4/M9): its own
    base, `rslave`, so grants made and taken later reach the running
    container -- and nothing else of the host does."""
    return ["--mount", f"type=bind,src={grant_base(iid)},dst={CONTAINER_BASE},"
            "bind-propagation=rslave"]


def ensure_base(iid):
    """Create the grant base empty and immutable on the system disk (M12).
    Never under the data directory, so no archive can carry it (§4.1)."""
    base = grant_base(iid)
    if ismount(base):
        return base
    os.makedirs(base, exist_ok=True)
    if os.listdir(base):
        # something wrote into the bare folder before it was protected --
        # a protection that sits on top of data is not one
        raise DiskRefused(f"grant base {base} is not empty; refusing to seal it.")
    sh(["chattr", "+i", base])
    return base


def missing(reg, grants):
    """Mandatory grants whose source is not there: (grant, source, disk).
    The start rule (§4.3) refuses on any."""
    out = []
    for g in grants or []:
        if g.get("optional"):
            continue
        s = reg["sources"].get(g["source"])
        if not source_available(reg, g["source"]):
            out.append((g["grant"], g["source"], (s or {}).get("disk", "?")))
    return out


def parse_mountinfo(text):
    """{mount point: per-mount options} from /proc/<pid>/mountinfo."""
    out = {}
    for line in text.splitlines():
        f = line.split(" ")
        if len(f) > 5:
            out[f[4].replace("\\040", " ")] = f[5].split(",")
    return out


def _remount_base(base, mode):
    sh(["mount", "-o", f"remount,{mode}", base])


def open_grants(reg, iid, grants, pid=None):
    """Make the base show exactly `grants`, mounting each whose source is
    there. Returns [(grant, outcome)], outcome one of `mounted`, `kept`,
    `absent`, `revoked: <why>`. `pid` is the running container's process,
    for the check from inside (M13); None skips it (container not up)."""
    base = ensure_base(iid)
    if not ismount(base):
        sh(["mount", "-t", "tmpfs", "-o", "size=1m,mode=0755", "oaap-grants", base])
        sh(["mount", "--make-shared", base])
        _remount_base(base, "ro")
    wanted = {g["grant"]: g for g in grants or []}
    have = set(os.listdir(base))
    if have != set(wanted):
        _remount_base(base, "rw")
        try:
            for stale in have - set(wanted):
                p = os.path.join(base, stale)
                if ismount(p):
                    sh(["umount", p])
                os.rmdir(p)
            for new in set(wanted) - have:
                os.mkdir(os.path.join(base, new), 0o755)
        finally:
            _remount_base(base, "ro")
    report = []
    for name, g in sorted(wanted.items()):
        p = os.path.join(base, name)
        if ismount(p):
            report.append((name, "kept"))
            continue
        if not source_available(reg, g["source"]):
            report.append((name, "absent"))
            continue
        # M13: ONE call. Never bind and remount ro afterwards.
        opt = "bind" if g.get("rw") else "bind,ro"
        sh(["mount", "-o", opt, source_path(reg, g["source"]), p])
        report.append((name, "mounted"))
    if pid:
        report = verify_inside(base, wanted, pid, report)
    return report


def verify_inside(base, wanted, pid, report):
    """Compare each grant's flag as the CONTAINER sees it with the record;
    a mismatch revokes the grant (§4.2 step 4)."""
    try:
        seen = parse_mountinfo(open(f"/proc/{pid}/mountinfo").read())
    except OSError:
        return report
    out = []
    for name, outcome in report:
        if outcome not in ("mounted", "kept"):
            out.append((name, outcome))
            continue
        opts = seen.get(f"{CONTAINER_BASE}/{name}")
        want_ro = not wanted[name].get("rw")
        if opts is None:
            out.append((name, outcome + " (not seen in the container)"))
        elif ("ro" in opts) != want_ro:
            sh(["umount", os.path.join(base, name)], check=False)
            out.append((name, f"revoked: container saw {'ro' if 'ro' in opts else 'rw'}, "
                              f"record says {'ro' if want_ro else 'rw'}"))
        else:
            out.append((name, outcome))
    return out


def close_grants(iid, only=None):
    """Unmount grants in a base (all, or the names in `only`); the
    placeholders stay. Returns the names unmounted."""
    base = grant_base(iid)
    if not ismount(base):
        return []
    done = []
    for name in sorted(os.listdir(base)):
        if only is not None and name not in only:
            continue
        p = os.path.join(base, name)
        if ismount(p):
            r = sh(["umount", p], check=False)
            if r.returncode != 0:
                raise DiskRefused(f"grant '{name}' could not be unmounted: "
                                  + (r.stderr or "").strip())
            done.append(name)
    return done


def remove_base(iid):
    """Take an instance's base away entirely (removal of the instance)."""
    base = grant_base(iid)
    close_grants(iid)
    if ismount(base):
        sh(["umount", base], check=False)
    if os.path.isdir(base):
        sh(["chattr", "-i", base], check=False)
        try:
            os.rmdir(base)
        except OSError:
            pass
