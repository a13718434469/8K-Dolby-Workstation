"""Reject known mismatched RPU mappings; profile-8 headers alone are insufficient."""
import json
import unittest

import pipeline
import test_delivery as fixtures


@unittest.skipUnless(fixtures.HAS_DOVI, "dovi_tool and FFmpeg dovi_rpu required")
class RpuCompatibilityTests(unittest.TestCase):
    def test_known_identity_pq_rpu_is_not_accepted_as_hlg_profile84_metadata(self):
        helper = fixtures.DeliveryTests()
        helper.setUp()
        self.addCleanup(helper.tearDown)
        helper.make_source(transfer="arib-std-b67")
        cfg = helper.root / "pq-rpu.json"
        cfg.write_text(json.dumps({"cm_version": "V40", "length": 6}), encoding="utf-8")
        rpu = helper.root / "pq-identity.bin"
        fixtures.command([fixtures.DOVI, "generate", "-j", cfg, "-p", "8.1", "-o", rpu])
        # A profile-8 RPU header and a matching frame count cannot justify
        # treating PQ identity reshaping as a valid HLG-to-PQ mapping.
        with self.assertRaises(pipeline.PipelineError):
            pipeline.Pipeline(helper.config(color_mode="dv84", rpu=str(rpu), delivery="bilibili")).run()
        self.assertFalse(helper.output.exists())
