# Weather Service

HTTP-сервис текущей погоды по названию города. Приложение состоит из трёх процессов: stateless backend (Flask/Gunicorn), stateless frontend (Nginx + статика) и stateful Redis как backing service. Приложение ставится в Kubernetes Helm-чартом `project-chart`, сам кластер (kubeadm + containerd + Calico) поднимается Ansible-playbook'ами из `ansible/`.

Назначение — учебный и операционный стенд, на котором отрабатываются практики 12-factor приложения, разделение конфига и секретов, probes, HPA, деградация при недоступности кэша и bootstrap кластера с нуля. Бизнес-логика намеренно узкая: один read-only эндпоинт погоды и два служебных probe.

## Состояние

| Часть | Статус |
|-------|--------|
| Backend, frontend, Redis | работают |
| Helm-чарт | ставится, `helm lint` чистый; probes backend смотрят на `/healthz` и `/readyz` |
| Ansible | готовит Debian-ноды и поднимает кластер: 1 control plane + 1 worker, Calico |
| CI (`.gitlab-ci.yml`) | только сборка образов |
| Доставка образов и деплой приложения | вручную |

Подробности — в разделе «Известные проблемы и что не доделано».

---

## Архитектура

### Приложение

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

Redis — attached resource, не часть процесса приложения. Если Redis недоступен в момент `GET`/`SET`, запрос погоды не падает: кэш пропускается, идём к провайдеру. Эндпоинт `/readyz` при этом возвращает `503`, пока Redis не отвечает или не задан `WEATHER_API_KEY`. В Deployment этот эндпоинт подключён как readinessProbe, поэтому Service не шлёт трафик в под, пока он не готов.

### Кластер

```
машина с Ansible / helm / docker
  │  ssh root@
  ├── k8s-controle-node  192.168.122.10   control plane (kubeadm), Calico
  └── k8s-work-node1     192.168.122.20   worker: поды приложения, том Redis /srv/redis
```

- Kubernetes `v1.36` из `pkgs.k8s.io`; `kubelet`, `kubeadm`, `kubectl` зафиксированы через `hold`.
- Container runtime — `containerd.io` из репозитория Docker, `SystemdCgroup = true`. Docker Engine на ноды не ставится.
- CNI — Calico `v3.32.2` (манифест `calico.yaml`), pod CIDR `10.0.0.0/16`.
- kubeadm вешает на control plane taint `NoSchedule`, поэтому поды приложения работают на worker.

---

## Быстрый старт

Путь от чистых Debian-хостов до работающего UI. Детали каждого шага — в соответствующих разделах ниже.

1. Поднять кластер playbook'ами из `ansible/` в указанном порядке (раздел «Ansible»).
2. Собрать образы и импортировать их в containerd на worker-ноде (раздел «Контейнеры»).
3. Выполнить `helm upgrade --install`, затем создать Secret с ключом OpenWeatherMap (раздел «Kubernetes / Helm»).
4. Поставить metrics-server, чтобы заработал HPA.
5. Сделать `port-forward` на `svc/frontend` и открыть UI.

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

Ошибки всегда отдаются JSON одного вида:

```json
{ "error": "bad_request", "message": "City name contains invalid characters" }
```

| Код | `error` | Когда |
|-----|---------|-------|
| 400 | `bad_request` | пустое/слишком длинное/невалидное имя города |
| 404 | `weather_provider_error` | провайдер не знает город |
| 404 | `not_found` | неизвестный маршрут |
| 405 | `method_not_allowed` | метод не GET |
| 500 | `weather_provider_error` | не задан `WEATHER_API_KEY` |
| 500 | `internal_error` | непойманное исключение; клиенту — generic message, детали только в логе |
| 502 | `weather_provider_error` | провайдер недоступен, 401 по ключу, не-JSON, неожиданная форма ответа |
| 504 | `weather_provider_error` | таймаут провайдера |

Повторы к провайдеру: urllib3 `Retry(total=2, backoff_factor=0.5)` на 500/502/503/504, только GET.

