import os
import json
import io
import requests
from flask import Flask, render_template, request, redirect, url_for, jsonify, send_from_directory, send_file, session
import psycopg2
import psycopg2.extras
from datetime import date, timedelta
import calendar
from openai import OpenAI
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz
from werkzeug.security import generate_password_hash, check_password_hash
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, PatternFill
from openpyxl.utils import get_column_letter

app = Flask(__name__)
DATABASE_URL = (
    os.environ.get("DATABASE_URL") or
    os.environ.get("POSTGRES_URL") or
    os.environ.get("POSTGRESQL_URL")
).replace("postgres://", "postgresql://", 1)
openai_client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

app.secret_key = os.environ["SECRET_KEY"]
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=True)

INVITE_CODE = os.environ.get("INVITE_CODE")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
RESEND_FROM = os.environ.get("RESEND_FROM", "onboarding@resend.dev")
NSK = pytz.timezone("Asia/Novosibirsk")

STATUSES = ["Новая", "В работе", "Завершена"]
PRIORITIES = ["Низкий", "Средний", "Высокий"]
ENERGIES = ["Лёгкая", "Средняя", "Тяжёлая"]
CATEGORIES = ["Работа", "Личное"]
RECURRENCES = ["Ежедневно", "Еженедельно", "Ежемесячно"]
STRATEGIC_AREAS = ["Здоровье", "Семья", "Работа", "Прогрессив", "Люди", "Мышление", "Яркость", "Деньги", "Публичность", "Принципы"]


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
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    email TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
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
            cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id) ON DELETE CASCADE")
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
            cur.execute("ALTER TABLE strategic_goals ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id) ON DELETE CASCADE")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS strategic_logs (
                    id SERIAL PRIMARY KEY,
                    goal_id INTEGER REFERENCES strategic_goals(id) ON DELETE CASCADE,
                    logged_at DATE DEFAULT CURRENT_DATE,
                    text TEXT NOT NULL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS strategic_snapshots (
                    id SERIAL PRIMARY KEY,
                    week_date DATE NOT NULL,
                    scores JSONB NOT NULL,
                    review_text TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("ALTER TABLE strategic_snapshots ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id) ON DELETE CASCADE")
            cur.execute("ALTER TABLE strategic_snapshots DROP CONSTRAINT IF EXISTS strategic_snapshots_week_date_key")
            cur.execute("""
                DO $$ BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint WHERE conname = 'strategic_snapshots_week_date_user_id_key'
                    ) THEN
                        ALTER TABLE strategic_snapshots
                            ADD CONSTRAINT strategic_snapshots_week_date_user_id_key UNIQUE (week_date, user_id);
                    END IF;
                END $$;
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS strategic_areas (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    created_at DATE DEFAULT CURRENT_DATE,
                    UNIQUE (user_id, name)
                )
            """)
            # Одноразовая миграция на модель "только категории": пока по целям нет ни одной
            # реальной записи (никто ещё не вёл журнал), безопасно очистить старые
            # предзаполненные цели и засеять дефолтные категории всем существующим пользователям
            cur.execute("SELECT COUNT(*) FROM strategic_logs")
            if cur.fetchone()[0] == 0:
                cur.execute("DELETE FROM strategic_goals")
            cur.execute("SELECT COUNT(*) FROM strategic_areas")
            if cur.fetchone()[0] == 0:
                cur.execute("SELECT id FROM users")
                for (uid,) in cur.fetchall():
                    seed_default_areas_for_user(cur, uid)


def seed_default_areas_for_user(cur, user_id):
    cur.executemany(
        "INSERT INTO strategic_areas (user_id, name) VALUES (%s, %s) ON CONFLICT (user_id, name) DO NOTHING",
        [(user_id, area) for area in STRATEGIC_AREAS],
    )


def current_user_id():
    return session.get("user_id")


def all_users():
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, email FROM users ORDER BY id")
            return cur.fetchall()


def send_email(to_addr, subject, body):
    if not RESEND_API_KEY or not to_addr:
        print(f"Email not configured, skipping send to {to_addr}")
        return
    res = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
        json={
            "from": RESEND_FROM,
            "to": [to_addr],
            "subject": subject,
            "text": body,
        },
        timeout=15,
    )
    if res.status_code >= 400:
        raise RuntimeError(f"Resend error {res.status_code}: {res.text}")


def build_digest(digest_type, user_id, to_email):
    today = date.today()
    today_str = today.isoformat()
    week_ago = (today - timedelta(days=7)).isoformat()

    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT title, status, priority, deadline, category, closed_at, progress, assignee "
                "FROM tasks WHERE user_id = %s ORDER BY created_at",
                (user_id,),
            )
            tasks = cur.fetchall()

    if not tasks:
        send_email(to_email, "Дайджест задач", "Задач пока нет.")
        return

    def fmt(t):
        line = f"- [{t['category'] or '?'}] «{t['title']}»"
        if t['deadline']:
            line += f" | дедлайн: {t['deadline']}"
        if t['closed_at']:
            line += f" | закрыта: {t['closed_at']}"
        if t['assignee']:
            line += f" | {t['assignee']}"
        if t['progress']:
            line += f" | прогресс: {t['progress'][:60]}"
        return line

    if digest_type == "weekly":
        closed_period = [t for t in tasks
                         if t['closed_at'] and str(t['closed_at'])[:10] >= week_ago]
        period_label = "за последние 7 дней"
        header = "Итоги недели — оценка выполнения обещаний"
        period = "за эту неделю"
    else:
        closed_period = [t for t in tasks
                         if t['closed_at'] and str(t['closed_at'])[:10] == today_str]
        period_label = "за сегодня"
        header = "Дайджест — оценка выполнения обещаний"
        period = "за сегодня"

    overdue = [t for t in tasks if t['status'] != 'Завершена' and t['deadline']
               and str(t['deadline']) < today_str]
    active_hp = [t for t in tasks if t['status'] != 'Завершена' and t['priority'] == 'Высокий'
                 and (not t['deadline'] or str(t['deadline']) >= today_str)]

    sections = []
    sections.append(
        f"Завершено {period_label}:\n" + "\n".join(fmt(t) for t in closed_period)
        if closed_period else f"Завершено {period_label}: ничего"
    )
    sections.append(
        "Просрочено (активные):\n" + "\n".join(fmt(t) for t in overdue)
        if overdue else "Просрочено: нет"
    )
    sections.append(
        "В работе (высокий приоритет):\n" + "\n".join(fmt(t) for t in active_hp)
        if active_hp else "В работе (высокий приоритет): нет"
    )

    tasks_structured = "\n\n".join(sections)

    prompt = f"""Ты жёсткий, честный коуч топ-менеджера. Без корпоративного языка, без комплиментов. Только факты и прямая оценка.

Сегодня {today_str}. Задачи структурированы по факту:

{tasks_structured}

ВАЖНО: раздел «Завершено» — это реально выполненные задачи. Учитывай их при выставлении оценки.

Сделай оценку выполнения обещаний {period} по двум категориям.

РАБОЧИЕ ЦЕЛИ (категория Работа)
1. Что обещано / в работе
2. Что реально выполнено (из раздела Завершено)
3. Что просрочено или зависло
4. Оценка: X/10

ЛИЧНЫЕ ЦЕЛИ (категория Личное)
1. Что обещано / в работе
2. Что реально выполнено (из раздела Завершено)
3. Что просрочено или зависло
4. Оценка: X/10

ИТОГО
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
    send_email(to_email, header, summary)


def daily_digest():
    for user in all_users():
        try:
            build_digest("daily", user["id"], user["email"])
        except Exception as e:
            print(f"daily_digest failed for {user['email']}: {e}")


def weekly_digest():
    for user in all_users():
        try:
            build_digest("weekly", user["id"], user["email"])
        except Exception as e:
            print(f"weekly_digest failed for {user['email']}: {e}")


def build_strategic_digest(user_id, to_email):
    today = date.today()
    today_str = today.isoformat()

    _AREA_EMOJI = {
        "Здоровье": "💪", "Семья": "❤️", "Работа": "⚡", "Прогрессив": "🤖",
        "Люди": "🤝", "Мышление": "🧠", "Яркость": "🎨", "Деньги": "💰",
        "Публичность": "📣", "Принципы": "⚙️",
    }

    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT name FROM strategic_areas WHERE user_id = %s ORDER BY id", (user_id,))
            user_areas = [r["name"] for r in cur.fetchall()]

            cur.execute("SELECT * FROM strategic_goals WHERE user_id = %s ORDER BY area, title", (user_id,))
            goals = cur.fetchall()
            if not goals:
                send_email(to_email, "Стратегический обзор", "Стратегических целей пока нет.")
                return
            goal_logs = {}
            for g in goals:
                cur.execute("""
                    SELECT text, logged_at FROM strategic_logs
                    WHERE goal_id = %s ORDER BY logged_at DESC, id DESC LIMIT 2
                """, (g['id'],))
                goal_logs[g['id']] = cur.fetchall()

    # Группировка по областям
    from collections import OrderedDict
    areas = OrderedDict()
    for area in user_areas:
        areas[area] = []
    for g in goals:
        area = g['area'] or "Прочее"
        if area not in areas:
            areas[area] = []
        areas[area].append(g)

    block_lines = []
    for area, area_goals in areas.items():
        if not area_goals:
            continue
        active_7 = []    # цели с записью за последние 7 дней
        active_30 = []   # цели с записью за 8–30 дней
        silent = []      # цели без единой записи или молчат 30+ дней

        for g in area_goals:
            logs = goal_logs.get(g['id'], [])
            if logs:
                last_date = logs[0]['logged_at']
                days_ago = (today - last_date).days
                if days_ago <= 7:
                    active_7.append((g, logs, days_ago))
                elif days_ago <= 30:
                    active_30.append((g, logs, days_ago))
                else:
                    silent.append(g)
            else:
                silent.append(g)

        emoji = _AREA_EMOJI.get(area, "•")
        lines = [f"БЛОК: {emoji} {area} ({len(area_goals)} целей)"]
        lines.append(f"Активных за 7 дней: {len(active_7)}")
        for g, logs, days_ago in active_7:
            for l in logs[:2]:
                lines.append(f"  - «{g['title']}» [{l['logged_at']}]: {l['text'][:80]}")
        lines.append(f"Молчат 8-30 дней: {len(active_30)}")
        if active_30:
            lines.append("  " + ", ".join(f"«{g['title']}»" for g, _, _ in active_30[:3]))
        lines.append(f"Застой / нет записей: {len(silent)}")
        block_lines.append("\n".join(lines))

    goals_structured = "\n\n".join(block_lines)

    prompt = f"""Ты честный жёсткий коуч топ-менеджера. Воскресный стратегический обзор.

