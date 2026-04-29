#!/usr/bin/env bash
# Авто-установка зависимостей ZubriTunnel для macOS.
# Ставит Homebrew (если нет) + Python 3 + Tkinter.
set -e

echo ""
echo "===================================================="
echo "  ZubriTunnel — установка зависимостей"
echo "===================================================="
echo ""

# 1. Homebrew
if ! command -v brew >/dev/null 2>&1; then
    echo "→ Homebrew не найден. Устанавливаю…"
    echo "  (потребуется пароль администратора)"
    echo ""
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    echo ""
    # Добавить brew в текущий PATH (для скрипта)
    if [ -x /opt/homebrew/bin/brew ]; then
        eval "$(/opt/homebrew/bin/brew shellenv)"
    elif [ -x /usr/local/bin/brew ]; then
        eval "$(/usr/local/bin/brew shellenv)"
    fi
else
    echo "✓ Homebrew уже установлен"
fi

# 2. Python 3
echo ""
echo "→ Установка Python 3…"
brew install python3

# 3. python-tk (Tkinter binding) — отдельный пакет на маке
echo ""
echo "→ Установка python-tk (Tkinter)…"
brew install python-tk || true

# 4. Проверка что всё работает
echo ""
echo "→ Проверка…"
PY=""
for p in /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    if [ -x "$p" ] && "$p" -c "import tkinter" 2>/dev/null; then
        PY="$p"
        break
    fi
done

if [ -z "$PY" ]; then
    echo ""
    echo "✗ Не удалось проверить Python после установки."
    echo "   Попробуй вручную:  brew reinstall python3 python-tk"
    echo ""
    read -n 1 -s -r -p "Нажми любую клавишу чтобы закрыть..."
    exit 1
fi

echo "✓ Python готов: $PY ($($PY --version 2>&1))"
echo ""
echo "===================================================="
echo "  Установка завершена!"
echo "  Закрой это окно и снова двойной клик по"
echo "  ZubriTunnel.app — он запустится."
echo "===================================================="
echo ""
read -n 1 -s -r -p "Нажми любую клавишу чтобы закрыть..."
