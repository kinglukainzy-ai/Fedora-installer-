#!/usr/bin/env bash
# smart-install-setup.sh — compatibility wrapper for older launcher installations
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$SCRIPT_DIR/setup.sh" "$@"
