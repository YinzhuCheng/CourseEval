import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


RUNNER = Path(__file__).resolve().parents[1] / "runner" / "execute_code.py"


class CodeRunnerTests(unittest.TestCase):
    def _run_runner(self, *, source_name: str, source: str, language: str) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_path = root / source_name
            source_path.write_text(source, encoding="utf-8")
            summary_path = root / "summary.json"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(RUNNER),
                    "--input",
                    str(source_path),
                    "--language",
                    language,
                    "--submission-mode",
                    "single_file",
                    "--stdout",
                    str(root / "stdout.txt"),
                    "--stderr",
                    str(root / "stderr.txt"),
                    "--summary",
                    str(summary_path),
                    "--visible-tests",
                    json.dumps([{"input": "2 3", "expected_output": "5", "points": 20}]),
                    "--hidden-tests",
                    "[]",
                    "--timeout",
                    "5",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            return json.loads(summary_path.read_text(encoding="utf-8"))

    def test_c_runner_executes_visible_test(self) -> None:
        summary = self._run_runner(
            source_name="main.c",
            language="c",
            source=(
                "#include <stdio.h>\n"
                "int main(void) { int a, b; if (scanf(\"%d %d\", &a, &b) != 2) return 0; printf(\"%d\\n\", a + b); return 0; }\n"
            ),
        )

        self.assertEqual(summary["auto_score"], 20)
        self.assertTrue(summary["compile_success"])

    def test_python_runner_executes_visible_test(self) -> None:
        summary = self._run_runner(
            source_name="main.py",
            language="python",
            source="a, b = map(int, input().split())\nprint(a + b)\n",
        )

        self.assertEqual(summary["auto_score"], 20)
        self.assertTrue(summary["compile_success"])

    def test_cpp_runner_executes_visible_test(self) -> None:
        summary = self._run_runner(
            source_name="main.cpp",
            language="cpp",
            source=(
                "#include <iostream>\n"
                "int main() { int a, b; if (!(std::cin >> a >> b)) return 0; std::cout << a + b << '\\n'; return 0; }\n"
            ),
        )

        self.assertEqual(summary["auto_score"], 20)
        self.assertTrue(summary["compile_success"])


if __name__ == "__main__":
    unittest.main()
