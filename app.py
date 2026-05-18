import os
import json
import requests
from flask import Flask, render_template, request, redirect, url_for, jsonify, send_from_directory
import psycopg2
import psycopg2.extras
from datetime import date, timedelta
import calendar
from openai import OpenAI
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz
import gcal

app = Flask(__name__)
DATABASE_URL = (
    os.environ.get("DATABASE_URL") or
    os.environ.get("POSTGRES_URL") or
    os.environ.get("POSTGRESQL_URL")
).replace("postgres://", "postgresql://", 1)
openai_client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
NSK = pytz.timezone("Asia/Novosibirsk")

STATUSES = ["Новая", "В работе", "Завершена"]
PRIORITIES = ["Низкий", "Средний", "Высокий"]
ENERGIES = ["Лёгкая", "Средняя", "Тяжёлая"]
CATEGORIES = ["Работа", "Личное"]
RECURRENCES = ["Ежедневно", "Еженедельно", "Ежемесячно"]
STRATEGIC_AREAS = ["Бизнес", "Карьера", "Люди", "Нетворкинг", "Личное"]

STRATEGIC_SEED = [
    ("Качество связи — проект с МСК", "Бизнес"),
    ("Предпринимательство — проект с МСК", "Бизнес"),
    ("Слабости конкурентов — проект с МСК", "Бизнес"),
    ("Growth Hacking — проект с МСК", "Бизнес"),
    ("Орг. модель — проект с МСК", "Бизнес"),
    ("Взаимодействие с Альфой — проект вне компании", "Бизнес"),
    ("Взаимодействие с B2B клиентом — проект вне компании", "Бизнес"),
    ("Сколково LIFT — войти в полноценную программу", "Карьера"),
    ("Занять C-level в крупной компании", "Карьера"),
    ("Составить топ компаний и пообщаться с хантерами", "Карьера"),
    ("Подготовка к дистанционному формату — консультант/эксперт", "Карьера"),
    ("Определить цели в деньгах и позиции", "Карьера"),
    ("Выступать на публичных площадках — ВЭФ, СМИ, блог, видео", "Карьера"),
    ("Растить людей, преемников, лидеров", "Люди"),
    ("Найти менти и работать с ним", "Люди"),
    ("Общаться с менторами — Симдякин, Пятков, Торбахов, Федорова", "Люди"),
    ("Вести себя как CEO — смело, поддерживающе, взвешенно", "Люди"),
    ("После каждого общения человек чувствует себя лучше", "Люди"),
    ("Семья — разговоры на равных", "Люди"),
    ("Растить нетворкинг — GMR7, B2B клиенты", "Нетворкинг"),
    ("Войти в клуб предпринимателей — Крона, акселераторы", "Нетворкинг"),
    ("Спортивная цель — проплыть 3 км за час", "Личное"),
    ("Организовать путешествие / выступить на барабанах", "Личное"),
    ("Сохранять внутреннюю тишину — медитации, спорт, GLAD", "Личное"),
    ("Визуализировать себя в 50 лет — семья, загар, учусь, инновации", "Личное"),
    ("Делать сложное простым — аналогии, простота в общении", "Личное"),
]


def calc_next_deadline(deadline, recurrence):
    if not deadline or not recurrence:
        return None
    dl = date.fromisoformat(str(deadline))
    if recurrence == "Ежедневно":
        return dl + timedelta(days=1)
    if recurrence == "Еженедельно":
        return dl + timedelta(weeks=1)
    if recurrence == "Ежемесячно":
        month = dl.month % 12 + 1
        year = dl.year + (dl.month // 12)
        day = min(dl.day, calendar.monthrange(year, month)[1])
        return date(year, month, day)
    return None


def get_db():
    return psycopg2.connect(DATABASE_URL)


def init_db():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id SERIAL PRIMARY KEY,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'Новая',
                    priority TEXT NOT NULL DEFAULT 'Средний',
                    deadline DATE,
                    created_at DATE DEFAULT CURRENT_DATE,
                    description TEXT,
                    project TEXT,
                    energy TEXT NOT NULL DEFAULT 'Средняя'
                )
            """)
            cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS description TEXT")
            cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS project TEXT")
            cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS energy TEXT NOT NULL DEFAULT 'Средняя'")
            cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS tags TEXT")
            cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS assignee TEXT")
            cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS progress TEXT")
            cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS calendar_event_id TEXT")
            cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS recurrence TEXT")
            cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS closed_at DATE")
            cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS category TEXT")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS subtasks (
                    id SERIAL PRIMARY KEY,
                    task_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
                    text TEXT NOT NULL,
                    done BOOLEAN DEFAULT FALSE
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS strategic_goals (
                    id SERIAL PRIMARY KEY,
                    title TEXT NOT NULL,
                    area TEXT,
                    created_at DATE DEFAULT CURRENT_DATE
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS strategic_logs (
                    id SERIAL PRIMARY KEY,
                    goal_id INTEGER REFERENCES strategic_goals(id) ON DELETE CASCADE,
                    logged_at DATE DEFAULT CURRENT_DATE,
                    text TEXT NOT NULL
                )
            """)
            cur.execute("SELECT COUNT(*) FROM strategic_goals")
            if cur.fetchone()[0] == 0:
                cur.executemany(
                    "INSERT INTO strategic_goals (title, area) VALUES (%s, %s)",
                    STRATEGIC_SEED
                )


