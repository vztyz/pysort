"""Rule engine that labels every indexed Python file. Optional AI pass on top."""
import hashlib
import os
import re
import time

from db import get_meta, set_meta
from scanner import parent_of

CATEGORIES = {
    "user_project": {"label": "Твои проекты", "color": "#35c48e", "user": True},
    "user_loose": {"label": "Твои файлы вне проектов", "color": "#7ddc5a", "user": True},
    "unknown": {"label": "Неопознанное", "color": "#d6a63a", "user": False},
    "venv": {"label": "Виртуальные окружения", "color": "#4e8ff7", "user": False},
    "third_party": {"label": "Библиотеки (pip/conda)", "color": "#8a7cf5", "user": False},
    "system_python": {"label": "Стандартная библиотека Python", "color": "#b06ad6", "user": False},
    "system_other": {"label": "Встроено в другие программы", "color": "#c76b9b", "user": False},
    "cache": {"label": "Кэши и мусор", "color": "#8a94a6", "user": False},
}

FILE_MARKERS = {"pyproject", "requirements", "setup", "pipfile", "poetry", "venv", "django"}
PROJECT_KINDS = {"git", "pyproject", "requirements", "setup", "pipfile", "poetry", "django", "idea"}

VENV_PATH_RE = re.compile(
    r"\\(?:\.?venv|virtualenv|env)\\(?:lib|lib64|scripts|bin)\\",
    re.I,
)
STDLIB_RE = re.compile(r"\\lib\\(?:[^\\]+\\)*[^\\]+\.py$", re.I)
CONDA_RE = re.compile(r"\\(?:miniconda3|anaconda3|miniforge3|mambaforge|pkgs|conda\\envs)\\", re.I)
SYSTEM_OTHER_RE = re.compile(
    r"^(?:[a-z]:\\windows\\|[a-z]:\\program files(?: \(x86\))?\\|"
    r"[a-z]:\\programdata\\|[a-z]:\\appdata\\local\\programs\\)",
    re.I,
)
CACHE_RE = re.compile(
    r"\\(?:__pycache__|\.pytest_cache|\.mypy_cache|\.ruff_cache|\.ipynb_checkpoints|"
    r"pip\\cache|uv\\cache|\.cache\\|node_modules\\\.cache)\\", re.I
)

# tooling that lives in the profile but is not the user's code
TOOL_RE = re.compile(
    r"\\(?:appdata\\(?:local|roaming|locallow)\\|"
    r"\.vscode(?:-shared)?\\|\.antigravity[^\\]*\\|\.gemini\\|\.cline\\|\.copilot\\|"
    r"\.openclaw\\|\.cursor\\|\.windsurf\\|\.continue\\|\.codeium\\|"
    r"\.android\\|\.gradle\\|\.m2\\|\.nuget\\|\.dotnet\\|\.docker\\|\.virtualbox\\|"
    r"\.expo\\|\.p2\\|\.eclipse\\|\.local\\|\.conda\\|\.templateengine\\|"
    r"\$sysreset\\|windows\.old\\|"
    r"(?:stable-diffusion-webui|webui|comfyui)\\"
    r"(?:extensions|extensions-builtin|repositories|custom_nodes)\\)",
    re.I,
)


def _nearest(path, roots, limit=18):
    d = parent_of(path)
    steps = 0
    while d and steps < limit:
        if d in roots:
            return d
        d = parent_of(d)
        steps += 1
    return None


def loose_group(path, profile):
    parts = path.split("\\")
    if profile:
        prof_parts = profile.split("\\")
        if len(parts) > len(prof_parts) + 1 and "\\".join(parts[: len(prof_parts)]).lower() == profile.lower():
            return "\\".join(parts[: len(prof_parts) + 1])
    if len(parts) > 2:
        return parts[0] + "\\" + parts[1]
    return path


def build_context(conn):
    ctx = {
        "site": set(),
        "venv": set(),
        "install": set(),
        "project": set(),
        "project_kind": {},
        "venv_path_re": VENV_PATH_RE,
        "profile": os.path.expanduser("~"),
    }
    for path, kind in conn.execute("SELECT path, kind FROM markers"):
        if kind in FILE_MARKERS or kind in ("git", "idea", "conda_env"):
            folder = parent_of(path) or path
        else:
            folder = path
        if kind == "site-packages":
            ctx["site"].add(folder)
        elif kind in ("venv", "conda_env"):
            ctx["venv"].add(folder)
        elif kind == "python-install":
            ctx["install"].add(folder)
        if kind in PROJECT_KINDS and folder not in ctx["project_kind"]:
            ctx["project_kind"][folder] = kind
            ctx["project"].add(folder)
    return ctx


