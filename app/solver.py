"""Модель распределения участников по проектам (OR-Tools CP-SAT) и независимая проверка результата."""
import hashlib
import json

from ortools.sat.python import cp_model

DEFAULT = dict(min_size=3, max_size=5, weeks=4, use_tasks=True, seed=1, time_limit=20,
               weights=dict(skills=50, exp=25, pref=15, size=10))


def merge(cfg):
    return {**DEFAULT, **cfg, "weights": {**DEFAULT["weights"], **cfg.get("weights", {})}}


def tok(s):
    return sorted({t.strip().lower() for t in (s or "").split(",") if t.strip()})


def sha(o):
    return hashlib.sha256(json.dumps(o, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def canon(people, projects, tasks, cfg):
    """Канонический (отсортированный) вход: одинаковые данные -> одинаковый хэш и решение."""
    by = lambda rows: sorted(rows, key=lambda r: r["id"])
    return dict(
        cfg=merge(cfg),
        people=[dict(id=p["id"], name=p["name"], skills=tok(p["skills"]), exp=p["exp"],
                     pref=(p["pref"] or "").strip().lower(), hours=p["hours"]) for p in by(people)],
        projects=[dict(id=j["id"], name=j["name"], skills=tok(j["skills"])) for j in by(projects)],
        tasks=[dict(id=t["id"], project_id=t["project_id"], title=t["title"], skill=(t["skill"] or "").strip().lower(),
                    hours=t["hours"], priority=t["priority"]) for t in by(tasks)])


def demand(inp):
    """D[j, s] — потребность проекта j в навыке s, ч.
    Есть задачи (и включён учёт задач): sum(часы * приоритет / 3). Задач нет: 16 ч * недель на каждый навык проекта."""
    c, d = inp["cfg"], {}
    for j, pr in enumerate(inp["projects"]):
        ts = [t for t in inp["tasks"] if t["project_id"] == pr["id"] and t["skill"]] if c["use_tasks"] else []
        if ts:
            for t in ts:
                d[j, t["skill"]] = d.get((j, t["skill"]), 0) + t["hours"] * t["priority"] / 3
        else:
            for s in pr["skills"]:
                d[j, s] = 16 * c["weeks"]
    return {k: max(1, round(v)) for k, v in d.items()}


def capacity(inp):
    """Cap[p, s] — вклад человека p в навык s за горизонт, ч = часы/нед * недели * (опыт / 5) / число навыков."""
    w = inp["cfg"]["weeks"]
    return {(i, s): round(p["hours"] * w * p["exp"] / 5 / len(p["skills"]))
            for i, p in enumerate(inp["people"]) for s in p["skills"]}


def evaluate(inp, asg):
    """Пересчёт метрик и проверка ограничений по готовому назначению {person_id: project_id} — без переменных решателя."""
    P, J, c = inp["people"], inp["projects"], inp["cfg"]
    n, D, C = len(P), demand(inp), capacity(inp)
    team = [[i for i, p in enumerate(P) if asg.get(p["id"]) == pr["id"]] for pr in J]
    bad, T, rows = [], sum(p["exp"] for p in P), []
    if sorted(asg) != sorted(p["id"] for p in P) or sum(map(len, team)) != n:
        bad.append("не каждый участник назначен ровно в одну команду")
    for j, t in enumerate(team):
        keys = [k for k in D if k[0] == j]
        got = {s: min(D[j, s], sum(C.get((i, s), 0) for i in t)) for _, s in keys}
        dem, req = sum(D[k] for k in keys), {s for _, s in keys}
        cap = sum(P[i]["hours"] * c["weeks"] * P[i]["exp"] / 5 for i in t)
        if not c["min_size"] <= len(t) <= c["max_size"]:
            bad.append(f"«{J[j]['name']}»: {len(t)} чел. вне диапазона {c['min_size']}–{c['max_size']}")
        rows.append(dict(
            id=J[j]["id"], name=J[j]["name"], size=len(t), avg_exp=sum(P[i]["exp"] for i in t) / len(t) if t else 0,
            demand=dem, covered=sum(got.values()), coverage=sum(got.values()) / dem if dem else 1,
            capacity=round(cap), utilization=dem / cap if cap else 0,
            missing=sorted(s for s in req if got[s] < D[j, s]), pref_hits=sum(P[i]["pref"] in req for i in t),
            dev=abs(sum(P[i]["exp"] * n - T for i in t)),
            members=[dict(name=P[i]["name"], skills=P[i]["skills"], exp=P[i]["exp"], pref_ok=P[i]["pref"] in req) for i in t]))
    totD, den_e = sum(D.values()), sum(abs(p["exp"] * n - T) for p in P)
    n_pref, sizes = sum(1 for p in P if p["pref"]), [r["size"] for r in rows]
    sc = dict(
        skills=sum(r["covered"] for r in rows) / totD if totD else 1,
        exp=1 - sum(r["dev"] for r in rows) / den_e if den_e else 1,
        pref=sum(r["pref_hits"] for r in rows) / n_pref if n_pref else 1,
        size=1 - (max(sizes) - min(sizes)) / max(1, c["max_size"] - c["min_size"]))
    w = c["weights"]
    sw = sum(w.values())
    return dict(ok=not bad, violations=bad, scores=sc, teams=rows, total=sum(w[k] * sc[k] for k in sc) / sw if sw else 0)


def solve(inp):
    P, J, c = inp["people"], inp["projects"], inp["cfg"]
    n, m, lo, hi = len(P), len(J), c["min_size"], c["max_size"]
    if not (n and m):
        raise ValueError("Нужен хотя бы один участник и один проект")
    if not m * lo <= n <= m * hi:
        raise ValueError(f"Ограничения невыполнимы: {n} участников, {m} команд, размер команды {lo}–{hi}")
    D, C = demand(inp), capacity(inp)
    M = cp_model.CpModel()
    x = [[M.NewBoolVar(f"x{i}_{j}") for j in range(m)] for i in range(n)]
    for i in range(n):
        M.AddExactlyOne(x[i])                                  # каждый — ровно в одной команде
    size = [sum(x[i][j] for i in range(n)) for j in range(m)]
    for j in range(m):
        M.Add(size[j] >= lo)
        M.Add(size[j] <= hi)
    cov = []                                                   # покрытие потребности: cov <= min(D, предложение)
    for (j, s), d in D.items():
        v = M.NewIntVar(0, d, f"cov{j}_{s}")
        M.Add(v <= sum(C[i, s] * x[i][j] for i in range(n) if (i, s) in C))
        cov.append(v)
    T = sum(p["exp"] for p in P)
    A = [p["exp"] * n - T for p in P]
    den_e = sum(map(abs, A))
    dev = []                                                   # отклонение опыта команды от «справедливой доли»
    for j in range(m if den_e else 0):
        d = M.NewIntVar(0, den_e, f"dev{j}")
        M.AddAbsEquality(d, sum(A[i] * x[i][j] for i in range(n)))
        dev.append(d)
    req = [{s for (jj, s) in D if jj == j} for j in range(m)]
    n_pref = sum(1 for p in P if p["pref"])
    mx, mn = M.NewIntVar(lo, hi, "mx"), M.NewIntVar(lo, hi, "mn")
    for j in range(m):
        M.Add(mx >= size[j])
        M.Add(mn <= size[j])
    w, K = c["weights"], 10 ** 6
    totD, den_s = sum(D.values()), max(1, hi - lo)
    obj = -round(w["size"] * K / den_s) * (mx - mn)
    if totD:
        obj += round(w["skills"] * K / totD) * sum(cov)
    if den_e:
        obj -= round(w["exp"] * K / den_e) * sum(dev)
    if n_pref:
        obj += round(w["pref"] * K / n_pref) * sum(x[i][j] for i in range(n) for j in range(m) if P[i]["pref"] in req[j])
    M.Maximize(obj)
    sv = cp_model.CpSolver()
    pr = sv.parameters
    pr.num_workers = 1                                         # один поток + seed => детерминированный поиск
    pr.random_seed = int(c["seed"])
    pr.max_time_in_seconds = float(c["time_limit"])
    pr.relative_gap_limit = 0.0
    pr.absolute_gap_limit = 0.0
    st = sv.Solve(M)
    if st not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise ValueError("Решение не найдено: " + sv.StatusName(st))
    asg = {P[i]["id"]: J[j]["id"] for i in range(n) for j in range(m) if sv.Value(x[i][j])}
    pairs, ob, bd = sorted(asg.items()), sv.ObjectiveValue(), sv.BestObjectiveBound()
    return dict(
        status=sv.StatusName(st), input_hash=sha(inp), result_hash=sha(pairs), assignment=pairs, verify=evaluate(inp, asg),
        solver=dict(objective=ob, bound=bd, gap=abs(bd - ob) / max(1.0, abs(ob)), seconds=round(sv.WallTime(), 3),
                    branches=sv.NumBranches(), conflicts=sv.NumConflicts(),
                    variables=len(M.Proto().variables), constraints=len(M.Proto().constraints)))
