#!/usr/bin/env bash
set -euo pipefail
umask 022

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-$ROOT/dist}"
NFPM_BIN="${NFPM_BIN:-nfpm}"
BUILD_ROOT="$ROOT/.build/linux-package"
STAGED_PACKAGE="$BUILD_ROOT/root"
OUT="$(realpath -m "$OUT")"

case "$OUT" in
  /|"$ROOT"|"$HOME")
    echo "Refusing to use unsafe package output directory: $OUT" >&2
    exit 2
    ;;
esac

cleanup() {
  rm -rf "$BUILD_ROOT"
}
trap cleanup EXIT

cd "$ROOT"
VERSION="$(python scripts/verify_release.py)"
export VERSION
export SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-$(git log -1 --format=%ct 2>/dev/null || date +%s)}"

if ! command -v "$NFPM_BIN" >/dev/null 2>&1; then
  echo "nFPM is required. Install it from https://nfpm.goreleaser.com/docs/install/" >&2
  exit 2
fi

rm -rf "$OUT" "$BUILD_ROOT"
mkdir -p "$OUT" "$STAGED_PACKAGE"
python scripts/stage_linux_package.py cameracopy2 "$STAGED_PACKAGE/cameracopy2"

"$NFPM_BIN" package --config packaging/linux/nfpm.yaml --packager deb \
  --target "$OUT/cameracopy_${VERSION}_amd64.deb"
"$NFPM_BIN" package --config packaging/linux/nfpm.yaml --packager rpm \
  --target "$OUT/cameracopy-${VERSION}-1.x86_64.rpm"
"$NFPM_BIN" package --config packaging/linux/nfpm.yaml --packager archlinux \
  --target "$OUT/cameracopy-${VERSION}-1-x86_64.pkg.tar.zst"

for package in "$OUT"/*; do
  test -s "$package" || {
    echo "Package was not created correctly: $package" >&2
    exit 1
  }
done

printf 'Created Linux packages in %s\n' "$OUT"
