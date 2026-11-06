"""Weight integrity and numerical agreement with the pinned NovaSR v1 model."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import music_sr_backend as backend


class ModelIntegrityTest(unittest.TestCase):
    def test_untrusted_download_never_replaces_local_file(self):
        import io
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, "weights.bin")
            path.write_bytes(b"old file")
            with patch.object(backend, "urlopen", return_value=io.BytesIO(b"invalid download")):
                with self.assertRaises(ValueError):
                    backend.install_model(path)
            self.assertEqual(path.read_bytes(), b"old file")
            with self.assertRaises(ValueError):
                backend.load_model(path)

    @unittest.skipUnless(backend.model_ready(), "requires optional NovaSR v1 weights")
    def test_fp32_upstream_reference_including_both_boundaries(self):
        # From the author's unmodified SynthesizerTrn.forward, Git revision
        # 5e3cb7946c70a537222dc26a24da460aff8b923b, v1, eval(), CPU/FP32.
        index = np.arange(313, dtype=np.float64)
        audio = (.18 * np.sin(.117 * index) + .09 * np.cos(.331 * index)
                 + .12 * np.sin(.002 * index ** 1.7)).astype(np.float32)
        positions = [0, 1, 2, 8, 32, 96, 111, 235, 292, 392, 511, 648, 845, 937, 938]
        reference = [.091040246, .095779553, .102224410, .114711307, .100986384,
                     -.057934184, .016557880, .091600411, -.223404929, .262524843,
                     .230686411, -.070149951, .131490946, -.275639802, -.275387317]
        actual = backend.upscale(backend.load_model(), audio)
        self.assertEqual(actual.shape, (939,))
        np.testing.assert_allclose(actual[positions], reference, rtol=0, atol=5e-5)


if __name__ == "__main__":
    unittest.main()
