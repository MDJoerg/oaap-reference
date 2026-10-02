"""The broker's certificate (RFC-0054 stage 3): where it comes from and how
it reaches the broker.

Host side, like `metrics_sender.py`: it takes the directories it works in
and the clock, and the acting (the minutely job, the command) is `cmd_broker`
in appctl.py. It shells out to `openssl`, which every OAAP node has.

Two sources, one result -- `server.crt` and `server.key` in a directory
only the broker's user can read:

  caddy   the certificate the gateway already obtained and renews for the
          node's external host name (the one place certificates are
          managed). Used whenever Caddy holds one.
  local   a certificate signed by a CA of this node's own, made here the
          first time and renewed before it runs out. For nodes with no
          public name, where the gateway has nothing to give (measured:
          the platform CA of RFC-0005 is not built, the gateway does only
          on-demand ACME). A client is given the CA certificate once.

A COPY, not a mount: Caddy keeps its files root-only (mode 0600) and
Mosquitto runs as uid 1000 and refuses to start on a key it cannot read
(measured on oaap-test, 2026-10-02). A renewed copy is picked up by the
running broker on SIGHUP, without a restart (same measurement); the very
first copy needs a restart, because a listener cannot be added by a
reload.
"""
import calendar
import glob
import ipaddress
import json
import os
import shutil
import subprocess
import time

BROKER_UID = 1000                       # `mosquitto` in the broker image
RENEW_DAYS = 30
LOCAL_DAYS = 397                        # a year and a month; renewed at 30 left
CA_DAYS = 3650
CERT = "server.crt"
KEY = "server.key"
STATE = "source.json"


class CertError(Exception):
    pass


def _openssl(*args, check=True):
    try:
        r = subprocess.run(["openssl", *args], capture_output=True, text=True)
    except OSError as exc:
        raise CertError(f"openssl is not available ({exc})") from None
    if check and r.returncode != 0:
        raise CertError("openssl " + args[0] + ": "
                        + (r.stderr or r.stdout).strip().splitlines()[-1]
                        if (r.stderr or r.stdout).strip() else "failed")
    return r


