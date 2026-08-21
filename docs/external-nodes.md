# External nodes (internet servers)

Nodes that live outside Jörg's LAN — rented servers with a public
address. First one: **oaapx01** (`x` = external; more may follow as
oaapx02, …). Unlike the [test VM](test-vm.md) these carry real data:
no `reset`, no experiments with the network stack.

*Deutsche Kurzfassung:* Dieses Dokument beschreibt, wie ein gemieteter
Internet-Server für die Fernwartung durch Claude vorbereitet wird
(Benutzer `oaap-admin`, sudo ohne Passwort, Login nur per SSH-Key) und
wie OAAP dort ohne Browser installiert wurde. Skripte:
`test/bootstrap-remote-admin.sh` (einmalig als root), `test/node.sh`
(laufender Zugriff). Zugangsdaten liegen nur in `test/.env`.

## Connection

Configured in `test/.env` (gitignored; template: `test/.env.example`),
one variable block per node, prefix = node name in upper case:

```text
OAAPX01_HOST=oaap.joomp.de
OAAPX01_IP=212.132.64.58
OAAPX01_USER=oaap-admin
OAAPX01_SSH_KEY=~/.ssh/oaap_strato     # dedicated key, NOT the LAN test key
OAAPX01_ADMIN_USER=...                 # first portal admin (server_admin)
OAAPX01_ADMIN_PASSWORD=...
```

A separate SSH key per trust zone: the LAN test machines use
`~/.ssh/oaap_test_vm`; the internet node uses its own key, so a leak of
one never opens the other.

## Helper: `test/node.sh`

```sh
bash test/node.sh oaapx01 run <cmd>   # run any command (e.g. 'sudo oaap status')
bash test/node.sh oaapx01 ssh         # interactive shell
bash test/node.sh oaapx01 update      # sudo oaap update (from the recorded git source)
bash test/node.sh oaapx01 push        # only for debugging — production nodes update via git
```

## One-time bootstrap (as root on the fresh machine)

The provider hands over a Debian 13 image with root access by SSH key.
Everything after this step runs as `oaap-admin`, never as root:

```sh
# on the dev machine: generate a dedicated key once
ssh-keygen -t ed25519 -N "" -C "claude@oaapx01" -f ~/.ssh/oaap_strato
# on the server (paste script + public key):
bash test/bootstrap-remote-admin.sh "$(cat ~/.ssh/oaap_strato.pub)" oaapx01
```

The script installs sudo/git/curl, creates `oaap-admin` **without a
password** (key-only login), grants passwordless sudo via
`/etc/sudoers.d/oaap-admin` (Claude works non-interactively), installs
the public key and sets the hostname. It leaves sshd alone — on
oaapx01 the image already had `PasswordAuthentication no`, root stays
reachable by key (`PermitRootLogin without-password`), Jörg's decision.

## Install OAAP headless (no browser needed)

```sh
bash test/node.sh oaapx01 run 'git clone https://github.com/MDJoerg/oaap-reference && cd oaap-reference && \
  sudo env OAAP_INSTALL_RUNTIME=1 OAAP_HOST=oaap.joomp.de OAAP_SERVER_MODE=1 \
           OAAP_STATIC_IP=skip OAAP_ADMIN_SUDO=0 OAAP_WLAN_WATCHDOG=0 bash install.sh'
# first admin via the setup endpoint from localhost (token never leaves the box)
bash test/node.sh oaapx01 run 'TOK=$(sudo oaap setup-token | grep -oE "[A-Za-z0-9_-]{20,}" | head -1); \
  curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost/setup \
    --data-urlencode token=$TOK --data-urlencode username=<admin> --data-urlencode password=<pw>'   # 303 = ok
bash test/node.sh oaapx01 run 'sudo oaap external set oaap.joomp.de'      # Let's Encrypt, ports 80/443 open
```

Over SSH there is no terminal, so every installer question is answered
by environment variables (`[ -t 0 ]` is false → unanswered questions
are skipped, not asked). A git clone is used deliberately: `oaap update`
pulls from the recorded source (`OAAP_PLATFORM_SOURCE`), a tar push
would not be updatable.

## Log: oaapx01

- **2026-08-21** bootstrap by Jörg (script above, hostname `oaapx01`),
  Claude access verified; 8 vCPU, 31 GB RAM, 1 TB disk, Debian 13.6.
- **2026-08-21** OAAP 0.1.40 installed from git (Docker auto-installed),
  keep-awake applied, static IP skipped (cloud /32). First admin created
  via local `/setup` POST, `oaap external set oaap.joomp.de` → certificate
  obtained within seconds, HTTP→HTTPS 301, portal login end-to-end over
  HTTPS verified. No apps yet.
- **2026-08-21** bdt-hub productive on oaapx01 (see
  `oaap-root/program/studio/runbooks/bdt-hub-produktivsetzung.md`):
  package built from the bdt-hub repo at commit `ce0035f` with
  `git archive --format=zip` (no repository credential reaches the node —
  RFC-0019 path), uploaded by scp, `oaap app install <zip> --name
  bdt-hub-test --channel test`, then `oaap app promote bdt-hub-test --to
  bdt-hub` (RFC-0020, same bytes, sha 796652fb0491). Root API keys set per
  instance via `oaap app config set` (value piped on stdin, never an
  argument), both `/healthz` 200, key check 200/401. Throttle left at the
  default 300/60. No deploy token yet. Public names: waiting for DNS
  wildcards (`*.oaap.joomp.de` for automatic instance names,
  `*.bdt.joomp.de` for product names) — until then Caddy's ACME attempts
  for `bdt-hub*.oaap.joomp.de` fail and back off (harmless).
- **2026-08-21 (later)** DNS wildcards `*.bdt.joomp.de` and `*.oaap.joomp.de`
  set by Jörg at united-domains (the form does accept `*.bdt` — an earlier
  refusal was an expired session with a misleading error). `oaap app
  address set bdt-hub hub.bdt.joomp.de`; certificates for
  `hub.bdt.joomp.de`, `bdt-hub.oaap.joomp.de`, `bdt-hub-test.oaap.joomp.de`
  obtained within ~20 s of the reload; all three answer `/healthz` 200 over
  valid TLS from outside, HTTP→HTTPS 301. **bdt-hub is live at
  https://hub.bdt.joomp.de/.**
