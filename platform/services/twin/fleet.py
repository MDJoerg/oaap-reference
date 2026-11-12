"""The fleet service on the hub (RFC-0085 §8, stage 2).

The same image as `twin` and `relay`, run by the compose service `fleet`
(node profile `fleet-hub`). It logs in to the hub's broker as the platform
principal `oaap.watch` (reads, never writes), keeps a persistent session
(QoS 1: what arrives while it restarts waits in the broker) and writes what
it sees into /fleet/state.json -- nothing else. Judging (silent, bridge
down, queue) and mailing are the host's minute job (appctl
`fleet_conditions`, services/fleet_watch.py): only the host sends mail
(RFC-0080), and a judge that reads a file can be tested without a broker.

What it reads:

    <root>/<n>/alive|status|broker|events/<type>      a node that reaches the hub
    oaap-via/<entry>/<n>/...                           the same, through an entry
    <root>/<hub>/bridge/<entry>                        a bridge's notification (stage 1)
    oaap-fleet/bridge/<entry>/<hub>                    the same, where configured so

state.json:

    {"v": 1, "started", "written", "connected", "messages",
     "nodes":   {"<via>/<n>" | "<n>": {"name", "via", "alive", "rx", "gaps",
                                       "resets", "status": {"digest", "rx", "bytes"}}},
     "bridges": {"<entry>@<hub>": {"entry", "hub", "up", "since", "rx"}},
     "brokers": {"<n>": {...the node's broker counters, "rx", "via"}},
     "events":  [the last 200: {"rx", "node", "via", "type", "op", "kurz", "text"}]}

The status documents themselves go to /fleet/status/<key>.json.
"""
import hashlib
import json
import os
import signal
import threading
import time

import paho.mqtt.client as mqtt
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties

BROKER_HOST = os.environ.get("BROKER_HOST", "broker")
BROKER_PORT = int(os.environ.get("BROKER_PORT", "1883"))
WATCH_USER = "oaap.watch"                      # = mqtt_acl.WATCH_USER
WATCH_KEY = os.environ.get("BROKER_WATCH_KEY", "")
ROOT = (os.environ.get("OAAP_METRICS_ROOT") or "").strip() or "oaap-node"
VIA = "oaap-via"                               # = mqtt_acl.VIA_ROOT
FLEET = "oaap-fleet"                           # = mqtt_acl.FLEET_ROOT
DIR = os.environ.get("FLEET_DIR", "/fleet")
STATE = os.path.join(DIR, "state.json")
CLIENT_ID = "oaap-fleet-hub"
EVENTS_KEEP = 200
WRITE_EVERY = 5            # seconds, when something changed
HEARTBEAT = 30             # seconds: state.json is rewritten at least this often
SESSION_EXPIRY = 7 * 86400
SUBSCRIPTIONS = [f"{ROOT}/+/alive", f"{ROOT}/+/status", f"{ROOT}/+/broker",
                 f"{ROOT}/+/events/#", f"{ROOT}/+/bridge/+",
                 f"{VIA}/+/+/alive", f"{VIA}/+/+/status", f"{VIA}/+/+/broker",
                 f"{VIA}/+/+/events/#", f"{FLEET}/bridge/#"]

lock = threading.Lock()
state = {"v": 1, "started": int(time.time()), "written": 0, "connected": False,
         "messages": 0, "bad": 0, "nodes": {}, "bridges": {}, "brokers": {}, "events": []}
dirty = True


def load_previous():
    """Keep what the last run knew -- the bridges' `since`, the nodes'
    sequence numbers -- across a restart of this service."""
    try:
        with open(STATE, encoding="utf-8") as f:
            old = json.load(f)
    except (OSError, ValueError):
        return
    for k in ("nodes", "bridges", "brokers", "events"):
        if isinstance(old.get(k), type(state[k])):
            state[k] = old[k]
    # a bridge record without its entry and hub is of an older build
    state["bridges"] = {k: b for k, b in state["bridges"].items()
                        if isinstance(b, dict) and b.get("entry") and b.get("hub")}


def write_state():
    global dirty
    with lock:
        state["written"] = int(time.time())
        data = json.dumps(state, ensure_ascii=False)
        dirty = False
    os.makedirs(DIR, exist_ok=True)
    tmp = STATE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(data)
    os.replace(tmp, STATE)


def _bridge(entry, hub, value, now):
    """`hub` is whose bridge it is: the topic names it. Kept, so that the
    judge counts only this hub's own bridges -- a hub that is ALSO an entry
    of another hub (a test bench, measured on oaap-test) sees that other
    hub's notification too."""
    up = value.strip() == "1"
    key = f"{entry}@{hub}"
    b = state["bridges"].get(key)
    if b is None or bool(b.get("up")) != up:
        state["bridges"][key] = {"entry": entry, "hub": hub, "up": up, "since": now, "rx": now}
    else:
        b["rx"] = now


