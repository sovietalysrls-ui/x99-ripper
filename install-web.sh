#!/usr/bin/env bash
set -euo pipefail
script_dir=$(dirname "$(readlink -f "$0")")
install_dir="$HOME/.local/share/x99-ripper"
unit_dir="$HOME/.config/systemd/user"
mkdir -p "$install_dir" "$unit_dir"
install -m 644 "$script_dir/ripper.py" "$install_dir/ripper.py"
install -m 644 "$script_dir/web.py" "$install_dir/web.py"
install -m 755 "$script_dir/run-web.sh" "$install_dir/run-web.sh"
cat > "$unit_dir/x99-ripper-web.service" <<EOF
[Unit]
Description=X99 Ripper browser controller
After=network-online.target
[Service]
Environment=X99_RIPPER_WEB_HOST=$(tailscale ip -4)
ExecStart=$install_dir/run-web.sh
Restart=on-failure
RestartSec=10
[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now x99-ripper-web.service
