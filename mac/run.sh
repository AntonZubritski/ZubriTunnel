#!/usr/bin/env bash
# CLI-запуск vpn-proxy без GUI. Собирает Go-бинарник из .app/Contents/Resources/
# при первом запуске и кладёт его в .app/Contents/MacOS/vpn-proxy.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
RES="$HERE/ZubriTunnel.app/Contents/Resources"
BIN="$HERE/ZubriTunnel.app/Contents/MacOS/vpn-proxy"

if [ ! -f "$BIN" ]; then
    if [ ! -f "$RES/main.go" ]; then
        echo "Не нашёл $RES/main.go — папка ZubriTunnel.app повреждена."
        exit 1
    fi
    echo "Сборка vpn-proxy (один раз, ~30 сек)…"
    (cd "$RES" && go build -o "$BIN" .)
fi

exec "$BIN" "$@"
