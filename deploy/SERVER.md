# Установка на сервер с нуля

Инструкция для **Ubuntu 24.04 LTS**, одного небольшого сервера (ориентир: 1–2 CPU, 2 ГБ RAM), домена и доступа sudo. Серверная часть полностью на Python; Nginx завершает HTTPS, systemd перезапускает web и worker. На других ОС команды установки пакетов отличаются.

Приложение устанавливается в `/opt/BMSeller` (регистр букв важен). Имя репозитория GitHub остаётся `BMSellere`, системный пользователь — `bmseller`.

## 1. Подготовить сервер

Направьте DNS A-запись вашего домена на IPv4 сервера; AAAA добавляйте только при настроенном IPv6. Замените во всех примерах `crm.example.ru` своим доменом. Разрешите входящие TCP 80/443 в сетевых настройках хостинга, сохраните SSH-доступ. Порт 8000 наружу открывать не нужно.

```sh
sudo apt update
sudo apt install -y git python3 python3-venv nginx certbot python3-certbot-nginx
sudo adduser --system --group --home /opt/BMSeller bmseller
sudo git clone --branch main https://github.com/CanDreamm/BMSellere.git /opt/BMSeller
sudo chown -R bmseller:bmseller /opt/BMSeller
sudo -u bmseller python3 -m venv /opt/BMSeller/.venv
sudo -u bmseller /opt/BMSeller/.venv/bin/python -m pip install -r /opt/BMSeller/requirements-dev.txt
cd /opt/BMSeller
sudo -u bmseller .venv/bin/python -m pytest -q
```

Если репозиторий закрытый, используйте предоставленный вам доступ GitHub (SSH/deploy key или HTTPS-аутентификацию); не помещайте токен в URL, инструкции или историю команд. Домашний каталог пользователя создан заранее, но не должен содержать файлы перед `git clone`.

## 2. Настройки приложения

```sh
sudo install -m 600 -o root -g root deploy/environment.example /etc/bmseller.env
sudoedit /etc/bmseller.env
```

Обязательно замените `APP_PASSWORD` на длинный уникальный пароль. Вход в интерфейс: пользователь `admin`, этот пароль. Все операторы используют одну учётную запись; ролей пока нет. Пароль хранится только в файле на сервере, не в Git. systemd читает файл до запуска процесса под пользователем bmseller. Не выполняйте `source /etc/bmseller.env` — это формат EnvironmentFile systemd.

База, сессионный ключ и PDF создаются в `/opt/BMSeller/data`. Задавать `SESSION_SECRET` не обязательно: приложение сохраняет случайный ключ в этом каталоге.

Источник компаний и почта необязательны для запуска интерфейса:

| Переменная | Назначение |
|---|---|
| `COMPANY_FEED_URL` | URL разрешённой XLSX-выгрузки компаний; без него автоматического пополнения реестра нет |
| `FEED_INTERVAL_SECONDS` | Интервал загрузки источника; 86400 по умолчанию |
| `JOB_DELAY_SECONDS` | Пауза между заданиями; 2 по умолчанию |
| `SMTP_HOST`, `SMTP_PORT` | Почтовый сервер STARTTLS, обычно 587 |
| `SMTP_USER`, `SMTP_PASSWORD` | Учётные данные SMTP |
| `SMTP_FROM` | Адрес отправителя, разрешённый почтовым провайдером |

Для реальной отправки проверьте разрешение исходящего SMTP у хостера, настройки отправителя и SPF/DKIM/DMARC у почтового провайдера. Порт 465 с неявным TLS текущий обработчик не поддерживает. Включайте worker с SMTP только после настройки получателей и проверки очереди.

## 3. Nginx и HTTPS

Пока службы приложения не запущены, настроим HTTPS. До получения сертификата Nginx может отвечать 502 — это ожидаемо.

```sh
sudo cp deploy/nginx.conf /etc/nginx/sites-available/bmseller
sudoedit /etc/nginx/sites-available/bmseller
sudo ln -s /etc/nginx/sites-available/bmseller /etc/nginx/sites-enabled/bmseller
sudo nginx -t
sudo systemctl enable --now nginx
sudo systemctl reload nginx
sudo certbot --nginx -d crm.example.ru --redirect
sudo certbot renew --dry-run
```

