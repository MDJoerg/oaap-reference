"""The fleet's types in the twin (RFC-0085 §9, amendment A2) -- pure.

One source for three readers: the host registers DATA_MODEL (origin
`platform:fleet`, `oaap fleet hub twin`), the fleet service writes the
groups and series named here (fleet.py), and the twin answers a type's
semantic ID in full (semantic.expand).

Groups are cut along Asset Administration Shell submodels
(program/aas-zuordnung-knotendaten.md): where a published IDTA template
fits, its semantic ID is written verbatim; where none does, ours is the
short form `oaap-aas:<Submodel>/<major>/<minor>[/<Element>]` -- never the
domain, so moving the domain is a setting, not a migration (Jörg,
2026-11-14).

Attribute and relation keys follow the twin's rule ([A-Za-z][A-Za-z0-9]*),
group keys `<namespace>.<name>` and unique across the three object types. Every
attribute and relation key is registered with the prefix `fleet` (ak()).
The fleet service writes the `oaap.*`
groups; `owner.*` are registered for people and never written by it.
"""

ORIGIN = "platform:fleet"
PACKAGE = "oaap-fleet"
VERSION = "0.2.0"     # 0.2.0: half-edges (RFC-0087)

NODE, BROKER, INSTANCE = "FleetNode", "FleetBroker", "FleetInstance"
# RFC-0087: the links between and behind nodes
TENANT, TUNNEL, DESTINATION, EXTERNAL = ("FleetTenant", "FleetTunnel", "FleetDestination",
                                         "FleetExternal")

IDTA_SOFTWARE = "https://admin-shell.io/idta/SoftwareNameplate/1/0"
IDTA_SW_INSTANCE = IDTA_SOFTWARE + "/SoftwareNameplate/SoftwareNameplateInstance/"
IDTA_IS_PART_OF = "https://admin-shell.io/idta/HierarchicalStructures/IsPartOf/1/0"
IDTA_CONTACT = "https://admin-shell.io/zvei/nameplate/1/0/ContactInformations"
IDTA_NAMEPLATE = "https://admin-shell.io/idta/nameplate/3/0/Nameplate"


def _own(path):
    return "oaap-aas:" + path


