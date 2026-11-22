"""The assistant templates an app brings (manifest 0.20 `assistant_templates`,
RFC-0119, oaap.apps.runtime 2.41).

Pure functions, no registry and no Docker, so the rules can be tested on
paper:

    declaration_error(m)        '' or the sentence for the operator
    folder_of(m)                the declared folder, or ''
    template_problems(doc, app_id)
                                why a template from this app is not allowed
                                (RFC-0119 section 4.2-4.4) -> sentences
    extract(pkg, folder, app_id, dest)
                                copy the valid templates of the folder into
                                dest/; returns (published, dropped)
    find(root, rid)             the file of a template in a tenant's folder
                                (the newest version over its instances)
    listing(root)               [(rid, path, meta)] of a tenant's folder

The package is data. Only `*.json` directly inside the declared folder is
read, no link is followed, nothing leaves the folder. A template that breaks
a rule is DROPPED with a reason -- the install never fails because of it.

An app speaks for itself only (section 4.3): its template builds a view of
its OWN server's reading tools, the chat's profile on that view, the one
assistant, and releases a model name to the chat. It installs nothing,
binds no destination, touches no other app and never chooses its own
server's light -- a light it needs is a parameter without default the
tenant admin answers (section 4.4, RFC-0113 D2).
"""
import json
import os
import re
import stat

import tenant_build as tb
import tenant_recipe

MAX_TEMPLATES = 20
META = "templates.json"
APP_NEED = "source"
# the apps a template may name besides its own (section 4.3)
GATEWAY_APP = "mcp-gateway"
MODELS_APP = "ai-gateway"
CHAT_APP = "chat"
GATEWAY_KINDS = ("refresh", "server_light", "view", "profile", "assistant")
MODELS_KINDS = ("client",)
# RFC-0119 3a: the first MCP gateway that takes `prompts` in a view
PROMPTS_GATEWAY = "0.42.0"
# RFC-0119 3b: the first MCP gateway that takes `writes` in a view
WRITES_GATEWAY = "0.43.0"
_PH = re.compile(r"^\{([a-z][a-z0-9_]*)\}$")


def _local_folder(p):
    if not isinstance(p, str) or not p.strip():
        return False
    p = p.strip()
    if p.startswith("/") or "\\" in p or "\x00" in p:
        return False
    return all(x not in ("", ".", "..") for x in p.rstrip("/").split("/"))


def declaration_error(m):
    """'' when `assistant_templates:` is valid, else one sentence."""
    if "assistant_templates" not in m:
        return ""
    d = m.get("assistant_templates")
    if not isinstance(d, dict):
        return "assistant_templates: object expected"
    if set(d) - {"path"}:
        return "assistant_templates: only 'path' is known (RFC-0119)"
    if not _local_folder(d.get("path")):
        return "assistant_templates.path: a folder inside the package (no /, no ..)"
    mcp = m.get("mcp")
    if not (isinstance(mcp, dict) and isinstance(mcp.get("path"), str) and mcp["path"]):
        return ("assistant_templates: only for an app that declares an MCP door "
                "(mcp.path) -- a template builds a view of its tools")
    return ""


def folder_of(m):
    d = (m or {}).get("assistant_templates")
    if isinstance(d, dict) and _local_folder(d.get("path")):
        return d["path"].strip().rstrip("/")
    return ""


def _placeholder(v):
    m = _PH.match(v) if isinstance(v, str) else None
    return m.group(1) if m else ""