Сегодня {today_str}.

Данные по 10 стратегическим блокам:

{goals_structured}

Задача: оцени каждый блок по шкале 0–10, где:
- 0-3 → полный застой (нет активности)
- 4-6 → слабое движение (1-2 цели из блока)
- 7-8 → нормальный темп (половина целей активна)
- 9-10 → сильное движение (большинство целей с записями за неделю)

Структура ответа — строго в таком формате:

```json
{{"Здоровье": 0, "Семья": 0, "Работа": 0, "Прогрессив": 0, "Люди": 0, "Мышление": 0, "Яркость": 0, "Деньги": 0, "Публичность": 0, "Принципы": 0}}
```

СТРАТЕГИЧЕСКИЙ ОБЗОР — {today_str}

[для каждого блока]:
{{эмодзи}} {{Название}} — {{X}}/10 {{стрелка ↑/→/↓}} — {{одна фраза: оценка блока}}
[если есть активные записи за 7 дней — перечисли только тексты записей, без названий целей, не более 4 на блок]:
  • {{текст записи}} [{{дата}}]

Если активных записей нет — строку с буллетами не добавляй.
Эмодзи для блоков: Здоровье💪 Семья❤️ Работа⚡ Прогрессив🤖 Люди🤝 Мышление🧠 Яркость🎨 Деньги💰 Публичность📣 Принципы⚙️

