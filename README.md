# oaap-reference

Open-source reference implementation of the OAAP specification
(`oaap-spec`), targeting Debian + Docker (ADR-0004/0005 in `oaap-root`).

**Current state: walking skeleton** — the complete path from an empty
machine to a logged-in admin, with every part still minimal:

```text
./install.sh  →  gateway (Caddy)  →  portal (first-run wizard)  →  first admin login
```

## Prerequisites (provider requirements)

- Debian stable (Tier 1) or Ubuntu LTS (Tier 2), amd64 or arm64
- Docker Engine with the Compose v2 plugin
- Root access, ~2 GB free disk, port 80 free (configurable)

## Install

```sh
sudo ./install.sh
```

The installer runs preflight checks (and changes nothing if they fail),
starts the core services, and prints the setup URL plus a one-time
token. Open the URL, enter the token, create the first admin — done.
Check the node anytime with `oaap status`; remove the platform again
with `sudo oaap uninstall` (add `--purge` to also delete all data —
useful on test hardware).

Configuration via environment: `OAAP_HTTP_PORT` (default 80),
`OAAP_DATA_DIR` (default `/var/lib/oaap`), `OAAP_HOST` (setup-URL host).

## Layout

| Path | Implements |
| ---- | ---------- |
| `install.sh` | `oaap.core.host` — bootstrap mode |
| `bin/oaap` | `oaap.core.host` — node CLI (`status`, `version`) |
| `platform/Caddyfile`, gateway service | `oaap.core.gateway` — default deny, forward auth |
| `platform/services/identity` | `oaap.core.identity` — built-in minimal provider |
| `platform/services/portal` | `oaap.core.portal` — first-run wizard + dashboard stub |

See [docs/walking-skeleton.md](docs/walking-skeleton.md) for the mapping
to the spec's conformance tests and known limitations.