def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, json={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
    }, timeout=10)


def build_digest(digest_type="daily"):
    today = date.today().isoformat()

    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT title, status, priority, deadline, category, closed_at, progress, assignee FROM tasks ORDER BY created_at")
            tasks = cur.fetchall()

    if not tasks:
        send_telegram("📋 *Дайджест задач*\n\nЗадач пока нет.")
        return

    def fmt(t):
        line = f"- [{t['category'] or '?'}] «{t['title']}»"
        if t['deadline']:
            line += f" | дедлайн: {t['deadline']}"
        if t['assignee']:
            line += f" | {t['assignee']}"
        if t['progress']:
            line += f" | прогресс: {t['progress'][:60]}"
        return line

    closed_today = [t for t in tasks if t['closed_at'] and str(t['closed_at'])[:10] == today]
    overdue = [t for t in tasks if t['status'] != 'Завершена' and t['deadline'] and str(t['deadline']) < today]
    active_hp = [t for t in tasks if t['status'] != 'Завершена' and t['priority'] == 'Высокий'
                 and (not t['deadline'] or str(t['deadline']) >= today)]

    sections = []
    sections.append(
        "✅ Завершено сегодня:\n" + "\n".join(fmt(t) for t in closed_today)
        if closed_today else "✅ Завершено сегодня: ничего"
    )
    sections.append(
        "🔴 Просрочено:\n" + "\n".join(fmt(t) for t in overdue)
        if overdue else "🔴 Просрочено: нет"
    )
    sections.append(
        "🔥 В работе (высокий приоритет):\n" + "\n".join(fmt(t) for t in active_hp)
        if active_hp else "🔥 В работе (высокий приоритет): нет"
    )

    tasks_structured = "\n\n".join(sections)

    if digest_type == "weekly":
        header = "📊 *Итоги недели — оценка выполнения обещаний*"
        period = "за эту неделю"
    else:
        header = "🌙 *Дайджест — оценка выполнения обещаний*"
        period = "за сегодня"

    prompt = f"""Ты жёсткий, честный коуч топ-менеджера. Без корпоративного языка, без комплиментов. Только факты и прямая оценка.

Сегодня {today}. Задачи структурированы по факту:

{tasks_structured}

Сделай оценку выполнения обещаний {period} по двум категориям.

🏢 РАБОЧИЕ ЦЕЛИ (категория Работа)
1. Что было обещано
2. Что реально выполнено
3. Что просрочено или зависло
4. Оценка: X/10

👤 ЛИЧНЫЕ ЦЕЛИ (категория Личное)
1. Что было обещано
2. Что реально выполнено
3. Что просрочено или зависло
4. Оценка: X/10

📌 ИТОГО
— Общая оценка: X/10
— Главный паттерн: что системно не выполняется
— Одна конкретная рекомендация

Шкала: 9-10 = отлично, 7-8 = хорошо, 5-6 = средне, ниже 5 = провал. Будь честным."""

    response = openai_client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {
                "role": "system",
                "content": "Ты жёсткий честный коуч. Отвечай на русском, структурированно, без воды и лести."
            },
            {
                "role": "user",
                "content": prompt
            }
        ]
    )

    summary = response.choices[0].message.content
    send_telegram(f"{header}\n\n{summary}")


