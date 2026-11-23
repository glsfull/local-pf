# local-pf

Агент проверяет доступные ссылки через браузер Chromium. Он открывает `ya.ru`,
отправляет запрос кнопкой «Найти» и открывает целевой сайт кликом по результату
выдачи. Ссылки на проверяемые страницы он ищет на странице, открытой из выдачи.
При появлении SmartCaptcha агент завершает проверку с кодом 1 и записывает
`captcha_encountered: true` в отчёт.

## 1. Запустите рабочий стол

Начните с проверки окружения. Выполните команды в PowerShell из папки проекта.

```powershell
docker ps --filter name=dp-agent
docker inspect dp-agent --format '{{json .Mounts}}'
```

Если папка проекта уже подключена к `/app`, используйте существующий контейнер.
Если монтирования нет, сохраните старый контейнер под другим именем и создайте
новый с монтированием. Откройте `http://localhost:56565` для наблюдения за браузером.

```powershell
$Project = (Get-Location).Path
docker stop dp-agent
docker rename dp-agent dp-agent-backup
docker run -d --name dp-agent --init -p 127.0.0.1:56565:80 --mount "type=bind,source=$Project,target=/app" qiudeng/drissionpage4-ubuntu-webdesktop-novnc
```

В проверенном образе команда `python3` запускает Python 3.8. Установите Python 3.11
в отдельное окружение и добавьте зависимости из `requirements.txt`. Окружение
хранится в контейнере и сохраняется при его обычном перезапуске.
[Описание образа](https://hub.docker.com/r/qiudeng/drissionpage4-ubuntu-webdesktop-novnc),
[установка Python через uv](https://docs.astral.sh/uv/guides/install-python/).

```powershell
docker exec dp-agent curl -fsSL -o /tmp/local-pf-uv-install.sh https://astral.sh/uv/0.13.9/install.sh
docker exec -e UV_UNMANAGED_INSTALL=/opt/local-pf-tools dp-agent sh /tmp/local-pf-uv-install.sh
docker exec -e UV_PYTHON_INSTALL_DIR=/opt/local-pf-python dp-agent /opt/local-pf-tools/uv venv --no-cache --python 3.11 /opt/local-pf
docker exec dp-agent /opt/local-pf-tools/uv pip install --no-cache --python /opt/local-pf/bin/python -r /app/requirements.txt
docker exec dp-agent /opt/local-pf/bin/python --version
docker exec -e DISPLAY=:1 dp-agent /opt/local-pf/bin/python /app/examples/hello_world.py
```

Последняя команда проверяет запуск Chromium и доступ к `ya.ru`. Если поиск
показывает SmartCaptcha, скрипт сообщит о проверке и завершится.

## 2. Настройте один запрос и одну страницу

Отредактируйте `config.yaml`.

- `SEARCH_KEYWORD` задаёт запрос, `TARGET_DOMAIN` задаёт домен без схемы и пути.
  Поддомены этого домена тоже подходят.
- `TARGET_PAGES` принимает пути вида `/about`, точные тексты ссылок и объекты с
  одним из полей `url`, `text`, `selector`. Для объекта можно добавить
  `expected_selector` с CSS-селектором ожидаемого элемента. По умолчанию
  агент проверяет видимость `h1`.
- `SEARCH_INPUT_SELECTOR`, `SEARCH_BUTTON_SELECTOR`, `RESULT_LINK_SELECTOR` и
  `CAPTCHA_SELECTOR` задают CSS-селекторы для текущей версии страниц поиска.
  `TIMEOUT_SECONDS` ограничивает ожидание отдельных операций браузера.

При необходимости скопируйте `.env.example` в `.env` и укажите путь к Chromium
через `CHROME_PATH` или папку отчётов через `LOG_DIR`. Системные переменные
окружения имеют приоритет. По умолчанию браузер берётся из `PATH`, а отчёты
попадают в `/app/logs`.

## 3. Проверьте поиск, переход и отчёт

Запустите агент одной командой. Держите окно noVNC открытым, чтобы подтвердить
отправку формы поиска, клик в выдаче, переход и пролистывание страницы.

```powershell
docker exec -e DISPLAY=:1 dp-agent /opt/local-pf/bin/python /app/agent.py
$LASTEXITCODE
Get-Content .\logs\agent.log -Encoding UTF8 -Tail 30
Get-Content .\logs\report.json -Encoding UTF8 -Raw | ConvertFrom-Json
```

Код 0 означает, что агент дошёл до целевого сайта и завершил проверку списка.
Смотрите `pages_checked` для результата каждой страницы. Код 1 означает
ошибку выполнения, отсутствие сайта в выдаче или остановку на ограничении
доступа. При этих остановках отчёт сохраняет результаты, собранные к этому моменту.

При изменениях интерфейса поиска запустите ту же команду с `--debug`. Лог
покажет домены ссылок, найденных по `RESULT_LINK_SELECTOR`.

## 4. Расширьте список и повторите проверки

Добавьте остальные страницы после успешной ручной проверки первого сценария.
Проверьте отсутствие ссылки и работу нескольких последовательных переходов.
Автоматические сценарии используют локальный HTTP-сервер и настоящий Chromium.

```powershell
docker exec -w /app dp-agent /opt/local-pf/bin/python -m unittest discover -s tests -v
```

Результаты и ограничения приёмки описаны в [отчёте о проверке](TESTING.md).
