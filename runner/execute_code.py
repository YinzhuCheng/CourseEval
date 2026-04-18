import argparse
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


LANGUAGE_ENTRYPOINTS = {
    "python": "main.py",
    "c": "main.c",
    "cpp": "main.cpp",
}
ALLOWED_SUFFIXES = {
    "python": {".py"},
    "c": {".c", ".h"},
    "cpp": {".cpp", ".cc", ".cxx", ".h", ".hpp"},
}
MAX_ZIP_FILES = 30
MAX_ZIP_FILE_BYTES = 512 * 1024


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Execute a code submission against visible and hidden tests.")
    parser.add_argument("--input", required=True, help="Path to submitted source file or zip archive")
    parser.add_argument("--language", required=True, choices=sorted(LANGUAGE_ENTRYPOINTS))
    parser.add_argument("--submission-mode", required=True, choices=["single_file", "zip"])
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


def safe_member_path(member_name: str) -> Path:
    candidate = Path(member_name)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError(f"Unsafe zip member path: {member_name}")
    if any(part.startswith(".") for part in candidate.parts):
        raise ValueError(f"Hidden files and directories are not allowed: {member_name}")
    return candidate


def prepare_workspace(input_path: Path, language: str, submission_mode: str, work_dir: Path) -> Path:
    suffixes = ALLOWED_SUFFIXES[language]
    entrypoint = LANGUAGE_ENTRYPOINTS[language]
    work_dir.mkdir(parents=True, exist_ok=True)

    if submission_mode == "single_file":
        target = work_dir / entrypoint
        if input_path.suffix.lower() not in suffixes:
            raise ValueError(f"Invalid file extension for {language}: {input_path.suffix}")
        shutil.copyfile(input_path, target)
        return target

    if not zipfile.is_zipfile(input_path):
        raise ValueError("Multi-file submissions must be zip archives.")

    extracted_files = 0
    with zipfile.ZipFile(input_path) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            relative_path = safe_member_path(info.filename)
            if relative_path.suffix.lower() not in suffixes:
                raise ValueError(f"Unsupported file type in zip: {info.filename}")
            if info.file_size > MAX_ZIP_FILE_BYTES:
                raise ValueError(f"File is too large in zip: {info.filename}")
            extracted_files += 1
            if extracted_files > MAX_ZIP_FILES:
                raise ValueError(f"Zip contains more than {MAX_ZIP_FILES} files.")
            target_path = work_dir / relative_path
            target_path.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target_path.open("wb") as target:
                shutil.copyfileobj(source, target)

    entrypoint_path = work_dir / entrypoint
    if not entrypoint_path.exists():
        raise ValueError(f"Multi-file submissions must include {entrypoint}.")
    return entrypoint_path


def build_command(language: str, work_dir: Path) -> tuple[list[str], Path | None]:
    if language == "python":
        return [sys.executable, str(work_dir / "main.py")], None

    binary_path = work_dir / "main"
    if language == "c":
        sources = sorted(str(path) for path in work_dir.rglob("*.c"))
        if not sources:
            raise ValueError("No C source files found.")
        return ["gcc", *sources, "-std=c11", "-O2", "-Wall", "-Wextra", "-lm", "-o", str(binary_path)], binary_path

    sources = [
        *sorted(str(path) for path in work_dir.rglob("*.cpp")),
        *sorted(str(path) for path in work_dir.rglob("*.cc")),
        *sorted(str(path) for path in work_dir.rglob("*.cxx")),
    ]
    if not sources:
        raise ValueError("No C++ source files found.")
    return ["g++", *sources, "-std=c++17", "-O2", "-Wall", "-Wextra", "-o", str(binary_path)], binary_path


def compile_submission(language: str, work_dir: Path, timeout: int) -> dict:
    compile_command, binary_path = build_command(language, work_dir)
    if binary_path is None:
        return {"success": True, "command": compile_command, "stdout": "", "stderr": ""}
    try:
        completed = subprocess.run(
            compile_command,
            text=True,
            capture_output=True,
            timeout=max(10, min(timeout, 30)),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "command": compile_command,
            "stdout": "",
            "stderr": "Compilation timed out.\n",
            "returncode": 124,
        }
    return {
        "success": completed.returncode == 0,
        "command": compile_command,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "returncode": completed.returncode,
        "binary": str(binary_path),
    }