def daily_digest():
    build_digest("daily")


def weekly_digest():
    build_digest("weekly")


def build_strategic_digest():
    today = date.today()
    today_str = today.isoformat()

    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM strategic_goals ORDER BY area, title")
            goals = cur.fetchall()
            if not goals:
                send_telegram("🧭 *Стратегический обзор*\n\nСтратегических целей пока нет.")
                return
            goal_logs = {}
            for g in goals:
                cur.execute("""
                    SELECT text, logged_at FROM strategic_logs
                    WHERE goal_id = %s ORDER BY logged_at DESC, id DESC LIMIT 3
                """, (g['id'],))
                goal_logs[g['id']] = cur.fetchall()

    lines = []
    for g in goals:
        logs = goal_logs.get(g['id'], [])
        if logs:
            last_date = logs[0]['logged_at']
            days_ago = (today - last_date).days
            log_entries = "\n".join(f"  [{l['logged_at']}] {l['text']}" for l in logs)
            activity = f"Последняя запись: {days_ago} дн. назад\n{log_entries}"
        else:
            days_ago = 999
            activity = "Записей нет — цель ни разу не обновлялась"
        lines.append(f"[{g['area']}] «{g['title']}»\n{activity}")

    goals_structured = "\n\n".join(lines)

    prompt = f"""Ты жёсткий честный коуч топ-менеджера. Еженедельный стратегический обзор.

Сегодня {today_str}.

Стратегические цели и активность:

{goals_structured}

Правила оценки каждой цели:
- Запись за последние 7 дней → отметь прогресс, спроси "что дальше?"
- 8-14 дней без записи → мягкий вызов: "что остановило?"
- 15-30 дней → жёсткий вызов: назови конкретное следующее действие
- 30+ дней → прямой вопрос: "Эта цель живая или ты её уже отпустил?"

Структура ответа:
🧭 СТРАТЕГИЧЕСКИЙ ОБЗОР

По каждой цели — одна строка: статус + вопрос или комментарий.

💪 ПРИЗНАНИЕ — что реально двигалось на этой неделе.

📌 ГЛАВНЫЙ ВОПРОС НЕДЕЛИ — один сильный вопрос по самой застывшей зоне.

Без воды. Без корпоративного языка. Максимально конкретно."""

    response = openai_client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": "Ты жёсткий честный коуч. Отвечай на русском, без воды."},
            {"role": "user", "content": prompt}
        ]
    )
    send_telegram(f"🧭 *Стратегический обзор*\n\n{response.choices[0].message.content}")


def strategic_review():
    build_strategic_digest()


# ── Планировщик ────────────────────────────────────────────
scheduler = BackgroundScheduler(timezone=NSK)
scheduler.add_job(daily_digest, CronTrigger(hour=21, minute=45, timezone=NSK))
scheduler.add_job(weekly_digest, CronTrigger(day_of_week="sun", hour=20, minute=0, timezone=NSK))
scheduler.add_job(strategic_review, CronTrigger(day_of_week="sun", hour=10, minute=0, timezone=NSK))
scheduler.start()


def parse_tags(raw):
    if not raw:
        return []
    return [t.strip().lstrip("#") for t in raw.replace(",", " ").split() if t.strip()]


def normalize_tags(raw):
    tags = parse_tags(raw)
    return ",".join(tags) if tags else None


@app.route("/tags")
def get_tags():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT tags FROM tasks WHERE tags IS NOT NULL AND tags != ''")
            rows = cur.fetchall()
    all_tags = set()
    for row in rows:
        all_tags.update(parse_tags(row[0]))
    return jsonify(sorted(all_tags))


