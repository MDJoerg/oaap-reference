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
- Root access, ~2 GB free disk, port 80 free (configurable)
- Docker Engine with the Compose v2 plugin — or let the installer
  install it for you: it asks when Docker is missing (non-interactive:
  `OAAP_INSTALL_RUNTIME=1`)

## Install

```sh
sudo ./install.sh
```

The installer runs preflight checks (and changes nothing if they fail),
offers the server-readiness fixes (keep the machine awake, pin the
current IP address — each only with your consent), starts the core
services, and prints the setup URL plus a one-time token. Open the URL,
enter the token, create the first admin — done. Check the node anytime
with `oaap status`; remove the platform again with `sudo oaap
uninstall` (add `--purge` to also delete all data — useful on test
hardware). Server readiness alone (also later, also on installed
machines): `sudo ./install.sh prepare`.

### Fresh Debian (netinstall)

A Debian netinstall where you set a root password ships **without
`sudo` and without `git`**. Get from zero to installed like this
(replace `<user>` with the user created during the Debian install):

```sh
su -                              # become root (root password)
apt install git
exit
git clone <repo-url> && cd oaap-reference
su -c "bash install.sh"           # installer offers to set up sudo for <user>
```

After the run, log out and in once — from then on `sudo` works and the
printed `oaap` commands behave as documented.

### Backup, restore & relocation (oaap.data.backup)

```sh
sudo oaap backup create [--to <path>]         # one archive with everything
sudo ./install.sh restore <backup.tar.gz>     # on a fresh, prepared machine
```

The backup is offline-consistent (app containers stop briefly) and
contains all platform state, users, app instances, and their data — it
must live **outside** the data directory and is secret-grade. Restore
brings the same platform up on another machine without a setup wizard:
existing users just sign in; app images are rebuilt or pulled from each
instance's recorded package source (a local package directory must be
copied to the same path on the new machine first). Moving house: back
up, restore on the new box, verify via LAN, then switch DNS and port
forwarding — and stop the old platform before it competes for the
hostname.

Configuration via environment: `OAAP_HTTP_PORT` (default 80),
`OAAP_DATA_DIR` (default `/var/lib/oaap`), `OAAP_HOST` (setup-URL host);
non-interactive consents: `OAAP_INSTALL_RUNTIME=1` (Docker),
`OAAP_SERVER_MODE=1` (keep-awake), `OAAP_STATIC_IP=current|<ip>|skip`
(pin address), `OAAP_ADMIN_SUDO=1` (sudo setup).

## Layout

| Path | Implements |
| ---- | ---------- |
| `install.sh` | `oaap.core.host` — bootstrap/prepare/restore modes |
| `bin/oaap` | `oaap.core.host` — node CLI; `oaap.data.backup` — `backup create` |
| `platform/Caddyfile`, gateway service | `oaap.core.gateway` — default deny, forward auth |
| `platform/services/identity` | `oaap.core.identity` — built-in minimal provider |
| `platform/services/portal` | `oaap.core.portal` — first-run wizard + dashboard stub |

See [docs/walking-skeleton.md](docs/walking-skeleton.md) for the mapping
to the spec's conformance tests and known limitations.
