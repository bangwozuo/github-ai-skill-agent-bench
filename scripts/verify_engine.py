from __future__ import annotations

import argparse
import json
import py_compile
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    output = Path(args.output).resolve()
    compile_error = ""
    try:
        py_compile.compile(str(root / "src/github_ai_bench/cli.py"), doraise=True)
        compile_status = "pass"
    except py_compile.PyCompileError as exc:
        compile_status = "fail"
        compile_error = str(exc)
    completed = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", str(root / "tests"), "-v"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    tests_status = "pass" if completed.returncode == 0 else "fail"
    passed = compile_status == "pass" and tests_status == "pass"
    receipt = {
        "schema": "GITHUB-AI-ENGINE-VERIFICATION-1.0",
        "checked_at": utc_now(),
        "status": "pass" if passed else "fail",
        "python": sys.version,
        "compile": {"status": compile_status, "error": compile_error},
        "tests": {
            "status": tests_status,
            "exit_code": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        },
        "claim_boundary": "该回执只证明引擎代码与合成测试通过，不证明任何第三方仓库已安装或适用。",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
