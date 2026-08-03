# Test VM (Hyper-V on the dev server)

Dedicated throwaway VM for automated installer/conformance testing.
Debian 13 netinstall, no GUI, standard tools + SSH. It may be destroyed
and reinstalled at any time — never put real data on it.

## Connection

Configured in `test/.env` (gitignored; template: `test/.env.example`):

```text
VM_HOST=<current IP>        # Hyper-V assigns a NEW IP on every reboot!
VM_USER=oaap-admin
VM_SSH_KEY=~/.ssh/oaap_test_vm
```

After a VM reboot: look up the new IP (Hyper-V console: `ip a`) and
update `VM_HOST` — nothing else needed.

## Helper: `test/vm.sh`

```sh
bash test/vm.sh push       # sync this repo to ~/oaap-reference on the VM
bash test/vm.sh install    # run installer (auto-installs Docker if missing)
bash test/vm.sh reset      # oaap uninstall --purge --yes
bash test/vm.sh run <cmd>  # run any command on the VM
```

Typical loop: `push` → `install` → checks → `reset`.

## One-time bootstrap that was performed (2026-08-03)

The Debian netinstall ships without `sudo`, so the first login used the
initial password; everything after runs key-based:

1. `sudo` installed (via `su -`), `oaap-admin` added to group `sudo`
2. Passwordless sudo: `/etc/sudoers.d/oaap-admin`
   (`oaap-admin ALL=(ALL) NOPASSWD:ALL` — acceptable on a throwaway VM only)
3. ed25519 public key installed to `~oaap-admin/.ssh/authorized_keys`;
   private key lives on the dev machine at `~/.ssh/oaap_test_vm`
4. Hardening: root SSH login disabled
   (`/etc/ssh/sshd_config.d/oaap-hardening.conf`: `PermitRootLogin no`).
   Password login for `oaap-admin` stays enabled as fallback/console.
5. No passwords are stored in the repo or in `.env`.

## Findings so far

- **2026-08-03, first automated run**: full path validated on fresh
  Debian 13 — runtime provisioning (Docker auto-install), bootstrap,
  default deny (T2), token checks (T3/T10), admin creation + login via
  HTTP (T1/T4), idempotence (T5), status HEALTHY (T7), uninstall
  round-trip (T9). Found & fixed: sourcing `/etc/os-release` clobbered
  the installer's `VERSION` variable (platform reported itself as
  "13 (trixie)").
- Test admin on the VM: user `joerg` (password known to Jörg).
