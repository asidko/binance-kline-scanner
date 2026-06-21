#!/usr/bin/env python3
"""build.py - compile a portable single-file binary with Nuitka.

Run via `uv run python build.py`. Build flags live next to the code in
`scanner.py` (nuitka-project comments); this script only invokes Nuitka and
names the artifact per OS/arch. CI calls this exact command on each runner.
"""
import os
import platform
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENTRY = ROOT / "scanner.py"
DIST = ROOT / "dist"

_OS = {"Linux": "linux", "Darwin": "macos", "Windows": "windows"}
_ARCH = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "arm64", "arm64": "arm64"}


def _target() -> str:
    # Termux/Android is bionic, not glibc - name it apart so it never collides with the linux build
    osname = "android" if "com.termux" in os.environ.get("PREFIX", "") else _OS.get(platform.system(), platform.system().lower())
    arch = _ARCH.get(platform.machine().lower(), platform.machine().lower())
    return f"bks-{osname}-{arch}"


def _stamp_build(ver: str, commit: str) -> None:
    """Freeze version + commit into _build.py; the binary has no git/pyproject."""
    (ROOT / "_build.py").write_text(f'VERSION = "{ver}"\nCOMMIT = "{commit}"\n')
    print(f"stamped build: {ver} ({commit})")


def main() -> int:
    sys.path.insert(0, str(ROOT))
    import version
    ver, commit = version.info()  # source-mode here, matches what source reports
    _stamp_build(ver, commit)
    cmd = [
        sys.executable, "-m", "nuitka",
        # _build is imported only when FROZEN; bundle it explicitly.
        "--include-module=_build",
        # feeds the {VERSION} token in --onefile-tempdir-spec so the onefile
        # unpacks to a stable per-version cache dir (extract once, reuse).
        f"--product-version={ver}",
        str(ENTRY),
    ]
    print("building:", " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=str(ROOT))

    built = DIST / "bks"  # must match --output-filename in scanner.py's nuitka-project comments
    if not built.exists():
        print(f"error: expected artifact missing at {built}", file=sys.stderr)
        return 1
    final = DIST / _target()
    built.replace(final)
    final.chmod(0o755)
    print("artifact:", final)
    return 0


if __name__ == "__main__":
    sys.exit(main())
