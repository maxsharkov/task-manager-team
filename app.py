import os
import json
from flask import Flask, render_template, request, redirect, url_for, jsonify
import psycopg2
import psycopg2.extras
from datetime import date
from openai import OpenAI

app = Flask(__name__)
DATABASE_URL = (
    os.environ.get("DATABASE_URL") or
    os.environ.get("POSTGRES_URL") or
    os.environ.get("POSTGRESQL_URL")
).replace("postgres://", "postgresql://", 1)
openai_client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

STATUSES = ["Новая", "В работе", "Завершена"]
PRIORITIES = ["Низкий", "Средний", "Высокий"]
ENERGIES = ["Лёгкая", "Средняя", "Тяжёлая"]


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


@app.route("/")
def index():
    status_filter = request.args.get("status", "")
    priority_filter = request.args.get("priority", "")

    query = "SELECT * FROM tasks WHERE 1=1"
    params = []

    if status_filter:
        query += " AND status = %s"
        params.append(status_filter)
    if priority_filter:
        query += " AND priority = %s"
        params.append(priority_filter)

    query += """
        ORDER BY
            CASE priority WHEN 'Высокий' THEN 1 WHEN 'Средний' THEN 2 ELSE 3 END,
            deadline ASC NULLS LAST
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
        status_filter=status_filter,
        priority_filter=priority_filter,
        today=date.today(),
    )


@app.route("/add", methods=["POST"])
def add():
    title = request.form.get("title", "").strip()
    if not title:
        return redirect(url_for("index"))

    status = request.form.get("status", "Новая")
    priority = request.form.get("priority", "Средний")
    deadline = request.form.get("deadline") or None
    description = request.form.get("description", "").strip() or None
    project = request.form.get("project", "").strip() or None
    energy = request.form.get("energy", "Средняя")

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO tasks (title, status, priority, deadline, description, project, energy) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (title, status, priority, deadline, description, project, energy),
            )
    return redirect(url_for("index"))


@app.route("/update/<int:task_id>", methods=["POST"])
def update(task_id):
    status = request.form.get("status")
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE tasks SET status = %s WHERE id = %s", (status, task_id))
    return redirect(url_for("index"))


@app.route("/edit/<int:task_id>", methods=["GET"])
def edit(task_id):
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM tasks WHERE id = %s", (task_id,))
            task = cur.fetchone()
    if not task:
        return redirect(url_for("index"))
    return render_template("edit.html", task=task, statuses=STATUSES, priorities=PRIORITIES, energies=ENERGIES, today=date.today())


@app.route("/edit/<int:task_id>", methods=["POST"])
def edit_save(task_id):
    title = request.form.get("title", "").strip()
    if not title:
        return redirect(url_for("edit", task_id=task_id))

    status = request.form.get("status", "Новая")
    priority = request.form.get("priority", "Средний")
    deadline = request.form.get("deadline") or None
    description = request.form.get("description", "").strip() or None
    project = request.form.get("project", "").strip() or None
    energy = request.form.get("energy", "Средняя")

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tasks SET title=%s, status=%s, priority=%s, deadline=%s, description=%s, project=%s, energy=%s WHERE id=%s",
                (title, status, priority, deadline, description, project, energy, task_id),
            )
    return redirect(url_for("index"))


@app.route("/delete/<int:task_id>", methods=["POST"])
def delete(task_id):
    with get_db() as conn:
        with conn.cursor() as cur:
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
                    "energy (одно из: Лёгкая, Средняя, Тяжёлая — оцени по сложности задачи). "
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


@app.route("/health")
def health():
    import traceback
    result = {}
    result["DATABASE_URL_set"] = bool(os.environ.get("DATABASE_URL"))
    result["OPENAI_API_KEY_set"] = bool(os.environ.get("OPENAI_API_KEY"))
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
