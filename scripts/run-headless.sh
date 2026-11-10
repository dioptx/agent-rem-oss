#!/usr/bin/env bash
# Portable manual/nightly entry point; Python owns execution and model permissions.
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec "$repo/bin/rem" night "$@"
