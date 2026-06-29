#!/usr/bin/env python3
"""
afterFileEdit hook: runs py_compile on edited .py files and returns
a context note if syntax errors are found.
Input: JSON on stdin with {"path": "...", "content": "..."}
Output: JSON on stdout with {"additional_context": "..."} or {}
"""
import json
import py_compile
import sys
import tempfile
import os


def main() -> None:
    try:
        data = json.loads(sys.stdin.read())
    except Exception:
        print("{}")
        return

    path: str = data.get("path", "")
    content: str = data.get("content", "")

    if not path.endswith(".py"):
        print("{}")
        return

    # Write content to temp file and compile
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", encoding="utf-8", delete=False
        ) as tmp:
            tmp.write(content)
            tmp_path = tmp.name

        py_compile.compile(tmp_path, doraise=True)
        os.unlink(tmp_path)
        # Syntax is clean — no additional context needed
        print("{}")
    except py_compile.PyCompileError as e:
        os.unlink(tmp_path)
        error_msg = str(e).replace(tmp_path, path)
        result = {
            "additional_context": (
                f"⚠️ SYNTAX ERROR in {os.path.basename(path)}: {error_msg}\n"
                "Fix the syntax error before proceeding. "
                "Run `python -m py_compile bot.py` to verify."
            )
        }
        print(json.dumps(result, ensure_ascii=False))
    except Exception as e:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        print("{}")


if __name__ == "__main__":
    main()
