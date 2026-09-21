# Weather Service

HTTP-сервис текущей погоды по названию города. Система состоит из трёх процессов: stateless backend (Flask/Gunicorn), stateless frontend (Nginx + статика) и stateful Redis как backing service. Оркестрация — Kubernetes, поставка манифестов — Helm-чарт `project-chart`.

Назначение — учебный и операционный стенд, на котором отрабатываются практики 12-factor приложения, разделение конфига и секретов, probes, HPA и деградация при недоступности кэша. Бизнес-логика намеренно узкая: один read-only эндпоинт погоды и два служебных probe.

---

## Архитектура

```
клиент
  │
  ▼
frontend (Nginx :8080)
  ├── /            → статика
  └── /api/*       → proxy_pass → backend:5000
                         │
                         ├── GET /weather/<city>
                         │     1. валидация города
                         │     2. cache-aside в Redis (TTL)
                         │     3. OpenWeatherMap Current Weather API
                         │     4. нормализация ответа
                         ├── GET /healthz   (liveness)
                         └── GET /readyz    (readiness)
```

| Процесс   | Роль                                                                 | Состояние                         | Масштабирование      |
|-----------|----------------------------------------------------------------------|-----------------------------------|----------------------|
| frontend  | отдача UI, reverse-proxy `/api/` на backend                          | нет                               | HPA по CPU           |
| backend   | валидация, кэш, вызов провайдера, нормализация контракта             | нет (кэш вынесен)                 | HPA по CPU           |
| redis     | TTL-кэш ответов провайдера, AOF на `hostPath`                        | да, replicaCount должен оставаться 1 | не масштабировать    |

Frontend не знает про OpenWeatherMap. Backend отдаёт стабильную JSON-схему; смена провайдера не должна ломать UI.

Redis — attached resource, не часть процесса приложения. Если Redis недоступен в момент `GET`/`SET`, запрос погоды не падает: кэш пропускается, идём к провайдеру. Readiness при этом честно возвращает `503`, пока Redis не отвечает или не задан `WEATHER_API_KEY`.

---

## 12-factor