@app.route("/")
def index():
    priority_filter = request.args.get("priority", "")
    search_query = request.args.get("q", "").strip()

    query = """
        SELECT t.*,
            COUNT(s.id) AS subtask_total,
            COUNT(s.id) FILTER (WHERE s.done) AS subtask_done
        FROM tasks t
        LEFT JOIN subtasks s ON s.task_id = t.id
        WHERE t.status != 'Завершена'
    """
    params = []

    if priority_filter:
        query += " AND t.priority = %s"
        params.append(priority_filter)
    if search_query:
        query += " AND (t.title ILIKE %s OR t.assignee ILIKE %s)"
        params.extend([f"%{search_query}%"] * 2)

    query += """
        GROUP BY t.id
        ORDER BY
            t.category NULLS LAST,
            CASE t.priority WHEN 'Высокий' THEN 1 WHEN 'Средний' THEN 2 ELSE 3 END,
            t.deadline ASC NULLS LAST
    """

    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, params)
            tasks = cur.fetchall()

            cur.execute("""
                SELECT * FROM tasks WHERE status = 'Завершена'
                ORDER BY closed_at DESC NULLS LAST, id DESC
            """)
            done_tasks = cur.fetchall()

            cur.execute("""
                SELECT g.*,
                    MAX(l.logged_at) AS last_log_date,
                    (SELECT text FROM strategic_logs
                     WHERE goal_id = g.id ORDER BY logged_at DESC, id DESC LIMIT 1) AS last_log_text
                FROM strategic_goals g
                LEFT JOIN strategic_logs l ON l.goal_id = g.id
                GROUP BY g.id
                ORDER BY g.area, g.title
            """)
            strategic_goals = cur.fetchall()

    return render_template(
        "index.html",
        tasks=tasks,
        done_tasks=done_tasks,
        strategic_goals=strategic_goals,
        statuses=STATUSES,
        priorities=PRIORITIES,
        energies=ENERGIES,
        recurrences=RECURRENCES,
        categories=CATEGORIES,
        strategic_areas=STRATEGIC_AREAS,
        priority_filter=priority_filter,
        search_query=search_query,
        today=date.today(),
        parse_tags=parse_tags,
    )


@app.route("/strategy/add", methods=["POST"])
def strategy_add():
    title = request.form.get("title", "").strip()
    area = request.form.get("area", "")
    if title:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO strategic_goals (title, area) VALUES (%s, %s)",
                    (title, area)
                )
    return redirect("/?tab=strategy")


@app.route("/strategy/log/<int:goal_id>", methods=["POST"])
def strategy_log(goal_id):
    text = request.form.get("text", "").strip()
    if text:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO strategic_logs (goal_id, text) VALUES (%s, %s)",
                    (goal_id, text)
                )
    return redirect("/?tab=strategy")


@app.route("/strategy/edit/<int:goal_id>", methods=["POST"])
def strategy_edit(goal_id):
    title = request.form.get("title", "").strip()
    area = request.form.get("area", "")
    if title:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE strategic_goals SET title=%s, area=%s WHERE id=%s",
                    (title, area, goal_id)
                )
    return redirect("/?tab=strategy")


@app.route("/strategy/delete/<int:goal_id>", methods=["POST"])
def strategy_delete(goal_id):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM strategic_goals WHERE id=%s", (goal_id,))
    return redirect("/?tab=strategy")


@app.route("/strategy/digest", methods=["POST"])
def strategy_digest_manual():
    try:
        build_strategic_digest()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/add", methods=["POST"])
def add():
    title = request.form.get("title", "").strip()
    if not title:
        return redirect(url_for("index"))

    status = request.form.get("status", "Новая")
    priority = request.form.get("priority", "Средний")
    deadline = request.form.get("deadline") or None
    project = request.form.get("project", "").strip() or None
    energy = request.form.get("energy", "Средняя")
    assignee = request.form.get("assignee", "").strip() or None
    recurrence = request.form.get("recurrence", "").strip() or None
    category = request.form.get("category", "Работа")

    event_id = gcal.create_event(title, deadline, priority, None, assignee, recurrence) if deadline else None

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO tasks (title, status, priority, deadline, energy, assignee, calendar_event_id, recurrence, category)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (title, status, priority, deadline, energy, assignee, event_id, recurrence, category),
            )
    return redirect(url_for("index"))