ИТОГ НЕДЕЛИ — средняя оценка по всем блокам, 1 предложение про общий тренд.

ГЛАВНЫЙ ВОПРОС — один сильный вопрос по самому застывшему блоку.

Без воды. Без корпоративного языка. Максимально конкретно."""

    response = openai_client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": "Ты жёсткий честный коуч. Отвечай на русском, без воды."},
            {"role": "user", "content": prompt}
        ]
    )
    raw = response.choices[0].message.content

    # Извлекаем JSON с оценками из блока ```json ... ```
    import re as _re
    import json as _json
    scores = {}
    json_match = _re.search(r'```json\s*(\{.*?\})\s*```', raw, _re.DOTALL)
    if json_match:
        try:
            scores = _json.loads(json_match.group(1))
        except Exception:
            pass
    # Нарратив — всё после блока с JSON
    narrative = _re.sub(r'```json\s*\{.*?\}\s*```\s*', '', raw, flags=_re.DOTALL).strip()

    # Сохраняем снимок в БД (upsert по week_date + user_id)
    if scores:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO strategic_snapshots (week_date, user_id, scores, review_text)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (week_date, user_id) DO UPDATE
                        SET scores = EXCLUDED.scores,
                            review_text = EXCLUDED.review_text,
                            created_at = NOW()
                """, (today, user_id, _json.dumps(scores, ensure_ascii=False), narrative))

    send_email(to_email, "Стратегический обзор", narrative)


