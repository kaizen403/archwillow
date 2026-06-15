#!/bin/bash
export WAYLAND_DISPLAY=wayland-1
export XDG_RUNTIME_DIR=/run/user/$(id -u)
export DISPLAY=:1
exec /home/kaizen/.local/share/hyprwhspr/venv/bin/python /home/kaizen/hyprwhspr/lib/main.py "$@"
