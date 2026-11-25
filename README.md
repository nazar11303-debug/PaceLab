# PaceLab

> **Українська** | [English](#english)

PaceLab — локальний застосунок для імпорту й аналізу бігових тренувань. Він
зберігає історію у SQLite на вашому комп'ютері та надає вебінтерфейс для
перегляду тренувань, навантаження й прогресу.

## Можливості

- Імпорт тренувань із файлів `.gpx` і `.fit`, збереження маршрутів і сплітів.
- Статистика темпу, пульсу, дистанції та набору висоти; особисті рекорди,
  тренувальне навантаження, форма й відновлення.
- Облік кросівок і їхнього пробігу, редагування тренувань та захист від дублів.
- Карта маршрутів, теплова карта, конструктор маршрутів і планувальник забігу.
- Погода для тренування, експорт історії у CSV та резервна копія у JSON.
- Адаптивний інтерфейс і підтримка встановлення як PWA.

База даних і завантажені тренувальні файли зберігаються локально в `PaceLab.db`
та `uploads/`. Ці дані, резервні копії й локальні середовища виключені з Git.
Для карт, прокладання маршрутів і отримання погоди потрібен інтернет; основні
локальні дані лишаються на вашому пристрої.

## Запуск (Windows)

**Швидкий спосіб:** запустіть `start.bat`. Скрипт за потреби створить `.venv`,
встановить залежності та запустить сервер. Відкрийте
<http://127.0.0.1:8000>. Щоб відкрити застосунок на телефоні в тій самій
Wi-Fi-мережі, скористайтеся IP-адресою, показаною скриптом.

**Ручний запуск** у PowerShell (потрібен Python 3.11 або новіший):

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Після запуску відкрийте <http://127.0.0.1:8000>. Щоб зупинити сервер, натисніть
`Ctrl+C` у консолі. Застосунок створює порожню базу даних під час першого
запуску.

## Перевірки

```powershell
.\.venv\Scripts\python.exe tools\smoke_test.py
.\.venv\Scripts\python.exe tools\stage3_test.py
```

## English

PaceLab is a local web app for importing and analyzing running workouts. It
keeps your activity history in a SQLite database on your computer and provides
a dashboard for exploring workouts, training load, and progress.

### Features

- Import `.gpx` and `.fit` workouts, including route data and pace splits.
- Review pace, heart rate, distance, and elevation; personal records, training
  load, fitness, and recovery metrics.
- Manage shoes and mileage, edit activities, and detect duplicate imports.
- Explore route maps and heatmaps, build routes, and plan a race.
- View workout weather, export activity history to CSV, and create JSON backups.
- Responsive interface with Progressive Web App (PWA) support.

The database and uploaded workout files are stored locally in `PaceLab.db` and
`uploads/`. They, along with backups and local environments, are excluded from
Git. Maps, route planning, and weather lookup require an internet connection;
your locally stored activity data stays on your device.

### Run on Windows

**Quick start:** run `start.bat`. It creates `.venv` and installs dependencies
if needed, then starts the server. Open <http://127.0.0.1:8000>. To access the
app from a phone on the same Wi-Fi network, use the IP address printed by the
script.

**Manual setup** in PowerShell (Python 3.11 or newer):

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Open <http://127.0.0.1:8000>. Press `Ctrl+C` in the console to stop the server.
On first launch, PaceLab creates an empty database.
