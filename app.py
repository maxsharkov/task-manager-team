from flask import Flask, render_template, request, redirect, url_for
import sqlite3
from datetime import date

app = Flask(__name__)
DB = "tasks.db"

STATUSES = ["Новая", "В работе", "Завершена"]
PRIORITIES = ["Низкий", "Средний", "Высокий"]


def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'Новая',
                priority TEXT NOT NULL DEFAULT 'Средний',
                deadline TEXT,
                created_at TEXT DEFAULT (date('now'))
            )
        """)


@app.route("/")
def index():
    status_filter = request.args.get("status", "")
    priority_filter = request.args.get("priority", "")

    query = "SELECT * FROM tasks WHERE 1=1"
    params = []

    if status_filter:
        query += " AND status = ?"
        params.append(status_filter)
    if priority_filter:
        query += " AND priority = ?"
        params.append(priority_filter)

    query += " ORDER BY CASE priority WHEN 'Высокий' THEN 1 WHEN 'Средний' THEN 2 ELSE 3 END, deadline ASC"

    with get_db() as conn:
        tasks = conn.execute(query, params).fetchall()

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
        conn.execute(
            "INSERT INTO tasks (title, status, priority, deadline) VALUES (?, ?, ?, ?)",
            (title, status, priority, deadline),
        )
    return redirect(url_for("index"))


@app.route("/update/<int:task_id>", methods=["POST"])
def update(task_id):
    status = request.form.get("status")
    with get_db() as conn:
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))
    return redirect(url_for("index"))


@app.route("/delete/<int:task_id>", methods=["POST"])
def delete(task_id):
    with get_db() as conn:
        conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    return redirect(url_for("index"))


if __name__ == "__main__":
    init_db()
    app.run()

init_db()
