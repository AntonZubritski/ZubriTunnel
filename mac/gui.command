#!/usr/bin/env bash
# Лаунчер ZubriTunnel — запуск GUI прямо из терминала, без .app
# Использует те же файлы что и ZubriTunnel.app/Contents/Resources/
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
RES="$HERE/ZubriTunnel.app/Contents/Resources"

if [ ! -f "$RES/gui.py" ]; then
    echo "Не нашёл $RES/gui.py — папка ZubriTunnel.app повреждена. Скачай свежий релиз."
    exit 1
fi

cd "$RES"
exec python3 gui.py "$@"
