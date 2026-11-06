#!/usr/bin/env bash
set -euo pipefail
config_file="$HOME/.config/x99-ripper/config"
if test -f "$config_file"; then source "$config_file"; fi
exec python3 "$(dirname "$(readlink -f "$0")")/web.py"