def strategic_review():
    for user in all_users():
        try:
            build_strategic_digest(user["id"], user["email"])
        except Exception as e:
            print(f"strategic_review failed for {user['email']}: {e}")


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


PUBLIC_ENDPOINTS = {"static", "health", "login", "register", "logout", "service_worker"}


@app.before_request
def require_login():
    if request.endpoint in PUBLIC_ENDPOINTS or request.endpoint is None:
        return
    if not session.get("user_id"):
        if request.path.startswith("/api/") or request.is_json:
            return jsonify({"error": "unauthorized"}), 401
        return redirect(url_for("login", next=request.path))


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "GET":
        return render_template("register.html", error=None, invite_required=bool(INVITE_CODE))

    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    invite_code = request.form.get("invite_code", "").strip()

    if INVITE_CODE and invite_code != INVITE_CODE:
        return render_template("register.html", error="Неверный инвайт-код", invite_required=True), 400
    if not email or not password:
        return render_template("register.html", error="Заполните все поля", invite_required=bool(INVITE_CODE)), 400

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE email=%s", (email,))
            if cur.fetchone():
                return render_template("register.html", error="Такой email уже зарегистрирован",
                                       invite_required=bool(INVITE_CODE)), 400
            cur.execute(
                "INSERT INTO users (email, password_hash) VALUES (%s, %s) RETURNING id",
                (email, generate_password_hash(password)),
            )
            user_id = cur.fetchone()[0]
            seed_default_areas_for_user(cur, user_id)

    session["user_id"] = user_id
    return redirect(url_for("index"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html", error=None, next=request.args.get("next", ""))

    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    next_path = request.form.get("next", "")

    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, password_hash FROM users WHERE email=%s", (email,))
            user = cur.fetchone()

    if not user or not check_password_hash(user["password_hash"], password):
        return render_template("login.html", error="Неверный email или пароль", next=next_path), 401

    session["user_id"] = user["id"]
    return redirect(next_path or url_for("index"))


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/tags")
def get_tags():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT tags FROM tasks WHERE tags IS NOT NULL AND tags != '' AND user_id = %s",
                        (session["user_id"],))
            rows = cur.fetchall()
    all_tags = set()
    for row in rows:
        all_tags.update(parse_tags(row[0]))
    return jsonify(sorted(all_tags))


@app.route("/")
def index():
    uid = session["user_id"]
    priority_filter = request.args.get("priority", "")
    search_query = request.args.get("q", "").strip()

    query = """
        SELECT t.*,
            COUNT(s.id) AS subtask_total,
            COUNT(s.id) FILTER (WHERE s.done) AS subtask_done
        FROM tasks t
        LEFT JOIN subtasks s ON s.task_id = t.id
        WHERE t.status != 'Завершена' AND t.user_id = %s
    """
    params = [uid]

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
                SELECT * FROM tasks WHERE status = 'Завершена' AND user_id = %s
                ORDER BY closed_at DESC NULLS LAST, id DESC
            """, (uid,))
            done_tasks = cur.fetchall()

            cur.execute("""
                SELECT g.*,
                    MAX(l.logged_at) AS last_log_date
                FROM strategic_goals g
                LEFT JOIN strategic_logs l ON l.goal_id = g.id
                WHERE g.user_id = %s
                GROUP BY g.id
                ORDER BY g.area, g.title
            """, (uid,))
            strategic_goals = cur.fetchall()

            # Загружаем все логи по каждой цели
            goal_logs = {}
            for g in strategic_goals:
                cur.execute("""
                    SELECT text, logged_at FROM strategic_logs
                    WHERE goal_id = %s ORDER BY logged_at DESC, id DESC
                """, (g['id'],))
                goal_logs[g['id']] = cur.fetchall()

            cur.execute("SELECT id, name FROM strategic_areas WHERE user_id = %s ORDER BY id", (uid,))
            strategic_areas = cur.fetchall()

    return render_template(
        "index.html",
        tasks=tasks,
        done_tasks=done_tasks,
        strategic_goals=strategic_goals,
        goal_logs=goal_logs,
        statuses=STATUSES,
        priorities=PRIORITIES,
        energies=ENERGIES,
        recurrences=RECURRENCES,
        categories=CATEGORIES,
        strategic_areas=strategic_areas,
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
                    "INSERT INTO strategic_goals (title, area, user_id) VALUES (%s, %s, %s)",
                    (title, area, session["user_id"])
                )
    return redirect("/?tab=strategy")


@app.route("/strategy/log/<int:goal_id>", methods=["POST"])
def strategy_log(goal_id):
    text = request.form.get("text", "").strip()
    if text:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM strategic_goals WHERE id=%s AND user_id=%s",
                            (goal_id, session["user_id"]))
                if cur.fetchone():
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
                    "UPDATE strategic_goals SET title=%s, area=%s WHERE id=%s AND user_id=%s",
                    (title, area, goal_id, session["user_id"])
                )
    return redirect("/?tab=strategy")


@app.route("/strategy/delete/<int:goal_id>", methods=["POST"])
def strategy_delete(goal_id):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM strategic_goals WHERE id=%s AND user_id=%s",
                        (goal_id, session["user_id"]))
    return redirect("/?tab=strategy")


@app.route("/strategy/area/add", methods=["POST"])
def strategy_area_add():
    name = request.form.get("name", "").strip()
    if name:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO strategic_areas (user_id, name) VALUES (%s, %s) ON CONFLICT (user_id, name) DO NOTHING",
                    (session["user_id"], name)
                )
    return redirect("/?tab=strategy")


@app.route("/strategy/area/delete/<int:area_id>", methods=["POST"])
def strategy_area_delete(area_id):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM strategic_areas WHERE id=%s AND user_id=%s",
                        (area_id, session["user_id"]))
    return redirect("/?tab=strategy")


@app.route("/strategy/digest", methods=["POST"])
def strategy_digest_manual():
    uid = session["user_id"]
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT email FROM users WHERE id=%s", (uid,))
            to_email = cur.fetchone()["email"]
    try:
        build_strategic_digest(uid, to_email)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/strategy/analyze", methods=["POST"])
def strategy_analyze():
    uid = session["user_id"]
    today = date.today().isoformat()
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM strategic_goals WHERE user_id=%s ORDER BY area, title", (uid,))
            goals = cur.fetchall()
            if not goals:
                return jsonify({"summary": "Стратегических целей пока нет."})
            goal_logs = {}
            for g in goals:
                cur.execute("""
                    SELECT text, logged_at FROM strategic_logs
                    WHERE goal_id = %s ORDER BY logged_at ASC, id ASC
                """, (g['id'],))
                goal_logs[g['id']] = cur.fetchall()

    lines = []
    for g in goals:
        logs = goal_logs.get(g['id'], [])
        if logs:
            log_entries = "\n".join(f"  [{l['logged_at']}] {l['text']}" for l in logs)
        else:
            log_entries = "  Записей нет"
        lines.append(f"[{g['area']}] «{g['title']}»\n{log_entries}")

    goals_text = "\n\n".join(lines)

    response = openai_client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {
                "role": "system",
                "content": "Ты жёсткий честный коуч топ-менеджера. Отвечай на русском, структурированно, без воды и лести."
            },
            {
                "role": "user",
                "content": (
                    f"Сегодня {today}. Стратегические цели и история всех записей:\n\n{goals_text}\n\n"
                    "Дай полный анализ:\n"
                    "1. По каждой области (Бизнес, Карьера, Люди, Нетворкинг, Личное) — краткий статус: что движется, что стоит.\n"
                    "2. Топ-3 цели с наилучшим прогрессом — отметь конкретно.\n"
                    "3. Топ-3 застывших цели — назови следующее конкретное действие для каждой.\n"
                    "4. Главный паттерн: что системно игнорируется.\n"
                    "5. Одна рекомендация на эту неделю — конкретная и измеримая."
                )
            }
        ]
    )

    return jsonify({"summary": response.choices[0].message.content})


def _goal_activity_status(last_log_date, today):
    if not last_log_date:
        return "Нет записей"
    days_ago = (today - last_log_date).days
    if days_ago <= 7:
        return "Активна"
    if days_ago <= 14:
        return "Замедление"
    return "Стагнация"


@app.route("/strategy/export")
def strategy_export():
    uid = session["user_id"]
    today = date.today()
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT g.*, MAX(l.logged_at) AS last_log_date, COUNT(l.id) AS log_count
                FROM strategic_goals g
                LEFT JOIN strategic_logs l ON l.goal_id = g.id
                WHERE g.user_id = %s
                GROUP BY g.id
                ORDER BY g.area, g.title
            """, (uid,))
            goals = cur.fetchall()

            cur.execute("""
                SELECT l.goal_id, l.logged_at, l.text, g.area, g.title
                FROM strategic_logs l
                JOIN strategic_goals g ON g.id = l.goal_id
                WHERE g.user_id = %s
                ORDER BY g.area, g.title, l.logged_at DESC, l.id DESC
            """, (uid,))
            logs = cur.fetchall()

    wb = Workbook()

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="1D1D1F", end_color="1D1D1F", fill_type="solid")
    wrap = Alignment(wrap_text=True, vertical="top")

    def style_header(ws, ncols):
        for col in range(1, ncols + 1):
            cell = ws.cell(row=1, column=col)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(vertical="center")
        ws.freeze_panes = "A2"

    ws1 = wb.active
    ws1.title = "Цели"
    ws1.append(["Область", "Цель", "Статус", "Дней с последней записи", "Последняя запись", "Всего записей", "Создана"])
    for g in goals:
        days_ago = (today - g["last_log_date"]).days if g["last_log_date"] else None
        ws1.append([
            g["area"],
            g["title"],
            _goal_activity_status(g["last_log_date"], today),
            days_ago if days_ago is not None else "",
            g["last_log_date"].isoformat() if g["last_log_date"] else "",
            g["log_count"],
            g["created_at"].isoformat() if g["created_at"] else "",
        ])
    widths1 = [14, 55, 14, 20, 16, 14, 12]
    for i, w in enumerate(widths1, start=1):
        ws1.column_dimensions[get_column_letter(i)].width = w
    style_header(ws1, len(widths1))

    ws2 = wb.create_sheet("История записей")
    ws2.append(["Область", "Цель", "Дата", "Комментарий"])
    for l in logs:
        ws2.append([l["area"], l["title"], l["logged_at"].isoformat(), l["text"]])
    for row in ws2.iter_rows(min_row=2):
        row[3].alignment = wrap
    widths2 = [14, 45, 12, 90]
    for i, w in enumerate(widths2, start=1):
        ws2.column_dimensions[get_column_letter(i)].width = w
    style_header(ws2, len(widths2))

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    filename = f"strategy_export_{today.isoformat()}.xlsx"
    return send_file(
        buf,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.route("/add", methods=["POST"])
def add():
    title = request.form.get("title", "").strip()
    if not title:
        return redirect(url_for("index"))

    status = request.form.get("status", "Новая")
    priority = request.form.get("priority", "Средний")
    deadline = request.form.get("deadline") or None
    energy = request.form.get("energy", "Средняя")
    assignee = request.form.get("assignee", "").strip() or None
    recurrence = request.form.get("recurrence", "").strip() or None
    category = request.form.get("category", "Работа")

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO tasks (title, status, priority, deadline, energy, assignee, recurrence, category, user_id)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (title, status, priority, deadline, energy, assignee, recurrence, category, session["user_id"]),
            )
    return redirect(url_for("index"))


