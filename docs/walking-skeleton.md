# Walking Skeleton — Spec Mapping & Limitations

Status: first end-to-end slice, 2026-08-03. Implements
`oaap.core.host` (bootstrap) against
`oaap-spec/spec/oaap.core.host.md`, with skeleton versions of
gateway, identity, and portal.

## Conformance test mapping (spec §5)

| # | Test | How to check here |
| - | ---- | ----------------- |
| 1 | Happy path | `sudo ./install.sh` on a fresh Debian VM → exit 0, URL + token printed, `/setup` reachable |
| 2 | Default deny before admin | `curl -i http://<host>/` → 303 to `/auth/login`; no app content without session |
| 3 | Token required | wizard submit with wrong token → error 403, no user created |
| 4 | Wizard closes | after admin creation: login works; `/setup` shows "already completed"; identity `/internal/setup` returns 410 |
| 5 | Idempotence | second `sudo ./install.sh` → message, exit 1, nothing changed |
| 6 | Preflight safety | e.g. stop Docker daemon → all problems listed, no files created |
| 7 | Node status | `oaap status` → HEALTHY 3/3; `docker stop oaap-portal-1` → DEGRADED, exit 1 |
| 8 | Offline | after images are built, no internet access is needed; base images must be available locally |
| 9 | Uninstall round-trip | `sudo oaap uninstall --yes` → `sudo ./install.sh` succeeds again; without `--purge` the data dir survives, with `--purge` it is gone |

## Known limitations (deliberate, tracked)

- **HTTP only** — TLS termination is specified for the gateway but not
  yet wired up (needs a hostname story; next iteration).
- **Test 8 partially**: the first build pulls `python:3.12-slim` and
  `caddy:2` from the internet. A fully offline artifact bundle
  (pre-exported images) is future work.
- Portal dashboard is a stub: shows identity, roles, version; no user
  management yet (comes with the full `oaap.core.portal` spec).
- Session is a signed cookie; the RFC-0002 open question
  (JWT vs. trusted headers) is answered here pragmatically with
  **trusted headers** (`X-OAAP-User`, `X-OAAP-Roles`) — to be confirmed
  or replaced when `oaap.core.identity`/`gateway` are fully specified.
- No `join`/`remote join` (reserved per RFC-0003), no updates yet.

## Manual smoke test (until CI exists)

1. Fresh Debian 12/13 VM with Docker → run installer.
2. Browser: setup URL → wrong token (must fail) → correct token, create
   admin.
3. `/` → login → dashboard shows user/roles → sign out.
4. `oaap status`, `oaap version`.
5. Re-run installer (must refuse).
6. `sudo oaap uninstall` (confirm) → installer runs again from scratch;
   repeat with `--purge` and check `/var/lib/oaap` is gone.
