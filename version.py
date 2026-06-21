"""version.py - version, build commit, and the --version banner.

Library module, not a CLI. From source it reads the version from pyproject.toml
and the short commit from live git; a frozen binary reads both from _build.py,
which build.py stamps at compile time (git is unavailable to an installed binary).

  import version; print(version.banner())
"""
import subprocess
import sys
from pathlib import Path

DESCRIPTION = "screen Binance USD-M futures for fresh same-color candle impulses"

# Nuitka does not set sys.frozen; it injects __compiled__ into __main__.
FROZEN = getattr(sys.modules.get("__main__"), "__compiled__", None) is not None


def info() -> tuple[str, str]:
    if FROZEN:
        try:
            import _build
            return _build.VERSION, _build.COMMIT
        except ImportError:
            return "unknown", "unknown"
    root = Path(__file__).resolve().parent
    return _pyproject_version(root), _git_short(root)


def banner() -> str:
    ver, commit = info()
    return f"bks {ver} ({commit}) - {DESCRIPTION}"


def _pyproject_version(root: Path) -> str:
    import tomllib
    try:
        with open(root / "pyproject.toml", "rb") as f:
            return tomllib.load(f)["project"]["version"]
    except (OSError, KeyError):
        return "0.0.0"


def _git_short(root: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=2,
        )
        return out.stdout.strip() if out.returncode == 0 else "unknown"
    except Exception:
        return "unknown"
