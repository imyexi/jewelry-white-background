from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image


SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "preprocess_reference_images.py"
)
SPEC = importlib.util.spec_from_file_location("preprocess_reference_images", SCRIPT_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class PreprocessReferenceImagesTests(unittest.TestCase):
    def test_heif_uses_external_converter_when_pillow_plugin_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "front.heic"
            source.write_bytes(b"fake-heic")

            def fake_convert(command, **kwargs):
                Image.new("RGB", (20, 30), "red").save(command[-1], format="JPEG")
                return subprocess.CompletedProcess(command, 0, "", "")

            with (
                mock.patch.object(MODULE, "PILLOW_HEIF_AVAILABLE", False),
                mock.patch.object(MODULE, "HEIF_CONVERTER", "heif-convert"),
                mock.patch.object(MODULE.subprocess, "run", side_effect=fake_convert),
            ):
                result = MODULE.preprocess_one(
                    MODULE.InputItem(str(source), "SY1537"),
                    root / "out",
                    1,
                    1280,
                    84,
                )

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["heif_decoder"], "heif-convert")
            self.assertTrue(Path(result["preprocessed_path"]).is_file())


if __name__ == "__main__":
    unittest.main()
