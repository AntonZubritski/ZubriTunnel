#!/usr/bin/env bash
# Zubritunnel launcher — same as Zubritunnel.app but no .app bundle
# Mac launcher for the GUI — двойной клик в Finder
cd "$(dirname "$0")"
exec python3 gui.py