UI для 404 и 5xx показывает собственные сообщения на русском; текст `message` из backend выводится только для остальных 4xx.

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

Если ключа нет при старте, процесс не убивается: `/healthz` зелёный, `/readyz` красный. ReadinessProbe переводит под в `NotReady`, а не в CrashLoopBackOff. В кластере это срабатывает, только когда Secret `backend-secrets` уже существует и ключ в нём пустой. Пока объекта Secret нет, контейнер не стартует (`CreateContainerConfigError`), и probes не выполняются.

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

### Локальный запуск без кластера

```bash
docker run -d --name weather-redis -p 6379:6379 redis:7-alpine
python3 -m venv .venv && . .venv/bin/activate
pip install -r backend/requirements.txt
export WEATHER_API_KEY=...
export REDIS_HOST=127.0.0.1
cd backend && gunicorn -c gunicorn.conf.py wsgi:app
# отладка: python wsgi.py  — только локально, не для k8s
```

Без Redis сервис тоже отвечает (кэш пропускается), но `/readyz` вернёт `503`.

---

## Контейнеры

**backend** (`dockerfile-backend`, python:3.12-slim):

- `PYTHONDONTWRITEBYTECODE=1`, `PYTHONUNBUFFERED=1` — логи сразу в stdout, pyc не пишутся в слой.
- Зависимости ставятся до копирования кода (кэш слоя).
- Непривилегированный `appuser:appgroup` (1001).
- `EXPOSE 5000`, `HEALTHCHECK` на `/healthz`. Kubernetes `HEALTHCHECK` из образа игнорирует — probes задаются в Deployment.
- Entrypoint: `gunicorn -c gunicorn.conf.py wsgi:app`.

**frontend** (`dockerfile-frontend`, `nginxinc/nginx-unprivileged:alpine`):

- Процесс Nginx работает от непривилегированного пользователя и слушает 8080, а не 80.
- Статика `index.html` / `app.js` / `styles.css`.
- `default.conf` из `frontend/nginx.conf`: `listen 8080`, `/api/` → `http://backend:5000/`.
- `EXPOSE 8080`.
- В Kubernetes `default.conf` перекрывается ConfigMap `frontend-config`; версия в ConfigMap дополнительно задаёт `proxy_http_version 1.1` и `X-Forwarded-Proto`.

### Сборка

Сборка из корня репозитория (контекст — `.`, чтобы работали `COPY ./backend` / `COPY frontend`). Теги совпадают с дефолтами `values.yaml` (`weather-backend:0.1`, `weather-frontend:0.1`) и с именами образов в CI, поэтому чарт ставится без `--set`:

```bash
docker build -f dockerfile-backend  -t weather-backend:0.1 .
docker build -f dockerfile-frontend -t weather-frontend:0.1 .
```

`.dockerignore` исключает чарт, git, `.env`, `secrets.yaml`, `*.md`.

### Доставка образов на ноду

В чарте `imagePullPolicy: Never`: kubelet не скачивает образ, а ищет его в локальном хранилище containerd (namespace `k8s.io`). Docker на нодах нет, и образы из `docker build` containerd не видит, поэтому их нужно импортировать на каждую worker-ноду:

```bash
docker save weather-backend:0.1 weather-frontend:0.1 -o weather-images.tar
scp weather-images.tar root@192.168.122.20:/tmp/
ssh root@192.168.122.20 'ctr -n k8s.io images import /tmp/weather-images.tar'
```

Без импорта поды остаются в `ErrImageNeverPull`. После пересборки с тем же тегом импорт нужно повторить и перезапустить Deployment, например `kubectl -n weather-app rollout restart deploy/backend-deployment`.

---

## Kubernetes / Helm

Чарт: `project-chart`, type `application`, version `0.1.0`. Namespace `weather-app` создаётся самим чартом.

Ресурсы:

| Kind | Имя | Примечание |
|------|-----|------------|
| Namespace | `weather-app` | |
| Deployment | `backend-deployment` | replicas из values только если HPA выключен |
| Deployment | `frontend-deployment` | то же |
| StatefulSet | `redis-statefull` | AOF, `hostPath` `/srv/redis` (`DirectoryOrCreate`) |
| Service | `backend` | ClusterIP `:5000` |
| Service | `frontend` | ClusterIP `:80` → target `http` (8080) |
| Service | `redis` | headless (`clusterIP: None`) |
| ConfigMap | `backend-config` | env backend |
| ConfigMap | `frontend-config` | nginx `default.conf` |
| Secret | `backend-secrets` | чарт создаёт **только** при `backend.secrets.create=true` |
| HPA | `backend-hpa`, `frontend-hpa` | CPU 50%, 1..3 реплики |

### Probes

| Компонент | liveness | readiness |
|-----------|----------|-----------|
| backend | `GET /healthz` на порт 5000 | `GET /readyz` на порт 5000 |
| frontend | `GET /` на порт `http` | `GET /` на порт `http` |
| redis | `redis-cli ping` | `redis-cli ping` |

Пока `/readyz` возвращает `503`, под backend остаётся `NotReady`, и Service на него не шлёт трафик. Так бывает, если Redis не отвечает или в Secret пустой `WEATHER_API_KEY`. Liveness при этом остаётся зелёной: процесс жив, Kubernetes его не перезапускает.

### Доступ к кластеру

Ansible не ставит Helm на ноды. `helm` и `kubectl` удобнее запускать с рабочей машины, забрав kubeconfig с control plane:

```bash
scp root@192.168.122.10:/etc/kubernetes/admin.conf ~/.kube/weather-lab.conf
export KUBECONFIG=~/.kube/weather-lab.conf
kubectl get nodes
```

### Установка

Релиз Helm называется `weather` и по умолчанию записывается в namespace `default`. Объекты приложения чарт создаёт в `weather-app`.

Ключ лежит в `.env` в корне репозитория. Файл в `.gitignore`, в образ не попадает (`.dockerignore`). Обязательна строка `WEATHER_API_KEY`; `REDIS_PASSWORD` можно не указывать.

```bash
helm upgrade --install weather ./project-chart

kubectl -n weather-app create secret generic backend-secrets \
  --from-env-file=.env

kubectl -n weather-app rollout status deploy/backend-deployment
```

Порядок важен:

- Namespace создаёт чарт, поэтому Secret создаётся после `helm upgrade --install`. Создать namespace вручную заранее не получится: Helm откажется ставить релиз, потому что Namespace уже существует и не принадлежит релизу.
- Пока Secret нет, backend-под висит в `CreateContainerConfigError`: `WEATHER_API_KEY` подключён через `secretKeyRef` без `optional`. Как только Secret создан, kubelet запускает контейнер сам.
- `REDIS_PASSWORD` опционален. Redis включает `--requirepass`, только если значение непустое, и читает его только при старте. Если пароль задан, после создания Secret перезапустите Redis: `kubectl -n weather-app rollout restart statefulset/redis-statefull`.
- `--from-literal` на командной строке оставляет ключ в истории shell. `--from-env-file` этого не делает.

Альтернатива для стенда — дать чарту создать Secret самому. Ключ при этом попадёт в Helm release и в историю shell:

```bash
helm upgrade --install weather ./project-chart \
  --set backend.secrets.create=true \
  --set backend.secrets.weatherApiKey='<key>'
```

### HPA и metrics-server

HPA масштабирует по CPU и без Metrics API не работает: `kubectl get hpa` показывает `<unknown>/50%`, реплик остаётся `minReplicas`. kubeadm metrics-server не ставит, Ansible и чарт — тоже. Для стенда:

```bash
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
kubectl -n kube-system patch deployment metrics-server --type=json \
  -p '[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]'
```

`--kubelet-insecure-tls` нужен, потому что kubeadm выдаёт kubelet самоподписанные serving-сертификаты. Для стенда допустимо, для прода — нет.