В файле Nginx замените `server_name`. Certbot добавит HTTPS и перенаправление с HTTP. Не входите в приложение по обычному HTTP: используется Basic Auth. Если DNS или выпуск сертификата не работают, исправьте это до публичного запуска.

## 4. Постоянные службы

```sh
cd /opt/BMSeller
sudo cp deploy/bmseller-web.service deploy/bmseller-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bmseller-web
sudo systemctl enable --now bmseller-worker
sudo systemctl status bmseller-web bmseller-worker --no-pager
```

Откройте свой HTTPS-домен, войдите как `admin`. Проверьте, что отображается база, а «Скачать Excel» отдаёт XLSX с заголовками. Затем импортируйте небольшую собственную выгрузку и проверьте сбор с известного публичного сайта. Для проверки SMTP отправьте одно тестовое КП на свой адрес с отмеченным согласием, затем проверьте почтовый ящик. Отметка `done` означает принятие письма SMTP-сервером, но не гарантирует доставку.

Службы запускаются после перезагрузки. Логи:

```sh
sudo journalctl -u bmseller-web -n 100 --no-pager
sudo journalctl -u bmseller-worker -n 100 --no-pager
```

Не публикуйте логи с контактами компаний, URL источников или секретами. Классы ошибок заданий видны в интерфейсе. `failed` автоматически повторно не отправляется. `running` после аварии требует ручной проверки: письмо могло уже уйти; не сбрасывайте такие задания массово.

## 5. Обновление

Перед обновлением сделайте резервную копию по разделу ниже. Каталог `data/` и `/etc/bmseller.env` не входят в Git и должны сохраниться.

```sh
sudo systemctl stop bmseller-worker bmseller-web
sudo -u bmseller git -C /opt/BMSeller pull --ff-only origin main
sudo -u bmseller /opt/BMSeller/.venv/bin/python -m pip install -r /opt/BMSeller/requirements-dev.txt
cd /opt/BMSeller
sudo -u bmseller .venv/bin/python -m pytest -q
sudo systemctl start bmseller-web bmseller-worker
```

При неуспешных проверках не запускайте обновлённый код; разберите ошибку. Автоматических миграций схемы пока нет; будущие изменения БД потребуют отдельной инструкции.

## 6. Резервная копия и восстановление

Для согласованной копии SQLite остановите обе службы. Пример создаёт архив всего `data/` и настроек, доступный только root. Храните его также вне сервера в защищённом хранилище: внутри контакты, презентации и секреты.

```sh
sudo systemctl stop bmseller-worker bmseller-web
sudo install -d -m 700 /var/backups/bmseller
sudo sh -c 'umask 077; tar -czf /var/backups/bmseller/backup-$(date +%Y%m%d-%H%M%S).tar.gz -C / opt/BMSeller/data etc/bmseller.env'
sudo systemctl start bmseller-web bmseller-worker
```

Если архивирование не удалось, разберите причину и убедитесь, что службы снова запущены. Для восстановления остановите службы на новом сервере с тем же путём `/opt/BMSeller`, распакуйте доверенную копию в `/` под root, верните владельца `bmseller:bmseller` каталогу `data/`, права 600 и владельца root файлу `/etc/bmseller.env`, затем запустите службы и проверьте интерфейс. Перед этим сохраните текущие данные. Отдельно проверьте задания `running`.

## Ограничения

Это первая версия для доверенных операторов, а не готовый многопользовательский публичный сервис. Ограничьте доступ на уровне сети сервера, если он нужен только вашей команде. Нет подключённого реестра ФНС, поиска неизвестных брендов/сайтов, обработки недоставок и автоматической отписки. Не импортируйте сайты из недоверенных источников: сборщик требует отдельной сетевой изоляции для такого сценария. Сведения о полноте базы и реальном статусе организаций проверяет оператор или поставщик исходных данных.

Приложение и Gunicorn проверены в среде разработки. Полное развёртывание Ubuntu/systemd/Nginx/Certbot на вашем сервере, реальная отправка и подключение поставщика данных пока не выполнялись.