def template_problems(doc, app_id):
    """Why this template may not come from the app `app_id` -> sentences."""
    if not isinstance(doc, dict):
        return ["not a recipe"]
    out = []
    rid = str(doc.get("id") or "")
    if doc.get("offers") != "assistant":
        out.append("an app brings assistant templates only (offers: assistant)")
    if not rid.startswith(app_id + "-") or len(rid) <= len(app_id) + 1:
        out.append(f"the id must begin with '{app_id}-' (RFC-0119 D7)")
    needs = doc.get("needs") if isinstance(doc.get("needs"), dict) else {}
    own = needs.get(APP_NEED)
    if not isinstance(own, dict) or own.get("app") != app_id:
        out.append(f"the need '{APP_NEED}' must name the app itself ({app_id})")
    by_app = {}
    for n, spec in needs.items():
        if not isinstance(spec, dict):
            continue
        a = spec.get("app")
        if spec.get("install") is True:
            out.append(f"need '{n}': an app's template installs nothing")
        if n != APP_NEED and a not in (GATEWAY_APP, MODELS_APP, CHAT_APP):
            out.append(f"need '{n}': an app's template names only itself, the MCP "
                       f"gateway, the model gateway and the chat, not '{a}'")
        by_app.setdefault(a, set()).add(n)
    own_key = APP_NEED + tenant_recipe.KEY_SUFFIX
    params = doc.get("params") if isinstance(doc.get("params"), dict) else {}
    views = set()
    prompt_views = set()
    writes_view = False
    for st in doc.get("steps") or []:
        if not isinstance(st, dict):
            continue
        sid = st.get("id", "")
        if st.get("type") != "app.setup":
            out.append(f"step '{sid}': an app's template only sets apps up "
                       f"(app.setup), not '{st.get('type')}'")
            continue
        target = _placeholder(st.get("name"))
        if target in by_app.get(GATEWAY_APP, set()):
            kinds = GATEWAY_KINDS
        elif target in by_app.get(MODELS_APP, set()):
            kinds = MODELS_KINDS
        else:
            out.append(f"step '{sid}': an app's template sets up only the MCP gateway "
                       "and the model gateway")
            continue
        for it in st.get("items") or []:
            if not isinstance(it, dict):
                continue
            k = it.get("kind")
            if k not in kinds:
                out.append(f"step '{sid}': '{k}' is not an item an app's template may set")
                continue
            if k in ("refresh", "server_light") and it.get("id") != "{" + own_key + "}":
                out.append(f"step '{sid}': {k} only of the app's own server "
                           f"({{{own_key}}})")
            if k == "server_light":
                p = _placeholder(it.get("light"))
                spec = params.get(p) if p else None
                if not (isinstance(spec, dict) and spec.get("kind") == "light"
                        and "default" not in spec and not spec.get("from")):
                    out.append(f"step '{sid}': the light of the app's own server is a "
                               "parameter of kind light without default -- the tenant "
                               "admin answers it, the app never chooses it (RFC-0119 D5)")
            if k == "view":
                tools = it.get("tools")
                if not (isinstance(tools, dict) and set(tools) == {"server", "effect"}
                        and tools.get("server") == "{" + own_key + "}"
                        and tools.get("effect") == "read"):
                    out.append(f"step '{sid}': a view of an app's template holds the "
                               f"reading tools of the app's own server only "
                               f"({{\"server\": \"{{{own_key}}}\", \"effect\": \"read\"}})")
                if "max_effect" in it and it.get("max_effect") != "read":
                    out.append(f"step '{sid}': the view reads only (RFC-0119 D4)")
                if "writes" in it:
                    # RFC-0119 3b (D13): writing tools of the app's own server, each one a
                    # yes/no a human answers -- never ticked beforehand
                    wr = it.get("writes")
                    seen = set()
                    if not isinstance(wr, dict) or not wr:
                        out.append(f"step '{sid}': writes names the app's writing tools, "
                                   "each with a parameter of kind bool")
                        wr = {}
                    for tool, v in wr.items():
                        p = _placeholder(v)
                        spec = params.get(p) if p else None
                        if not (isinstance(spec, dict) and spec.get("kind") == "bool"
                                and str(spec.get("default", "false")).lower() == "false"
                                and spec.get("shape") is not True):
                            out.append(f"step '{sid}': writes '{tool}' is a parameter of kind "
                                       "bool that is not ticked beforehand -- the tenant admin "
                                       "ticks each writing tool (RFC-0119 D13)")
                        elif p in seen:
                            out.append(f"step '{sid}': each writing tool has its own parameter")
                        seen.add(p)
                    mv = str((needs.get(target) or {}).get("min_version") or "")
                    if not (tb.VERSION_RE.match(mv)
                            and tb.version_key(mv) >= tb.version_key(WRITES_GATEWAY)):
                        out.append(f"step '{sid}': writing tools in a view need the MCP gateway "
                                   f"{WRITES_GATEWAY} or newer -- the need '{target}' says "
                                   f"min_version: {WRITES_GATEWAY}")
                    writes_view = True
                if "prompts" in it:
                    prompt_views.add(str(it.get("id") or ""))
                    # RFC-0119 3a (D11): the prompts of the app's own server, in
                    # addition to its reading tools, from a gateway that knows them
                    if it.get("prompts") != {"server": "{" + own_key + "}"}:
                        out.append(f"step '{sid}': a view of an app's template holds the "
                                   f"prompts of the app's own server only "
                                   f"({{\"server\": \"{{{own_key}}}\"}})")
                    mv = str((needs.get(target) or {}).get("min_version") or "")
                    if not (tb.VERSION_RE.match(mv)
                            and tb.version_key(mv) >= tb.version_key(PROMPTS_GATEWAY)):
                        out.append(f"step '{sid}': prompts in a view need the MCP gateway "
                                   f"{PROMPTS_GATEWAY} or newer -- the need '{target}' says "
                                   f"min_version: {PROMPTS_GATEWAY}")
                views.add(str(it.get("id") or ""))
            if k == "profile":
                if "max_effect" in it and it.get("max_effect") not in ("read", "write"):
                    out.append(f"step '{sid}': the profile reads, or writes with writes "
                               "in the view (RFC-0119 D13)")
    for _sid, st in [(s.get("id", ""), s) for s in doc.get("steps") or [] if isinstance(s, dict)]:
        for it in st.get("items") or []:
            if not isinstance(it, dict):
                continue
            if it.get("kind") == "profile" and it.get("max_effect") == "write" and not writes_view:
                out.append(f"step '{_sid}': the profile writes only when the view names "
                           "writing tools (writes) (RFC-0119 D4/D13)")
            if it.get("kind") == "profile":
                bad = [v for v in it.get("views") or [] if v not in views]
                if bad or not it.get("views"):
                    out.append(f"step '{_sid}': the profile names only the template's own view")
            if it.get("kind") == "assistant":
                if it.get("view") not in views:
                    out.append(f"step '{_sid}': the assistant uses the template's own view")
                if it.get("views") or it.get("support_queue"):
                    out.append(f"step '{_sid}': the assistant of an app's template has one "
                               "view and nothing else (views, support_queue)")
                # RFC-0119 3a: the chat offers a view's prompts as /name only to an
                # assistant with commands: true -- allowed exactly when its view has them
                if "commands" in it and not (it.get("commands") is True
                                             and it.get("view") in prompt_views):
                    out.append(f"step '{_sid}': commands: true only when the assistant's view "
                               "names the app's prompts")
    return out


