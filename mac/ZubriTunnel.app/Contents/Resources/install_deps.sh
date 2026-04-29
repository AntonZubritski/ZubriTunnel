#!/usr/bin/env bash
# Авто-установка зависимостей ZubriTunnel для macOS.
# Ставит Homebrew (если нет) + Python 3 + Tkinter.
# Аккуратно обрабатывает запуск под Rosetta на Apple Silicon.
set -e

echo ""
echo "===================================================="
echo "  ZubriTunnel — установка зависимостей"
echo "===================================================="
echo ""

# Определяем архитектуру системы (а не текущего процесса).
# sysctl надёжнее uname на Apple Silicon под Rosetta.
HW_ARCH="$(sysctl -n hw.optional.arm64 2>/dev/null || echo 0)"
if [ "$HW_ARCH" = "1" ]; then
    SYSTEM_ARCH="arm64"
else
    SYSTEM_ARCH="x86_64"
fi
echo "Архитектура системы: $SYSTEM_ARCH (текущий процесс: $(uname -m))"

# Префикс для запуска brew в нужной арке.
# На Apple Silicon brew в /opt/homebrew = arm64; на Intel в /usr/local = x86_64.
brew_run() {
    if [ "$SYSTEM_ARCH" = "arm64" ] && [ -x /opt/homebrew/bin/brew ]; then
        arch -arm64 /opt/homebrew/bin/brew "$@"
    elif [ -x /usr/local/bin/brew ]; then
        /usr/local/bin/brew "$@"
    else
        brew "$@"
    fi
}

# 1. Homebrew
if [ "$SYSTEM_ARCH" = "arm64" ] && [ -x /opt/homebrew/bin/brew ]; then
    echo "✓ Homebrew (arm64) найден в /opt/homebrew"
elif [ -x /usr/local/bin/brew ]; then
    echo "✓ Homebrew найден в /usr/local"
elif command -v brew >/dev/null 2>&1; then
    echo "✓ Homebrew найден ($(command -v brew))"
else
    echo ""
    echo "→ Homebrew не найден. Устанавливаю…"
    echo "  (потребуется пароль администратора)"
    echo ""
    if [ "$SYSTEM_ARCH" = "arm64" ]; then
        arch -arm64 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    else
        /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    fi
fi

# 2. Python 3
echo ""
echo "→ Установка Python 3 (под $SYSTEM_ARCH)…"
brew_run install python3

# 3. python-tk
echo ""
echo "→ Установка python-tk (Tkinter)…"
brew_run install python-tk || true

# 4. Проверка
echo ""
echo "→ Проверка…"
PY=""
for p in /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    if [ -x "$p" ] && "$p" -c "import tkinter; r=tkinter.Tk(); r.withdraw(); r.destroy()" 2>/dev/null; then
        PY="$p"
        break
    fi
done

if [ -z "$PY" ]; then
    echo ""
    echo "✗ Не удалось проверить Python после установки."
    echo "   Попробуй вручную:"
    echo "   arch -$SYSTEM_ARCH brew reinstall python3 python-tk"
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
