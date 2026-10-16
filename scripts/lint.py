"""Offline syntax/hygiene lint. Ruff is an optional additional check."""
import ast
from pathlib import Path
import sys
import tokenize

ROOT = Path(__file__).resolve().parents[1]
EXCLUDE = {".git", ".venv", ".tooling", "__pycache__", "results", ".ruff_cache"}


def main():
    errors = []
    count = 0
    for path in sorted(ROOT.rglob("*.py")):
        if EXCLUDE.intersection(path.relative_to(ROOT).parts):
            continue
        count += 1
        try:
            with tokenize.open(path) as f:
                source = f.read()
            ast.parse(source, filename=str(path))
            for n, line in enumerate(source.splitlines(), 1):
                if line.rstrip() != line:
                    errors.append(f"{path.relative_to(ROOT)}:{n}: trailing whitespace")
                if "\t" in line:
                    errors.append(f"{path.relative_to(ROOT)}:{n}: tab character")
                if line.startswith(("<<<<<<< ", "=======", ">>>>>>> ")):
                    errors.append(f"{path.relative_to(ROOT)}:{n}: conflict marker")
        except (SyntaxError, UnicodeError) as exc:
            errors.append(str(exc))
    print("\n".join(errors) if errors else f"Syntax/hygiene lint passed: {count} Python files")
    return bool(errors)


if __name__ == "__main__":
    sys.exit(main())
