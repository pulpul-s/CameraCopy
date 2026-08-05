#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PACKAGES="${1:-$ROOT/release-assets}"
PACKAGES="$(cd "$PACKAGES" && pwd)"
EXPECTED_VERSION="$(cat "$ROOT/VERSION")"
TARGET="${2:-all}"

command -v docker >/dev/null 2>&1 || {
  echo "Docker is required for clean-distribution package tests." >&2
  exit 2
}

run_deb_test() {
  local image="$1"
  echo "Testing Debian package on $image"
  docker run --rm \
    -e DEBIAN_FRONTEND=noninteractive \
    -e QT_QPA_PLATFORM=offscreen \
    -e EXPECTED_VERSION="$EXPECTED_VERSION" \
    -v "$PACKAGES:/packages:ro" \
    "$image" bash -euo pipefail -c '
      apt-get update
      apt-get install -y /packages/cameracopy_*_amd64.deb
      test "$(cameracopy --version)" = "$EXPECTED_VERSION"
      cameracopy --self-test
      dpkg-query -W -f="\${Status}\n" cameracopy | grep -Fx "install ok installed"
      ! find /usr/lib/cameracopy -type d -name __pycache__ -print -quit | grep -q .
      ! find /usr/lib/cameracopy -type f \( -name "*.pyc" -o -name "*.pyo" \) -print -quit | grep -q .
    '
}

run_rpm_test() {
  echo "Testing RPM package on Fedora 44"
  docker run --rm \
    -e QT_QPA_PLATFORM=offscreen \
    -e EXPECTED_VERSION="$EXPECTED_VERSION" \
    -v "$PACKAGES:/packages:ro" \
    fedora:44 bash -euo pipefail -c '
      dnf install -y /packages/cameracopy-*.x86_64.rpm
      test "$(cameracopy --version)" = "$EXPECTED_VERSION"
      cameracopy --self-test
      rpm -q cameracopy
      ! find /usr/lib/cameracopy -type d -name __pycache__ -print -quit | grep -q .
      ! find /usr/lib/cameracopy -type f \( -name "*.pyc" -o -name "*.pyo" \) -print -quit | grep -q .
    '
}

run_arch_test() {
  echo "Testing Arch package"
  docker run --rm \
    -e QT_QPA_PLATFORM=offscreen \
    -e EXPECTED_VERSION="$EXPECTED_VERSION" \
    -v "$PACKAGES:/packages:ro" \
    archlinux:base bash -euo pipefail -c '
      pacman -Syu --noconfirm
      package="$(printf "%s\n" /packages/cameracopy-*-x86_64.pkg.tar.zst | head -n 1)"
      pacman -U --noconfirm "$package"
      pacman -U --noconfirm "$package" 2>&1 | tee /tmp/cameracopy-upgrade.log
      ! grep -F "directory permissions differ" /tmp/cameracopy-upgrade.log
      test "$(cameracopy --version)" = "$EXPECTED_VERSION"
      cameracopy --self-test
      pacman -Q cameracopy
      ! find /usr/lib/cameracopy -type d -name __pycache__ -print -quit | grep -q .
      ! find /usr/lib/cameracopy -type f \( -name "*.pyc" -o -name "*.pyo" \) -print -quit | grep -q .
    '
}

case "$TARGET" in
  all)
    run_deb_test debian:13
    run_deb_test ubuntu:26.04
    run_rpm_test
    run_arch_test
    ;;
  debian)
    run_deb_test debian:13
    ;;
  ubuntu)
    run_deb_test ubuntu:26.04
    ;;
  fedora)
    run_rpm_test
    ;;
  arch)
    run_arch_test
    ;;
  *)
    echo "Unknown package-test target: $TARGET" >&2
    exit 2
    ;;
esac
