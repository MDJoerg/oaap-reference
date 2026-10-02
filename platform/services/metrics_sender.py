"""The metrics sender: forwards the node's outbound queue to an MQTT broker.

RFC-0052 stage 1. Host side only -- like `metrics.py` it takes the
directories it works in and the time it works at, and the acting (the
minutely job, the command) is `cmd_metrics` in appctl.py.

The whole contract in four lines:

  * the queue is read oldest first and a sample is deleted from it only
    after the broker has acknowledged every message of it;
  * delivery is at least once -- a crash between the acknowledgement and
    the deletion sends the sample again;
  * a run never raises: a broker that is down is a counted, shown delay
    (`sender-state.json`), with a backoff, never a failed unit;
  * the secret is never in any text this module returns.

The client speaks MQTT 5 and nothing but what it needs: CONNECT, PUBLISH
at QoS 1, PUBACK, DISCONNECT. MQTT 5 and not 3.1.1 for one reason: in
3.1.1 a publish the broker's ACL denies is acknowledged and dropped, so
the node would delete data nobody received. MQTT 5 puts the verdict into
the PUBACK's reason code. No library: the host has none and should not
need one.
"""
import ipaddress
import json
import os
import re
import socket
import ssl
import struct
import time

import metrics

DEFAULT_ROOT = "oaap-node"
CONNECT_TIMEOUT = 5
RUN_BUDGET = 20
KEEPALIVE = 30
BACKOFF_STEP = 60
BACKOFF_MAX = 15 * 60
ACK_EVERY = 200             # entries; the queue file is rewritten by an ack
STATE_FILE = metrics.SENDER_STATE_FILE
CA_FILE = "ca.crt"
INFO_FILE = metrics.SENDER_INFO_FILE
CONFIG_FILE = "sender.json"
SECRET_FILE = "sender.secret"

_NODE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,38}[a-z0-9]$")
_LEVEL_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class ConfigError(Exception):
    """The operator's input or the stored configuration is not usable."""


class SenderError(Exception):
    """A network, TLS or protocol failure; worth a retry with backoff."""
    kind = "network"


class AuthRefused(SenderError):
    """The broker said no: wrong login, or not allowed to publish there.
    Retrying soon changes nothing and a wrong password hammered at a
    broker gets a node banned -- so this waits the long time."""
    kind = "auth"


# --- the operator's input ---------------------------------------------

def normalise_node(name):
    n = re.sub(r"[^a-z0-9-]+", "-", (name or "").lower()).strip("-")
    if not _NODE_RE.match(n):
        raise ConfigError("node name: 2-40 characters, lower-case letters, "
                          "digits and '-', starting and ending with a letter "
                          "or digit")
    return n


def validate_root(root):
    if not root or len(root) > 64 or root.startswith("/") or root.endswith("/"):
        raise ConfigError("topic root: 1-64 characters, no leading or "
                          "trailing '/'")
    for level in root.split("/"):
        if not _LEVEL_RE.match(level):
            raise ConfigError("topic root: each level starts with a "
                              "lower-case letter or digit and holds only "
                              "lower-case letters, digits, '.', '_', '-' "
                              "(no wildcard, nothing starting with '$')")
    return root


def parse_url(url):
    """(scheme, host, port) of an mqtt:// or mqtts:// URL."""
    m = re.match(r"^(mqtts?)://([^/:@\s]+)(?::(\d{1,5}))?/?$", url or "")
    if not m:
        raise ConfigError("url: mqtts://host[:port] (or mqtt://host[:port] "
                          "with --allow-plain)")
    scheme, host = m.group(1), m.group(2)
    port = int(m.group(3)) if m.group(3) else (8883 if scheme == "mqtts" else 1883)
    if not 0 < port < 65536:
        raise ConfigError("url: port out of range")
    return scheme, host, port


def is_private_host(host):
    """True when every address `host` names is private or loopback."""
    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    addrs = {i[4][0] for i in infos}
    return bool(addrs) and all(ipaddress.ip_address(a.split("%")[0]).is_private
                               for a in addrs)


def topic(cfg, series):
    return f"{cfg['root']}/{cfg['node']}/metrics/{series}"


# --- the stored configuration -----------------------------------------