@app.route("/update/<int:task_id>", methods=["POST"])
def update(task_id):
    status = request.form.get("status")
    uid = session["user_id"]
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM tasks WHERE id=%s AND user_id=%s", (task_id, uid))
            task = cur.fetchone()
            if not task:
                return redirect(url_for("index"))

            was_completed = task["status"] == "Завершена"
            now_completed = status == "Завершена"
            closed_at = date.today() if now_completed else None

            cur.execute("UPDATE tasks SET status=%s, closed_at=%s WHERE id=%s AND user_id=%s",
                        (status, closed_at, task_id, uid))

            if now_completed and not was_completed and task["recurrence"]:
                # Для повторяющихся — создаём следующий инстанс
                next_dl = calc_next_deadline(task["deadline"], task["recurrence"])
                cur.execute(
                    "INSERT INTO tasks (title, status, priority, deadline, energy,"
                    " assignee, recurrence, category, user_id)"
                    " VALUES (%s,'Новая',%s,%s,%s,%s,%s,%s,%s)",
                    (task["title"], task["priority"], next_dl,
                     task["energy"], task["assignee"], task["recurrence"],
                     task["category"], uid)
                )
    return redirect(url_for("index"))


@app.route("/edit/<int:task_id>", methods=["GET"])
def edit(task_id):
    uid = session["user_id"]
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM tasks WHERE id = %s AND user_id = %s", (task_id, uid))
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
    uid = session["user_id"]
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
            cur.execute("SELECT status, closed_at FROM tasks WHERE id=%s AND user_id=%s", (task_id, uid))
            row = cur.fetchone()

    if not row:
        return redirect(url_for("index"))

    closed_at = None
    if status == "Завершена" and row["status"] != "Завершена":
        closed_at = date.today()
    elif status == "Завершена":
        closed_at = row["closed_at"]

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tasks SET title=%s, status=%s, priority=%s, deadline=%s,"
                " energy=%s, assignee=%s, progress=%s, recurrence=%s,"
                " closed_at=%s, category=%s WHERE id=%s AND user_id=%s",
                (title, status, priority, deadline, energy, assignee, progress,
                 recurrence, closed_at, category, task_id, uid),
            )
    return redirect(url_for("index"))


