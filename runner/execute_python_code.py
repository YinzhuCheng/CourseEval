import argparse
import json
import subprocess
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Execute a Python submission against visible and hidden tests.")
    parser.add_argument("--input", required=True, help="Path to the submitted Python file")
    parser.add_argument("--stdout", required=True, help="Path to save combined stdout")
    parser.add_argument("--stderr", required=True, help="Path to save combined stderr")
    parser.add_argument("--summary", required=True, help="Path to save structured evaluation summary JSON")
    parser.add_argument("--visible-tests", required=True, help="JSON list of visible test cases")
    parser.add_argument("--hidden-tests", required=True, help="JSON list of hidden test cases")
    parser.add_argument("--timeout", required=True, type=int, help="Timeout per test case in seconds")
    return parser.parse_args()


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def normalize_output(value: str) -> str:
    return value.replace("\r\n", "\n").strip()


def expected_output_for_case(test_case: dict) -> str:
    return test_case.get("expected_output", test_case.get("output", ""))


def points_for_case(test_case: dict) -> float:
    raw_points = test_case.get("points")
    try:
        return float(raw_points) if raw_points not in (None, "") else 20.0
    except (TypeError, ValueError):
        return 20.0


def run_test_case(script_path: Path, test_case: dict, timeout: int) -> dict:
    expected_output = expected_output_for_case(test_case)
    score = points_for_case(test_case)
    input_text = test_case.get("input", "")
    command = [sys.executable, str(script_path)]

    try:
        completed = subprocess.run(
            command,
            input=input_text,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "name": test_case.get("name") or "test",
            "passed": False,
            "score": 0,
            "points": score,
            "message": f"Timed out after {timeout} seconds.",
            "stdout": "",
            "stderr": f"Timed out after {timeout} seconds.\n",
            "returncode": 124,
            "input": input_text,
            "expected_output": expected_output,
            "actual_output": "",
        }

    actual_output = completed.stdout
    passed = completed.returncode == 0 and normalize_output(actual_output) == normalize_output(expected_output)
    if completed.returncode != 0:
        message = f"Program exited with code {completed.returncode}."
    elif passed:
        message = "Output matched expected result."
    else:
        message = "Output did not match expected result."

    return {
        "name": test_case.get("name") or "test",
        "passed": passed,
        "score": score if passed else 0,
        "points": score,
        "message": message,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "returncode": completed.returncode,
        "input": input_text,
        "expected_output": expected_output,
        "actual_output": actual_output,
    }


def format_case_block(result: dict, *, include_expectation: bool) -> str:
    lines = [
        f"[{result['name']}] {result['message']}",
        "Input:",
        result.get("input", "") or "(empty)",
        "stdout:",
        result.get("actual_output", "") or "(empty)",
    ]
    if include_expectation:
        lines.extend(
            [
                "Expected output:",
                result.get("expected_output", "") or "(empty)",
            ]
        )
    if result.get("stderr"):
        lines.extend(["stderr:", result["stderr"]])
    return "\n".join(lines).strip()


def main() -> int:
    args = parse_args()
    submission_path = Path(args.input)
    stdout_path = Path(args.stdout)
    stderr_path = Path(args.stderr)
    summary_path = Path(args.summary)

    for path in (stdout_path, stderr_path, summary_path):
        ensure_parent(path)

    visible_tests = json.loads(args.visible_tests)
    hidden_tests = json.loads(args.hidden_tests)

    visible_results = [run_test_case(submission_path, test_case, args.timeout) for test_case in visible_tests]
    hidden_results = [run_test_case(submission_path, test_case, args.timeout) for test_case in hidden_tests]

    combined_stdout_sections: list[str] = []
    combined_stderr_sections: list[str] = []

    if visible_results:
        visible_blocks = [format_case_block(result, include_expectation=True) for result in visible_results]
        combined_stdout_sections.append("=== Visible Tests ===\n" + "\n\n".join(visible_blocks))

    if hidden_results:
        hidden_blocks = [format_case_block(result, include_expectation=False) for result in hidden_results]
        combined_stdout_sections.append("=== Hidden Tests ===\n" + "\n\n".join(hidden_blocks))

    visible_stderr = [result["stderr"] for result in visible_results if result.get("stderr")]
    hidden_stderr = [result["stderr"] for result in hidden_results if result.get("stderr")]
    if visible_stderr:
        combined_stderr_sections.append("=== Visible Test stderr ===\n" + "\n\n".join(visible_stderr))
    if hidden_stderr:
        combined_stderr_sections.append("=== Hidden Test stderr ===\n" + "\n\n".join(hidden_stderr))

    stdout_path.write_text("\n\n".join(combined_stdout_sections).strip() + ("\n" if combined_stdout_sections else ""), encoding="utf-8")
    stderr_path.write_text("\n\n".join(combined_stderr_sections).strip() + ("\n" if combined_stderr_sections else ""), encoding="utf-8")

    visible_score = sum(float(result["score"]) for result in visible_results)
    hidden_score = sum(float(result["score"]) for result in hidden_results)
    auto_score = round(visible_score + hidden_score, 4)
    run_success = all(result["returncode"] == 0 for result in visible_results + hidden_results)
    visible_passed = all(result["passed"] for result in visible_results) if visible_results else True
    hidden_passed = all(result["passed"] for result in hidden_results) if hidden_results else True

    summary = {
        "run_success": run_success,
        "visible_score": visible_score,
        "hidden_score": hidden_score,
        "auto_score": auto_score,
        "visible_message": f"{sum(1 for item in visible_results if item['passed'])}/{len(visible_results)} visible tests passed." if visible_results else "No visible tests configured.",
        "hidden_message": f"{sum(1 for item in hidden_results if item['passed'])}/{len(hidden_results)} hidden tests passed." if hidden_results else "No hidden tests configured.",
        "message": "Python code evaluation completed." if run_success and visible_passed and hidden_passed else "Python code evaluation found failing tests.",
        "visible_cases": [
            {
                "name": item["name"],
                "passed": item["passed"],
                "points": item["points"],
                "input": item["input"],
                "expected_output": item["expected_output"],
                "actual_output": item["actual_output"],
                "message": item["message"],
            }
            for item in visible_results
        ],
        "hidden_cases": [
            {
                "name": item["name"],
                "passed": item["passed"],
                "points": item["points"],
                "input": item["input"],
                "expected_output": item["expected_output"],
                "actual_output": item["actual_output"],
                "message": item["message"],
            }
            for item in hidden_results
        ],
    }
    if not run_success or not visible_passed or not hidden_passed:
        summary["failure_type"] = "answer_error"

    summary_path.write_text(json.dumps(summary, ensure_ascii=True, indent=2), encoding="utf-8")
    return 0 if run_success and visible_passed and hidden_passed else 1


if __name__ == "__main__":
    sys.exit(main())
