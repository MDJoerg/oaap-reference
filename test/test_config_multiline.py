#!/usr/bin/env python3
"""A config value with a newline (found on oaap-test 2026-11-15, 0.1.289).

`oaap app config set <inst> AIGW_SUPPLIERS "a=...<newline>b=..."` wrote the
value into instance.env as it was; the next `docker run` failed and the
instance had no container any more. instance.env keeps one line per key:
a multiline key is stored `;`-separated (as the portal's textarea stores
it and as cohorts already did), a single-line key refuses a newline.

Run: python3 test/test_config_multiline.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["OAAP_DATA_DIR"] = tempfile.mkdtemp(prefix="oaap-config-ml-")
sys.path.insert(0, os.path.join(HERE, "..", "platform"))

import appctl as m                                            # noqa: E402

fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {str(detail)[:600]}")


V = m.config_env_value
ok("mehrzeilig: Zeilen werden mit ; verbunden, leere und CR fallen weg",
   V("K", "a=1\r\n\n  b=2  \n", True) == "a=1;b=2", V("K", "a=1\r\n\n  b=2  \n", True))
ok("mehrzeilig, schon in der gespeicherten Form (Portal): bleibt, wie es ist",
   V("K", "a=1;b=2", True) == "a=1;b=2")
try:
    V("K", "a=1\nb=2;c=3", True)
    ok("mehrzeilig mit ; in einer Zeile: abgelehnt", False)
except ValueError as e:
    ok("mehrzeilig mit ; in einer Zeile: abgelehnt", "may not contain ';'" in str(e), e)
try:
    V("K", "eins\nzwei", False)
    ok("einzeilig mit Zeilenumbruch: abgelehnt", False)
except ValueError as e:
    ok("einzeilig mit Zeilenumbruch: abgelehnt", "takes one line" in str(e), e)
ok("einzeilig ohne Umbruch: unverändert (auch mit ;)", V("K", "x;y", False) == "x;y")

# a file as `config set` left it on oaap-test: the value's second line became
# keys of their own ('fern', 'chat-fern ' with a space -- docker refuses that)
damaged = os.path.join(os.environ["OAAP_DATA_DIR"], "instance.env")
with open(damaged, "w", encoding="utf-8") as f:
    f.write("AIGW_SUPPLIERS=gaming=http://g/v1 light=green\nfern=https://f/v1\n"
            "AIGW_ALIASES=chat-gaming = gaming:m\nchat-fern = fern:x\nAIGW_TIMEOUT_SECONDS=600\n")
real_env_path = m.env_path
m.env_path = lambda name, inst=None: damaged
env = m.load_env("gw")
ok("load_env: eine Zeile ohne gültigen Namen wird nicht gelesen ('chat-fern ')",
   "chat-fern " not in env and "chat-fern" not in env and env["AIGW_TIMEOUT_SECONDS"] == "600", env)
m.config_view_write = lambda: None
m.save_env("gw", dict(env, **{"kaputt name": "x", "ZWEI": "a\nb"}))
written = open(damaged, encoding="utf-8").read()
ok("save_env: schreibt nur Zeilen, die docker --env-file annimmt",
   "chat-fern" not in written and "kaputt name" not in written and "ZWEI" not in written
   and all(m.ENV_NAME_RE.match(l.split("=", 1)[0]) for l in written.splitlines()), written)
m.env_path = real_env_path

calls = []
m.recreate_instance_containers = lambda *a, **k: calls.append(a)
m.instance_services = lambda inst: []
m.config_entries = lambda name, inst: [{"key": "AIGW_SUPPLIERS", "multiline": True},
                                        {"key": "AIGW_TIMEOUT_SECONDS", "multiline": False}]
store = {}
m.load_env = lambda name: dict(store)
m.save_env = lambda name, env, *a: store.update(env)
msg = m.apply_config("gw", {}, {"AIGW_SUPPLIERS": "gaming=http://g/v1 light=green\nfern=https://f/v1"})
ok("apply_config: der mehrzeilige Wert landet als eine Zeile",
   store.get("AIGW_SUPPLIERS") == "gaming=http://g/v1 light=green;fern=https://f/v1"
   and "\n" not in store["AIGW_SUPPLIERS"] and calls, (msg, store))
calls.clear()
ok("apply_config: dieselbe Angabe noch einmal (einmal mit Umbruch, einmal mit ;) ändert nichts",
   m.apply_config("gw", {}, {"AIGW_SUPPLIERS": "gaming=http://g/v1 light=green;fern=https://f/v1"})
   == "no change" and not calls)
try:
    m.apply_config("gw", {}, {"AIGW_TIMEOUT_SECONDS": "600\n700"})
    ok("apply_config: ein einzeiliger Schlüssel mit Umbruch wird abgelehnt, ehe ein Container stirbt",
       False)
except ValueError:
    ok("apply_config: ein einzeiliger Schlüssel mit Umbruch wird abgelehnt, ehe ein Container stirbt",
       not calls and "AIGW_TIMEOUT_SECONDS" not in store)

print("")
print("ALLE GRUEN" if not fails else f"{fails} FEHLER")
sys.exit(1 if fails else 0)
