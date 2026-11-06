#!/usr/bin/env python3
"""Store catalogue rules (RFC-0012 §1.2, §3, §6).

What the store page does with several sources, with a vocabulary value
it has never seen, with an image URL pointing at a stranger's server,
and what it filters by default. These are RFC decisions, so they are
tested apart from the rendering — `store_view.py` carries no Flask.

Run: python3 test/test_store_view.py
"""
import io
import os
import re
import sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services", "portal"))

import store_view as sv  # noqa: E402

TODAY = date(2026, 8, 9)
fails = 0


def ok(label, cond, detail=""):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"      {detail}")


def source(sid, trust, name=None):
    return {"id": sid, "trust": trust, "name": name or sid,
            "url": f"https://example.invalid/{sid}/oaap-store.json",
            "origin": ""}


def app_entry(app_id, **over):
    e = {"id": app_id, "name": app_id.title(), "version": "1.0.0",
         "package": {"git": "https://example.invalid/pkg"}}
    e.update(over)
    return e


def build(pairs, installed=None, pending=(), profiles=()):
    return sv.merge_catalogue(pairs, installed or {}, set(pending),
                              list(profiles), today=TODAY)


PLATFORM = source("oaap.platform", "platform", "Plattform-Apps")
COMMUNITY = source("oaap.community", "verified", "Community-Liste")
FOREIGN = source("fremd", "unverified", "Fremde Liste")

print("=== one catalogue, resolved like the host (§3) ===")
cat = build([
    (FOREIGN, {"apps": [app_entry("studio", name="Studio (Übernahme)",
                                  version="9.9.9"),
                        app_entry("nur-fremd", name="Nur fremd")]}),
    (COMMUNITY, {"apps": [app_entry("studio", version="0.0.9"),
                          app_entry("uptime-kuma", name="Uptime Kuma")]}),
    (PLATFORM, {"apps": [app_entry("studio", name="OAAP Studio")]}),
])
by_id = {e["id"]: e for e in cat}
ok("one entry per app id", len(cat) == 3, str(sorted(by_id)))
ok("the platform list wins over a foreign list configured first",
   by_id["studio"]["source"]["id"] == "oaap.platform",
   by_id["studio"]["source"]["id"])
ok("the losing lists are named, not silently dropped",
   sorted(by_id["studio"]["also_in"]) == ["Community-Liste", "Fremde Liste"],
   str(by_id["studio"]["also_in"]))
ok("an app only a foreign list has still appears",
   by_id["nur-fremd"]["source"]["id"] == "fremd")
ok("sorted by name", [e["id"] for e in cat] == sorted(by_id, key=lambda i: by_id[i]["name"].lower()))
ok("an entry without a git package is skipped",
   not build([(PLATFORM, {"apps": [{"id": "kaputt", "name": "Kaputt"}]})]))
ok("a source that could not be read simply is not there",
   len(build([(PLATFORM, None), (COMMUNITY, {"apps": [app_entry("a")]})])) == 1)

print("\n=== unknown values are shown, never a reason to refuse (§1.2) ===")
e = build([(PLATFORM, {"apps": [app_entry(
    "neu-app", categories=["monitoring", "quantenphysik"],
    audience=["everyone"], maturity="stable", status="active")]})])[0]
ok("a known category gets its German label",
   "Überwachung" in e["category_labels"])
ok("an unknown category survives verbatim",
   "quantenphysik" in e["category_labels"], str(e["category_labels"]))
opts = sv.options([e], "categories", sv.CATEGORY_LABEL)
ok("and lands under Sonstiges in the filter",
   opts[-1] == {"value": "quantenphysik", "label": "quantenphysik (Sonstiges)"},
   str(opts))
ok("known values come first in the filter",
   opts[0]["value"] == "monitoring")

print("\n=== images: only from the list's own repository (§1.1) ===")
e = build([(PLATFORM, {"apps": [app_entry(
    "bild", icon="icons/x.svg",
    screenshots=[{"src": "shots/a.png", "caption": "A"},
                 {"src": "https://tracker.invalid/pixel.png"},
                 {"src": "../../etc/passwd"},
                 {"src": "/absolut.png"}])]})])[0]
