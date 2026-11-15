"""The tenant's asset universe in its own twin (RFC-0088 §3) -- pure.

The fleet writes what apps name of themselves into the HUB's twin, as
pseudonyms (RFC-0087 §8.2, fleet_model `FleetAsset`). This is the second
reader of the same door: the node writes it into the twin of the
instance's OWN tenant, origin `platform:assets`, with the fields only the
tenant may see (keys by label, their end and budget, cohorts, sign-ins,
tools, agents). One source for three readers, as fleet_model: the host
registers DATA_MODEL per tenant (`oaap assets`), assets_run writes the
groups, the twin's `/internal/twin/assets` reads them for the portal page.

Keys follow the twin's rules; every attribute and relation key carries the
prefix `assets` (ak()), because attribute and relation keys are node-wide.
"""

ORIGIN = "platform:assets"
PACKAGE = "oaap-assets"
VERSION = "0.1.0"

APP, ITEM = "AssetApp", "AssetItem"

# what a door may name (RFC-0088 §2); stage 1 writes every kind it is given
KINDS = ("source", "model", "key", "cohort", "signin", "tool", "agent")
STATES = ("ok", "down", "idle")
REMOVED = "removed"


def _own(path):
    return "oaap-aas:" + path


# (key, value_type, semantic id, title)
_ATTRS = [
    # oaap.assetapp -- the instance that names the assets
    ("instance", "text", _own("TenantAssets/0/1/App/Instance"), "Instanz"),
    ("app", "text", _own("TenantAssets/0/1/App/App"), "App"),
    ("appState", "text", _own("TenantAssets/0/1/App/State"), "Zustand der Tür"),
    # oaap.assetitem -- one asset
    ("name", "text", _own("TenantAssets/0/1/Item/Name"), "Name"),
    ("kind", "text", _own("TenantAssets/0/1/Item/Kind"), "Art"),
    ("state", "text", _own("TenantAssets/0/1/Item/State"), "Zustand"),
    ("light", "text", _own("TenantAssets/0/1/Item/Light"), "Ampel"),
    ("checked", "datetime", _own("TenantAssets/0/1/Item/Checked"), "Zuletzt geprüft"),
    ("checkedOk", "bool", _own("TenantAssets/0/1/Item/CheckedOk"), "Prüfung erfolgreich"),
    ("modelCount", "int", _own("TenantAssets/0/1/Item/ModelCount"), "Modelle bei der Quelle"),
    ("upstream", "text", _own("TenantAssets/0/1/Item/Upstream"), "Kennungen bei der Quelle"),
    ("destination", "text", _own("TenantAssets/0/1/Item/Destination"), "Destination"),
    ("expires", "datetime", _own("TenantAssets/0/1/Item/Expires"), "Endet"),
    ("budget", "decimal", _own("TenantAssets/0/1/Item/Budget"), "Budget"),
    ("used", "decimal", _own("TenantAssets/0/1/Item/Used"), "Verbraucht"),
    ("seats", "int", _own("TenantAssets/0/1/Item/Seats"), "Plätze"),
    ("owner", "text", _own("TenantAssets/0/1/Item/Owner"), "Verantwortlich"),
]

GROUPS = [
    # (key, on, semantic id, attributes, relations, title)
    ("oaap.assetapp", APP, _own("TenantAssets/0/1/App"), ["instance", "app", "appState"], [],
     "App"),
    ("oaap.assetitem", ITEM, _own("TenantAssets/0/1/Item"),
     ["name", "kind", "state", "light", "checked", "checkedOk", "modelCount", "upstream",
      "destination", "expires", "budget", "used", "seats", "owner"],
     ["assetOf", "uses", "mayUse", "toolOf", "opens", "agentUses"], "Asset"),
]

RELATIONS = [
    # (key, from, to, semantic id, title, title_inverse)
    ("assetOf", ITEM, APP, _own("TenantAssets/0/1/Rel/AssetOf"), "gehört zu", "nennt"),
    ("uses", ITEM, ITEM, _own("TenantAssets/0/1/Rel/Uses"), "nutzt", "wird genutzt von"),
    ("mayUse", ITEM, ITEM, _own("TenantAssets/0/1/Rel/MayUse"), "darf nutzen",
     "darf genutzt werden von"),
    ("toolOf", ITEM, ITEM, _own("TenantAssets/0/1/Rel/ToolOf"), "Werkzeug von", "bietet"),
    ("opens", ITEM, ITEM, _own("TenantAssets/0/1/Rel/Opens"), "öffnet", "geöffnet durch"),
    ("agentUses", ITEM, ITEM, _own("TenantAssets/0/1/Rel/AgentUses"), "arbeitet mit",
     "wird genutzt von Agent"),
]

OBJECTS = [
    (APP, "App mit Assets", "Apps mit Assets", _own("TenantAssets/0/1/App")),
    (ITEM, "Asset", "Assets", _own("TenantAssets/0/1/Item")),
]

# The text series of an asset's state over time (RFC-0086), one per source
# and model name: "when was the gaming PC unreachable last week".
TEXT_STATE = "state"
SERIES_KINDS = ("source", "model")


def ak(name):
    """`state` -> `assetsState`: node-wide keys, so a generic name gets the
    prefix of its origin (the fleet's are `fleet…`)."""
    return "assets" + name[0].upper() + name[1:]


def attrs(values):
    """{short name: value} -> {registered key: value}, None left out."""
    return {ak(k): v for k, v in values.items() if v is not None}


def data_model():
    """The `data_model` section (oaap.data.model §2.2) of the tenant reader."""
    known = {a[0] for a in _ATTRS}
    return {
        "object_types": [{"key": k, "title": t, "title_plural": tp, "semantic_id": s}
                         for k, t, tp, s in OBJECTS],
        "attribute_types": [{"key": ak(k), "title": t, "value_type": vt, "semantic_id": s}
                            for k, vt, s, t in _ATTRS],
        "group_types": [{"key": g, "on": on, "title": t, "semantic_id": s,
                         "attributes": [ak(a) for a in at if a in known],
                         "relations": [ak(r) for r in rel]}
                        for g, on, s, at, rel, t in GROUPS],
        "relation_types": [{"key": ak(k), "from": f, "to": to, "semantic_id": s,
                            "title": t, "title_inverse": ti}
                           for k, f, to, s, t, ti in RELATIONS],
    }
