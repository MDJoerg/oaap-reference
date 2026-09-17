# App Runtime (increment 1)

Implements the first slice of `oaap.apps.runtime` (spec draft 0.1):
install an app package on the node and run it behind the gateway.

## Usage

```sh
sudo oaap app install <package-dir> [--name NAME] [--channel production|test]
sudo oaap app list
sudo oaap app remove <name> [--purge]
sudo oaap app config list|set|unset <name> [key] [value]
sudo oaap app logs <name> [--tail N] [--service S]
sudo oaap app restart <name>
sudo oaap app diagnose open|close|status <name> [--minutes 15|30|60]
sudo oaap app diagnose sweep
```

A package is a directory with `oaap-app.yaml` (validated against
`oaap-spec/schema/oaap-app.schema.json`) plus build context/Dockerfile.

What `install` does: validate manifest → build image on this node
(`native`) or pull (`image`/`wrapped`) → allocate a gateway listener
port from 8100–8199 (persisted per instance) → create per-instance
storage under `/var/lib/oaap/apps/<name>/storage/`, chowned to the
container's runtime UID → write `instance.env` (0600) with config
defaults and a stable `OAAP_APP_SECRET` → start the container on the
internal network only → generate a Caddy site with per-route
`forward_auth` (roles passed as `?roles=` to identity `/verify`) and
reload the gateway.

Redeploy: `production` requires a version bump; `test` may redeploy the
same version.

## Instance configuration (spec 2.8)

`oaap app config` edits the values an app declares in its manifest's
`config` block, for the life of the instance — on both channels and
without a version bump, because configuring is not deploying:

```sh
sudo oaap app config list bdt-hub                 # secrets shown only as set/empty
sudo oaap app config set bdt-hub BDT_HUB_ROOT_API_KEY   # prompts, hidden input
sudo oaap app config unset bdt-hub BDT_HUB_ROOT_API_KEY # back to the manifest default
```

Only declared keys are accepted; `OAAP_APP_SECRET` is platform-owned
and refused. Saving rewrites `instance.env` (0600) and **recreates the
container** — `docker restart` would keep the old values, since env
vars are fixed at `docker run` time. Storage, port, version and
visibility are untouched. Operator values win over manifest defaults on
every later redeploy. The portal offers the same on an instance's
object page (`server_admin` only), queued through the host-side worker.

Instances installed before 0.1.11 have no recorded config declaration;
their keys are read back from `instance.env` and all treated as secret
until the next redeploy records the manifest's real labels and flags.

## Diagnosis: state, logs, restart (RFC-0038)

`oaap app logs` and `oaap app restart` need no window: whoever is at the
machine already has the container runtime. `restart` **recreates** the
containers — the same operation a configuration save performs, so
install, restore, config and restart cannot drift apart — and is refused
while a deployment of that instance is queued or running.

```sh
sudo oaap app logs studio --tail 50
sudo oaap app restart studio          # recreates, audited as actor `cli`
```

`oaap app diagnose` is the machine-side view of what the **portal**
offers: a window of 15/30/60 minutes during which the gateway writes an
access log for that one instance and the portal may show the app's log.
Opening and closing rewrite that instance's gateway sites and reload the
gateway; closing deletes everything collected.

```sh
sudo oaap app diagnose open studio --minutes 15
sudo oaap app diagnose status studio
sudo oaap app diagnose close studio
```

Two jobs run every minute from `oaap-instance-watch.timer`:
`appctl.py state-index` writes the container facts the portal shows
(it has no access to the container runtime, deliberately), and
`appctl.py diagnose sweep` closes windows whose time is up. The second
is not housekeeping: the time limit is the promise the window makes.

Container logs are bounded at 3 × 10 MB per container, set where
containers are created. An existing container receives the limit at its
next recreate — no forced restart of every app on update.

## Instance public address (RFC-0009)

An instance's automatic external name is a subdomain of the *node*
(`<instance>.<node hostname>`). For an app whose address ends up inside
distributed clients, that ties the published address to the machine it
first ran on. `oaap app address` adds a hostname of the instance's own,
served **in addition** to the automatic one:

```sh
sudo oaap app address set bdt-hub hub.example.org
sudo oaap app address show bdt-hub
sudo oaap app address remove bdt-hub
```

The generated site (`apps-caddy/instance-addresses.caddy`) reuses the
instance's own route/role/group block, so nothing about enforcement
changes. Direct nodes get a TLS site plus an HTTP→HTTPS redirect;
behind-edge nodes get plain HTTP with the edge guard and no ACME. The
name survives redeploy and is refused if it collides with the node's
external hostname, an edge route, or another instance.

DNS and port forwarding remain the operator's job: the name must
resolve to the node's public address (or, behind an edge, to the edge,
with `oaap edge add <name> <node>` there).

## Public-route throttling (RFC-0010)

Public routes get no authentication, so the gateway applies the one
control left: requests per client address per instance, on by default
(300 per 60 s), one budget across all of the instance's entry points.

```sh
sudo oaap app throttle show bdt-hub
sudo oaap app throttle set bdt-hub 600/60
sudo oaap app throttle off bdt-hub     # warns if a public route exists
```

Over the limit the client gets `429` with `Retry-After` and the app is
not reached. Counting happens in identity's memory, per gunicorn
worker, so the real ceiling is roughly `limit × workers` — this is a
flood brake, not a credential control. See RFC-0010 for what it
deliberately does not promise.

## Portal launchpad (increment 2)

The portal dashboard shows installed instances as tiles (name,
description, version, channel badge, platform-generated URL). Tiles are
**role-filtered**: the registry stores each instance's route roles;
users only see tiles their roles permit (`admin` sees all). The filter
is UX — the gateway enforces the same roles on every request anyway.
The portal reads the registry via a read-only mount; no API between
portal and runtime yet.

## Compose converter (increment 3)

```sh
oaap app convert <docker-compose.yml> [--out DIR] [--profile NAME]
```

Generates one **wrapped**-app package per HTTP service plus `REPORT.md`
for human review (roles, health path, storage and config are
heuristics — review before installing). Design decisions, validated
against a real 24-service training stack:

- **Profiles map to app sets**, not to apps: `--profile aas` converts
  that scenario's services; each service becomes its own app.
- **Non-HTTP services are skipped** with a reason (databases, MQTT,
  Kafka …): the gateway routes HTTP(S) only — TCP passthrough is
  future work. Databases will later be platform capabilities
  (`oaap.data.*`) rather than user-facing apps anyway.
- Env vars become manifest config (secret heuristic on
  PASS/SECRET/TOKEN/KEY); config-file mounts, `depends_on`,
  service-to-service URLs, multiple ports, and non-semver image tags
  are flagged in the report.
- First converted app validated end to end: Node-RED from the training
  stack, installed behind the gateway with login enforced.

## Limitations (tracked)

- Exactly one service per app; no subpath/hostname levels yet (port
  level only, RFC-0005); `oaap status` does not yet include app
  instances; backup of app storage pending `oaap.data.files`; no user
  management yet, so role filtering is verified in code but not with a
  second user.
- First validated app: BDT 0.188.1 as channel `test` on the VM —
  default deny on the app port, role check, per-instance storage
  (UID-writable), secret outside storage, same-version test redeploy.