ok("a relative icon resolves against the list URL",
   e["icon"] == "https://example.invalid/oaap.platform/icons/x.svg", e["icon"])
ok("only the relative screenshot survives",
   [s["src"] for s in e["screenshots"]]
   == ["https://example.invalid/oaap.platform/shots/a.png"],
   str(e["screenshots"]))
ok("a foreign image URL is dropped", sv.list_relative("https://a/b.json",
                                                      "https://x.invalid/i.png") == "")
ok("a path escape is dropped", sv.list_relative("https://a/b.json", "../x.png") == "")

print("\n=== 'neu' is computed, never stored (§1.1) ===")
def released(d):
    return build([(PLATFORM, {"apps": [app_entry("x", released=d)]})])[0]
ok("a fresh release is new", released("2026-08-04")["is_new"])
ok("an old release is not", not released("2026-01-01")["is_new"])
ok("no date, no badge", not released("")["is_new"])
ok("an unreadable date is not a reason to refuse the entry",
   released("gestern")["is_new"] is False and released("gestern")["id"] == "x")

print("\n=== 0.1 entries still render (§1) ===")
e = build([(COMMUNITY, {"apps": [{
    "id": "alt", "name": "Alte App", "version": "1.0.0", "type": "wrapped",
    "description": "Beschreibung aus 0.1",
    "package": {"git": "https://example.invalid/pkg"},
    "license": "MIT", "homepage": "https://example.invalid/alt"}]})])[0]
ok("description stands in for a missing summary",
   e["summary"] == "Beschreibung aus 0.1")
ok("homepage becomes a typed link",
   e["links"] == [{"rel": "homepage", "url": "https://example.invalid/alt",
                   "label": "Website"}], str(e["links"]))
ok("status defaults to active", e["status"] == "active")
ok("an entry without ref is marked unpinned", not e["pinned"])

print("\n=== profiles warn, they never refuse (RFC-0011) ===")
cat = build([(PLATFORM, {"apps": [app_entry("studio", profiles=["dev"]),
                                  app_entry("normal")]})], profiles=[])
by_id = {e["id"]: e for e in cat}
ok("an app wanting 'dev' does not fit a plain node",
   not by_id["studio"]["profile_fit"])
ok("an app wanting nothing fits everywhere", by_id["normal"]["profile_fit"])
ok("it is filtered by default", not sv.matches(by_id["studio"], {}))
ok("and one click brings it back",
   sv.matches(by_id["studio"], {"all_profiles": True}))
ok("on a dev node it fits",
   build([(PLATFORM, {"apps": [app_entry("s", profiles=["dev"])]})],
         profiles=["dev"])[0]["profile_fit"])

print("\n=== the three filter defaults (§6) ===")
cat = build([(PLATFORM, {"apps": [
    app_entry("aktiv", status="active"),
    app_entry("alt", status="archived"),
    app_entry("veraltet", status="deprecated"),
    app_entry("nur-profis", audience=["expert"])]})])
shown = [e["id"] for e in cat if sv.matches(e, {})]
ok("archived is filtered out by default", "alt" not in shown, str(shown))
ok("deprecated stays visible", "veraltet" in shown)
ok("expert is NOT filtered out — marked, not hidden", "nur-profis" in shown)
ok("expert is marked",
   [e for e in cat if e["id"] == "nur-profis"][0]["expert"])
ok("archived comes back with one click",
   "alt" in [e["id"] for e in cat if sv.matches(e, {"all_status": True})])

print("\n=== search and filters (§6) ===")
cat = build([
    (PLATFORM, {"apps": [app_entry("studio", name="OAAP Studio",
                                   summary="Briefings erzeugen",
                                   tags=["ki", "vorhaben"],
                                   categories=["development"],
                                   app_class="frontend", maturity="beta",
                                   audience=["developer"], license="MIT")]}),
    (COMMUNITY, {"apps": [app_entry("ollama", name="Ollama",
                                    summary="Sprachmodelle als Schnittstelle",
                                    tags=["llm"], categories=["ai"],
                                    app_class="service", maturity="stable",
                                    audience=["operator"], license="MIT")]}),
], installed={"ollama": "0.9.0"})