@app.route("/update/<int:task_id>", methods=["POST"])
def update(task_id):
    status = request.form.get("status")
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM tasks WHERE id=%s", (task_id,))
            task = cur.fetchone()
            if not task:
                return redirect(url_for("index"))

            was_completed = task["status"] == "Завершена"
            now_completed = status == "Завершена"
            closed_at = date.today() if now_completed else None

            cur.execute("UPDATE tasks SET status=%s, closed_at=%s WHERE id=%s",
                        (status, closed_at, task_id))

            if now_completed and not was_completed and task["recurrence"]:
                next_dl = calc_next_deadline(task["deadline"], task["recurrence"])
                if task["calendar_event_id"]:
                    gcal.delete_event(task["calendar_event_id"])
                new_event_id = gcal.create_event(
                    task["title"], next_dl, task["priority"],
                    None, task["assignee"], task["recurrence"]
                ) if next_dl else None
                cur.execute(
                    "UPDATE tasks SET calendar_event_id=NULL WHERE id=%s", (task_id,)
                )
                cur.execute(
                    "INSERT INTO tasks (title, status, priority, deadline, energy,"
                    " assignee, recurrence, calendar_event_id, category)"
                    " VALUES (%s,'Новая',%s,%s,%s,%s,%s,%s,%s)",
                    (task["title"], task["priority"], next_dl,
                     task["energy"], task["assignee"], task["recurrence"],
                     new_event_id, task["category"])
                )
    return redirect(url_for("index"))


@app.route("/edit/<int:task_id>", methods=["GET"])
def edit(task_id):
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM tasks WHERE id = %s", (task_id,))
            task = cur.fetchone()
    if not task:
        return redirect(url_for("index"))
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM subtasks WHERE task_id=%s ORDER BY id", (task_id,))
            subtasks = cur.fetchall()
    return render_template("edit.html", task=task, subtasks=subtasks,
                           statuses=STATUSES, priorities=PRIORITIES, energies=ENERGIES,
                           recurrences=RECURRENCES, categories=CATEGORIES,
                           today=date.today(), parse_tags=parse_tags)


@app.route("/edit/<int:task_id>", methods=["POST"])
def edit_save(task_id):
    title = request.form.get("title", "").strip()
    if not title:
        return redirect(url_for("edit", task_id=task_id))

    status = request.form.get("status", "Новая")
    priority = request.form.get("priority", "Средний")
    deadline = request.form.get("deadline") or None
    energy = request.form.get("energy", "Средняя")
    assignee = request.form.get("assignee", "").strip() or None
    progress = request.form.get("progress", "").strip() or None
    recurrence = request.form.get("recurrence", "").strip() or None
    category = request.form.get("category", "Работа")

    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT calendar_event_id, status, closed_at FROM tasks WHERE id=%s", (task_id,))
            row = cur.fetchone()

    existing_event_id = row["calendar_event_id"] if row else None
    new_event_id = existing_event_id

    if deadline:
        if existing_event_id:
            gcal.update_event(existing_event_id, title, deadline, priority, None, assignee, recurrence)
        else:
            new_event_id = gcal.create_event(title, deadline, priority, None, assignee, recurrence)
    else:
        if existing_event_id:
            gcal.delete_event(existing_event_id)
            new_event_id = None

    closed_at = None
    if row:
        if status == "Завершена" and row["status"] != "Завершена":
            closed_at = date.today()
        elif status == "Завершена":
            closed_at = row["closed_at"]

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tasks SET title=%s, status=%s, priority=%s, deadline=%s,"
                " energy=%s, assignee=%s, progress=%s, calendar_event_id=%s, recurrence=%s,"
                " closed_at=%s, category=%s WHERE id=%s",
                (title, status, priority, deadline, energy, assignee, progress,
                 new_event_id, recurrence, closed_at, category, task_id),
            )
    return redirect(url_for("index"))


@app.route("/delete/<int:task_id>", methods=["POST"])
def delete(task_id):
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT calendar_event_id FROM tasks WHERE id=%s", (task_id,))
            row = cur.fetchone()
            if row and row["calendar_event_id"]:
                gcal.delete_event(row["calendar_event_id"])
            cur.execute("DELETE FROM tasks WHERE id = %s", (task_id,))
    return redirect(url_for("index"))


