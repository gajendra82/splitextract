"""Keep default discovery off the live recovery path.

``load_dotenv()`` does not override a variable that is already set. This module
sorts first, so a normal ``unittest discover`` does not inherit
PHASE2_RECOVERY_ENABLED from the developer environment. Live photo, scan, and
provider tests stay opt-in through RUN_LIVE_EXTRACTION=1.
"""

import os
import unittest

_LIVE = os.environ.get("RUN_LIVE_EXTRACTION", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
if not _LIVE:
    os.environ["PHASE2_RECOVERY_ENABLED"] = "false"


class TestUnitDiscoveryEnv(unittest.TestCase):
    def test_unit_discovery_does_not_enable_live_recovery(self):
        if _LIVE:
            self.skipTest("RUN_LIVE_EXTRACTION is set")
        self.assertEqual(os.environ.get("PHASE2_RECOVERY_ENABLED"), "false")
