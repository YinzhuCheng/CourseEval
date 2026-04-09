import argparse
import io
import json
import sys
import traceback
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import nbformat
from nbclient import NotebookClient
from nbconvert import HTMLExporter


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Execute a notebook and export artifacts.")
    parser.add_argument("--input", required=True, help="Path to the input notebook")
    parser.add_argument("--executed", required=True, help="Path to save the executed notebook")
    parser.add_argument("--html", required=True, help="Path to save the HTML export")
    parser.add_argument("--stdout", required=True, help="Path to save merged stdout streams")
    parser.add_argument("--stderr", required=True, help="Path to save merged stderr streams")
    parser.add_argument("--summary", required=True, help="Path to save structured evaluation summary JSON")
    parser.add_argument("--visible-tests", default="", help="Inline Python source for visible tests")
    parser.add_argument("--hidden-tests", default="", help="Inline Python source for hidden tests")
    parser.add_argument("--execution-weight", default="0", help="Execution score weight")
    parser.add_argument("--visible-weight", default="100", help="Visible test score weight")
    parser.add_argument("--hidden-weight", default="0", help="Hidden test score weight")
    parser.add_argument("--timeout", required=True, type=int, help="Cell execution timeout in seconds")
    return parser.parse_args()


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def extract_streams(notebook) -> tuple[str, str]:
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    for cell in notebook.cells:
        for output in cell.get("outputs", []):
            if output.get("output_type") != "stream":
                continue
            if output.get("name") == "stdout":
                stdout_chunks.append(output.get("text", ""))
            elif output.get("name") == "stderr":
                stderr_chunks.append(output.get("text", ""))
    return "".join(stdout_chunks), "".join(stderr_chunks)


def run_inline_tests(source: str, context: dict) -> tuple[dict, str, str]:
    if not source.strip():
        return {"passed": True, "score": 0.0, "message": "No tests configured."}, "", ""

    stdout_buffer = io.StringIO()
    stderr_buffer = io.StringIO()
    test_globals = {"__builtins__": __builtins__}
    test_locals = dict(context)

    try:
        with redirect_stdout(stdout_buffer), redirect_stderr(stderr_buffer):
            exec(source, test_globals, test_locals)
        result = test_locals.get("RESULT") or test_globals.get("RESULT") or {}
        if not isinstance(result, dict):
            result = {"passed": True, "score": 100.0, "message": str(result)}
        result.setdefault("passed", True)
        result.setdefault("score", 100.0 if result.get("passed") else 0.0)
        result.setdefault("message", "Tests completed.")
    except Exception:
        result = {
            "passed": False,
            "score": 0.0,
            "message": "Test execution raised an exception.",
            "traceback": traceback.format_exc(),
        }
        stderr_buffer.write(result["traceback"])
    return result, stdout_buffer.getvalue(), stderr_buffer.getvalue()


def compute_auto_score(execution_ok: bool, visible_score: float, hidden_score: float, execution_weight: float, visible_weight: float, hidden_weight: float) -> float:
    execution_component = execution_weight if execution_ok else 0.0
    visible_component = (visible_score / 100.0) * visible_weight if visible_weight else 0.0
    hidden_component = (hidden_score / 100.0) * hidden_weight if hidden_weight else 0.0
    return round(execution_component + visible_component + hidden_component, 4)