# (key, value_type, semantic id or "", title)
_ATTRS = [
    # oaap.software -- Software Nameplate (IDTA 02007), the platform/app as installed
    ("platformVersion", "text", IDTA_SW_INSTANCE + "InstalledVersion", "Plattform-Version"),
    ("os", "text", IDTA_SW_INSTANCE + "InstalledOnOS", "Betriebssystem"),
    ("profiles", "text", IDTA_SW_INSTANCE + "InstalledModules", "Profile"),
    ("app", "text", _own("InstanceRuntime/0/1/App"), "App"),
    ("appVersion", "text", IDTA_SW_INSTANCE + "InstalledVersion", "App-Version"),
    ("instanceName", "text", IDTA_SW_INSTANCE + "InstanceName", "Instanz"),
    # oaap.placement
    ("alias", "text", _own("Placement/0/1/Alias"), "Name im Verbund"),
    ("place", "text", _own("Placement/0/1/Place"), "Ort"),
    ("tags", "text", _own("Placement/0/1/Tags"), "Tags"),
    ("address", "text", _own("Placement/0/1/Address"), "Adresse"),
    ("via", "text", _own("Placement/0/1/Via"), "Eingang"),
    # oaap.hostcare -- no published template (02030/02043 in development)
    ("measured", "datetime", _own("HostCare/0/1/Measured"), "Gemessen"),
    ("kernelRunning", "text", _own("HostCare/0/1/KernelRunning"), "Kernel läuft"),
    ("kernelInstalled", "text", _own("HostCare/0/1/KernelInstalled"), "Kernel installiert"),
    ("rebootRequired", "bool", _own("HostCare/0/1/RebootRequired"), "Neustart nötig"),
    ("autoUpdates", "text", _own("HostCare/0/1/AutoUpdates"), "Automatische Updates"),
    ("autoUpdatesLastRun", "datetime", _own("HostCare/0/1/AutoUpdatesLastRun"), "Updates zuletzt"),
    ("rebootWindow", "text", _own("HostCare/0/1/RebootWindow"), "Neustartfenster"),
    ("rebootPlanAt", "datetime", _own("HostCare/0/1/RebootPlan/At"), "Neustart geplant"),
    ("rebootPlanUrgency", "text", _own("HostCare/0/1/RebootPlan/Urgency"), "Dringlichkeit"),
    ("rebootPlanState", "text", _own("HostCare/0/1/RebootPlan/State"), "Neustart-Zustand"),
    ("advisoriesOpen", "int", _own("HostCare/0/1/Advisories/Open"), "Offene Sicherheitsmeldungen"),
    ("advisoriesOldest", "date", _own("HostCare/0/1/Advisories/Oldest"), "Älteste Meldung"),
    ("advisoriesStale", "bool", _own("HostCare/0/1/Advisories/Stale"), "Liste veraltet"),
    ("imagesOpen", "int", _own("HostCare/0/1/Images/Open"), "Abbilder mit Meldungen"),
    ("protectionsCount", "int", _own("HostCare/0/1/Protections/Count"), "Schutzfristen"),
    ("protectionsUntil", "date", _own("HostCare/0/1/Protections/Until"), "Geschützt bis"),
    # oaap.services
    ("coreServices", "text", _own("CoreServices/0/1/States"), "Kerndienste"),
    ("coreWorst", "text", _own("CoreServices/0/1/Worst"), "Schlechtester Zustand"),
    # oaap.attention -- a projection of Service Request Notification (02010), not 1:1
    ("attentionCount", "int", _own("Attention/0/1/Count"), "Braucht einen Menschen"),
    ("attentionKinds", "text", _own("Attention/0/1/Kinds"), "Arten"),
    ("attentionDetail", "text", _own("Attention/0/1/Detail"), "Einzelheiten"),
    # oaap.dns
    ("publicIp", "text", _own("DnsNames/0/1/PublicIp"), "Öffentliche Adresse"),
    ("namesTotal", "int", _own("DnsNames/0/1/Total"), "Namen"),
    ("namesNotOk", "int", _own("DnsNames/0/1/NotOk"), "Namen nicht in Ordnung"),
    ("namesProblems", "text", _own("DnsNames/0/1/Problems"), "Namensprobleme"),
    # oaap.liveness -- the host's verdict (fleet_watch), never the fleet service's own
    ("state", "text", _own("Liveness/0/1/State"), "Zustand"),
    ("since", "datetime", _own("Liveness/0/1/Since"), "Seit"),
    ("interval", "int", _own("Liveness/0/1/Interval"), "Intervall (s)"),
    ("lostTotal", "int", _own("Liveness/0/1/Lost"), "Verlorene Messzeilen"),
    ("eventsDropped", "int", _own("Liveness/0/1/EventsDropped"), "Verworfene Ereignisse"),
    # oaap.runtime -- instances
    ("channel", "text", _own("InstanceRuntime/0/1/Channel"), "Kanal"),
    ("runState", "text", _own("InstanceRuntime/0/1/State"), "Zustand"),
    ("origin", "text", _own("InstanceRuntime/0/1/Origin"), "Herkunft"),
    ("autoAddress", "text", _own("InstanceRuntime/0/1/AutoAddress"), "Automatische Adresse"),
    ("autoState", "text", _own("InstanceRuntime/0/1/AutoState"), "Zustand automatische Adresse"),
    # oaap.broker
    ("role", "text", _own("Broker/0/1/Role"), "Rolle"),
    # oaap.tenant (RFC-0087 §4)
    ("tenantName", "text", _own("FleetTenant/0/1/Name"), "Mandant"),
    ("level", "text", _own("FleetTenant/0/1/Level"), "Preisgabe"),
    ("instanceCount", "int", _own("FleetTenant/0/1/Instances"), "Instanzen"),
    # oaap.tunnel.outer / .inner -- each half written from its own node (§5)
    ("outerLabel", "text", _own("FleetTunnel/0/1/Outer/Label"), "Name außen"),
    ("outerState", "text", _own("FleetTunnel/0/1/Outer/State"), "Zustand außen"),
    ("outerSince", "datetime", _own("FleetTunnel/0/1/Outer/Since"), "Verbunden seit"),
    ("outerOffers", "text", _own("FleetTunnel/0/1/Outer/Offers"), "Angekündigte Angebote"),
    ("innerLabel", "text", _own("FleetTunnel/0/1/Inner/Label"), "Name innen"),
    ("innerState", "text", _own("FleetTunnel/0/1/Inner/State"), "Zustand innen"),
    ("innerPaused", "bool", _own("FleetTunnel/0/1/Inner/Paused"), "Angehalten"),
    ("innerOffers", "text", _own("FleetTunnel/0/1/Inner/Offers"), "Angebote"),
    # oaap.destination
    ("destName", "text", _own("FleetDestination/0/1/Name"), "Destination"),
    ("destKind", "text", _own("FleetDestination/0/1/Kind"), "Weg"),
    ("destOffer", "text", _own("FleetDestination/0/1/Offer"), "Angebot"),
    ("destState", "text", _own("FleetDestination/0/1/State"), "Zustand"),
    ("boundCount", "int", _own("FleetDestination/0/1/Bound"), "Gebunden"),
    # oaap.external -- the edge of what the fleet knows (§5.1)
    ("extName", "text", _own("FleetExternal/0/1/Name"), "Ziel"),
    ("extKind", "text", _own("FleetExternal/0/1/Kind"), "Art"),
    ("extState", "text", _own("FleetExternal/0/1/State"), "Zustand"),
    # owner.link -- people only: the far end in someone else's world (§5.1)
    ("externalId", "text", _own("FleetExternal/0/1/Link/ExternalId"), "Externe Kennung"),
    ("externalSource", "text", _own("FleetExternal/0/1/Link/Source"), "Wessen Welt"),
    # owner.* -- written by people only
    ("contactName", "text", "0173-1#02-AAO205#002", "Ansprechpartner"),
    ("contactRole", "text", "0173-1#02-AAO204#003", "Rolle"),
    ("contactEmail", "text", "0173-1#02-AAO198#002", "E-Mail"),
    ("contactPhone", "text", "0173-1#02-AAO136#002", "Telefon"),
    ("company", "text", "0173-1#02-AAW001#001", "Firma"),
    ("manufacturer", "text", "0112/2///61987#ABA565#009", "Hersteller"),
    ("serialNumber", "text", "0112/2///61987#ABA951#009", "Seriennummer"),
    ("hardwareVersion", "text", "0112/2///61987#ABA926#008", "Hardware-Stand"),
    ("purchased", "date", _own("Owner/0/1/Purchased"), "Gekauft"),
    ("warrantyUntil", "date", _own("Owner/0/1/WarrantyUntil"), "Garantie bis"),
    ("notes", "text", _own("Owner/0/1/Notes"), "Notizen"),
]

