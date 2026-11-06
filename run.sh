#!/usr/bin/env bash
set -euo pipefail
config_file="$HOME/.config/x99-ripper/config"
if test -f "$config_file"; then
  # Configuration is created locally by install.sh and is never committed.
  source "$config_file"
fi
for tool in python3 makemkvcon ffprobe ffmpeg eject; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    printf 'Manca il programma: %s\n' "$tool" >&2
    exit 1
  fi
done
if ! command -v HandBrakeCLI >/dev/null 2>&1; then
  if ! command -v flatpak >/dev/null 2>&1 || ! flatpak info fr.handbrake.ghb >/dev/null 2>&1; then
    printf 'Manca HandBrakeCLI o HandBrake Flatpak.\n' >&2
    exit 1
  fi
fi
output_dir="${X99_RIPPER_OUTPUT_DIR:-/mnt/x99/DVD_RIPPATI}"
if ! test -d "$output_dir" || ! test -w "$output_dir"; then
  printf 'La cartella di destinazione %s non esiste o non è scrivibile.\n' "$output_dir" >&2
  exit 1
fi
python3 -c 'import tkinter' || { printf 'Manca python3-tk.\n' >&2; exit 1; }
exec python3 "$(dirname "$(readlink -f "$0")")/ripper.py"