@app.route("/delete/<int:task_id>", methods=["POST"])
def delete(task_id):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM tasks WHERE id = %s AND user_id = %s", (task_id, session["user_id"]))
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


@app.route("/ai-search", methods=["POST"])
def ai_search():
    query = (request.json or {}).get("query", "").strip()
    if not query:
        return jsonify({"ids": [], "explanation": "Пустой запрос"}), 400

    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, title, status, priority, deadline, description, project, tags "
                "FROM tasks WHERE user_id = %s ORDER BY created_at",
                (session["user_id"],),
            )
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


@app.route("/api/tactic-stats")
def tactic_stats():
    uid = session["user_id"]
    today = date.today()
    week_ago  = today - timedelta(days=7)
    month_ago = today - timedelta(days=30)
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT status, COUNT(*) AS cnt FROM tasks WHERE user_id=%s GROUP BY status", (uid,))
            by_status = {r["status"]: r["cnt"] for r in cur.fetchall()}
            cur.execute("SELECT priority, COUNT(*) AS cnt FROM tasks WHERE status != 'Завершена' AND user_id=%s GROUP BY priority", (uid,))
            by_priority = {r["priority"]: r["cnt"] for r in cur.fetchall()}
            cur.execute("SELECT COUNT(*) AS cnt FROM tasks WHERE status='Завершена' AND closed_at >= %s AND user_id=%s", (week_ago, uid))
            closed_7 = cur.fetchone()["cnt"]
            cur.execute("SELECT COUNT(*) AS cnt FROM tasks WHERE status='Завершена' AND closed_at >= %s AND user_id=%s", (month_ago, uid))
            closed_30 = cur.fetchone()["cnt"]
            cur.execute("SELECT COUNT(*) AS cnt FROM tasks WHERE deadline < %s AND status != 'Завершена' AND user_id=%s", (today, uid))
            overdue = cur.fetchone()["cnt"]
            cur.execute("""
                SELECT project, COUNT(*) AS cnt FROM tasks
                WHERE status != 'Завершена' AND project IS NOT NULL AND project != '' AND user_id=%s
                GROUP BY project ORDER BY cnt DESC LIMIT 8
            """, (uid,))
            by_project = [dict(r) for r in cur.fetchall()]
            cur.execute("SELECT COUNT(*) AS cnt FROM tasks WHERE status != 'Завершена' AND user_id=%s", (uid,))
            open_total = cur.fetchone()["cnt"]
            cur.execute("SELECT ROUND(AVG(CURRENT_DATE - created_at)) AS avg_age FROM tasks WHERE status != 'Завершена' AND user_id=%s", (uid,))
            avg_age = int(cur.fetchone()["avg_age"] or 0)
            closed_all = by_status.get("Завершена", 0)
    return jsonify({
        "open_total": open_total, "closed_all": closed_all,
        "closed_7": closed_7, "closed_30": closed_30,
        "overdue": overdue, "avg_age": avg_age,
        "by_status": by_status, "by_priority": by_priority,
        "by_project": by_project,
    })