# (key, on, semantic id, attributes, relations, title)
GROUPS = [
    ("oaap.software", NODE, IDTA_SOFTWARE, ["platformVersion", "os", "profiles"], [],
     "Software"),
    ("oaap.placement", NODE, _own("Placement/0/1"), ["alias", "place", "tags", "address", "via"],
     ["reportsThrough"], "Platzierung"),
    ("oaap.hostcare", NODE, _own("HostCare/0/1"),
     ["measured", "kernelRunning", "kernelInstalled", "rebootRequired", "autoUpdates",
      "autoUpdatesLastRun", "rebootWindow", "rebootPlanAt", "rebootPlanUrgency",
      "rebootPlanState", "advisoriesOpen", "advisoriesOldest", "advisoriesStale",
      "imagesOpen", "protectionsCount", "protectionsUntil"], [], "Pflege"),
    ("oaap.services", NODE, _own("CoreServices/0/1"), ["coreServices", "coreWorst"], [],
     "Kerndienste"),
    ("oaap.attention", NODE, _own("Attention/0/1"),
     ["attentionCount", "attentionKinds", "attentionDetail"], [], "Braucht einen Menschen"),
    ("oaap.dns", NODE, _own("DnsNames/0/1"),
     ["publicIp", "namesTotal", "namesNotOk", "namesProblems"], [], "Namen"),
    ("oaap.liveness", NODE, _own("Liveness/0/1"),
     ["state", "since", "interval", "lostTotal", "eventsDropped"], [], "Lebenszeichen"),
    ("owner.contact", NODE, IDTA_CONTACT,
     ["contactName", "contactRole", "contactEmail", "contactPhone", "company"], [],
     "Kontakt"),
    ("owner.nameplate", NODE, IDTA_NAMEPLATE,
     ["manufacturer", "serialNumber", "hardwareVersion", "purchased", "warrantyUntil", "notes"],
     [], "Typenschild"),
    ("oaap.appsoftware", INSTANCE, IDTA_SOFTWARE, ["app", "appVersion", "instanceName"], [],
     "Software"),
    ("oaap.runtime", INSTANCE, _own("InstanceRuntime/0/1"),
     ["channel", "runState", "origin", "address", "autoAddress", "autoState"],
     ["isPartOf", "heldBy", "bindsTo"], "Laufzeit"),
    ("oaap.broker", BROKER, _own("Broker/0/1"), ["role"], ["bridgedTo"], "Broker"),
    # RFC-0087
    ("oaap.tenant", TENANT, _own("FleetTenant/0/1"), ["tenantName", "level", "instanceCount"],
     ["tenantOn"], "Mandant"),
    ("oaap.tunnel.outer", TUNNEL, _own("FleetTunnel/0/1/Outer"),
     ["outerLabel", "outerState", "outerSince", "outerOffers"], ["servedBy"], "Äußere Seite"),
    ("oaap.tunnel.inner", TUNNEL, _own("FleetTunnel/0/1/Inner"),
     ["innerLabel", "innerState", "innerPaused", "innerOffers"],
     ["dialedBy", "reaches"], "Innere Seite"),
    ("oaap.destination", DESTINATION, _own("FleetDestination/0/1"),
     ["destName", "destKind", "destOffer", "destState", "boundCount"],
     ["definedOn", "destOf", "usesTunnel", "usesExternal"], "Destination"),
    ("oaap.external", EXTERNAL, _own("FleetExternal/0/1"), ["extName", "extKind", "extState"],
     ["behind"], "Externes Ende"),
    ("owner.link", EXTERNAL, _own("FleetExternal/0/1/Link"),
     ["externalId", "externalSource", "notes"], [], "Verknüpfung"),
]

