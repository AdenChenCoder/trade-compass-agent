#!/usr/bin/env bash
set -euo pipefail

readonly GITLEAKS_VERSION="8.30.1"
readonly GITLEAKS_LINUX_X64_SHA256="551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"

scan_tmp=""

cleanup() {
  if [ -n "$scan_tmp" ]; then
    rm -rf -- "$scan_tmp"
  fi
}

trap cleanup EXIT

if [ "${GITHUB_ACTIONS:-false}" = "true" ]; then
  if [ "$(uname -s)" != "Linux" ] || [ "$(uname -m)" != "x86_64" ]; then
    echo "Unsupported GitHub Actions runner; expected Linux x86_64." >&2
    exit 1
  fi

  scan_tmp="$(mktemp -d)"
  archive="$scan_tmp/gitleaks.tar.gz"
  download_url="https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz"

  curl --proto '=https' --tlsv1.2 --fail --location --silent --show-error \
    --output "$archive" \
    "$download_url"
  printf '%s  %s\n' "$GITLEAKS_LINUX_X64_SHA256" "$archive" | sha256sum --check -
  tar --extract --gzip --file "$archive" --directory "$scan_tmp" gitleaks
  gitleaks_bin="$scan_tmp/gitleaks"
else
  gitleaks_bin="$(command -v gitleaks || true)"
  if [ -z "$gitleaks_bin" ]; then
    echo "Gitleaks is required. Install version ${GITLEAKS_VERSION} and retry." >&2
    exit 1
  fi
fi

installed_version="$("$gitleaks_bin" version)"
if [ "$installed_version" != "$GITLEAKS_VERSION" ]; then
  echo "Expected Gitleaks ${GITLEAKS_VERSION}, found ${installed_version}." >&2
  exit 1
fi

echo "Running Gitleaks ${installed_version} against complete Git history"
"$gitleaks_bin" git . --redact=100 --no-banner --no-color
