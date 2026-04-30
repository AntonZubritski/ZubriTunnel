# ZubriTunnel

> Точечный VPN для конкретных приложений. Системный трафик не трогается.

Локальный HTTP-прокси над Outline / Shadowsocks с поддержкой `prefix` (TLS-обфускация для обхода DPI). Запускает выбранные программы (VSCode, Chrome, Git Bash и т.д.) с прокси-окружением — внутри них трафик идёт через VPN, всё остальное в системе работает напрямую.

[![Latest release](https://img.shields.io/github/v/release/AntonZubritski/ZubriTunnel)](https://github.com/AntonZubritski/ZubriTunnel/releases/latest)

---

## Установка

### macOS

**Самый простой способ — `.pkg` installer:**

1. Скачай [`ZubriTunnel.pkg`](https://github.com/AntonZubritski/ZubriTunnel/releases/latest) с последнего релиза
2. Двойной клик → пройди стандартный installer wizard → готово
3. Открывай через Spotlight (`Cmd+Space → ZubriTunnel`) или Launchpad

**Альтернатива — одной командой в Terminal:**

```bash
curl -fsSL https://raw.githubusercontent.com/AntonZubritski/ZubriTunnel/main/install.sh | bash
```

Скрипт сам скачает релиз, снимет Gatekeeper-карантин, установит в `/Applications/`, запустит.

> Ничего ставить заранее не нужно — Python 3.12 встроен в `.app` для arm64 и x86_64.

### Windows

1. Скачай [`ZubriTunnel-windows.zip`](https://github.com/AntonZubritski/ZubriTunnel/releases/latest)
2. Распакуй
3. Правой кнопкой по `windows\install_shortcut.ps1` → **Run with PowerShell**
4. На рабочем столе появится ярлык **ZubriTunnel** — двойной клик

> Нужен Python 3 в системе ([python.org](https://www.python.org/downloads/), при установке поставь галку **Add Python to PATH**). Если Python не установлен — лаунчер укажет это в окне «⚙ проверка системы».

---

## Быстрый старт

1. Открой ZubriTunnel
2. Жми **`+ ssconf://`** → вставь ссылку от провайдера VPN (формат `ssconf://...`)
3. Ключ появится в списке → выдели → **Подключить**
4. Жми **Тест ключа** для проверки exit-IP
5. Жми кнопку нужной программы (VSCode / Chrome / Git Bash / ...) — она запустится через прокси

Системный трафик всё это время идёт **напрямую**, провайдер видит только что ты подключён к VPN-серверу, не куда конкретно ходит браузер/мессенджер.

---

## Возможности

### Параллельные прокси

Несколько ключей могут работать одновременно — каждый на своём порту. Выделяешь один ключ, жмёшь Подключить → 8080. Выделяешь второй, Подключить → 8081. Кнопки запуска используют прокси **выделенного** ключа: можно держать Chrome через Германию и VSCode через Вьетнам параллельно.

### Сменить регион (VanyaVPN и совместимые)

Кнопка **сменить регион** для ключей VanyaVPN: тянет список доступных стран из API провайдера и одним кликом переключает сервер. Сама ссылка `ssconf://` остаётся, провайдер на бэке меняет, на какой сервер она указывает.

### Клонирование ключа

Кнопка **клонировать** делает копию `<имя>-2.json`. Удобно когда у одного провайдера хочется иметь активные подключения к нескольким регионам — клонируешь, на одном меняешь регион, оба теперь параллельные.

### Запуск приложений с прокси

**macOS:** VSCode, Cursor, Sublime Text, PyCharm, WebStorm, IntelliJ IDEA, Terminal, iTerm, Warp, Chrome, Firefox, Safari, Edge, Brave, Opera, Vivaldi, Yandex, Arc, Slack, Discord, Telegram, Spotify, Postman.

**Windows:** VSCode, Git Bash, PowerShell, Windows Terminal, Cursor, Chrome, Firefox, Edge.

**Любое другое:** кнопка **Custom…** — file picker.

Список фильтруется по тому что у тебя установлено. Браузеры запускаются с **изолированным профилем** (`--user-data-dir=...`), чтобы реально использовать прокси и не трогать твой основной профиль.

### IDE terminals on/off

Терминалы внутри VSCode/Cursor по умолчанию наследуют env родителя. Если IDE стартовала без прокси, integrated terminal тоже без прокси. Кнопка **IDE terminals on**:
1. Находит `settings.json` у VSCode/Cursor/VSCodium
2. Добавляет `terminal.integrated.env.osx/.windows/.linux` с твоим прокси
3. Бэкапит `settings.json.bak` перед изменением

После перезапуска IDE — `echo $HTTPS_PROXY` в integrated terminal покажет твой прокси.

### git proxy on/off

Включает / выключает `git config --global http.proxy` для глобального git. Удобно когда хочется чтобы `git push/pull` шли через VPN, а `npm`/`curl` — мимо.

### Запущенные приложения

Двойной клик по ключу или кнопка **Запущенные приложения** → диалог со списком программ запущенных через этот прокси: имя, PID, статус (работает / завершено), время запуска. macOS-launched apps отслеживаются через `pgrep` для корректного PID.

### Disconnect — закрыть открытые приложения?

При нажатии **Отключить** если есть запущенные через прокси приложения — диалог: «Закрыть их вместе с отключением?». Без этого Chrome бы продолжил работать с настройкой прокси на мёртвый порт и интернет в нём не работал бы.

### Проверка системы

Кнопка **⚙ проверка системы** в шапке: список зависимостей с ✓/✗ и кнопками установки:

- Python 3 + Tkinter
- vpn-proxy binary (есть кнопка **Собрать** → `go build`)
- Go SDK (есть кнопка **Установить** → `brew install go` или ссылка на go.dev/dl)
- Homebrew (только Mac, кнопка **Установить** → официальный installer)

### Темы

Тёмная / светлая / системная (по умолчанию). Тёмная палитра в нейтральном grey с тёмно-бирюзовым акцентом, светлая — на белом. На Windows подкручивает и сам title bar (DwmSetWindowAttribute) — никаких светлых полосок наверху в тёмной теме.

### Авто-выбор порта

Если 8080 занят, GUI сам подхватит 8081. Если несколько прокси параллельно — каждый на своём порту автоматически. Кнопка **свободный** — пересчитать вручную.

---

## Структура релиза

```
ZubriTunnel.app/                          ← перетащить в /Applications
├── Contents/
│   ├── Info.plist                        CFBundleName=ZubriTunnel
│   ├── MacOS/
│   │   ├── launcher                      bash, exec -a "ZubriTunnel" python3 gui.py
│   │   └── vpn-proxy                     universal binary (arm64+x86_64)
│   ├── Frameworks/
│   │   ├── Python-arm64/                 cpython-3.12 для Apple Silicon
│   │   └── Python-x86_64/                cpython-3.12 для Intel Mac
│   └── Resources/
│       ├── gui.py                        Tkinter GUI (~3000 строк)
│       ├── main.go, go.mod, go.sum       Go-исходник vpn-proxy
│       ├── install_deps.sh               Fallback-инсталлер если bundled Python отвалился
│       ├── icon.icns                     squircle 22.4%
│       └── keys/example.json.template
```

Лаунчер внутри bundle:
1. Находит рабочий Python (bundled первый, system как fallback)
2. Если ни один не работает — диалог «Установить Python через Homebrew»
3. Лог в `~/Library/Logs/ZubriTunnel.log`

---

## CLI (без GUI)

Если хочешь пользоваться без GUI или из CI:

```bash
# macOS, в папке релиза
mac/ZubriTunnel.app/Contents/MacOS/vpn-proxy -ssconf "ssconf://..." -no-menu
```

Все флаги:

```
-key <name>           ключ из keys/ по имени файла без .json
-keys-dir <path>      путь до папки с ключами
-config <path>        путь до отдельного JSON
-ssconf <url>         скачать ключ напрямую из ssconf:// и пользоваться
-addr 127.0.0.1:9090  сменить порт прокси
-launch code|bash     сразу запустить программу
-no-menu              просто держать прокси
```

---

## Сборка из исходников

Нужны: Python 3.10+, Go 1.22+, Pillow (`pip install Pillow`), ffmpeg (для иконок).

```bash
git clone https://github.com/AntonZubritski/ZubriTunnel.git
cd ZubriTunnel

# Иконки (один раз)
python rebuild_icons.py

# Windows
cd windows
go install github.com/akavel/rsrc@latest
rsrc -ico icon.ico -manifest "" -o rsrc_windows.syso
go build -o vpn-proxy.exe .
python gui.py    # или install_shortcut.ps1 для ярлыка

# macOS
cd ../mac/ZubriTunnel.app/Contents/Resources
go build -o ../MacOS/vpn-proxy .
cd ../../../..
python3 mac/ZubriTunnel.app/Contents/Resources/gui.py
```

---

## Релизы

Тег вида `vX.Y.Z` запускает GitHub Actions, которые:
1. Собирают `vpn-proxy.exe` под Windows + ZIP
2. Скачивают cpython-3.12 для arm64 и x86_64 (~80 МБ)
3. Кросс-компилят `vpn-proxy` для обеих арок и сливают `lipo` в universal binary
4. Делают `pkgbuild` → `.pkg` инсталлер
5. Ad-hoc codesign (без Apple Developer ID)
6. Создают GitHub Release с `.pkg`, `.tar.gz`, `.zip`

```bash
git tag -a v1.0.22 -m "..."
git push origin v1.0.22
# через 5-7 минут — релиз готов на github.com/.../releases
```

---

## Тонкости

### Gatekeeper на macOS

`.pkg` installer сам разбирается с карантином (через `pkgbuild` без notarization макос всё равно может ругнуться один раз — нажми правой кнопкой → Open). Для `.tar.gz` снимай вручную:

```bash
xattr -dr com.apple.quarantine ZubriTunnel.app
```

### TCC и `~/Documents`

Если `.app` лежит внутри `~/Documents/`/`~/Desktop/`/`~/Downloads/`, macOS sandbox блокирует bash доступ к siblings. Реши одним из:
- Переместить .app в `/Applications/` (рекомендую — `.pkg` это и делает)
- Перенести проект в `~/Developer/`

### IDE terminal без прокси

Если жмёшь VSCode когда он уже запущен → новое окно открывается в существующем процессе **без env-переменных прокси**. Решение:
- Закрой VSCode полностью (`Cmd+Q`) перед запуском из ZubriTunnel
- Или нажми **IDE terminals on** — пропишет настройки в `settings.json`, переживёт перезапуск VSCode

### Браузеры запускаются с изолированным профилем

`--user-data-dir=%TEMP%\vpn-proxy-<brand>` — без этого новый Chrome открыл бы окно в твоём основном профиле, который **не использует прокси**. Изолированный профиль даёт реальный VPN-трафик. Закладок, истории и расширений основного профиля в этом окне нет — by design.

### Браузеры на macOS используют `open -na`

`subprocess.Popen(["open", "-na", "Chrome.app", "--args", "--proxy-server=..."])` — стандартный macOS-способ. Прямой запуск бинарника `Contents/MacOS/Google Chrome` иногда не находил helper-приложения и тихо вылетал.

---

## Лицензия / благодарности

- [Outline SDK](https://github.com/Jigsaw-Code/outline-sdk) (Apache-2.0) — vpn-proxy использует `mobileproxy` для Shadowsocks с prefix
- [python-build-standalone](https://github.com/astral-sh/python-build-standalone) — bundled Python runtimes для macOS
- Иконка: [realfavicongenerator.net](https://realfavicongenerator.net/) → squircle-маска через Pillow

Code: MIT.
