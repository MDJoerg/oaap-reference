# App Runtime (increment 1)

Implements the first slice of `oaap.apps.runtime` (spec draft 0.1):
install an app package on the node and run it behind the gateway.

## Usage

```sh
sudo oaap app install <package-dir> [--name NAME] [--channel production|test]
sudo oaap app list
sudo oaap app remove <name> [--purge]
sudo oaap app config list|set|unset <name> [key] [value]
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
