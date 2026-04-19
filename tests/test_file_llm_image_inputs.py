import unittest
from pathlib import Path
from unittest import mock

from app.services.llm import ImageInput
from app.services.submissions import _image_inputs_from_png_paths


class FileLlmImageInputsTests(unittest.TestCase):
    def test_image_inputs_from_png_paths(self) -> None:
        with mock.patch.object(Path, "read_bytes", return_value=b"\x89PNG\r\n\x1a\nfake"):
            paths = [Path("/tmp/page-1.png")]
            images = _image_inputs_from_png_paths(paths)
        self.assertEqual(len(images), 1)
        self.assertIsInstance(images[0], ImageInput)
        self.assertEqual(images[0].mime_type, "image/png")
        self.assertEqual(images[0].data, b"\x89PNG\r\n\x1a\nfake")


if __name__ == "__main__":
    unittest.main()