@app.route("/voice", methods=["POST"])
def voice():
    audio_file = request.files.get("audio")
    if not audio_file:
        return jsonify({"error": "Нет аудио"}), 400

    transcript = openai_client.audio.transcriptions.create(
        model="whisper-1",
        file=("audio.webm", audio_file.stream, audio_file.mimetype or "audio/webm"),
        language="ru",
    )
    text = transcript.text

    today = date.today().isoformat()
    response = openai_client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {
                "role": "system",
                "content": (
                    "Ты помощник, который извлекает поля задачи из голосового сообщения. "
                    "Верни ТОЛЬКО валидный JSON без markdown-блоков. "
                    "Поля: title (строка), priority (одно из: Низкий, Средний, Высокий), "
                    "status (одно из: Новая, В работе, Завершена), "
                    "deadline (дата YYYY-MM-DD или null), "
                    "description (краткое описание или контекст задачи, строка или null), "
                    "project (название проекта или направления, строка или null), "
                    "energy (одно из: Лёгкая, Средняя, Тяжёлая — оцени по сложности задачи), "
                    "assignee (кто должен сделать задачу, строка или null), "
                    "category (одно из: Работа, Личное — определи по смыслу задачи). "
                    "Если приоритет не упомянут — Средний. Если статус не упомянут — Новая. "
                    "Если энергия не упомянута — оцени самостоятельно по смыслу задачи."
                )
            },
            {
                "role": "user",
                "content": f"Сегодня {today}. Голосовое сообщение: «{text}»"
            }
        ],
        response_format={"type": "json_object"},
    )

    fields = json.loads(response.choices[0].message.content)
    fields["transcript"] = text
    return jsonify(fields)


def tg_reply(chat_id, text):
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
        timeout=10,
    )


def get_tg_file(file_id):
    info = requests.get(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile",
        params={"file_id": file_id}, timeout=10
    ).json()
    file_path = info["result"]["file_path"]
    audio = requests.get(
        f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}", timeout=30
    )
    return audio.content


def get_all_tasks_text():
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, title, status, priority, deadline, project, tags, energy FROM tasks ORDER BY created_at")
            tasks = cur.fetchall()
    if not tasks:
        return None, []
    lines = []
    for t in tasks:
        line = f"[{t['id']}] «{t['title']}» | {t['status']} | {t['priority']}"
        if t['project']:
            line += f" | {t['project']}"
        if t['deadline']:
            line += f" | до {t['deadline']}"
        if t['tags']:
            line += f" | #{' #'.join(parse_tags(t['tags']))}"
        lines.append(line)
    return "\n".join(lines), tasks


def process_bot_message(text):
    today = date.today().isoformat()
    tasks_text, _ = get_all_tasks_text()
    tasks_context = f"Список задач:\n{tasks_text}" if tasks_text else "Задач пока нет."

    response = openai_client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {
                "role": "system",
                "content": (
                    f"Ты умный ассистент таск-менеджера. Сегодня {today}.\n"
                    f"{tasks_context}\n\n"
                    "Ты можешь:\n"
                    "1. Отвечать на вопросы о задачах\n"
                    "2. Добавлять новые задачи\n"
                    "3. Давать дайджест/статус\n\n"
                    "Верни JSON: {\"action\": \"answer\"|\"add_task\", \"text\": \"...\", "
                    "\"task\": {\"title\": ..., \"priority\": \"Низкий|Средний|Высокий\", "
                    "\"status\": \"Новая|В работе|Завершена\", \"deadline\": \"YYYY-MM-DD или null\", "
                    "\"project\": \"...\", \"assignee\": \"...\", \"energy\": \"Лёгкая|Средняя|Тяжёлая\"}}.\n"
                    "action=add_task если пользователь хочет добавить задачу. "
                    "action=answer для всего остального. "
                    "text — ответ пользователю на русском, кратко."
                )
            },
            {"role": "user", "content": text}
        ],
        response_format={"type": "json_object"},
    )

    result = json.loads(response.choices[0].message.content)

    if result.get("action") == "add_task" and result.get("task"):
        t = result["task"]
        title = t.get("title", "Без названия")
        deadline = t.get("deadline") or None
        priority = t.get("priority", "Средний")
        assignee = t.get("assignee") or None
        event_id = gcal.create_event(title, deadline, priority, None, assignee) if deadline else None
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO tasks (title, status, priority, deadline, energy, assignee, calendar_event_id)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (
                        title,
                        t.get("status", "Новая"),
                        priority,
                        deadline,
                        t.get("energy", "Средняя"),
                        assignee,
                        event_id,
                    )
                )
        return result.get("text", "✅ Задача добавлена")

    return result.get("text", "Не понял запрос — попробуй ещё раз")


