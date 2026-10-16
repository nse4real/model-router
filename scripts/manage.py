"""Portable Makefile entry points; no shell expressions."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else "test"
    commands = {
        "install": [sys.executable, "-m", "pip", "install", "-e", ".[dev]"],
        "test": [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        "lint": [sys.executable, "scripts/lint.py"],
        "up": ["docker", "compose", "up", "--build", "-d"],
        "down": ["docker", "compose", "down"],
        "benchmark-mock": [sys.executable, "-m", "benchmark.run", "--split", "dev"],
        "load-test-mock": [sys.executable, "-m", "benchmark.load_test"],
    }
    if target not in commands:
        raise SystemExit("Unknown target: " + target)
    raise SystemExit(subprocess.call(commands[target], cwd=ROOT))


if __name__ == "__main__":
    main()
