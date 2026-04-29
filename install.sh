#!/usr/bin/env bash
# ZubriTunnel one-line installer for macOS
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/AntonZubritski/ZubriTunnel/main/install.sh | bash
#
# Скачивает последний релиз, снимает quarantine, устанавливает в /Applications,
# запускает GUI. Без brew, без Python, без Terminal-знаний.

set -e

REPO="AntonZubritski/ZubriTunnel"
APP_DIR="/Applications/ZubriTunnel.app"

cyan()  { printf "\033[36m%s\033[0m\n" "$1"; }
green() { printf "\033[32m%s\033[0m\n" "$1"; }
red()   { printf "\033[31m%s\033[0m\n" "$1"; }

echo ""
cyan "═══════════════════════════════════════"
cyan "  ZubriTunnel — установка"
cyan "═══════════════════════════════════════"
echo ""

echo "→ Узнаю последнюю версию..."
TAG="$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" \
        | grep '"tag_name"' | head -1 | sed 's/.*"\(v[^"]*\)".*/\1/')"
if [ -z "$TAG" ]; then
    red "✗ Не удалось получить последнюю версию (репозиторий приватный или нет релизов)"
    exit 1
fi
green "  $TAG"

URL="https://github.com/$REPO/releases/download/$TAG/ZubriTunnel-mac.tar.gz"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo ""
echo "→ Качаю ZubriTunnel-mac.tar.gz (~80 МБ)..."
curl -fL --progress-bar "$URL" -o "$TMP/zt.tar.gz"

echo ""
echo "→ Распаковываю..."
tar -xzf "$TMP/zt.tar.gz" -C "$TMP"

echo ""
echo "→ Снимаю карантин Gatekeeper..."
xattr -dr com.apple.quarantine "$TMP/mac/ZubriTunnel.app" 2>/dev/null || true

if [ -d "$APP_DIR" ]; then
    echo "→ Удаляю старую версию из /Applications..."
    rm -rf "$APP_DIR"
fi

echo "→ Устанавливаю в /Applications/..."
mv "$TMP/mac/ZubriTunnel.app" "$APP_DIR"

echo ""
green "✓ Готово! ZubriTunnel установлен."
echo ""
echo "Запускаю..."
open "$APP_DIR"

echo ""
echo "В дальнейшем открывай через:"
echo "  • Spotlight (Cmd+Space → ZubriTunnel)"
echo "  • Launchpad (значок ракеты в Dock)"
echo "  • Finder → Applications → ZubriTunnel"
echo ""
