#!/usr/bin/env bash
set -euo pipefail
if test $# -ne 1; then
  echo 'Uso: ./install-player-handler.sh IP_TAILSCALE_OPTIPLEX' >&2
  exit 2
fi
host=$1
if [[ ! "$host" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]; then
  echo 'Usa l’indirizzo IPv4 Tailscale dell’OptiPlex.' >&2
  exit 2
fi
script_dir=$(dirname "$(readlink -f "$0")")
app_dir="$HOME/.local/share/discflow"
desktop_dir="$HOME/.local/share/applications"
config_dir="$HOME/.config/discflow"
mkdir -p "$app_dir" "$desktop_dir" "$config_dir"
install -m 755 "$script_dir/open-in-player.py" "$app_dir/open-in-player.py"
printf '%s\n' "$host" > "$config_dir/player-host"
cat > "$desktop_dir/discflow-player.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=DiscFlow · apri nel lettore
Exec=$app_dir/open-in-player.py %u
NoDisplay=true
Terminal=false
MimeType=x-scheme-handler/discflow-player;
EOF
xdg-mime default discflow-player.desktop x-scheme-handler/discflow-player
echo 'Lettore esterno collegato a DiscFlow.'