def _check(raw, app_id):
    """-> (doc, '') or (None, reason)."""
    if len(raw) > tb.MAX_PROFILE_BYTES:
        return None, f"larger than {tb.MAX_PROFILE_BYTES // 1024} KB"
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None, "not valid JSON"
    problems = tenant_recipe.recipe_problems(doc) if isinstance(doc, dict) else ["not a recipe"]
    if not problems:
        problems = template_problems(doc, app_id)
    if problems:
        return None, "; ".join(problems[:3]) + (f" (and {len(problems) - 3} more)"
                                                if len(problems) > 3 else "")
    return doc, ""


def extract(pkg, folder, app_id, dest):
    """Copy the valid templates of `pkg/folder` into `dest` (created) ->
    (published [{id, version, title}], dropped [{file, reason}])."""
    published, dropped = [], []
    src = os.path.join(pkg, folder)
    try:
        st = os.lstat(src)
    except OSError:
        return [], [{"file": folder, "reason": "the folder is not in the package"}]
    if not stat.S_ISDIR(st.st_mode):
        return [], [{"file": folder, "reason": "not a folder (a link is not followed)"}]
    names = sorted(n for n in os.listdir(src) if n.endswith(".json"))
    os.makedirs(dest, mode=0o755, exist_ok=True)
    seen = set()
    for n in names:
        p = os.path.join(src, n)
        try:
            if not stat.S_ISREG(os.lstat(p).st_mode):
                dropped.append({"file": n, "reason": "not a plain file (a link is not followed)"})
                continue
            with open(p, "rb") as f:
                raw = f.read(tb.MAX_PROFILE_BYTES + 1)
        except OSError as exc:
            dropped.append({"file": n, "reason": type(exc).__name__})
            continue
        doc, why = _check(raw, app_id)
        if doc is None:
            dropped.append({"file": n, "reason": why})
            continue
        rid = doc["id"]
        if rid in seen:
            dropped.append({"file": n, "reason": f"the id '{rid}' twice in the package"})
            continue
        if len(published) >= MAX_TEMPLATES:
            dropped.append({"file": n, "reason": f"more than {MAX_TEMPLATES} templates"})
            continue
        seen.add(rid)
        out = os.path.join(dest, rid + ".json")
        with open(out, "wb") as f:
            f.write(raw)
        os.chmod(out, 0o644)
        published.append({"id": rid, "version": tb.profile_version(doc),
                          "title": str((doc.get("card") or {}).get("title") or doc.get("title") or rid)})
    return published, dropped


def write_json(path, doc):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    os.chmod(path, 0o644)


def _vkey(v):
    m = re.match(r"^([0-9]+)\.([0-9]+)\.([0-9]+)", str(v or ""))
    return tuple(int(x) for x in m.groups()) if m else (-1, -1, -1)


def listing(root):
    """[(rid, path, meta)] of a tenant's app templates: per id the newest
    version over the instances that bring it. `meta` is the instance's
    meta row plus the template's version."""
    best = {}
    try:
        iids = sorted(os.listdir(root))
    except OSError:
        return []
    for iid in iids:
        base = os.path.join(root, iid)
        if iid.startswith(".") or not os.path.isdir(base):
            continue
        try:
            with open(os.path.join(base, META), encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            continue
        for row in meta.get("templates") or []:
            rid = str(row.get("id") or "")
            path = os.path.join(base, rid + ".json")
            if not tb.ID_RE.match(rid) or not os.path.isfile(path):
                continue
            have = best.get(rid)
            if have and _vkey(have[2].get("template_version")) >= _vkey(row.get("version")):
                continue
            best[rid] = (rid, path, {"instance": meta.get("instance", ""),
                                     "name": meta.get("name", ""),
                                     "instance_id": meta.get("instance_id", iid),
                                     "app_id": meta.get("app_id", ""),
                                     "app_name": meta.get("app_name", ""),
                                     "app_version": meta.get("version", ""),
                                     "template_version": row.get("version", "")})
    return [best[k] for k in sorted(best)]


def find(root, rid):
    """The file of the template `rid` in this tenant's folder, or ''."""
    for r, path, _meta in listing(root):
        if r == rid:
            return path
    return ""


def meta_of(root, rid):
    for r, _path, meta in listing(root):
        if r == rid:
            return meta
    return None