### Проверка

```bash
kubectl -n weather-app get pods,svc,hpa
kubectl -n weather-app port-forward svc/frontend 8080:80
# UI: http://127.0.0.1:8080
# API через proxy: http://127.0.0.1:8080/api/weather/Moscow
```

Если `kubectl` запущен на самой control plane-ноде, добавьте `--address 0.0.0.0` и открывайте `http://192.168.122.10:8080`.

---

## Ansible

`ansible/` готовит Debian-хосты и поднимает на них kubeadm-кластер. Приложение Ansible не ставит — это задача Helm.

### Inventory

| Группа | Хост | Адрес |
|--------|------|-------|
| `control_plane` | `k8s-controle-node` | `192.168.122.10` |
| `work_nodes` | `k8s-work-node1` | `192.168.122.20` |

Подключение под `root` (`ansible_user=root`). Имя `k8s-controle-node` захардкожено в `work_node.yaml` (`delegate_to`), а адрес `192.168.122.10` — в переменной `apiserver` playbook'а control plane. При смене inventory их нужно поправить и там.

### Playbook'и

| Playbook | Хосты | Что делает |
|----------|-------|------------|
| `fix_apt_sources.yaml` | все | комментирует `/etc/apt/sources.list`, добавляет репозитории Debian и debian-security в формате deb822, `apt update` |
| `dns.yaml` | все | переводит `/etc/resolv.conf` на resolvconf; nameservers — шлюз по умолчанию, `8.8.8.8`, `1.1.1.1` |
| `server_settings.yaml` | все | базовые пакеты, timezone `Europe/Moscow` и chrony, отключение swap, модули `overlay`/`br_netfilter`, sysctl для k8s, репозитории Docker и Kubernetes, `containerd.io`, `kubelet`/`kubeadm`/`kubectl` |
| `control_plan_initialization_with_calico.yaml` | `control_plane` | `kubeadm init`, kubeconfig в `/root/.kube/config`, установка Calico, ожидание `Ready` |
| `work_node.yaml` | `work_nodes` | берёт join-команду с control plane, `kubeadm join --node-name=<inventory_hostname>`, ждёт `Ready` ноды |

Ключевые шаги безопасно перезапускать: `kubeadm init` пропускается, если есть `/etc/kubernetes/admin.conf`; `kubeadm join` — если есть `/etc/kubernetes/kubelet.conf`; Calico применяется, только если нет DaemonSet `calico-node`.

Версии и сеть задаются в `vars` внутри playbook'ов: `kubernetes_version: "1.36"` и `system_timezone` в `server_settings.yaml`; `apiserver`, `cidr`, `calico_version` в `control_plan_initialization_with_calico.yaml`.

### Запуск

Требования к хостам: Debian, SSH-доступ под root, `python3`, `python3-debian` (нужен модулю `deb822_repository` уже в первом playbook) и `resolvconf` (его использует `dns.yaml`, но ни один playbook не ставит).

```bash
cd ansible
ansible-galaxy collection install -r requirements.yml

ansible-playbook -i hosts.ini playbooks/fix_apt_sources.yaml
ansible -i hosts.ini all -m ansible.builtin.apt -a "name=resolvconf state=present"  # если resolvconf ещё нет
ansible-playbook -i hosts.ini playbooks/dns.yaml
ansible-playbook -i hosts.ini playbooks/server_settings.yaml
ansible-playbook -i hosts.ini playbooks/control_plan_initialization_with_calico.yaml
ansible-playbook -i hosts.ini playbooks/work_node.yaml
```

Проверка на control plane:

```bash
kubectl get nodes -o wide
kubectl -n kube-system get pods
```

---

## 12-factor