def ak(name):
    """The registered key of one of the short names above: every fleet key
    starts with `fleet`, because attribute and relation keys are node-wide
    and a generic `state` or `origin` would collide with an app's types."""
    return "fleet" + name[0].upper() + name[1:]


def attrs(values):
    """{short name: value} -> {registered key: value}, None left out."""
    return {ak(k): v for k, v in values.items() if v is not None}


RELATIONS = [
    # (key, from, to, semantic id, title, title_inverse)
    ("isPartOf", INSTANCE, NODE, IDTA_IS_PART_OF, "läuft auf", "betreibt"),
    ("reportsThrough", NODE, BROKER, _own("PeerRelations/0/1/ReportsThrough"),
     "meldet über", "nimmt Meldungen an von"),
    ("bridgedTo", BROKER, BROKER, _own("PeerRelations/0/1/BridgedTo"),
     "ist gebrückt zu", "nimmt Brücke an von"),
    # RFC-0087 §5
    ("tenantOn", TENANT, NODE, IDTA_IS_PART_OF, "gehört zu Knoten", "hält Mandant"),
    ("heldBy", INSTANCE, TENANT, _own("PeerRelations/0/2/HeldBy"), "gehört Mandant", "hat Instanz"),
    ("bindsTo", INSTANCE, DESTINATION, _own("PeerRelations/0/2/BindsTo"),
     "ruft über", "wird gerufen von"),
    ("servedBy", TUNNEL, NODE, _own("PeerRelations/0/2/ServedBy"),
     "endet außen auf", "nimmt Tunnel an"),
    ("dialedBy", TUNNEL, NODE, _own("PeerRelations/0/2/DialedBy"),
     "wird aufgebaut von", "baut Tunnel auf"),
    ("reaches", TUNNEL, INSTANCE, _own("PeerRelations/0/2/Reaches"),
     "erreicht", "erreichbar über"),
    ("definedOn", DESTINATION, NODE, _own("PeerRelations/0/2/DefinedOn"),
     "liegt auf", "hat Destination"),
    ("destOf", DESTINATION, TENANT, _own("PeerRelations/0/2/DestinationOf"),
     "gehört Mandant", "hat Destination"),
    ("usesTunnel", DESTINATION, TUNNEL, _own("PeerRelations/0/2/UsesTunnel"),
     "geht durch", "trägt"),
    ("usesExternal", DESTINATION, EXTERNAL, _own("PeerRelations/0/2/UsesExternal"),
     "führt zu", "erreicht über"),
    ("behind", EXTERNAL, INSTANCE, _own("PeerRelations/0/2/Behind"),
     "liegt hinter", "führt zu"),
]