def main() -> int:
    args = parse_args()
    input_path = Path(args.input)
    executed_path = Path(args.executed)
    html_path = Path(args.html)
    stdout_path = Path(args.stdout)
    stderr_path = Path(args.stderr)
    summary_path = Path(args.summary)

    for path in (executed_path, html_path, stdout_path, stderr_path, summary_path):
        ensure_parent(path)

    notebook = nbformat.read(input_path, as_version=4)

    try:
        client = NotebookClient(
            notebook,
            timeout=args.timeout,
            kernel_name="python3",
            resources={"metadata": {"path": str(input_path.parent)}},
        )
        client.execute()
        execution_error = None
    except Exception:
        execution_error = traceback.format_exc()

    nbformat.write(notebook, executed_path)

    stdout_text, stderr_text = extract_streams(notebook)
    aggregated_stdout = stdout_text
    stderr_content = stderr_text
    if execution_error:
        stderr_content = f"{stderr_content}\n{execution_error}".strip() + "\n"

    try:
        exporter = HTMLExporter()
        html_body, _ = exporter.from_notebook_node(notebook)
        html_path.write_text(html_body, encoding="utf-8")
    except Exception:
        html_error = traceback.format_exc()
        stderr_content = f"{stderr_content}\n{html_error}".strip() + "\n"
        stderr_path.write_text(stderr_content, encoding="utf-8")
        summary_path.write_text(
            json.dumps(
                {
                    "run_success": execution_error is None,
                    "failure_type": "system_error",
                    "message": "HTML export failed.",
                    "visible_score": 0,
                    "hidden_score": 0,
                    "auto_score": 0,
                },
                ensure_ascii=True,
                indent=2,
            ),
            encoding="utf-8",
        )
        return 1

    visible_result, visible_stdout, visible_stderr = run_inline_tests(
        args.visible_tests,
        {
            "notebook": notebook,
            "executed_notebook_path": str(executed_path),
            "html_path": str(html_path),
            "summary": {"run_success": execution_error is None},
        },
    )
    hidden_result, hidden_stdout, hidden_stderr = run_inline_tests(
        args.hidden_tests,
        {
            "notebook": notebook,
            "executed_notebook_path": str(executed_path),
            "html_path": str(html_path),
            "summary": {"run_success": execution_error is None},
        },
    )

    execution_weight = float(args.execution_weight)
    visible_weight = float(args.visible_weight)
    hidden_weight = float(args.hidden_weight)
    visible_score = float(visible_result.get("score", 0.0))
    hidden_score = float(hidden_result.get("score", 0.0))
    run_success = execution_error is None
    auto_score = compute_auto_score(
        run_success,
        visible_score,
        hidden_score,
        execution_weight,
        visible_weight,
        hidden_weight,
    )

    if visible_stdout:
        aggregated_stdout = f"{aggregated_stdout}\n\n=== Visible Tests ===\n{visible_stdout}".strip() + "\n"
    if hidden_stdout:
        aggregated_stdout = f"{aggregated_stdout}\n\n=== Hidden Tests ===\n{hidden_stdout}".strip() + "\n"
    if visible_stderr:
        stderr_content = f"{stderr_content}\n\n=== Visible Test stderr ===\n{visible_stderr}".strip() + "\n"
    if hidden_stderr:
        stderr_content = f"{stderr_content}\n\n=== Hidden Test stderr ===\n{hidden_stderr}".strip() + "\n"

    stdout_path.write_text(aggregated_stdout, encoding="utf-8")
    stderr_path.write_text(stderr_content, encoding="utf-8")

    summary = {
        "run_success": run_success,
        "visible_score": visible_score,
        "hidden_score": hidden_score,
        "auto_score": auto_score,
        "execution_weight": execution_weight,
        "visible_weight": visible_weight,
        "hidden_weight": hidden_weight,
        "visible_passed": bool(visible_result.get("passed", True)),
        "hidden_passed": bool(hidden_result.get("passed", True)),
        "visible_message": visible_result.get("message"),
        "hidden_message": hidden_result.get("message"),
        "message": "Automatic evaluation completed." if run_success else "Notebook execution failed.",
    }
    if execution_error:
        summary["failure_type"] = "answer_error"
    elif not visible_result.get("passed", True) or not hidden_result.get("passed", True):
        summary["failure_type"] = "answer_error"
    summary_path.write_text(json.dumps(summary, ensure_ascii=True, indent=2), encoding="utf-8")

    if execution_error:
        return 1
    if not visible_result.get("passed", True):
        return 1
    if not hidden_result.get("passed", True):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
