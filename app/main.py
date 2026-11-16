"""FastAPI-сервер: CRUD на PostgreSQL, расчёт OR-Tools, история запусков, проверка воспроизводимости."""
import os
from contextlib import asynccontextmanager

import psycopg
from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from . import solver as S

DSN = os.getenv("DATABASE_URL", "postgresql://team:team@localhost:5432/team")
COLS = {"people": ["name", "skills", "exp", "pref", "hours"], "projects": ["name", "skills"],
        "tasks": ["project_id", "title", "skill", "hours", "priority"]}
SCHEMA = """
CREATE TABLE IF NOT EXISTS people(id SERIAL PRIMARY KEY, name TEXT NOT NULL DEFAULT 'Новый участник', skills TEXT NOT NULL DEFAULT '',
  exp INT NOT NULL DEFAULT 3 CHECK(exp BETWEEN 1 AND 5), pref TEXT NOT NULL DEFAULT '', hours INT NOT NULL DEFAULT 40 CHECK(hours > 0));
CREATE TABLE IF NOT EXISTS projects(id SERIAL PRIMARY KEY, name TEXT NOT NULL DEFAULT 'Новый проект', skills TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS tasks(id SERIAL PRIMARY KEY, project_id INT NOT NULL REFERENCES projects ON DELETE CASCADE,
  title TEXT NOT NULL DEFAULT 'Новая задача', skill TEXT NOT NULL DEFAULT '', hours INT NOT NULL DEFAULT 8 CHECK(hours > 0),
  priority INT NOT NULL DEFAULT 3 CHECK(priority BETWEEN 1 AND 5));
CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v JSONB NOT NULL);
CREATE TABLE IF NOT EXISTS runs(id SERIAL PRIMARY KEY, created TIMESTAMPTZ NOT NULL DEFAULT now(), input_hash TEXT NOT NULL,
  result_hash TEXT NOT NULL, status TEXT NOT NULL, input JSONB NOT NULL, result JSONB NOT NULL);
"""
SEED_P = [("Анна", "аналитика, управление", 5, "аналитика"), ("Борис", "разработка, тестирование", 4, "разработка"),
          ("Вера", "дизайн, аналитика", 3, "дизайн"), ("Глеб", "разработка, базы данных", 5, "разработка"),
          ("Дарья", "маркетинг, коммуникации", 2, "маркетинг"), ("Егор", "тестирование, аналитика", 3, "аналитика"),
          ("Жанна", "дизайн, коммуникации", 4, "дизайн"), ("Илья", "базы данных, разработка", 2, "разработка")]
SEED_J = [("Цифровой сервис", "разработка, аналитика, дизайн"), ("Исследование рынка", "маркетинг, аналитика, коммуникации")]


def db():
    return psycopg.connect(DSN, row_factory=dict_row)


@asynccontextmanager
async def lifespan(_):
    with db() as c:
        c.execute(SCHEMA)
        if not c.execute("SELECT 1 FROM people").fetchone():
            cur = c.cursor()
            cur.executemany("INSERT INTO people(name,skills,exp,pref) VALUES(%s,%s,%s,%s)", SEED_P)
            cur.executemany("INSERT INTO projects(name,skills) VALUES(%s,%s)", SEED_J)
    yield


app = FastAPI(title="TeamOptimizer", lifespan=lifespan)


@app.exception_handler(ValueError)
async def _value_err(_, e):
    return JSONResponse({"detail": str(e)}, 422)


@app.exception_handler(psycopg.Error)
async def _pg_err(_, e):
    return JSONResponse({"detail": str(e).split("\n")[0]}, 422)


def load(c):
    d = {t: c.execute(f"SELECT * FROM {t} ORDER BY id").fetchall() for t in COLS}
    row = c.execute("SELECT v FROM settings WHERE k='cfg'").fetchone()
    d["cfg"] = S.merge(row["v"] if row else {})
    return d


def inp_of(d):
    return S.canon(d["people"], d["projects"], d["tasks"], d["cfg"])


@app.get("/api/state")
def state():
    with db() as c:
        return load(c)


@app.put("/api/settings")
def put_settings(body: dict = Body(...)):
    with db() as c:
        cur = c.execute("SELECT v FROM settings WHERE k='cfg'").fetchone()
        new = S.merge({**(cur["v"] if cur else {}), **body})
        c.execute("INSERT INTO settings VALUES('cfg',%s) ON CONFLICT(k) DO UPDATE SET v=EXCLUDED.v", [Jsonb(new)])
    return new