OBJECTS = [
    (NODE, "Knoten", "Knoten", _own("FleetNode/0/1")),
    (BROKER, "Broker", "Broker", _own("FleetBroker/0/1")),
    (INSTANCE, "Instanz", "Instanzen", _own("FleetInstance/0/1")),
    (TENANT, "Mandant", "Mandanten", _own("FleetTenant/0/1")),
    (TUNNEL, "Tunnel", "Tunnel", _own("FleetTunnel/0/1")),
    (DESTINATION, "Destination", "Destinationen", _own("FleetDestination/0/1")),
    (EXTERNAL, "Externes Ende", "Externe Enden", _own("FleetExternal/0/1")),
]

# The node's series (RFC-0086), one per metric of the sender (metrics.SERIES).
# (measure, unit, semantic id). No verified ECLASS/IEC CDD ID exists for
# these (research 2026-11-14) -- ours until one does.
METRICS = [
    ("cpu", "%", _own("Metrics/0/1/Cpu")),
    ("mem", "%", _own("Metrics/0/1/Memory")),
    ("disk", "%", _own("Metrics/0/1/Disk")),
    ("req", "/min", _own("Metrics/0/1/Requests")),
    ("lat", "ms", _own("Metrics/0/1/Latency")),
    ("tx", "B/s", _own("Metrics/0/1/Transmit")),
    ("rx", "B/s", _own("Metrics/0/1/Receive")),
    ("conn", "", _own("Metrics/0/1/Connections")),
    ("ctop", "% Kern", _own("Metrics/0/1/TopContainerCpu")),
]
METRIC_UNITS = {m: u for m, u, _s in METRICS}
# Text series: the liveness verdict and each core service's state.
TEXT_STATE = "state"
TEXT_CORE_PREFIX = "core."


def data_model():
    """The `data_model` section (oaap.data.model §2.2) of the fleet."""
    known = {a[0] for a in _ATTRS}
    return {
        "object_types": [{"key": k, "title": t, "title_plural": tp, "semantic_id": s}
                         for k, t, tp, s in OBJECTS],
        "attribute_types": [{"key": ak(k), "title": t, "value_type": vt,
                             **({"semantic_id": s} if s else {})}
                            for k, vt, s, t in _ATTRS],
        "group_types": [{"key": g, "on": on, "title": t, "semantic_id": s,
                         "attributes": [ak(a) for a in at if a in known],
                         "relations": [ak(r) for r in rel]}
                        for g, on, s, at, rel, t in GROUPS],
        "relation_types": [{"key": ak(k), "from": f, "to": to, "semantic_id": s,
                            "title": t, "title_inverse": ti}
                           for k, f, to, s, t, ti in RELATIONS],
    }