def runtime_command(language: str, work_dir: Path) -> list[str]:
    if language == "python":
        return [sys.executable, str(work_dir / "main.py")]
    return [str(work_dir / "main")]


def run_test_case(language: str, work_dir: Path, test_case: dict, timeout: int) -> dict:
    expected_output = expected_output_for_case(test_case)
    score = points_for_case(test_case)
    input_text = test_case.get("input", "")

    try:
        completed = subprocess.run(
            runtime_command(language, work_dir),
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
        lines.extend(["Expected output:", result.get("expected_output", "") or "(empty)"])
    if result.get("stderr"):
        lines.extend(["stderr:", result["stderr"]])
    return "\n".join(lines).strip()


def write_failure(stdout_path: Path, stderr_path: Path, summary_path: Path, message: str, failure_type: str, *, stderr: str = "") -> int:
    stdout_path.write_text("", encoding="utf-8")
    stderr_path.write_text((stderr or message) + ("\n" if not (stderr or message).endswith("\n") else ""), encoding="utf-8")
    summary = {
        "run_success": False,
        "compile_success": failure_type != "compile_error",
        "visible_score": 0,
        "hidden_score": 0,
        "auto_score": 0,
        "message": message,
        "failure_type": failure_type,
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=True, indent=2), encoding="utf-8")
    return 1


def main() -> int:
    args = parse_args()
    input_path = Path(args.input)
    stdout_path = Path(args.stdout)
    stderr_path = Path(args.stderr)
    summary_path = Path(args.summary)
    work_dir = summary_path.parent / "work"

    for path in (stdout_path, stderr_path, summary_path):
        ensure_parent(path)

    try:
        visible_tests = json.loads(args.visible_tests)
        hidden_tests = json.loads(args.hidden_tests)
        prepare_workspace(input_path, args.language, args.submission_mode, work_dir)
        compile_result = compile_submission(args.language, work_dir, args.timeout)
    except Exception as exc:
        return write_failure(stdout_path, stderr_path, summary_path, str(exc), "system_error")

    if not compile_result["success"]:
        message = "Compilation failed."
        return write_failure(
            stdout_path,
            stderr_path,
            summary_path,
            message,
            "compile_error",
            stderr=compile_result.get("stderr", ""),
        )

    visible_results = [run_test_case(args.language, work_dir, test_case, args.timeout) for test_case in visible_tests]
    hidden_results = [run_test_case(args.language, work_dir, test_case, args.timeout) for test_case in hidden_tests]

    combined_stdout_sections: list[str] = []
    combined_stderr_sections: list[str] = []

    if visible_results:
        visible_blocks = [format_case_block(result, include_expectation=True) for result in visible_results]
        combined_stdout_sections.append("=== Visible Tests ===\n" + "\n\n".join(visible_blocks))
    if hidden_results:
        hidden_blocks = [format_case_block(result, include_expectation=False) for result in hidden_results]
        combined_stdout_sections.append("=== Hidden Tests ===\n" + "\n\n".join(hidden_blocks))

    if compile_result.get("stderr"):
        combined_stderr_sections.append("=== Compile stderr ===\n" + compile_result["stderr"])
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
        "language": args.language,
        "submission_mode": args.submission_mode,
        "compile_success": True,
        "run_success": run_success,
        "visible_score": visible_score,
        "hidden_score": hidden_score,
        "auto_score": auto_score,
        "visible_message": f"{sum(1 for item in visible_results if item['passed'])}/{len(visible_results)} visible tests passed." if visible_results else "No visible tests configured.",
        "hidden_message": f"{sum(1 for item in hidden_results if item['passed'])}/{len(hidden_results)} hidden tests passed." if hidden_results else "No hidden tests configured.",
        "message": "Code evaluation completed." if run_success and visible_passed and hidden_passed else "Code evaluation found failing tests.",
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