Приложение проектируется как [12-factor app](https://12factor.net/). Ниже — не декларация намерений, а фактическое соответствие коду и чарту.

| # | Фактор | Реализация |
|---|--------|------------|
| I | **Codebase** | Один git-репозиторий, один деплой-артефакт (Helm release в namespace `weather-app`). Нет форков «для прода» и «для локалки». |
| II | **Dependencies** | Python: `backend/requirements.txt`, изолируется слоем образа. Frontend: только Nginx + статика, без runtime-пакетного менеджера в контейнере. Системные пакеты хоста в runtime приложения не подразумеваются. |
| III | **Config** | Вся конфигурация — переменные окружения (`backend/app/config.py`). В кластере несекретное — ConfigMap `backend-config`, секретное — Secret `backend-secrets`. В values чарта ключ API по умолчанию не хранится (`backend.secrets.create: false`). |
| IV | **Backing services** | Redis и OpenWeatherMap — подключаемые ресурсы. Хост/порт/TTL/URL/таймаут задаются env. Смена Redis не требует правки кода. |
| V | **Build, release, run** | Build: `dockerfile-backend` / `dockerfile-frontend`. Release: образ + Helm values (config + secrets + теги). Run: Gunicorn / Nginx в подах. Сборка на лету внутри пода не выполняется. |
| VI | **Processes** | Backend и frontend — stateless. Сессии на диске пода не пишутся. Кэш и AOF живут в Redis, не в файловой системе backend-пода. |
| VII | **Port binding** | Backend слушает `PORT` (по умолчанию 5000) через Gunicorn. Frontend — `listenPort` (8080). Сервисы Kubernetes публикуют эти порты; приложение само является HTTP-сервером, не модулем внешнего контейнера приложений. |
| VIII | **Concurrency** | Горизонтальное масштабирование Deployment через HPA (`minReplicas`/`maxReplicas`, CPU target 50%). Внутри процесса — `GUNICORN_WORKERS` × `GUNICORN_THREADS`. Redis из этой модели выведен: `replicaCount > 1` без Redis Cluster/Sentinel даст split-brain. |
| IX | **Disposability** | Backend стартует без обязательного Redis и без ключа API (иначе CrashLoopBackOff вместо понятного `not_ready`). Gunicorn: `graceful_timeout=30`, `timeout` из env. Контейнер работает от непривилегированного `appuser` (uid 1001). |
| X | **Dev/prod parity** | Один и тот же Docker-образ и тот же Helm-чарт. Различие сред — values и способ поставки образа (`imagePullPolicy: Never` для локальной сборки на ноде, `IfNotPresent` предполагается в CI). |
| XI | **Logs** | Stdout/stderr. Gunicorn: `accesslog = "-"`, `errorlog = "-"`. Формат приложения: timestamp, level, logger name, message. Сбор логов — задача платформы, не приложения. |
| XII | **Admin processes** | Одноразовые операции (создание Secret, `helm upgrade`, отладка `kubectl exec`) выполняются вне основного процесса. В репозитории нет встроенных migrate/cron внутри backend. |

Отклонения, которые нужно держать в голове:

- Redis persistence через `hostPath` (`/srv/redis`) привязывает том к ноде. Это не portable volume и не HA-хранилище. Для стенда допустимо; для нескольких нод — нет.
- Nginx-конфиг фронтенда в образе есть, но в кластере его перекрывает ConfigMap. Это удобно для смены `proxy_pass` без пересборки, ценой расхождения «образ vs runtime».
- CI и Ansible фактор V (build/release/run) и X (parity) пока не замыкают. См. раздел «Не завершено».

---

## Дерево репозитория

```
.
├── backend/                      # Flask application factory
│   ├── app/
│   │   ├── __init__.py           # create_app, CORS, wiring cache/client
│   │   ├── config.py             # только os.getenv
│   │   ├── routes.py             # /weather, /healthz, /readyz
│   │   ├── validators.py
│   │   ├── weather_client.py     # OpenWeatherMap + retry + нормализация
│   │   ├── cache.py              # cache-aside, деградация при RedisError
│   │   ├── extensions.py         # ConnectionPool, ленивый connect
│   │   └── errors.py             # JSON-ошибки, без утечки internals
│   ├── gunicorn.conf.py
│   ├── wsgi.py
│   └── requirements.txt
├── frontend/                     # статика + nginx.conf для локального образа
├── dockerfile-backend
├── dockerfile-frontend
├── project-chart/                # Helm application chart
│   ├── Chart.yaml
│   ├── values.yaml
│   └── templates/
│       ├── project-namespace.yaml
│       ├── backend-template/
│       ├── frontend-template/
│       └── redis-template/
├── ansible/                      # заготовка bootstrap ноды (не готово)
│   ├── hosts.ini
│   └── base-playbook.yaml
└── .gitlab-ci.yml                # заготовка pipeline (не готово)
```

---

## HTTP API

Базовый путь с фронтенда: `/api/...` (Nginx срезает префикс `/api/` и проксирует на backend). Напрямую к backend — без префикса.

### `GET /weather/<city>`

`<city>` — 1..100 символов, unicode-буквы, пробелы, дефис, апостроф. Цифры и спецсимволы отклоняются (`400`).

Успех `200`:

```json
{
  "city": "Moscow",
  "country": "RU",
  "temperature": 12.3,
  "feels_like": 11.0,
  "humidity": 70,
  "pressure": 1012,
  "wind_speed": 3.1,
  "description": "overcast clouds",
  "icon": "04d",
  "units": "metric",
  "fetched_at": "2026-09-21T18:00:00+00:00",
  "cached": false
}
```

`cached: true` — ответ из Redis, провайдер не вызывался.

| Код | Когда |
|-----|--------|
| 400 | пустое/слишком длинное/невалидное имя города |
| 404 | провайдер не знает город; неизвестный маршрут |
| 405 | метод не GET |
| 500 | нет `WEATHER_API_KEY`; непойманное исключение (клиенту — generic message) |
| 502 | провайдер недоступен, 401 по ключу, не-JSON, неожиданная форма ответа |
| 504 | таймаут провайдера |

Повторы к провайдеру: urllib3 `Retry(total=2, backoff_factor=0.5)` на 500/502/503/504, только GET.

### `GET /healthz`

Liveness. Процесс жив и отвечает по HTTP. Redis и ключ API не проверяются.

```json
{ "status": "ok" }
```

### `GET /readyz`

Readiness. `200` только если `cache.ping()` успешен **и** `WEATHER_API_KEY` задан. Иначе `503`:

```json
{
  "status": "not_ready",
  "redis": false,
  "weather_api_key_configured": true
}
```

Если ключа нет при старте, процесс не убивается: под поднимается, `/healthz` зелёный, `/readyz` красный. Это сознательно, чтобы в Kubernetes было `not_ready`, а не CrashLoopBackOff.

---

## Конфигурация backend

Источник истины — окружение. Значения в таблице — дефолты из `config.py` / `gunicorn.conf.py`.

| Переменная | Default | Назначение |
|------------|---------|------------|
| `REDIS_HOST` | `localhost` | backing service Redis |
| `REDIS_PORT` | `6379` | |
| `REDIS_DB` | `0` | |
| `REDIS_PASSWORD` | unset | optional; в k8s — из Secret, `optional: true` |
| `REDIS_SOCKET_TIMEOUT` | `2` | socket + connect timeout |
| `CACHE_TTL` | `600` | TTL ключа `weather:<city>` (секунды) |
| `WEATHER_API_KEY` | unset | секрет, обязателен для ready и `/weather` |
| `WEATHER_API_BASE_URL` | OpenWeatherMap `/data/2.5/weather` | |
| `WEATHER_API_TIMEOUT` | `5` | |
| `WEATHER_API_UNITS` | `metric` | |
| `CORS_ORIGINS` | `*` | в кластере трафик идёт same-origin через Nginx |
| `LOG_LEVEL` | `INFO` | |
| `PORT` | `5000` | bind Gunicorn |
| `GUNICORN_WORKERS` | `max(2, cpu_count)` локально; в values чарта `2` | |
| `GUNICORN_THREADS` | `2` | |
| `GUNICORN_TIMEOUT` | `30` | |

Ключ кэша: `weather:<city.strip().lower()>`.

---

## Контейнеры

**backend** (`dockerfile-backend`, python:3.12-slim):

- `PYTHONDONTWRITEBYTECODE=1`, `PYTHONUNBUFFERED=1` — логи сразу в stdout, pyc не пишутся в слой.
- Зависимости ставятся до копирования кода (кэш слоя).
- Непривилегированный `appuser:appgroup` (1001).
- `EXPOSE 5000`, `HEALTHCHECK` на `/healthz`.
- Entrypoint: `gunicorn -c gunicorn.conf.py wsgi:app`.

**frontend** (`dockerfile-frontend`, nginx:alpine):

- Статика `index.html` / `app.js` / `styles.css`.
- `EXPOSE 8080`.
- В Kubernetes `default.conf` монтируется из ConfigMap `frontend-config`.

Сборка с корня репозитория (контекст — `.`, чтобы `COPY ./backend` / `COPY frontend` работали):

```bash
docker build -f dockerfile-backend -t weather-backend:0.1 .
docker build -f dockerfile-frontend -t frontend:0.1 .
```

Имена и теги по умолчанию в `values.yaml`: `fff:0.1` (backend), `frontend:0.1`. `imagePullPolicy: Never` рассчитан на `docker build` на той же ноде, где kubelet. `.dockerignore` исключает чарт, git, `.env`, `secrets.yaml`.

---

## Kubernetes / Helm

Чарт: `project-chart`, type `application`, version `0.1.0`. Namespace создаётся чартом: `weather-app`.

Ресурсы:

| Kind | Имя | Примечание |
|------|-----|------------|
| Namespace | `weather-app` | |
| Deployment | `backend-deployment` | replicas из values только если HPA выключен |
| Deployment | `frontend-deployment` | то же |
| StatefulSet | `redis-statefull` | AOF, `hostPath` `/srv/redis` |
| Service | `backend` | ClusterIP `:5000` |
| Service | `frontend` | ClusterIP `:80` → target `http` (8080) |
| Service | `redis` | headless (`clusterIP: None`) |
| ConfigMap | `backend-config` | env backend |
| ConfigMap | `frontend-config` | nginx `default.conf` |
| Secret | `backend-secrets` | чарт создаёт **только** при `backend.secrets.create=true` |
| HPA | `backend-hpa`, `frontend-hpa` | CPU 50%, 1..3 реплики |

Поставка секрета вне чарта (рекомендуемый путь):

```bash
kubectl -n weather-app create secret generic backend-secrets \
  --from-literal=WEATHER_API_KEY='<key>' \
  --from-literal=REDIS_PASSWORD=''
```

`REDIS_PASSWORD` в deployment optional. Redis включает `--requirepass` только если переменная непустая.

Установка:

```bash
helm upgrade --install weather ./project-chart
```

Проверка:

```bash
kubectl -n weather-app get pods,svc,hpa
kubectl -n weather-app port-forward svc/frontend 8080:80
# UI: http://127.0.0.1:8080
# API через proxy: http://127.0.0.1:8080/api/weather/Moscow
```

Локальный backend без кластера (нужен Redis и ключ):

```bash
export WEATHER_API_KEY=...
export REDIS_HOST=127.0.0.1
pip install -r backend/requirements.txt
cd backend && gunicorn -c gunicorn.conf.py wsgi:app
# отладка: python wsgi.py  — только локально, не для k8s
```

---

## Границы и сознательные упрощения

- Один инстанс Redis, persistence на `hostPath`. Не Redis Cluster, не PVC, не anti-affinity.
- Нет Ingress / TLS в чарте. Точка входа на стенде — Service + port-forward (или ручной Ingress снаружи).
- Нет аутентификации пользователя. Ключ провайдера — серверный секрет.
- Нет очередей, нет записи пользовательских данных.
- Frontend валидирует город зеркально backend; источник истины для отказа — backend.
- CORS по умолчанию `*`; в кластере браузер ходит same-origin на Nginx, CORS на backend — запасной контур.

---

## Не завершено

### CI/CD

`.gitlab-ci.yml` объявляет стадии `build` → `test` → `deploy`. Реализована только сборка образов.

Сделано:

- `build_backend` / `build_frontend` по `changes` в соответствующих деревьях;
- тег `${CI_COMMIT_SHORT_SHA}` + `:latest`;
- опциональный push в `CI_REGISTRY`, если переменные registry заданы;
- dotenv-артефакт `BACKEND_IMAGE` / `FRONTEND_IMAGE` (+ tag) на 4 часа.

Не сделано:

- стадия `test` (линтеры, unit, контракт `/weather` и probes) — джоб нет;
- стадия `deploy` — нет `helm upgrade`, нет проброса image/tag из dotenv в values, нет `imagePullPolicy=IfNotPresent`;
- нет единой сборки, если меняется только чарт;
- нет отдельного релиза Secret (`WEATHER_API_KEY` не должен попадать в values и в git);
- runners с тегами `build` и `ci` предполагаются, но не описаны как код инфраструктуры.

Пока pipeline не закрывает фактор V: build есть, release/run в кластер — вручную.

### Ansible

`ansible/hosts.ini` + `ansible/base-playbook.yaml` — черновик bootstrap Debian-ноды под последующий kubeadm, не рабочий playbook.

Сделано по смыслу: inventory (`debian01`, `192.168.122.100`, `ansible_user=root`), apt-пакеты для Debian, timezone `Europe/Moscow`, намерение отключить swap.

Не сделано / сломано:

- синтаксис задач swap (`loop` у `mount`, `command` для `swapoff`) не доведён;
- последняя задача пустая;
- нет установки container runtime, kubeadm/kubelet/kubectl, инициализации кластера, CNI;
- нет копирования/сборки образов на ноду и `helm upgrade`;
- нет идемпотентного создания namespace/Secret;
- community-коллекции (`community.general`, `ansible.posix`) не зафиксированы.

Итог: Ansible не поднимает ни хост k8s, ни приложение. После доводки playbook должен закрывать подготовку ноды; поставка приложения остаётся за Helm (+ CI).