@app.route("/webhook", methods=["POST"])
def webhook():
    if not TELEGRAM_TOKEN:
        return jsonify({"ok": False}), 400

    update = request.json or {}
    message = update.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    if not chat_id:
        return jsonify({"ok": True})

    # Только от владельца
    if str(chat_id) != str(TELEGRAM_CHAT_ID):
        tg_reply(chat_id, "Нет доступа.")
        return jsonify({"ok": True})

    try:
        # Голосовое сообщение
        if "voice" in message:
            tg_reply(chat_id, "🎙 Обрабатываю...")
            audio_bytes = get_tg_file(message["voice"]["file_id"])
            import io
            transcript = openai_client.audio.transcriptions.create(
                model="whisper-1",
                file=("voice.ogg", io.BytesIO(audio_bytes), "audio/ogg"),
                language="ru",
            )
            text = transcript.text
            reply = process_bot_message(text)
            tg_reply(chat_id, f"🎙 «{text}»\n\n{reply}")

        # Текстовое сообщение
        elif "text" in message:
            text = message["text"]
            if text == "/start":
                tg_reply(chat_id, "👋 Привет! Я таск-менеджер.\n\nМогу:\n• Показать задачи\n• Добавить задачу голосом или текстом\n• Дать дайджест\n\nПросто напиши или надиктуй.")
            else:
                reply = process_bot_message(text)
                tg_reply(chat_id, reply)

    except Exception as e:
        tg_reply(chat_id, f"⚠️ Ошибка: {str(e)[:200]}")

    return jsonify({"ok": True})


@app.route("/setup-webhook")
def setup_webhook():
    if not TELEGRAM_TOKEN or not WEBHOOK_URL:
        return jsonify({"error": "TELEGRAM_BOT_TOKEN или WEBHOOK_URL не настроены"}), 400
    res = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook",
        json={"url": WEBHOOK_URL},
        timeout=10,
    ).json()
    return jsonify(res)


@app.route("/ai-search", methods=["POST"])
def ai_search():
    query = (request.json or {}).get("query", "").strip()
    if not query:
        return jsonify({"ids": [], "explanation": "Пустой запрос"}), 400

    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, title, status, priority, deadline, description, project, tags FROM tasks ORDER BY created_at")
            tasks = cur.fetchall()

    if not tasks:
        return jsonify({"ids": [], "explanation": "Задач нет"})

    tasks_text = "\n".join([
        f"[{t['id']}] «{t['title']}»"
        + (f" | проект: {t['project']}" if t['project'] else "")
        + (f" | теги: {t['tags']}" if t['tags'] else "")
        + (f" | {t['description'][:100]}" if t['description'] else "")
        + f" | {t['status']} | {t['priority']}"
        for t in tasks
    ])

    response = openai_client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {
                "role": "system",
                "content": (
                    "Ты помощник для поиска задач. Найди задачи, которые соответствуют запросу по смыслу. "
                    "Верни JSON: {\"ids\": [список id], \"explanation\": \"краткое объяснение на русском\"}. "
                    "ids — массив числовых id подходящих задач. Если ничего не найдено — пустой массив."
                )
            },
            {
                "role": "user",
                "content": f"Запрос: «{query}»\n\nЗадачи:\n{tasks_text}"
            }
        ],
        response_format={"type": "json_object"},
    )

    result = json.loads(response.choices[0].message.content)
    return jsonify(result)


@app.route("/digest", methods=["POST"])
def digest_manual():
    digest_type = request.json.get("type", "daily") if request.is_json else "daily"
    try:
        build_digest(digest_type)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/analyze", methods=["POST"])
def analyze():
    today = date.today().isoformat()

    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT title, status, priority, deadline FROM tasks ORDER BY created_at")
            tasks = cur.fetchall()

    if not tasks:
        return jsonify({"summary": "Задач пока нет — добавьте первую!"})

    tasks_text = "\n".join([
        f"- «{t['title']}» | статус: {t['status']} | приоритет: {t['priority']} | дедлайн: {t['deadline'] or 'не указан'}"
        for t in tasks
    ])

    response = openai_client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {
                "role": "system",
                "content": "Ты помощник-менеджер задач. Отвечай на русском языке, кратко и структурированно."
            },
            {
                "role": "user",
                "content": f"Сегодня {today}. Вот список задач:\n{tasks_text}\n\n"
                           f"Сделай резюме: что выполнено, что просрочено, что в работе, "
                           f"что ещё не начато. Предложи как сгруппировать задачи по смыслу."
            }
        ]
    )

    summary = response.choices[0].message.content
    return jsonify({"summary": summary})


