#!/bin/sh
# install.sh - fetch a bks release binary for this OS/arch (latest by default).
# Termux (Android, bionic): arm64 has a prebuilt binary; other arches install from source.
#   curl -fsSL https://raw.githubusercontent.com/asidko/binance-kline-scanner/main/install.sh | sh
#   curl -fsSL .../install.sh | sh -s -- --tag v1.0.0     # pin a version
#   curl -fsSL .../install.sh | sh -s -- --remove
set -eu

REPO="asidko/binance-kline-scanner"
BIN="bks"
INSTALL_DIR="${BKS_INSTALL_DIR:-$HOME/.local/bin}"
TAG=""
OS=$(uname -s)
TERMUX_LIB="${PREFIX:-}/share/bks"

is_termux() {
    case "${PREFIX:-}" in *com.termux*) return 0 ;; esac
    [ -n "${TERMUX_VERSION:-}" ]
}

# Termux is Android/bionic - the glibc release binaries can't run there. Install from source instead
# (stdlib only) and drop a shim that runs it with Termux's Python.
install_termux() {
    command -v python3 >/dev/null 2>&1 || { echo "python3 missing - run: pkg install python" >&2; exit 1; }
    ref="${TAG:-main}"
    raw="https://raw.githubusercontent.com/${REPO}/${ref}"
    bindir="${BKS_INSTALL_DIR:-$PREFIX/bin}"
    mkdir -p "$bindir" "$TERMUX_LIB"
    echo "Termux: installing bks from source (${ref})"
    for f in scanner.py klines_seq_detector.py version.py pyproject.toml scan_symbols.txt; do
        curl -fSL "$raw/$f" -o "$TERMUX_LIB/$f"
    done
    printf '#!%s/bin/sh\nexec python3 "%s/scanner.py" "$@"\n' "$PREFIX" "$TERMUX_LIB" > "$bindir/$BIN"
    chmod 755 "$bindir/$BIN"
    echo "installed $bindir/$BIN"
    "$bindir/$BIN" --version 2>/dev/null || true
    echo "done. run: $BIN --help"
    exit 0
}

detect_target() {
    arch=$(uname -m)
    if is_termux; then
        os=android
    else
        case "$OS" in
            Linux) os=linux ;;
            Darwin) os=macos ;;
            *) echo "unsupported OS: $OS" >&2; exit 1 ;;
        esac
    fi
    case "$arch" in
        x86_64|amd64) arch=x86_64 ;;
        aarch64|arm64) arch=arm64 ;;
        *) echo "unsupported arch: $arch" >&2; exit 1 ;;
    esac
    echo "${os}-${arch}"
}

do_remove() {
    if is_termux; then
        rm -f "${BKS_INSTALL_DIR:-$PREFIX/bin}/$BIN"
        rm -rf "$TERMUX_LIB"
        echo "removed $BIN (Termux source install)"
        exit 0
    fi
    if [ -f "$INSTALL_DIR/$BIN" ]; then
        rm -f "$INSTALL_DIR/$BIN"
        echo "removed $INSTALL_DIR/$BIN"
    else
        echo "$BIN not installed in $INSTALL_DIR"
    fi
    exit 0
}

while [ $# -gt 0 ]; do
    case "$1" in
        --remove|remove|uninstall) do_remove ;;
        --tag) [ $# -ge 2 ] || { echo "--tag needs a value" >&2; exit 2; }; TAG="$2"; shift 2 ;;
        --tag=*) TAG="${1#--tag=}"; shift ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

command -v curl >/dev/null 2>&1 || { echo "curl is required" >&2; exit 1; }

# Termux arm64 has a prebuilt binary (built under emulation in CI); other Termux arches build from source
if is_termux; then
    INSTALL_DIR="${BKS_INSTALL_DIR:-$PREFIX/bin}"
    case "$(uname -m)" in
        aarch64|arm64) ;;
        *) install_termux ;;
    esac
fi

target=$(detect_target)
if [ "$target" = "macos-x86_64" ]; then
    echo "no prebuilt binary for Intel macOS - build from source: https://github.com/${REPO}#develop--build-from-source" >&2
    exit 1
fi
if [ -n "$TAG" ]; then
    base="https://github.com/${REPO}/releases/download/${TAG}"
else
    base="https://github.com/${REPO}/releases/latest/download"
fi

asset="${BIN}-${target}"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

echo "downloading ${asset} (${TAG:-latest})"
curl -fSL "$base/$asset" -o "$tmp/$asset"
curl -fSL "$base/SHA256SUMS" -o "$tmp/SHA256SUMS"

# verify the download against the release checksum before trusting the binary
want=$(awk -v f="$asset" '$2 == f {print $1}' "$tmp/SHA256SUMS")
[ -n "$want" ] || { echo "no checksum for $asset in SHA256SUMS" >&2; exit 1; }
if command -v sha256sum >/dev/null 2>&1; then
    got=$(sha256sum "$tmp/$asset" | awk '{print $1}')
else
    got=$(shasum -a 256 "$tmp/$asset" | awk '{print $1}')
fi
[ "$want" = "$got" ] || { echo "checksum mismatch for $asset" >&2; exit 1; }
echo "checksum ok"

mkdir -p "$INSTALL_DIR"
mv "$tmp/$asset" "$INSTALL_DIR/$BIN"
chmod 755 "$INSTALL_DIR/$BIN"
# macOS: strip the Gatekeeper quarantine flag so the binary runs without a prompt
# (matters for browser-downloaded binaries; a no-op for plain curl downloads)
if [ "$OS" = "Darwin" ]; then
    xattr -d com.apple.quarantine "$INSTALL_DIR/$BIN" 2>/dev/null || true
fi
echo "installed $INSTALL_DIR/$BIN"

case ":$PATH:" in
    *":$INSTALL_DIR:"*) ;;
    *) echo "warning: $INSTALL_DIR is not in PATH - add it to your shell profile" ;;
esac

echo "running first-time setup (unpacks the binary, may take a moment)..."
"$INSTALL_DIR/$BIN" --version >/dev/null 2>&1 || true
echo "done. run: $BIN --help"