def _path(directory, name):
    return os.path.join(directory, name)


def load_config(cfg_dir):
    try:
        with open(_path(cfg_dir, CONFIG_FILE), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def load_secret(cfg_dir):
    try:
        with open(_path(cfg_dir, SECRET_FILE), encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def _write_private(path, text):
    """Create `path` with mode 0600 FROM THE START (never world-readable
    for even a moment), replacing any earlier file."""
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def configure(cfg_dir, url, user, secret, node=None, root=None, ca=None,
              allow_plain=False, default_node=None):
    """Validate and store. The secret goes into its own file."""
    scheme, host, port = parse_url(url)
    if scheme == "mqtt":
        if not allow_plain:
            raise ConfigError("mqtt:// sends the password in the clear; "
                              "use mqtts://, or --allow-plain for a "
                              "private address")
        if not is_private_host(host):
            raise ConfigError("--allow-plain is only accepted for a private "
                              "address; this one is not (or does not resolve)")
    if not user or not re.match(r"^[\x21-\x7e]{1,64}$", user):
        raise ConfigError("user: 1-64 printable characters, no spaces")
    if not secret:
        raise ConfigError("the secret is empty (it is read from standard "
                          "input)")
    cfg = {"url": f"{scheme}://{host}:{port}", "user": user,
           "node": normalise_node(node or default_node or "node"),
           "root": validate_root(root or DEFAULT_ROOT),
           "allow_plain": bool(allow_plain)}
    if ca and not os.path.isfile(ca):
        raise ConfigError(f"--ca: no such file: {ca}")
    os.makedirs(cfg_dir, exist_ok=True)
    try:
        os.chmod(cfg_dir, 0o700)
    except OSError:
        pass
    if ca:
        # a COPY: the file the operator named may live in /tmp, which a
        # reboot empties (measured on a Raspberry Pi, RFC-0054 stage 5)
        keep = _path(cfg_dir, CA_FILE)
        if not (os.path.exists(keep) and os.path.samefile(ca, keep)):
            with open(ca, "rb") as src, open(keep, "wb") as dst:
                dst.write(src.read())
        cfg["ca"] = keep
    _write_private(_path(cfg_dir, SECRET_FILE), secret + "\n")
    _write_private(_path(cfg_dir, CONFIG_FILE), json.dumps(cfg, indent=2) + "\n")
    return cfg


def write_info(metrics_dir, cfg):
    """The one non-secret fact the health page needs: that a sender exists,
    and where it sends. The portal mounts `metrics_dir` read-only; the
    configuration and the secret are not there."""
    scheme, host, port = parse_url(cfg["url"])
    os.makedirs(metrics_dir, exist_ok=True)
    tmp = _path(metrics_dir, INFO_FILE + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"host": host, "port": port, "node": cfg["node"],
                   "root": cfg["root"]}, f)
    os.replace(tmp, _path(metrics_dir, INFO_FILE))


def reset_state(metrics_dir):
    """A new target starts clean: the failures and the wait of the OLD one
    say nothing about it (measured: a corrected key waited out the old
    back-off)."""
    try:
        os.remove(_path(metrics_dir, STATE_FILE))
    except OSError:
        pass


def remove_config(cfg_dir, metrics_dir):
    """Delete configuration, secret and state. The queue stays."""
    gone = False
    for d, name in ((cfg_dir, CONFIG_FILE), (cfg_dir, SECRET_FILE),
                    (metrics_dir, STATE_FILE), (metrics_dir, INFO_FILE)):
        try:
            os.remove(_path(d, name))
            gone = True
        except OSError:
            pass
    return gone


# --- the run state ----------------------------------------------------

def load_state(metrics_dir):
    try:
        with open(_path(metrics_dir, STATE_FILE), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _save_state(metrics_dir, st):
    os.makedirs(metrics_dir, exist_ok=True)
    tmp = _path(metrics_dir, STATE_FILE + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f)
    os.replace(tmp, _path(metrics_dir, STATE_FILE))


def backoff_seconds(fails, kind):
    if kind in ("auth", "config"):
        return BACKOFF_MAX
    return min(BACKOFF_STEP * 2 ** max(0, fails - 1), BACKOFF_MAX)


# --- the wire (MQTT 5) ------------------------------------------------

def _varlen(n):
    out = bytearray()
    while True:
        b, n = n % 128, n // 128
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _utf8(s):
    b = s.encode("utf-8")
    return struct.pack(">H", len(b)) + b


def connect_packet(client_id, user, secret, keepalive=KEEPALIVE):
    body = (_utf8("MQTT") + b"\x05" + b"\xc2" + struct.pack(">H", keepalive)
            + b"\x00"                       # no properties
            + _utf8(client_id) + _utf8(user) + _utf8(secret))
    return b"\x10" + _varlen(len(body)) + body


def publish_packet(topic_name, payload, packet_id, retain=True):
    body = (_utf8(topic_name) + struct.pack(">H", packet_id) + b"\x00"
            + payload)
    return bytes([0x32 | (1 if retain else 0)]) + _varlen(len(body)) + body


DISCONNECT = b"\xe0\x00"


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise SenderError("the broker closed the connection")
        buf += chunk
    return buf


def read_packet(sock):
    """(type, flags, body) of the next packet."""
    first = _recv_exact(sock, 1)[0]
    mult, length = 1, 0
    for _ in range(4):
        b = _recv_exact(sock, 1)[0]
        length += (b & 127) * mult
        if not b & 128:
            break
        mult *= 128
    else:
        raise SenderError("malformed packet from the broker")
    return first >> 4, first & 15, _recv_exact(sock, length) if length else b""


class Client:
    """One connection. `deadline` is a time.monotonic() value for the
    whole run; every wait is cut to what is left of it."""

    def __init__(self, cfg, deadline):
        self.cfg = cfg
        self.deadline = deadline
        self.sock = None
        self.pid = 0

    def _timeout(self, cap):
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise SenderError("the time budget of this run is used up")
        return max(0.5, min(cap, left))

    def connect(self, secret):
        scheme, host, port = parse_url(self.cfg["url"])
        try:
            sock = socket.create_connection(
                (host, port), timeout=self._timeout(CONNECT_TIMEOUT))
        except OSError as exc:
            raise SenderError(f"cannot connect to {host}:{port} "
                              f"({type(exc).__name__})") from None
        if scheme == "mqtts":
            try:
                ctx = ssl.create_default_context(cafile=self.cfg.get("ca"))
                sock = ctx.wrap_socket(sock, server_hostname=host)
            except (ssl.SSLError, OSError) as exc:
                sock.close()
                raise SenderError(f"TLS to {host}:{port} failed, nothing "
                                  f"was sent ({type(exc).__name__})") from None
        self.sock = sock
        try:
            sock.settimeout(self._timeout(CONNECT_TIMEOUT))
            sock.sendall(connect_packet("oaap-metrics-" + self.cfg["node"],
                                        self.cfg["user"], secret))
            ptype, _flags, body = read_packet(sock)
        except (OSError, struct.error) as exc:
            self.close()
            raise SenderError(f"no answer to CONNECT ({type(exc).__name__})") \
                from None
        if ptype != 2 or len(body) < 2:
            self.close()
            raise SenderError("the broker did not answer CONNECT with CONNACK")
        rc = body[1]
        if rc == 0:
            return
        self.close()
        if rc in (0x86, 0x87, 0x8a):        # bad login, not authorised, banned
            raise AuthRefused(f"the broker refused the login (reason 0x{rc:02x})")
        if rc == 0x84:
            raise SenderError("the broker does not speak MQTT 5")
        raise SenderError(f"the broker refused CONNECT (reason 0x{rc:02x})")

    def publish(self, topic_name, payload):
        self.pid = self.pid % 65535 + 1
        try:
            self.sock.settimeout(self._timeout(CONNECT_TIMEOUT))
            self.sock.sendall(publish_packet(topic_name, payload, self.pid))
            while True:
                ptype, _flags, body = read_packet(self.sock)
                if ptype == 4 and len(body) >= 2 and \
                        struct.unpack(">H", body[:2])[0] == self.pid:
                    break               # (anything else is not ours: skip it)
        except (OSError, struct.error) as exc:
            raise SenderError(f"no acknowledgement ({type(exc).__name__})") \
                from None
        rc = body[2] if len(body) > 2 else 0
        if rc >= 0x80:
            if rc in (0x87, 0x86):
                raise AuthRefused("the broker refused this publish: the "
                                  "account may not write to "
                                  f"{topic_name} (reason 0x{rc:02x})")
            raise SenderError(f"the broker refused a publish (reason 0x{rc:02x})")

    def close(self):
        if self.sock is not None:
            try:
                self.sock.sendall(DISCONNECT)
            except OSError:
                pass
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None


# --- one run ----------------------------------------------------------

def _clean(text, secret):
    return text.replace(secret, "***") if secret else text


def _fail(metrics_dir, st, now, exc, secret):
    kind = getattr(exc, "kind", "config" if isinstance(exc, ConfigError)
                   else "network")
    fails = int(st.get("fails", 0)) + 1
    st.update({"fails": fails, "kind": kind, "err": _clean(str(exc), secret),
               "err_at": now,
               "next_try": now + backoff_seconds(fails, kind)})
    _save_state(metrics_dir, st)
    return {"status": "error", "kind": kind, "error": st["err"], "sent": 0}


def run(metrics_dir, cfg_dir, now=None, client_factory=Client):
    """Send what is waiting. Never raises; returns what happened:
    {"status": "unconfigured"|"backoff"|"idle"|"ok"|"error", "sent": n}."""
    now = int(time.time() if now is None else now)
    cfg = load_config(cfg_dir)
    if not cfg:
        return {"status": "unconfigured", "sent": 0}
    if not os.path.exists(_path(metrics_dir, INFO_FILE)):
        try:
            write_info(metrics_dir, cfg)        # a hand-made configuration
        except (OSError, ConfigError):
            pass
    st = load_state(metrics_dir)
    if st.get("next_try", 0) > now:
        return {"status": "backoff", "sent": 0}
    secret = load_secret(cfg_dir)
    sent = 0
    done = None
    client = None
    try:
        if not secret:
            raise ConfigError("the secret file is missing or empty")
        scheme, host, _port = parse_url(cfg["url"])
        if scheme == "mqtt" and not (cfg.get("allow_plain")
                                     and is_private_host(host)):
            raise ConfigError("a plain mqtt:// target is not allowed here")
        if not metrics.queue_pending(metrics_dir, 1):
            return {"status": "idle", "sent": 0}
        client = client_factory(cfg, time.monotonic() + RUN_BUDGET)
        client.connect(secret)
        for e in metrics.queue_pending(metrics_dir):
            for line in metrics.sample_lines(cfg["node"], e):
                client.publish(topic(cfg, line["m"]),
                               json.dumps(line, separators=(",", ":"))
                               .encode("utf-8"))
            done = e["q"]
            sent += 1
            if sent % ACK_EVERY == 0:
                metrics.queue_ack(metrics_dir, done)
        client.close()
        client = None
    except SenderError as exc:
        if "budget" not in str(exc) or not sent:
            if done:
                metrics.queue_ack(metrics_dir, done)
            return dict(_fail(metrics_dir, st, now, exc, secret), sent=sent)
        # the budget ran out between entries: that is not a failure
    except Exception as exc:                # noqa: BLE001 -- must never raise
        if done:
            metrics.queue_ack(metrics_dir, done)
        return dict(_fail(metrics_dir, st, now, exc, secret), sent=sent)
    finally:
        if client is not None:
            client.close()
    if done:
        metrics.queue_ack(metrics_dir, done)
    st.update({"fails": 0, "kind": "", "err": "", "next_try": 0,
               "ok_at": now, "sent_total": int(st.get("sent_total", 0)) + sent})
    _save_state(metrics_dir, st)
    return {"status": "ok", "sent": sent}


def test(cfg_dir):
    """Connect, publish nothing, say why or why not: (ok, sentence)."""
    cfg = load_config(cfg_dir)
    if not cfg:
        return False, "no sender configured"
    secret = load_secret(cfg_dir)
    if not secret:
        return False, "the secret file is missing or empty"
    client = Client(cfg, time.monotonic() + RUN_BUDGET)
    try:
        client.connect(secret)
    except SenderError as exc:
        return False, _clean(str(exc), secret)
    finally:
        client.close()
    return True, f"connected to {cfg['url']} as {cfg['user']}; nothing was published"