@app.get("/api/stats")
def stats():
    with db() as c:
        inp = inp_of(load(c))
    P, J, cf = inp["people"], inp["projects"], inp["cfg"]
    D, C = S.demand(inp), S.capacity(inp)
    sk = sorted({s for _, s in D} | {s for _, s in C})
    return dict(
        n_people=len(P), n_projects=len(J), n_tasks=len(inp["tasks"]), demand=sum(D.values()), capacity=sum(C.values()),
        avg_exp=sum(p["exp"] for p in P) / len(P) if P else 0, exp_hist=[sum(p["exp"] == e for p in P) for e in range(1, 6)],
        feasible=bool(J) and len(J) * cf["min_size"] <= len(P) <= len(J) * cf["max_size"],
        skills=[dict(skill=s, people=sum(k == s for _, k in C), demand=sum(v for (_, k), v in D.items() if k == s),
                     capacity=sum(v for (_, k), v in C.items() if k == s)) for s in sk],
        projects=[dict(id=pr["id"], name=pr["name"], demand=sum(v for (j2, _), v in D.items() if j2 == j),
                       tasks=sum(t["project_id"] == pr["id"] for t in inp["tasks"])) for j, pr in enumerate(J)])


@app.post("/api/optimize")
def optimize():
    with db() as c:
        inp = inp_of(load(c))
        res = S.solve(inp)
        res["run_id"] = c.execute(
            "INSERT INTO runs(input_hash,result_hash,status,input,result) VALUES(%s,%s,%s,%s,%s) RETURNING id",
            [res["input_hash"], res["result_hash"], res["status"], Jsonb(inp), Jsonb(res)]).fetchone()["id"]
    return res


@app.get("/api/runs")
def runs():
    with db() as c:
        rows = c.execute("SELECT id, created, input_hash, result_hash, status, (result->'verify'->>'total')::float AS total, "
                         "(result->'solver'->>'seconds')::float AS seconds FROM runs ORDER BY id DESC LIMIT 50").fetchall()
    return [{**r, "created": r["created"].isoformat()} for r in rows]


@app.get("/api/runs/{i}")
def get_run(i: int):
    with db() as c:
        r = c.execute("SELECT result FROM runs WHERE id=%s", [i]).fetchone()
    if not r:
        raise HTTPException(404, "запуск не найден")
    return {**r["result"], "run_id": i}


@app.post("/api/runs/{i}/replay")
def replay(i: int):
    """Повторный расчёт по сохранённому входу и seed: решение должно совпасть побитно (по хэшу)."""
    with db() as c:
        r = c.execute("SELECT input, result_hash FROM runs WHERE id=%s", [i]).fetchone()
    if not r:
        raise HTTPException(404, "запуск не найден")
    res = S.solve(r["input"])
    return dict(reproducible=res["result_hash"] == r["result_hash"], verified=res["verify"]["ok"],
                status=res["status"], result_hash=res["result_hash"], stored_hash=r["result_hash"])


def chk(t):
    if t not in COLS:
        raise HTTPException(404, "неизвестная таблица")


@app.post("/api/{t}")
def add(t: str, body: dict = Body(...)):
    chk(t)
    ks = [k for k in COLS[t] if k in body]
    sql = (f"INSERT INTO {t}({','.join(ks)}) VALUES({','.join(['%s'] * len(ks))}) RETURNING *" if ks
           else f"INSERT INTO {t} DEFAULT VALUES RETURNING *")
    with db() as c:
        return c.execute(sql, [body[k] for k in ks]).fetchone()


@app.patch("/api/{t}/{i}")
def update(t: str, i: int, body: dict = Body(...)):
    chk(t)
    ks = [k for k in COLS[t] if k in body]
    if not ks:
        raise HTTPException(422, "нет полей для обновления")
    with db() as c:
        r = c.execute(f"UPDATE {t} SET {','.join(k + '=%s' for k in ks)} WHERE id=%s RETURNING *",
                      [body[k] for k in ks] + [i]).fetchone()
    if not r:
        raise HTTPException(404, "запись не найдена")
    return r


@app.delete("/api/{t}/{i}")
def remove(t: str, i: int):
    chk(t)
    with db() as c:
        c.execute(f"DELETE FROM {t} WHERE id=%s", [i])
    return {"ok": True}


app.mount("/", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static"), html=True), name="ui")
