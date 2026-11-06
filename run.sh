#!/usr/bin/env bash
set -euo pipefail
for tool in python3 makemkvcon flatpak ffprobe ffmpeg eject; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    printf 'Manca il programma: %s\n' "$tool" >&2
    exit 1
  fi
done
if ! flatpak info fr.handbrake.ghb >/dev/null 2>&1; then
  printf 'Manca HandBrake Flatpak: fr.handbrake.ghb\n' >&2
  exit 1
fi
if ! mountpoint -q /mnt/x99 || ! test -w /mnt/x99/DVD_RIPPATI; then
  printf 'Il Samsung /mnt/x99 non è montato o DVD_RIPPATI non è scrivibile.\n' >&2
  exit 1
fi
python3 -c 'import tkinter' || { printf 'Manca python3-tk.\n' >&2; exit 1; }
exec python3 "$(dirname "$(readlink -f "$0")")/ripper.py"
