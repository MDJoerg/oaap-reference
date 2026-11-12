#!/usr/bin/env python3
"""Das Portal-Abbild kopiert jedes Modul, das das Portal lädt.

Das Dockerfile des Portals kopiert seine Module einzeln. Ein neues
`import x` in portal/app.py, dessen x.py in platform/services/ liegt, aber
im Dockerfile fehlt, läuft in jedem Test (der den Quellbaum einhängt) und
legt erst das eingespielte Portal still (gefunden 2026-11-12 mit report.py:
"Worker failed to boot", Neustartschleife).

Aufruf: python3 test/test_portal_image_modules.py
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SVC = os.path.join(HERE, "..", "platform", "services")
src = open(os.path.join(SVC, "portal", "app.py"), encoding="utf-8").read()
docker = open(os.path.join(SVC, "portal", "Dockerfile"), encoding="utf-8").read()
copied = set(re.findall(r"([\w/]+\.py)", " ".join(l for l in docker.splitlines()
                                                    if l.strip().startswith("COPY"))))
copied = {os.path.basename(c) for c in copied}
imported = set(re.findall(r"^\s*import (\w+)", src, re.M)) | set(
    re.findall(r"^\s*from (\w+) import", src, re.M))
missing = sorted(m for m in imported
                 if (os.path.isfile(os.path.join(SVC, m + ".py"))
                     or os.path.isfile(os.path.join(SVC, "portal", m + ".py")))
                 and m + ".py" not in copied)
print("PASS  jedes geladene Modul ist im Abbild" if not missing
      else f"FAIL  im Dockerfile fehlen: {', '.join(m + '.py' for m in missing)}")
sys.exit(1 if missing else 0)
