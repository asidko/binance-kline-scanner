#!/bin/sh
# install.sh - fetch a bks release binary for this OS/arch (latest by default).
#   curl -fsSL https://raw.githubusercontent.com/asidko/binance-kline-scanner/main/install.sh | sh
#   curl -fsSL .../install.sh | sh -s -- --tag v1.0.0     # pin a version
#   curl -fsSL .../install.sh | sh -s -- --remove
set -eu

REPO="asidko/binance-kline-scanner"
BIN="bks"
INSTALL_DIR="${BKS_INSTALL_DIR:-$HOME/.local/bin}"
TAG=""
OS=$(uname -s)

detect_target() {
    os=$OS
    arch=$(uname -m)
    case "$os" in
        Linux) os=linux ;;
        Darwin) os=macos ;;
        *) echo "unsupported OS: $os" >&2; exit 1 ;;
    esac
    case "$arch" in
        x86_64|amd64) arch=x86_64 ;;
        aarch64|arm64) arch=arm64 ;;
        *) echo "unsupported arch: $arch" >&2; exit 1 ;;
    esac
    echo "${os}-${arch}"
}

do_remove() {
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