def days_left(crt_path, now=None):
    """Whole days until the certificate ends; negative when it has."""
    r = _openssl("x509", "-noout", "-enddate", "-in", crt_path)
    end = r.stdout.strip().partition("=")[2]
    t = calendar.timegm(time.strptime(end, "%b %d %H:%M:%S %Y %Z"))
    return int((t - (time.time() if now is None else now)) // 86400)


def names_of(crt_path):
    """The subject alternative names of a certificate, as a sorted list."""
    r = _openssl("x509", "-noout", "-ext", "subjectAltName", "-in", crt_path)
    out = []
    for part in r.stdout.replace("\n", ",").split(","):
        part = part.strip()
        if part.startswith("DNS:"):
            out.append(part[4:])
        elif part.startswith("IP Address:"):
            out.append(part[11:])
    return sorted(out)


def caddy_cert_for(caddy_data, host):
    """(crt, key) Caddy holds for `host`, or None. Caddy stores them as
    caddy/certificates/<issuer>/<host>/<host>.crt and .key."""
    if not host:
        return None
    for crt in sorted(glob.glob(os.path.join(
            caddy_data, "caddy", "certificates", "*", host, host + ".crt"))):
        key = crt[:-4] + ".key"
        if os.path.isfile(key):
            return crt, key
    return None


def local_names(hostname, external, address):
    """The names the local certificate must carry. `broker` is the
    platform-network name an in-platform client (the relay, a probe)
    reaches the broker by."""
    names = ["broker"]
    for n in (hostname, external):
        if n and n not in names:
            names.append(n)
    ips = []
    if address:
        try:
            ipaddress.ip_address(address)
            ips.append(address)
        except ValueError:
            pass
    return names, ips


def ensure_ca(ca_dir):
    """The node's own CA, made once. Returns the CA certificate's path."""
    crt, key = os.path.join(ca_dir, "ca.crt"), os.path.join(ca_dir, "ca.key")
    if os.path.isfile(crt) and os.path.isfile(key):
        return crt
    os.makedirs(ca_dir, exist_ok=True)
    os.chmod(ca_dir, 0o700)
    fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.close(fd)
    _openssl("req", "-x509", "-newkey", "rsa:3072", "-nodes", "-keyout", key,
             "-out", crt, "-days", str(CA_DAYS), "-subj", "/CN=OAAP node CA",
             "-addext", "basicConstraints=critical,CA:TRUE",
             "-addext", "keyUsage=critical,keyCertSign,cRLSign")
    os.chmod(key, 0o600)
    return crt


def issue_local(ca_dir, names, ips, out_crt, out_key):
    """A server certificate signed by the node's CA."""
    ca_crt = ensure_ca(ca_dir)
    ca_key = os.path.join(ca_dir, "ca.key")
    tmp = os.path.join(ca_dir, "issue")
    os.makedirs(tmp, exist_ok=True)
    csr, ext = os.path.join(tmp, "s.csr"), os.path.join(tmp, "s.ext")
    san = ",".join([f"DNS:{n}" for n in names] + [f"IP:{i}" for i in ips])
    with open(ext, "w", encoding="utf-8") as f:
        f.write(f"subjectAltName={san}\nbasicConstraints=CA:FALSE\n"
                "keyUsage=digitalSignature,keyEncipherment\n"
                "extendedKeyUsage=serverAuth\n")
    _openssl("req", "-newkey", "rsa:2048", "-nodes", "-keyout", out_key,
             "-out", csr, "-subj", f"/CN={names[0]}")
    _openssl("x509", "-req", "-in", csr, "-CA", ca_crt, "-CAkey", ca_key,
             "-CAcreateserial", "-days", str(LOCAL_DAYS), "-extfile", ext,
             "-out", out_crt)
    for f in (csr, ext):
        try:
            os.remove(f)
        except OSError:
            pass


def _install(src_crt, src_key, cert_dir):
    """Put the pair where the broker reads it: atomically, mode 0600 for
    the key, owned by the broker's user where we are allowed to."""
    os.makedirs(cert_dir, exist_ok=True)
    for src, name, mode in ((src_key, KEY, 0o600), (src_crt, CERT, 0o644)):
        tmp = os.path.join(cert_dir, name + ".tmp")
        shutil.copyfile(src, tmp)
        os.chmod(tmp, mode)
        try:
            os.chown(tmp, BROKER_UID, BROKER_UID)
        except (AttributeError, PermissionError):
            pass                        # not root (a test): the mode is enough there
        os.replace(tmp, os.path.join(cert_dir, name))


def _state(cert_dir):
    try:
        with open(os.path.join(cert_dir, STATE), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(cert_dir, d):
    with open(os.path.join(cert_dir, STATE), "w", encoding="utf-8") as f:
        json.dump(d, f)


def sync(cert_dir, ca_dir, caddy_data, hostname, external, address, now=None):
    """Make the broker's certificate current. Returns
    {"action": "none"|"unchanged"|"installed"|"renewed"|"switched",
     "source": "caddy"|"local", "first": bool, "days": n, "names": [...]}.

    `first` says no certificate was in place before: the broker has to be
    RESTARTED to get its TLS listener. Anything else is a SIGHUP.
    """
    prev = _state(cert_dir)
    have = (os.path.isfile(os.path.join(cert_dir, CERT))
            and os.path.isfile(os.path.join(cert_dir, KEY)))
    held = caddy_cert_for(caddy_data, external)
    if held:
        crt, key = held
        names = names_of(crt)
        marker = {"source": "caddy", "from": crt,
                  "mtime": int(os.path.getmtime(crt))}
        if have and prev.get("source") == "caddy" and prev.get("from") == crt \
                and prev.get("mtime") == marker["mtime"]:
            return {"action": "unchanged", "source": "caddy", "first": False,
                    "days": days_left(os.path.join(cert_dir, CERT), now),
                    "names": names}
        _install(crt, key, cert_dir)
        _save_state(cert_dir, marker)
        action = ("installed" if not have else
                  "switched" if prev.get("source") != "caddy" else "renewed")
        return {"action": action, "source": "caddy", "first": not have,
                "days": days_left(os.path.join(cert_dir, CERT), now),
                "names": names}
    names, ips = local_names(hostname, external, address)
    want = sorted(names + ips)
    if have and prev.get("source") == "local" and prev.get("names") == want \
            and days_left(os.path.join(cert_dir, CERT), now) > RENEW_DAYS:
        return {"action": "unchanged", "source": "local", "first": False,
                "days": days_left(os.path.join(cert_dir, CERT), now),
                "names": want}
    os.makedirs(ca_dir, exist_ok=True)
    tmp = os.path.join(ca_dir, "new")
    os.makedirs(tmp, exist_ok=True)
    n_crt, n_key = os.path.join(tmp, CERT), os.path.join(tmp, KEY)
    fd = os.open(n_key, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.close(fd)
    issue_local(ca_dir, names, ips, n_crt, n_key)
    _install(n_crt, n_key, cert_dir)
    for f in (n_crt, n_key):
        try:
            os.remove(f)
        except OSError:
            pass
    _save_state(cert_dir, {"source": "local", "names": want})
    action = ("installed" if not have else
              "switched" if prev.get("source") != "local" else "renewed")
    return {"action": action, "source": "local", "first": not have,
            "days": days_left(os.path.join(cert_dir, CERT), now), "names": want}
