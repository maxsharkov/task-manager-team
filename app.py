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
            cur.execute("SELECT title, status, priority, deadline, energy, category, closed_at, progress, assignee FROM tasks ORDER BY created_at")
            tasks = cur.fetchall()

    if not tasks:
        send_telegram("📋 *Дайджест задач*\n\nЗадач пока нет.")
        return

    tasks_text = "\n".join([
        f"- [{t['category'] or '?'}] «{t['title']}» | {t['status']} | {t['priority']}"
        f" | дедлайн: {t['deadline'] or 'не указан'}"
        + (f" | закрыта: {t['closed_at']}" if t['closed_at'] else "")
        + (f" | исполнитель: {t['assignee']}" if t['assignee'] else "")
        + (f" | прогресс: {t['progress']}" if t['progress'] else "")
        for t in tasks
    ])

    if digest_type == "weekly":
        header = "📊 *Итоги недели — оценка выполнения обещаний*"
        period = "за эту неделю"
    else:
        header = "🌙 *Дайджест — оценка выполнения обещаний*"
        period = "за сегодня"

    prompt = f"""Ты жёсткий, честный коуч топ-менеджера. Без корпоративного языка, без комплиментов. Только факты и прямая оценка.

Сегодня {today}. Задачи (категория указана в скобках):
{tasks_text}

Сделай оценку выполнения обещаний по двум категориям.

🏢 РАБОЧИЕ ЦЕЛИ
1. Что было обещано (высокий приоритет или дедлайн ≤ сегодня, категория Работа)
2. Что реально выполнено
3. Что просрочено или зависло
4. Оценка: X/10

👤 ЛИЧНЫЕ ЦЕЛИ
1. Что было обещано (высокий приоритет или дедлайн ≤ сегодня, категория Личное)
2. Что реально выполнено
3. Что просрочено или зависло
4. Оценка: X/10

📌 ИТОГО
— Общая оценка: X/10
— Главный паттерн: что системно не выполняется
— Одна конкретная рекомендация на следующие 7 дней

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


# ── Планировщик ────────────────────────────────────────────
scheduler = BackgroundScheduler(timezone=NSK)
# Ежедневно в 21:45 по Новосибирску
scheduler.add_job(daily_digest, CronTrigger(hour=21, minute=45, timezone=NSK))
# Воскресенье в 20:00 по Новосибирску
scheduler.add_job(weekly_digest, CronTrigger(day_of_week="sun", hour=20, minute=0, timezone=NSK))
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
    status_filter = request.args.get("status", "")
    priority_filter = request.args.get("priority", "")
    tag_filter = request.args.get("tag", "")
    search_query = request.args.get("q", "").strip()

    query = """
        SELECT t.*,
            COUNT(s.id) AS subtask_total,
            COUNT(s.id) FILTER (WHERE s.done) AS subtask_done
        FROM tasks t
        LEFT JOIN subtasks s ON s.task_id = t.id
        WHERE 1=1
    """
    params = []

    if status_filter:
        query += " AND t.status = %s"
        params.append(status_filter)
    if priority_filter:
        query += " AND t.priority = %s"
        params.append(priority_filter)
    if tag_filter:
        query += " AND t.tags ILIKE %s"
        params.append(f"%{tag_filter}%")
    if search_query:
        query += " AND (t.title ILIKE %s OR t.project ILIKE %s OR t.assignee ILIKE %s)"
        params.extend([f"%{search_query}%"] * 3)

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

    return render_template(
        "index.html",
        tasks=tasks,
        statuses=STATUSES,
        priorities=PRIORITIES,
        energies=ENERGIES,
        recurrences=RECURRENCES,
        categories=CATEGORIES,
        status_filter=status_filter,
        priority_filter=priority_filter,
        tag_filter=tag_filter,
        search_query=search_query,
        today=date.today(),
        parse_tags=parse_tags,
    )


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

    event_id = gcal.create_event(title, deadline, priority, None, assignee) if deadline else None

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
                event_id = gcal.create_event(task["title"], next_dl, task["priority"],
                                             task["project"], task["assignee"]) if next_dl else None
                cur.execute(
                    "INSERT INTO tasks (title, status, priority, deadline, project, energy,"
                    " assignee, recurrence, calendar_event_id)"
                    " VALUES (%s,'Новая',%s,%s,%s,%s,%s,%s,%s)",
                    (task["title"], task["priority"], next_dl, task["project"],
                     task["energy"], task["assignee"], task["recurrence"], event_id)
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
            gcal.update_event(existing_event_id, title, deadline, priority, project, assignee)
        else:
            new_event_id = gcal.create_event(title, deadline, priority, project, assignee)
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
                "UPDATE tasks SET title=%s, status=%s, priority=%s, deadline=%s, project=%s,"
                " energy=%s, assignee=%s, progress=%s, calendar_event_id=%s, recurrence=%s,"
                " closed_at=%s, category=%s WHERE id=%s",
                (title, status, priority, deadline, project, energy, assignee, progress,
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
        project = t.get("project") or None
        assignee = t.get("assignee") or None
        event_id = gcal.create_event(title, deadline, priority, project, assignee) if deadline else None
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO tasks (title, status, priority, deadline, project, energy, assignee, calendar_event_id)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        title,
                        t.get("status", "Новая"),
                        priority,
                        deadline,
                        project,
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
