# App Runtime (increment 1)

Implements the first slice of `oaap.apps.runtime` (spec draft 0.1):
install an app package on the node and run it behind the gateway.

## Usage

```sh
sudo oaap app install <package-dir> [--name NAME] [--channel production|test]
sudo oaap app list
sudo oaap app remove <name> [--purge]
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

## Portal launchpad (increment 2)

The portal dashboard shows installed instances as tiles (name,
description, version, channel badge, platform-generated URL). Tiles are
**role-filtered**: the registry stores each instance's route roles;
users only see tiles their roles permit (`admin` sees all). The filter
is UX — the gateway enforces the same roles on every request anyway.
The portal reads the registry via a read-only mount; no API between
portal and runtime yet.

## Limitations (tracked)

- Exactly one service per app; no subpath/hostname levels yet (port
  level only, RFC-0005); `oaap status` does not yet include app
  instances; backup of app storage pending `oaap.data.files`; no user
  management yet, so role filtering is verified in code but not with a
  second user.
- First validated app: BDT 0.188.1 as channel `test` on the VM —
  default deny on the app port, role check, per-instance storage
  (UID-writable), secret outside storage, same-version test redeploy.