def _alive(key, name, via, payload, now):
    n = state["nodes"].setdefault(key, {"name": name, "via": via, "gaps": 0, "resets": 0})
    n["name"], n["via"] = name, via
    prev = n.get("alive") or {}
    seq, pseq = payload.get("seq"), prev.get("seq")
    if isinstance(seq, int) and isinstance(pseq, int):
        if seq == pseq and payload.get("t") == prev.get("t"):
            return                  # the retained one again (resubscribe, redelivery)
        if seq > pseq + 1:
            n["gaps"] = int(n.get("gaps", 0)) + seq - pseq - 1
        elif seq <= pseq:
            n["resets"] = int(n.get("resets", 0)) + 1   # the node's state began anew
    n["alive"] = {k: payload.get(k) for k in ("t", "seq", "version", "interval", "lost",
                                              "events_dropped")}
    n["rx"] = now


def handle(topic, raw, now):
    """One message -> state. Returns False for a message it cannot place."""
    p = topic.split("/")
    if p[0] == FLEET and len(p) == 4 and p[1] == "bridge":
        _bridge(p[2], p[3], raw.decode("utf-8", "replace"), now)
        return True
    if p[0] == ROOT and len(p) >= 3:
        via, name, rest = "", p[1], p[2:]
        if rest[0] == "bridge" and len(rest) == 2:          # the hub's own notification
            _bridge(rest[1], name, raw.decode("utf-8", "replace"), now)
            return True
    elif p[0] == VIA and len(p) >= 4:
        via, name, rest = p[1], p[2], p[3:]
    else:
        return False
    key = f"{via}/{name}" if via else name
    cls = rest[0]
    if cls == "events":
        try:
            ev = json.loads(raw)
        except ValueError:
            return False
        state["events"].append({"rx": now, "node": name, "via": via,
                                "type": "/".join(rest[1:])[:60],
                                **{k: str(ev.get(k, ""))[:300] for k in ("op", "kurz", "text",
                                                                          "tenant", "instanz")
                                   if ev.get(k)}})
        del state["events"][:-EVENTS_KEEP]
        return True
    try:
        payload = json.loads(raw)
    except ValueError:
        return False
    if not isinstance(payload, (dict, list)):
        return False
    if cls == "alive" and isinstance(payload, dict):
        _alive(key, name, via, payload, now)
        return True
    if cls == "broker" and isinstance(payload, dict):
        state["brokers"][name] = dict(payload, rx=now, via=via)
        return True
    if cls == "status":
        digest = hashlib.sha256(raw).hexdigest()[:16]
        n = state["nodes"].setdefault(key, {"name": name, "via": via, "gaps": 0, "resets": 0})
        n["status"] = {"digest": digest, "rx": now, "bytes": len(raw)}
        os.makedirs(os.path.join(DIR, "status"), exist_ok=True)
        path = os.path.join(DIR, "status", key.replace("/", "__") + ".json")
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(raw)
        os.replace(tmp, path)
        return True
    return False


def on_connect(client, _userdata, _flags, reason_code, _properties):
    with lock:
        state["connected"] = not reason_code.is_failure
    if reason_code.is_failure:
        print(f"fleet: the broker refused the login ({reason_code})", flush=True)
        return
    client.subscribe([(t, 1) for t in SUBSCRIPTIONS])
    print(f"fleet: connected as {WATCH_USER}, {len(SUBSCRIPTIONS)} subscriptions", flush=True)


def on_disconnect(_client, _userdata, _flags, reason_code, _properties):
    global dirty
    with lock:
        state["connected"] = False
        dirty = True
    print(f"fleet: disconnected ({reason_code})", flush=True)


def on_message(_client, _userdata, msg):
    global dirty
    now = int(time.time())
    with lock:
        state["messages"] += 1
        try:
            placed = handle(msg.topic, msg.payload, now)
        except Exception as exc:  # noqa: BLE001 -- one bad message must not stop the fleet
            placed = False
            print(f"fleet: {msg.topic}: {type(exc).__name__}: {exc}", flush=True)
        if not placed:
            state["bad"] += 1
        dirty = True


def main():
    if not WATCH_KEY:
        # fail visibly, never silently: the host's minute job sees a stale
        # state.json and keeps open matters open (fleet_watch.trusted)
        print("fleet: no BROKER_WATCH_KEY -- 'sudo oaap update' makes one", flush=True)
        while True:
            time.sleep(3600)
    load_previous()
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=CLIENT_ID,
                         protocol=mqtt.MQTTv5)
    client.username_pw_set(WATCH_USER, WATCH_KEY)
    client.on_connect, client.on_disconnect, client.on_message = on_connect, on_disconnect, on_message
    client.reconnect_delay_set(1, 30)
    props = Properties(PacketTypes.CONNECT)
    props.SessionExpiryInterval = SESSION_EXPIRY
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    while not stop.is_set():
        try:
            client.connect(BROKER_HOST, BROKER_PORT, keepalive=30, clean_start=False,
                           properties=props)
            break
        except OSError as exc:
            print(f"fleet: broker not reachable ({exc}), retrying", flush=True)
            stop.wait(5)
    client.loop_start()
    last = 0
    while not stop.wait(1):
        now = time.time()
        if (dirty and now - last >= WRITE_EVERY) or now - last >= HEARTBEAT:
            try:
                write_state()
                last = now
            except OSError as exc:
                print(f"fleet: state not written ({exc})", flush=True)
    client.loop_stop()
    client.disconnect()
    write_state()


if __name__ == "__main__":
    main()