@app.route("/api/strategy-chart")
def strategy_chart():
    import json as _json
    uid = session["user_id"]
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT name FROM strategic_areas WHERE user_id = %s ORDER BY id", (uid,))
            user_areas = [r["name"] for r in cur.fetchall()]

            cur.execute("""
                SELECT week_date, scores, review_text
                FROM strategic_snapshots
                WHERE user_id = %s
                ORDER BY week_date DESC
                LIMIT 12
            """, (uid,))
            rows = cur.fetchall()
    rows = list(reversed(rows))  # хронологический порядок
    weeks = [str(r['week_date']) for r in rows]
    reviews = {str(r['week_date']): r['review_text'] for r in rows}
    series = {area: [] for area in user_areas}
    for r in rows:
        sc = r['scores'] if isinstance(r['scores'], dict) else _json.loads(r['scores'])
        for area in user_areas:
            val = sc.get(area)
            series[area].append(val if isinstance(val, (int, float)) else None)
    return jsonify({"weeks": weeks, "series": series, "reviews": reviews})


@app.route("/api/calendar-tasks")
def calendar_tasks():
    _PRIORITY_COLOR = {"Высокий": "#ff3b30", "Средний": "#ff9500", "Низкий": "#34c759"}
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, title, priority, category, status, deadline, assignee, energy, progress
                FROM tasks
                WHERE status != 'Завершена' AND deadline IS NOT NULL AND user_id = %s
                ORDER BY deadline
            """, (session["user_id"],))
            tasks = cur.fetchall()
    events = []
    for t in tasks:
        events.append({
            "id": t["id"],
            "title": t["title"],
            "start": str(t["deadline"]),
            "color": _PRIORITY_COLOR.get(t["priority"], "#aeaeb2"),
            "textColor": "white",
            "extendedProps": {
                "priority": t["priority"] or "",
                "category": t["category"] or "",
                "status": t["status"] or "",
                "assignee": t["assignee"] or "",
                "energy": t["energy"] or "",
                "progress": t["progress"] or "",
            }
        })
    return jsonify(events)


@app.route("/digest", methods=["POST"])
def digest_manual():
    uid = session["user_id"]
    digest_type = request.json.get("type", "daily") if request.is_json else "daily"
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT email FROM users WHERE id=%s", (uid,))
            to_email = cur.fetchone()["email"]
    try:
        build_digest(digest_type, uid, to_email)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/analyze", methods=["POST"])
def analyze():
    today = date.today().isoformat()

    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT title, status, priority, deadline FROM tasks WHERE user_id=%s ORDER BY created_at",
                        (session["user_id"],))
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
                cur.execute("SELECT id FROM tasks WHERE id=%s AND user_id=%s", (task_id, session["user_id"]))
                if cur.fetchone():
                    cur.execute("INSERT INTO subtasks (task_id, text) VALUES (%s, %s)", (task_id, text))
    return redirect(url_for("edit", task_id=task_id))


@app.route("/subtask/toggle/<int:sub_id>", methods=["POST"])
def subtask_toggle(sub_id):
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT s.task_id, s.done FROM subtasks s
                JOIN tasks t ON t.id = s.task_id
                WHERE s.id=%s AND t.user_id=%s
            """, (sub_id, session["user_id"]))
            row = cur.fetchone()
            if row:
                cur.execute("UPDATE subtasks SET done=%s WHERE id=%s", (not row["done"], sub_id))
                return redirect(url_for("edit", task_id=row["task_id"]))
    return redirect(url_for("index"))


