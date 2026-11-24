#!/usr/bin/env python3
"""Ein Update, das die Form einer Instanz ändert, lässt keinen alten Container
laufen (reference 0.1.376, gemessen 24.11.2026 mit forgejo: aus einem Dienst
wurden zwei, oaap-app-<inst> lief neben oaap-app-<inst>-web auf denselben
Daten weiter).

    python3 test/test_stale_containers.py

Gehalten wird:

A. stale_containers nennt, was das Register kannte und die neue Form nicht
   mehr nennt -- ein Dienst -> zwei, zwei -> einer, ein Dienst umbenannt;
   dieselbe Form nennt nichts; nur Namen dieser Instanz (oaap-app-...).
B. recreate_instance_containers entfernt sie VOR dem Anlegen der neuen.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "platform"))
sys.path.insert(0, os.path.join(HERE, "..", "platform", "services"))
import appctl as m  # noqa: E402

SINGLE = {"container": "oaap-app-git",
          "services": [{"service": "", "container": "oaap-app-git"}]}
MULTI = [{"service": "web", "container": "oaap-app-git-web"},
         {"service": "oaap", "container": "oaap-app-git-oaap"}]


class StaleTest(unittest.TestCase):
    def test_single_to_multi(self):
        self.assertEqual(m.stale_containers(SINGLE, MULTI), ["oaap-app-git"])

    def test_multi_to_single(self):
        stored = {"container": "oaap-app-git-web", "services": MULTI}
        self.assertEqual(m.stale_containers(stored, SINGLE["services"]),
                         ["oaap-app-git-web", "oaap-app-git-oaap"])

    def test_same_shape_names_nothing(self):
        self.assertEqual(m.stale_containers(SINGLE, SINGLE["services"]), [])
        self.assertEqual(m.stale_containers({"container": "oaap-app-git-web", "services": MULTI}, MULTI), [])

    def test_new_instance_and_old_record_without_services(self):
        self.assertEqual(m.stale_containers({}, MULTI), [])
        self.assertEqual(m.stale_containers(None, MULTI), [])
        self.assertEqual(m.stale_containers({"container": "oaap-app-git"}, MULTI), ["oaap-app-git"])

    def test_bait_never_a_foreign_name(self):
        stored = {"container": "oaap-gateway", "services": [{"container": "postgres"}, {"container": ""}]}
        self.assertEqual(m.stale_containers(stored, MULTI), [])

    def test_removed_before_the_new_ones(self):
        src = open(os.path.join(HERE, "..", "platform", "appctl.py"), encoding="utf-8").read()
        body = src[src.index("def recreate_instance_containers"):]
        body = body[:body.index("\ndef ")]
        self.assertLess(body.index("stale_containers(stored, services)"),
                        body.index('["docker", "rm", "-f", s["container"]]'))


if __name__ == "__main__":
    unittest.main()
