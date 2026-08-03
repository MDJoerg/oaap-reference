# Repository Roadmap

1. ~~Walking skeleton~~ — done 2026-08-03: installer → gateway → portal
   wizard → first admin login (see `docs/walking-skeleton.md`)
2. Validate the skeleton on real hardware — Debian VM done 2026-08-03
   (install → token recovery over SSH → first admin → login); still open:
   Raspberry Pi / arm64 and the full conformance test walkthrough
3. TLS at the gateway; offline artifact bundle (pre-exported images)
4. Grow portal/identity/gateway alongside their full capability specs
5. Executable conformance test suite for `oaap.core.host`