def classify_one(row, ctx):
    path, name, size, mtime = row
    low = path.lower()

    if CACHE_RE.search(low):
        return "cache", 0.97, ["путь кэша"]

    if _nearest(path, ctx["venv"]):
        return "venv", 0.97, ["внутри виртуального окружения"]

    if _nearest(path, ctx["site"]):
        return "third_party", 0.98, ["site-packages"]

    install = _nearest(path, ctx["install"])
    if install:
        rel = low[len(install):]
        if "\\lib\\" in rel or "\\scripts\\" in rel or "\\dlls\\" in rel:
            return "system_python", 0.96, ["установленный Python: " + install]
        return "system_python", 0.8, ["рядом с установкой Python"]

    if CONDA_RE.search(low):
        return "venv", 0.9, ["conda-окружение"]

    if SYSTEM_OTHER_RE.search(low):
        return "system_other", 0.93, ["системная папка Windows/Program Files"]

    if TOOL_RE.search(low):
        return "system_other", 0.9, ["файлы инструмента/расширения, не твой код"]

    if ctx["venv_path_re"].search(low):
        return "venv", 0.88, ["путь похож на venv"]

    project = _nearest(path, ctx["project"])
    if project:
        kind = ctx["project_kind"].get(project, "проект")
        return "user_project", 0.9, [f"проект ({kind}): {project}"], project

    if STDLIB_RE.search(low) and re.search(r"\\python\d", low):
        return "system_python", 0.85, ["похоже на Lib установленного Python"]

    profile = ctx["profile"]
    if profile and low.startswith(profile.lower() + "\\"):
        return "user_loose", 0.72, ["в профиле пользователя, без маркеров проекта"]

    if low.startswith(("d:\\", "e:\\")) or "\\users\\" in low:
        return "user_loose", 0.55, ["похоже на пользовательский файл"]

    return "unknown", 0.4, ["нет надёжных признаков"]


def classify(conn, log=print):
    ctx = build_context(conn)
    log(
        f"  маркеры: проектов {len(ctx['project'])}, "
        f"venv {len(ctx['venv'])}, site-packages {len(ctx['site'])}, "
        f"установок Python {len(ctx['install'])}"
    )
    rows = conn.execute("SELECT path, name, size, mtime FROM files").fetchall()
    updates = []
    counts = {}
    for path, name, size, mtime in rows:
        result = classify_one((path, name, size, mtime), ctx)
        cat, conf, reasons = result[0], result[1], result[2]
        project = result[3] if len(result) > 3 else None
        if cat == "user_loose":
            project = loose_group(path, ctx["profile"])
        elif cat == "user_project" and not project:
            project = _nearest(path, ctx["project"])
        updates.append(("; ".join(reasons), cat, conf, project, path))
        counts[cat] = counts.get(cat, 0) + 1

    conn.executemany(
        "UPDATE files SET reasons=?, category=?, confidence=?, project_root=? WHERE path=?",
        updates,
    )
    conn.commit()
    set_meta(conn, "classify_finished_at", int(time.time()))
    for cat, n in sorted(counts.items(), key=lambda x: -x[1]):
        log(f"    {CATEGORIES.get(cat, {}).get('label', cat):38s} {n:,}")
    return counts


def ambiguous_rows(conn, limit=0, max_conf=0.8):
    sql = (
        "SELECT path, size, mtime, category, confidence FROM files "
        "WHERE confidence < ? AND ext IN ('.py','.pyw','.pyi','.pyx','.ipynb') "
        "ORDER BY size DESC"
    )
    rows = conn.execute(sql, (max_conf,)).fetchall()
    if limit:
        rows = rows[:limit]
    return rows


def _cache_key(path, size, mtime, model):
    raw = f"{path.lower()}|{size}|{mtime}|{model}".encode("utf-8", "replace")
    return hashlib.sha1(raw).hexdigest()


def apply_ai(conn, client, limit=600, with_content=True, log=print):
    rows = ambiguous_rows(conn, limit=limit)
    if not rows:
        log("  спорных файлов нет — AI не нужен")
        return 0

    cached = {}
    for key, label, reason, conf in conn.execute(
        "SELECT key, label, reason, confidence FROM ai_cache"
    ):
        cached[key] = (label, reason, conf)

    todo = []
    hits = 0
    for path, size, mtime, cat, conf in rows:
        key = _cache_key(path, size, mtime, client.model)
        if key in cached:
            label, reason, aconf = cached[key]
            conn.execute(
                "UPDATE files SET ai_label=?, ai_reason=?, "
                "category=CASE WHEN confidence < 0.9 THEN ? ELSE category END, "
                "confidence=CASE WHEN confidence < 0.9 THEN ? ELSE confidence END "
                "WHERE path=?",
                (label, reason, label, aconf, path),
            )
            hits += 1
        else:
            todo.append({"path": path, "size": size, "mtime": mtime, "key": key, "markers": []})

    conn.commit()
    if hits:
        log(f"  из кэша AI: {hits:,}")
    log(f"  отправляю в AI ({client.backend}/{client.model}): {len(todo):,} файлов")
    if not todo:
        return hits

    from ai import classify_paths

    by_path = {t["path"]: t for t in todo}
    done = 0
    now = int(time.time())
    for path, verdict in classify_paths(client, todo, with_content=with_content, log=log):
        item = by_path.get(path)
        if not item:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO ai_cache(key,label,reason,confidence,model,ts) "
            "VALUES(?,?,?,?,?,?)",
            (item["key"], verdict["label"], verdict["reason"], verdict["confidence"], client.model, now),
        )
        conn.execute(
            "UPDATE files SET ai_label=?, ai_reason=?, "
            "category=CASE WHEN confidence < 0.9 THEN ? ELSE category END, "
            "confidence=CASE WHEN confidence < 0.9 THEN ? ELSE confidence END WHERE path=?",
            (verdict["label"], verdict["reason"], verdict["label"], verdict["confidence"], path),
        )
        done += 1
        if done % 25 == 0:
            conn.commit()
            log(f"    AI: {done:,}/{len(todo):,}")
    conn.commit()
    set_meta(conn, "ai_finished_at", now)
    return hits + done
