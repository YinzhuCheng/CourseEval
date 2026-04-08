import argparse
import sys
import traceback
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


def main() -> int:
    args = parse_args()
    input_path = Path(args.input)
    executed_path = Path(args.executed)
    html_path = Path(args.html)
    stdout_path = Path(args.stdout)
    stderr_path = Path(args.stderr)

    for path in (executed_path, html_path, stdout_path, stderr_path):
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
    stdout_path.write_text(stdout_text, encoding="utf-8")
    stderr_content = stderr_text
    if execution_error:
        stderr_content = f"{stderr_content}\n{execution_error}".strip() + "\n"
    stderr_path.write_text(stderr_content, encoding="utf-8")

    try:
        exporter = HTMLExporter()
        html_body, _ = exporter.from_notebook_node(notebook)
        html_path.write_text(html_body, encoding="utf-8")
    except Exception:
        html_error = traceback.format_exc()
        stderr_path.write_text(f"{stderr_content}\n{html_error}".strip() + "\n", encoding="utf-8")
        return 1

    if execution_error:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