@app.route("/subtask/delete/<int:sub_id>", methods=["POST"])
def subtask_delete(sub_id):
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT s.task_id FROM subtasks s
                JOIN tasks t ON t.id = s.task_id
                WHERE s.id=%s AND t.user_id=%s
            """, (sub_id, session["user_id"]))
            row = cur.fetchone()
            if row:
                cur.execute("DELETE FROM subtasks WHERE id=%s", (sub_id,))
                return redirect(url_for("edit", task_id=row["task_id"]))
    return redirect(url_for("index"))


@app.route("/dashboard")
def dashboard():
    uid = session["user_id"]
    today = date.today()
    week_ago = today - timedelta(days=7)
    month_ago = today - timedelta(days=30)

    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT status, COUNT(*) AS cnt FROM tasks WHERE user_id=%s GROUP BY status", (uid,))
            by_status = {r["status"]: r["cnt"] for r in cur.fetchall()}

            cur.execute("SELECT priority, COUNT(*) AS cnt FROM tasks WHERE status != 'Завершена' AND user_id=%s GROUP BY priority", (uid,))
            by_priority = {r["priority"]: r["cnt"] for r in cur.fetchall()}

            cur.execute("SELECT COUNT(*) AS cnt FROM tasks WHERE status='Завершена' AND closed_at >= %s AND user_id=%s", (week_ago, uid))
            closed_7 = cur.fetchone()["cnt"]

            cur.execute("SELECT COUNT(*) AS cnt FROM tasks WHERE status='Завершена' AND closed_at >= %s AND user_id=%s", (month_ago, uid))
            closed_30 = cur.fetchone()["cnt"]

            cur.execute("SELECT COUNT(*) AS cnt FROM tasks WHERE deadline < %s AND status != 'Завершена' AND user_id=%s", (today, uid))
            overdue = cur.fetchone()["cnt"]

            cur.execute("""
                SELECT project, COUNT(*) AS cnt
                FROM tasks WHERE status != 'Завершена' AND project IS NOT NULL AND user_id=%s
                GROUP BY project ORDER BY cnt DESC LIMIT 8
            """, (uid,))
            by_project = cur.fetchall()

            cur.execute("SELECT COUNT(*) AS cnt FROM tasks WHERE status != 'Завершена' AND user_id=%s", (uid,))
            open_total = cur.fetchone()["cnt"]

            cur.execute("SELECT ROUND(AVG(CURRENT_DATE - created_at)) AS avg_age FROM tasks WHERE status != 'Завершена' AND user_id=%s", (uid,))
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
    result["RESEND_configured"] = bool(RESEND_API_KEY)
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
