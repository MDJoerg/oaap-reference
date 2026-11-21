"""The twin as an MCP source (oaap.data.twin 0.9 §2.16, RFC-0118 P6).

JSON-RPC only -- no Flask, no Postgres: `answer(msg, who, store)` gets the
message, who is asking (from the headers the door's forward_auth set) and a
`store` that reads the caller's tenant (app.py binds it to the tenant the
door named). So the rules of this door are testable without a database.

The shape is the one of every OAAP app door (oaap-apps lib/mcpdoor):
JSON answers, no session. Copied rather than imported, like every other
helper between these containers -- there is no shared module.

Who may call a tool: a PERSON (not `instance:`/`remote:`) with one of the
twin's reader roles (§2.12). The discovery door lets only the catalog
methods through, as the gateway's machine principal; those answer anyone.
Descriptions are fixed: a changed description locks the tool in the
gateway until someone approves it again."""
import json
import re

MCP_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
SERVER = "oaap-twin"
VERSION = "0.9"
READERS = {"user", "keyuser", "admin", "tenant_admin"}
PAGE = 50
OBJ_RE = re.compile(r"^(?:urn:oaap:obj:)?([0-9a-fA-F-]{36})$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
INSTRUCTIONS = ("Der digitale Zwilling dieses Mandanten: Geschäftsobjekte wie Firmen, "
                "Personen, Projekte und Anlagen, mit den Gruppen, die Apps und Menschen "
                "daran pflegen. Erst twin_types oder twin_search, dann twin_object für "
                "ein Objekt. Nur lesen.")


def _tool(name, title, description, properties=None, required=()):
    return {"name": name, "title": title, "description": description,
            "inputSchema": {"type": "object", "properties": properties or {},
                            **({"required": list(required)} if required else {})},
            "annotations": {"title": title, "readOnlyHint": True,
                            "destructiveHint": False, "openWorldHint": False}}


TOOLS = [
    _tool("twin_types", "Objekttypen",
          "Nennt die Objekttypen im Zwilling dieses Mandanten: Schlüssel, Art und Titel."),
    _tool("twin_search", "Objekte suchen",
          "Sucht Objekte im Zwilling nach Wörtern im Titel, wahlweise nur eines Typs. "
          "Höchstens 50 je Seite, nach Titel geordnet; more sagt, ob es weitergeht.",
          {"q": {"type": "string", "description": "Wörter, die alle im Titel stehen"},
           "type": {"type": "string", "description": "Schlüssel eines Objekttyps (twin_types)"},
           "offset": {"type": "integer", "description": "so viele Treffer überspringen", "minimum": 0}}),
    _tool("twin_object", "Objekt lesen",
          "Liest ein Objekt des Zwillings: Kopf, alle Gruppen mit Attributen, Beziehungen "
          "(mit dem Titel des Ziels) und Aktivitäten.",
          {"id": {"type": "string", "description": "urn:oaap:obj:<uuid> oder die uuid"},
           "at": {"type": "string", "description": "Stand an diesem Tag (JJJJ-MM-TT), sonst heute"}},
          required=("id",)),
]


class Refused(Exception):
    """A sentence that goes back as a tool result with isError."""


def who_from(headers):
    """Who is asking -- from the headers the door's forward_auth copied."""
    def h(name):
        return (headers.get(name) or "").strip()
    return {"user": h("X-OAAP-User"),
            "roles": {r.strip() for r in h("X-OAAP-Roles").split(",") if r.strip()}}


def person_refusal(who):
    """'' when `who` may call a tool, else the sentence why not."""
    user = who.get("user") or ""
    if not user or user.startswith(("instance:", "remote:")):
        return ("Der Zwilling antwortet nur einer angemeldeten Person -- ohne Person "
                "gibt es nur den Katalog (oaap.data.twin §2.16).")
    if not (set(who.get("roles") or ()) & READERS):
        return "Diese Person hat keine Rolle, die den Zwilling lesen darf."
    return ""


def words_of(q):
    """At most eight words of at most 200 characters -- the rule of §2.6a."""
    return [w for w in str(q or "").strip()[:200].split() if w][:8]


def _call(name, args, store):
    if name == "twin_types":
        return {"types": store.types()}
    if name == "twin_search":
        try:
            offset = max(0, int(args.get("offset") or 0))
        except (TypeError, ValueError):
            raise Refused("offset ist eine Zahl.") from None
        type_key = str(args.get("type") or "").strip()[:100]
        rows = store.search(type_key, words_of(args.get("q")), offset, PAGE + 1)
        return {"objects": rows[:PAGE], "more": len(rows) > PAGE, "offset": offset}
    if name == "twin_object":
        m = OBJ_RE.match(str(args.get("id") or "").strip())
        if not m:
            raise Refused("id ist urn:oaap:obj:<uuid> oder eine uuid.")
        at = str(args.get("at") or "").strip()
        if at and not DATE_RE.match(at):
            raise Refused("at ist ein Tag wie 2026-11-21.")
        obj = store.object(m.group(1).lower(), at or None)
        if obj is None:
            raise Refused("Dieses Objekt gibt es im Zwilling des Mandanten nicht.")
        return obj
    return None


def answer(msg, who, store):
    """The answer to one JSON-RPC message; None for a notification."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not msg.get("method"):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
    if "id" not in msg:
        return None
    method = msg["method"]
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}

    def ok(result):
        return {"jsonrpc": "2.0", "id": msg["id"], "result": result}

    def err(code, text):
        return {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": code, "message": text}}

    def said(text):
        return ok({"content": [{"type": "text", "text": text}], "isError": True})

    if method == "initialize":
        asked = params.get("protocolVersion")
        return ok({"protocolVersion": asked if asked in MCP_VERSIONS else MCP_VERSIONS[0],
                   "capabilities": {"tools": {}},
                   "serverInfo": {"name": SERVER, "version": VERSION},
                   "instructions": INSTRUCTIONS})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": TOOLS})
    if method == "prompts/list":
        return ok({"prompts": []})
    if method == "resources/list":
        return ok({"resources": []})
    if method != "tools/call":
        return err(-32601, "Method not found")
    name = params.get("name")
    if name not in {t["name"] for t in TOOLS}:
        return err(-32602, "Unknown tool")
    why = person_refusal(who)
    if why:
        return said(why)
    args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
    try:
        result = _call(name, args, store)
    except Refused as e:
        return said(str(e))
    except Exception as e:  # noqa: BLE001 -- a sentence, never a crash of the door
        return said(f"Der Zwilling konnte das nicht lesen ({type(e).__name__}).")
    return ok({"content": [{"type": "text",
                            "text": json.dumps(result, ensure_ascii=False, default=str)}],
               "isError": False})