@app.route("/subtask/add", methods=["POST"])
def subtask_add():
    task_id = request.form.get("task_id", type=int)
    text = request.form.get("text", "").strip()
    if task_id and text:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO subtasks (task_id, text) VALUES (%s, %s)", (task_id, text))
    return redirect(url_for("edit", task_id=task_id))


@app.route("/subtask/toggle/<int:sub_id>", methods=["POST"])
def subtask_toggle(sub_id):
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT task_id, done FROM subtasks WHERE id=%s", (sub_id,))
            row = cur.fetchone()
            if row:
                cur.execute("UPDATE subtasks SET done=%s WHERE id=%s", (not row["done"], sub_id))
                return redirect(url_for("edit", task_id=row["task_id"]))
    return redirect(url_for("index"))


@app.route("/subtask/delete/<int:sub_id>", methods=["POST"])
def subtask_delete(sub_id):
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT task_id FROM subtasks WHERE id=%s", (sub_id,))
            row = cur.fetchone()
            cur.execute("DELETE FROM subtasks WHERE id=%s", (sub_id,))
            if row:
                return redirect(url_for("edit", task_id=row["task_id"]))
    return redirect(url_for("index"))


@app.route("/dashboard")
def dashboard():
    today = date.today()
    week_ago = today - timedelta(days=7)
    month_ago = today - timedelta(days=30)

    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT status, COUNT(*) AS cnt FROM tasks GROUP BY status")
            by_status = {r["status"]: r["cnt"] for r in cur.fetchall()}

            cur.execute("SELECT priority, COUNT(*) AS cnt FROM tasks WHERE status != 'Завершена' GROUP BY priority")
            by_priority = {r["priority"]: r["cnt"] for r in cur.fetchall()}

            cur.execute("SELECT COUNT(*) AS cnt FROM tasks WHERE status='Завершена' AND closed_at >= %s", (week_ago,))
            closed_7 = cur.fetchone()["cnt"]

            cur.execute("SELECT COUNT(*) AS cnt FROM tasks WHERE status='Завершена' AND closed_at >= %s", (month_ago,))
            closed_30 = cur.fetchone()["cnt"]

            cur.execute("SELECT COUNT(*) AS cnt FROM tasks WHERE deadline < %s AND status != 'Завершена'", (today,))
            overdue = cur.fetchone()["cnt"]

            cur.execute("""
                SELECT project, COUNT(*) AS cnt
                FROM tasks WHERE status != 'Завершена' AND project IS NOT NULL
                GROUP BY project ORDER BY cnt DESC LIMIT 8
            """)
            by_project = cur.fetchall()

            cur.execute("SELECT COUNT(*) AS cnt FROM tasks WHERE status != 'Завершена'")
            open_total = cur.fetchone()["cnt"]

            cur.execute("SELECT ROUND(AVG(CURRENT_DATE - created_at)) AS avg_age FROM tasks WHERE status != 'Завершена'")
            avg_age = cur.fetchone()["avg_age"] or 0

    return render_template("dashboard.html",
        by_status=by_status,
        by_priority=by_priority,
        closed_7=closed_7,
        closed_30=closed_30,
        overdue=overdue,
        by_project=by_project,
        open_total=open_total,
        avg_age=int(avg_age),
        today=today,
    )


@app.route("/static/sw.js")
def service_worker():
    return send_from_directory("static", "sw.js", mimetype="application/javascript")


@app.route("/health")
def health():
    result = {}
    result["DATABASE_URL_set"] = bool(os.environ.get("DATABASE_URL"))
    result["OPENAI_API_KEY_set"] = bool(os.environ.get("OPENAI_API_KEY"))
    result["TELEGRAM_configured"] = bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID)
    result["DATABASE_URL_prefix"] = (os.environ.get("DATABASE_URL") or "")[:30]
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        result["db"] = "OK"
    except Exception as e:
        result["db"] = str(e)
    return jsonify(result)


@app.errorhandler(500)
def server_error(e):
    import traceback
    return f"<pre>{traceback.format_exc()}</pre>", 500


try:
    init_db()
except Exception as e:
    print(f"init_db error: {e}")

if __name__ == "__main__":
    app.run()
