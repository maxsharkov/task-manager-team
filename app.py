import os
from flask import Flask, render_template, request, redirect, url_for
import psycopg2
import psycopg2.extras
from datetime import date

app = Flask(__name__)
DATABASE_URL = os.environ["DATABASE_URL"].replace("postgres://", "postgresql://", 1)

STATUSES = ["Новая", "В работе", "Завершена"]
PRIORITIES = ["Низкий", "Средний", "Высокий"]


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
                    created_at DATE DEFAULT CURRENT_DATE
                )
            """)


@app.route("/")
def index():
    status_filter = request.args.get("status", "")
    priority_filter = request.args.get("priority", "")

    query = """
        SELECT * FROM tasks WHERE 1=1
    """
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
        status_filter=status_filter,
        priority_filter=priority_filter,
        today=date.today().isoformat(),
    )


@app.route("/add", methods=["POST"])
def add():
    title = request.form.get("title", "").strip()
    if not title:
        return redirect(url_for("index"))

    status = request.form.get("status", "Новая")
    priority = request.form.get("priority", "Средний")
    deadline = request.form.get("deadline") or None

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO tasks (title, status, priority, deadline) VALUES (%s, %s, %s, %s)",
                (title, status, priority, deadline),
            )
    return redirect(url_for("index"))


@app.route("/update/<int:task_id>", methods=["POST"])
def update(task_id):
    status = request.form.get("status")
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE tasks SET status = %s WHERE id = %s", (status, task_id))
    return redirect(url_for("index"))


@app.route("/delete/<int:task_id>", methods=["POST"])
def delete(task_id):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM tasks WHERE id = %s", (task_id,))
    return redirect(url_for("index"))


init_db()

if __name__ == "__main__":
    app.run()
