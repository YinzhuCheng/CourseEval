import json
import tempfile
import unittest
from pathlib import Path

from app.services.notebook_multimodal import sanitize_notebook_for_llm


def _write_nb(path: Path, cells: list) -> None:
    nb = {"nbformat": 4, "nbformat_minor": 5, "metadata": {}, "cells": cells}
    path.write_text(json.dumps(nb), encoding="utf-8")


class NotebookMultimodalTests(unittest.TestCase):
    def test_output_image_png_becomes_placeholder(self) -> None:
        png_b64 = (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
        )
        cells = [
            {
                "id": "c1",
                "cell_type": "code",
                "execution_count": 1,
                "metadata": {},
                "outputs": [
                    {
                        "output_type": "display_data",
                        "data": {"image/png": png_b64, "text/plain": ["<Figure>"]},
                        "metadata": {},
                    }
                ],
                "source": "plot()",
            }
        ]
        with tempfile.NamedTemporaryFile(suffix=".ipynb", delete=False) as f:
            path = Path(f.name)
        try:
            _write_nb(path, cells)
            result = sanitize_notebook_for_llm(path, require_outputs=False)
            self.assertIn("[[IMAGE:IMG_0001|mime=image/png]]", result.text)
            self.assertEqual(len(result.images), 1)
            self.assertEqual(result.images[0].mime_type, "image/png")
            self.assertGreater(len(result.images[0].data), 10)
        finally:
            path.unlink(missing_ok=True)

    def test_data_uri_in_markdown_source(self) -> None:
        png_b64 = (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
        )
        uri = f"data:image/png;base64,{png_b64}"
        cells = [
            {
                "id": "m1",
                "cell_type": "markdown",
                "metadata": {},
                "source": f"Here: {uri}",
            }
        ]
        with tempfile.NamedTemporaryFile(suffix=".ipynb", delete=False) as f:
            path = Path(f.name)
        try:
            _write_nb(path, cells)
            result = sanitize_notebook_for_llm(path, require_outputs=False)
            self.assertIn("[[IMAGE:IMG_0001|mime=image/png]]", result.text)
            self.assertNotIn("data:image", result.text)
            self.assertEqual(len(result.images), 1)
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
