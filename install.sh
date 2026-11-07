#!/usr/bin/env bash
set -euo pipefail
script_dir=$(dirname "$(readlink -f "$0")")
install_dir="$HOME/.local/share/x99-ripper"
launcher="$HOME/.local/share/applications/x99-ripper.desktop"
config_dir="$HOME/.config/x99-ripper"
mkdir -p "$install_dir" "$(dirname "$launcher")"
if test -n "${X99_RIPPER_OUTPUT_DIR:-}"; then
  mkdir -p "$config_dir"
  printf 'export X99_RIPPER_OUTPUT_DIR=%q\n' "$X99_RIPPER_OUTPUT_DIR" > "$config_dir/config"
  if test -n "${X99_RIPPER_DEVICE:-}"; then
    printf 'export X99_RIPPER_DEVICE=%q\n' "$X99_RIPPER_DEVICE" >> "$config_dir/config"
  fi
  chmod 600 "$config_dir/config"
fi
install -m 644 "$script_dir/ripper.py" "$install_dir/ripper.py"
install -m 644 "$script_dir/recover.py" "$install_dir/recover.py"
install -m 644 "$script_dir/monitor.py" "$install_dir/monitor.py"
install -m 755 "$script_dir/run.sh" "$install_dir/run.sh"
cat > "$launcher" <<EOF
[Desktop Entry]
Type=Application
Name=X99 Ripper
Comment=Estrazione e compressione DVD in coda
Exec=$install_dir/run.sh
Terminal=false
Categories=AudioVideo;Video;
EOF
chmod 644 "$launcher"
printf 'Installato. Apri “X99 Ripper” dal menu applicazioni oppure esegui: %s\n' "$install_dir/run.sh"