def find(**f):
    return sorted(e["id"] for e in cat if sv.matches(e, f))


ok("search hits the hashtag", find(q="vorhaben") == ["studio"])
ok("search hits the summary", find(q="sprachmodelle") == ["ollama"])
ok("search hits the name", find(q="oaap") == ["studio"])
ok("search misses cleanly", find(q="gibtsnicht") == [])
ok("filter by category", find(categories="ai") == ["ollama"])
ok("filter by application class", find(app_class="service") == ["ollama"])
ok("filter by audience", find(audience="developer") == ["studio"])
ok("filter by maturity", find(maturity="beta") == ["studio"])
ok("filter by trust class", find(trust="platform") == ["studio"])
ok("filter by source", find(source="oaap.community") == ["ollama"])
ok("filter by licence keeps both", find(license="MIT") == ["ollama", "studio"])
ok("filter installed", find(installed="yes") == ["ollama"])
ok("filter not installed", find(installed="no") == ["studio"])
ok("filters combine", find(categories="ai", installed="no") == [])
ok("installed version is shown",
   [e for e in cat if e["id"] == "ollama"][0]["installed"] == "0.9.0")

print("\n=== the CLI line the object page offers ===")
e = build([(PLATFORM, {"apps": [app_entry("x", package={
    "git": "https://example.invalid/repo", "path": "apps/x",
    "ref": "v1.2.3"})]})])[0]
ok("a pinned entry says so", e["pinned"] and e["ref"] == "v1.2.3")
ok("the command carries path and ref",
   e["command"] == "sudo oaap app install https://example.invalid/repo "
                   "--path apps/x --ref v1.2.3", e["command"])

print("\n=== the two profile tables must not drift apart ===")
# Der Fund vom 2026-08-24 auf oaapx01: Der Knoten trug `exposed`, das
# Portal zeigte "keine Profile", und LiveKit blieb im Store gefiltert —
# weil `node_profiles()` alles verwirft, was nicht in PROFILE_LABELS
# steht, und dort nur `dev` stand. Ein Profil, das die CLI kennt und das
# Portal nicht, ist damit kein Schönheitsfehler, sondern macht Apps
# unsichtbar. Also wird der Abgleich hier geprüft, nicht der Disziplin
# überlassen: gelesen wird aus beiden Dateien, ohne sie zu importieren
# (das Portal braucht Flask, appctl braucht root).
def table_keys(path, marker):
    text = io.open(path, encoding="utf-8").read()
    body = text[text.index(marker):]
    body = body[:body.index("\n}")]
    return set(re.findall(r'^\s{4}"([a-z0-9_-]+)":', body, re.M))


cli = table_keys(os.path.join(HERE, "..", "platform", "appctl.py"), "PROFILES = {")
portal = table_keys(os.path.join(HERE, "..", "platform", "services", "portal", "app.py"),
                    "PROFILE_LABELS = {")
ok("the CLI knows at least dev and exposed", {"dev", "exposed"} <= cli, sorted(cli))
ok("the portal labels every profile the CLI knows", cli <= portal,
   f"missing in the portal: {sorted(cli - portal)}")
ok("and invents none of its own", portal <= cli,
   f"unknown to the CLI: {sorted(portal - cli)}")


print("=== care at a glance: update and advisories (store filter, instance list) ===")
ok("a newer catalogue version is an update", sv.update_to("0.2.1", "0.5.3") == "0.5.3")
ok("numbers compare as numbers (0.1.10 > 0.1.9)", sv.update_to("0.1.9", "0.1.10") == "0.1.10")
ok("an older catalogue entry is no update", sv.update_to("0.4.4", "0.4.3") == "")
ok("the same version is no update", sv.update_to("1.0.0", "1.0.0") == "")
ok("a pre-release falls back to 'differs'", sv.update_to("1.0.0", "1.1.0-rc1") == "1.1.0-rc1")
ok("nothing in the catalogue, nothing offered", sv.update_to("1.0.0", "") == "")