Приложение проектируется как [12-factor app](https://12factor.net/). Ниже — не декларация намерений, а фактическое соответствие коду и чарту.

| # | Фактор | Реализация |
|---|--------|------------|
| I | **Codebase** | Один git-репозиторий: код, Dockerfile'ы, чарт и инфраструктура. Один деплой-артефакт — Helm release в namespace `weather-app`. Нет форков «для прода» и «для локалки». |
| II | **Dependencies** | Python: `backend/requirements.txt` с зафиксированными версиями, изолируется слоем образа. Frontend: только Nginx + статика, без runtime-пакетного менеджера в контейнере. Системные пакеты хоста в runtime приложения не подразумеваются. |
| III | **Config** | Вся конфигурация — переменные окружения (`backend/app/config.py`). В кластере несекретное — ConfigMap `backend-config`, секретное — Secret `backend-secrets`. В values чарта ключ API по умолчанию не хранится (`backend.secrets.create: false`). |
| IV | **Backing services** | Redis и OpenWeatherMap — подключаемые ресурсы. Хост/порт/TTL/URL/таймаут задаются env. Смена Redis не требует правки кода. |
| V | **Build, release, run** | Build: `dockerfile-backend` / `dockerfile-frontend` (локально или в CI). Release: образ + Helm values (config + secrets + теги). Run: Gunicorn / Nginx в подах. Сборка на лету внутри пода не выполняется. |
| VI | **Processes** | Backend и frontend — stateless. Сессии на диске пода не пишутся. Кэш и AOF живут в Redis, не в файловой системе backend-пода. |
| VII | **Port binding** | Backend слушает `PORT` (по умолчанию 5000) через Gunicorn. Frontend — `listenPort` (8080). Сервисы Kubernetes публикуют эти порты; приложение само является HTTP-сервером, не модулем внешнего контейнера приложений. |
| VIII | **Concurrency** | Горизонтальное масштабирование Deployment через HPA (`minReplicas`/`maxReplicas`, CPU target 50%; нужен metrics-server). Внутри процесса — `GUNICORN_WORKERS` × `GUNICORN_THREADS`. Redis из этой модели выведен: `replicaCount > 1` без Redis Cluster/Sentinel даст split-brain. |
| IX | **Disposability** | Backend стартует без Redis и без ключа API: процесс жив, `/healthz` — `200`, `/readyz` — `503` (вместо CrashLoopBackOff). Gunicorn: `graceful_timeout=30`, `timeout` из env. Контейнер работает от непривилегированного `appuser` (uid 1001). |
| X | **Dev/prod parity** | Один и тот же Docker-образ и тот же Helm-чарт. Различие сред — values и способ поставки образа: на стенде `imagePullPolicy: Never` и ручной импорт в containerd, в перспективе — registry и `IfNotPresent` из CI. |
| XI | **Logs** | Stdout/stderr. Gunicorn: `accesslog = "-"`, `errorlog = "-"`. Формат приложения: timestamp, level, logger name, message. Сбор логов — задача платформы, не приложения. |
| XII | **Admin processes** | Одноразовые операции (создание Secret, `helm upgrade`, отладка `kubectl exec`) выполняются вне основного процесса. В репозитории нет встроенных migrate/cron внутри backend. |

Отклонения, которые нужно держать в голове:

- Redis persistence через `hostPath` (`/srv/redis`) привязывает данные к ноде, на которую попал под (сейчас это единственный worker). Это не portable volume и не HA-хранилище. Для стенда допустимо; для нескольких worker-нод — нет.
- Nginx-конфиг фронтенда в образе есть, но в кластере его перекрывает ConfigMap. Это удобно для смены `proxy_pass` без пересборки, ценой расхождения «образ vs runtime».
- ReadinessProbe backend смотрит на `/readyz`. Пока Redis недоступен или ключ пустой, под `NotReady` и не получает трафик. Если объекта Secret нет совсем, контейнер не стартует раньше probes.
- В кластере Secret `backend-secrets` с ключом `WEATHER_API_KEY` обязателен, иначе под не стартует (`CreateContainerConfigError`). «Старт без ключа» в Kubernetes работает, только если в Secret пустое значение.
- CI фактор V (build/release/run) и X (parity) пока не замыкает: release и run в кластер выполняются вручную.

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
├── frontend/                     # index.html, app.js, styles.css + nginx.conf для образа
├── dockerfile-backend
├── dockerfile-frontend
├── project-chart/                # Helm application chart
│   ├── Chart.yaml
│   ├── values.yaml
│   └── templates/
│       ├── project-namespace.yaml
│       ├── backend-template/     # deployment, service, configmap, secret, hpa
│       ├── frontend-template/    # deployment, service, configmap, hpa
│       └── redis-template/       # statefulset, headless service
├── ansible/                      # подготовка нод и kubeadm-кластер
│   ├── hosts.ini                 # control_plane + work_nodes
│   ├── requirements.yml          # community.general, ansible.posix
│   └── playbooks/
│       ├── fix_apt_sources.yaml
│       ├── dns.yaml
│       ├── server_settings.yaml
│       ├── control_plan_initialization_with_calico.yaml
│       └── work_node.yaml
└── .gitlab-ci.yml                # сборка образов (test/deploy не готовы)
```

---

## Границы и сознательные упрощения

- Один инстанс Redis, persistence на `hostPath`. Не Redis Cluster, не PVC, не anti-affinity.
- Кластер из одной control plane-ноды и одного worker, без HA control plane.
- Нет Ingress / TLS в чарте. Точка входа на стенде — Service + port-forward (или ручной Ingress снаружи).
- Нет NetworkPolicy, хотя Calico их поддерживает: внутри namespace любой под может ходить в Redis.
- Нет аутентификации пользователя. Ключ провайдера — серверный секрет.
- Нет очередей, нет записи пользовательских данных.
- Frontend валидирует город зеркально backend; источник истины для отказа — backend.
- CORS по умолчанию `*`; в кластере браузер ходит same-origin на Nginx, CORS на backend — запасной контур.

---

## Известные проблемы и что не доделано

### Helm-чарт

- Имена образов в `values.yaml` совпадают с CI: `weather-backend` и `weather-frontend`, тег `0.1`. Комментарий в values «CI выставляет IfNotPresent» по-прежнему не соответствует действительности: CI ничего не деплоит, в чарте остаётся `imagePullPolicy: Never`.
- `containerPort: 5000` в backend Deployment захардкожен и не следует за `backend.config.port`. То же у `livenessProbe` и `readinessProbe`: порт `5000` не берётся из `backend.config.port`.
- В `Chart.yaml` остались дефолты `helm create`: `description` и `appVersion: "1.16.0"` к проекту не относятся.

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
- без registry образ остаётся в Docker на runner и до containerd на нодах не доходит;
- нет единой сборки, если меняется только чарт;
- нет отдельного релиза Secret (`WEATHER_API_KEY` не должен попадать в values и в git);
- runners с тегами `build` и `ci` предполагаются, но не описаны как код инфраструктуры.

Пока pipeline не закрывает фактор V: build есть, release/run в кластер — вручную.

### Ansible

Сделано: путь от чистого Debian до кластера из двух нод — apt-репозитории, DNS, подготовка ядра и sysctl, containerd, kubeadm init с Calico, join worker. Коллекции `community.general` и `ansible.posix` зафиксированы в `requirements.yml`. Все playbook'и проходят `ansible-playbook --syntax-check`.

Не сделано:

- `resolvconf` и `python3-debian` нужны playbook'ам, но ими не ставятся;
- нет общего playbook (`site.yaml`), который запускает шаги в правильном порядке;
- не ставятся Helm и metrics-server;
- нет доставки образов на ноды (`ctr images import`) и `helm upgrade`;
- нет идемпотентного создания Secret `backend-secrets`;
- `work_node.yaml` создаёт новый bootstrap-токен при каждом запуске, даже если нода уже в кластере;
- версии, CIDR и адрес API-сервера заданы в `vars` внутри playbook'ов, а не в `group_vars`.

Итог: Ansible закрывает подготовку кластера; поставка приложения остаётся ручной (импорт образов + Helm), пока CI не дойдёт до deploy.