IMAGES = {"instances": {
    "ai-gateway": {"state": "open", "services": [{"state": "open", "open": 4},
                                                 {"state": "open", "open": 2}]},
    "fleetview": {"state": "fresh", "services": [{"state": "fresh", "open": 0}]},
    "ollama": {"state": "behind", "services": [{"state": "behind", "open": 0}]},
}}
INSTS = {
    "ai-gateway": {"app_id": "ai-gateway", "version": "0.2.1"},
    "fleetview": {"app_id": "fleetview", "version": "0.3.4"},
    "ollama": {"app_id": "ollama", "version": "0.1.0"},
    "ai-gateway-test": {"app_id": "ai-gateway", "version": "0.5.3"},
}
CAT = {"ai-gateway": {"version": "0.5.3", "source_id": "oaap.platform"},
       "fleetview": {"version": "0.3.4", "source_id": "oaap.platform"}}
c = {k: sv.instance_care(k, i, CAT, IMAGES) for k, i in INSTS.items()}
ok("old instance: update and the advisories of all services summed",
   c["ai-gateway"]["newer"] == "0.5.3" and c["ai-gateway"]["open"] == 6
   and c["ai-gateway"]["update_source"] == "oaap.platform", str(c["ai-gateway"]))
ok("up to date and fresh: nothing to say",
   not c["fleetview"]["attention"] and not c["fleetview"]["behind"], str(c["fleetview"]))
ok("a moved base is said, but is no attention",
   c["ollama"]["behind"] and not c["ollama"]["attention"], str(c["ollama"]))
ok("an instance the image reading does not know has 0 open",
   c["ai-gateway-test"]["open"] == 0 and not c["ai-gateway-test"]["newer"])
ok("an app not in the catalogue offers no update", c["ollama"]["newer"] == "")

entries = build([(PLATFORM, {"apps": [app_entry("ai-gateway", version="0.5.3"),
                                      app_entry("fleetview", version="0.3.4"),
                                      app_entry("raci", version="0.1.2",
                                                status="archived")]})])
sv.annotate_care(entries, INSTS, IMAGES)
e = {x["id"]: x for x in entries}
ok("store card: one of two instances has an update",
   e["ai-gateway"]["update_count"] == 1 and e["ai-gateway"]["open_count"] == 6
   and e["ai-gateway"]["open_instances"] == 1, str(e["ai-gateway"]))
ok("store card: an app without instances counts nothing",
   e["raci"]["update_count"] == 0 and e["raci"]["open_count"] == 0)
ok("filter 'update' keeps only the app with an update",
   [x["id"] for x in entries if sv.matches(x, {"care": "update"})] == ["ai-gateway"])
ok("filter 'security' keeps only the app with advisories",
   [x["id"] for x in entries if sv.matches(x, {"care": "security"})] == ["ai-gateway"])
ok("no care filter: the defaults stay (archived hidden)",
   [x["id"] for x in entries if sv.matches(x, {})] == ["ai-gateway", "fleetview"])
bare = {k: v for k, v in e["ai-gateway"].items() if k != "update_count"}
ok("a care filter on an unannotated entry keeps nothing",
   not sv.matches(bare, {"care": "attention"}))


def row(key, care, state="ok", channel="production", tenant="", app="App"):
    return {"key": key, "name": key, "app_name": app, "tenant": tenant,
            "status": {"state": state}, "channel": channel, "care": care}


rows = [row("ai-gateway", c["ai-gateway"], tenant="pxx", app="KI-Gateway"),
        row("fleetview", c["fleetview"], channel="test"),
        row("ollama", c["ollama"], state="down")]


def pick(f):
    return [r["key"] for r in rows if sv.instance_matches(r, f)]


ok("instance list: no filter shows all", pick({}) == ["ai-gateway", "fleetview", "ollama"])
ok("instance list: 'attention'", pick({"care": "attention"}) == ["ai-gateway"])
ok("instance list: search reaches the app name", pick({"q": "ki-gat"}) == ["ai-gateway"])
ok("instance list: status, channel, tenant",
   pick({"state": "down"}) == ["ollama"] and pick({"channel": "test"}) == ["fleetview"]
   and pick({"tenant": "pxx"}) == ["ai-gateway"])

print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAILURES'}")
sys.exit(1 if fails else 0)
