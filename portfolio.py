"""Project discovery + portfolio readiness report."""
import csv
import datetime as dt
import os
import re
import sys

from scanner import parent_of

PROJECT_KINDS = ("git", "pyproject", "requirements", "setup", "pipfile", "poetry", "idea")
FILE_KINDS = {"pyproject", "requirements", "setup", "pipfile", "poetry", "venv", "django"}

STACK = [
    ("django", ("django",), "Django"),
    ("flask", ("flask",), "Flask"),
    ("fastapi", ("fastapi",), "FastAPI"),
    ("streamlit", ("streamlit",), "Streamlit"),
    ("telegram", ("aiogram", "telebot", "telethon", "pyrogram"), "Telegram-бот"),
    ("discord", ("discord",), "Discord-бот"),
    ("vk", ("vk_api", "vkbottle"), "VK-бот"),
    ("ml", ("torch", "tensorflow", "keras", "sklearn", "transformers", "xgboost"), "ML/AI"),
    ("cv", ("cv2", "opencv", "PIL", "pillow", "easyocr", "mediapipe"), "Компьютерное зрение"),
    ("data", ("pandas", "numpy", "matplotlib", "seaborn", "plotly"), "Данные/графики"),
    ("scraper", ("requests", "bs4", "beautifulsoup4", "selenium", "playwright", "scrapy", "httpx"), "Скрапинг/API"),
    ("desktop", ("PyQt5", "PyQt6", "PySide6", "customtkinter", "tkinter", "kivy", "wx"), "Десктоп-приложение"),
    ("game", ("pygame", "arcade"), "Игра"),
    ("audio", ("pydub", "whisper", "librosa", "soundfile"), "Аудио"),
    ("video", ("moviepy", "ffmpeg", "imageio"), "Видео"),
    ("db", ("sqlalchemy", "sqlite3", "psycopg2", "pymongo", "redis"), "База данных"),
    ("parsing", ("docx", "openpyxl", "pypdf", "pdfplumber", "xlrd"), "Документы"),
]

NAME_FLAGS = ("test", "lab", "untitled", "copy", "копия", "new folder", "pythonproject",
              "tutorial", "lesson", "practice", "черновик", "draft", "tmp", "temp", "учеб")

SHELL_FOLDERS = {"desktop", "downloads", "documents", "onedrive", "users", "pictures",
                 "videos", "music", "рабочий стол", "загрузки", "документы", "изображения"}

# widely-known open-source projects that are often downloaded but are NOT the user's code
FOREIGN_HINTS = (
    "gpt4free", "gpt4-free", "stable-diffusion", "stable dif", "stabledif", "webui",
    "fooocus", "roop", "comfyui", "hermes-agent", "binwalk", "flutter", "ffmpeg",
    "ocrmypdf", "aistudio", "ghidra", "pyaudio-master", "screencast-keys", "yolov",
    "autogen", "langchain", "open-webui", "everything-claude-code", "antigravity",
)
FOREIGN_GIT = (
    "github.com/nousresearch", "github.com/coqui-ai", "github.com/refirmlabs",
    "github.com/flutter", "github.com/lllyasviel", "git.ffmpeg.org",
    "github.com/ocrmypdf", "github.com/microsoft", "github.com/google",
)

INFLUX = {"os", "sys", "re", "json", "time", "datetime", "pathlib", "typing", "math",
          "random", "logging", "subprocess", "threading", "collections", "itertools",
          "asyncio", "csv", "io", "abc", "functools", "traceback", "argparse", "shutil",
          "hashlib", "uuid", "socket", "urllib", "http", "base64", "copy", "glob",
          "__future__", "dataclasses", "enum", "textwrap", "string", "warnings"}


def _roots(conn):
    roots = set()
    for path, kind in conn.execute(
        "SELECT path, kind FROM markers WHERE kind IN (%s)"
        % ",".join("?" * len(PROJECT_KINDS)),
        PROJECT_KINDS,
    ):
        roots.add(parent_of(path) if kind in FILE_KINDS or kind in ("git", "idea") else path)
    return {
        r for r in roots
        if r and os.path.basename(r.rstrip("\\")).lower() not in SHELL_FOLDERS
        and len(r.rstrip("\\")) > 3
    }


def _assign(conn, roots):
    projects = {}
    loose = {}
    for path, size, mtime in conn.execute(
        "SELECT path, size, mtime FROM files WHERE category IN ('user_project','user_loose')"
    ):
        best = None
        d = parent_of(path)
        steps = 0
        while d and steps < 24:
            if d in roots:
                best = d
            d = parent_of(d)
            steps += 1
        if best:
            projects.setdefault(best, []).append((path, size, mtime))
        else:
            loose.setdefault(parent_of(path), []).append((path, size, mtime))
    for folder, items in loose.items():
        base = os.path.basename((folder or "").rstrip("\\")).lower()
        if not folder or len(folder) <= 3 or base in SHELL_FOLDERS:
            continue
        if len(items) >= 3:
            projects.setdefault(folder, []).extend(items)
    return projects


def _read_text(path, limit=200_000):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read(limit)
    except OSError:
        return ""


def _find_readme(root):
    try:
        names = os.listdir(root)
    except OSError:
        return None, ""
    for name in names:
        if name.lower().startswith("readme"):
            text = _read_text(os.path.join(root, name))
            return name, text
    return None, ""


def _describe(readme_text):
    if not readme_text:
        return ""
    text = re.sub(r"```.*?```", "", readme_text, flags=re.S)
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith(("#", "![", "[!", "<", "|", "-", "*", ">")):
            continue
        s = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", s)
        s = re.sub(r"[*_`]", "", s)
        if len(s) > 25:
            return s[:280]
    return ""


def _git_remote(root):
    cfg = os.path.join(root, ".git", "config")
    if not os.path.isfile(cfg):
        return ""
    text = _read_text(cfg, 20000)
    m = re.search(r"^\s*url\s*=\s*(.+)$", text, re.M)
    if not m:
        return ""
    url = m.group(1).strip()
    url = re.sub(r"^git@([^:]+):", r"https://\1/", url)
    return url[:-4] if url.endswith(".git") else url


def _requirements(root):
    pkgs = []
    for name in ("requirements.txt", "requirements-dev.txt"):
        text = _read_text(os.path.join(root, name))
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith(("#", "-")):
                continue
            pkg = re.split(r"[=<>!~\[; ]", line)[0].strip()
            if pkg:
                pkgs.append(pkg)
    for name in ("pyproject.toml", "Pipfile"):
        text = _read_text(os.path.join(root, name))
        for m in re.finditer(r'^\s*"?([A-Za-z0-9_.\-]+)"?\s*=\s*"[^"]*"', text, re.M):
            if m.group(1).lower() not in ("name", "version", "python", "requires"):
                pkgs.append(m.group(1))
    seen, out = set(), []
    for p in pkgs:
        low = p.lower()
        if low not in seen:
            seen.add(low)
            out.append(p)
    return out[:40]


IMPORT_RE = re.compile(r"^\s*(?:from\s+([A-Za-z_][\w\.]*)|import\s+([A-Za-z_][\w\.]*))", re.M)


def _scan_files(root, items):
    imports = {}
    loc = 0
    for path, size, _ in items:
        if size > 1_000_000:
            continue
        text = _read_text(path, 120_000)
        loc += text.count("\n") + 1
        for m in IMPORT_RE.finditer(text):
            mod = (m.group(1) or m.group(2)).split(".")[0]
            if mod and mod not in INFLUX:
                imports[mod] = imports.get(mod, 0) + 1
    return imports, loc


def _stack(imports, reqs):
    hay = {k.lower() for k in imports} | {r.lower() for r in reqs}
    labels = []
    for _, keys, label in STACK:
        if any(k.lower() in hay for k in keys):
            labels.append(label)
    return labels[:6]


def _foreign_reason(root, name, git):
    low = root.lower()
    nlow = name.lower()
    for hint in FOREIGN_HINTS:
        if hint in nlow or hint in low:
            return f"известный открытый проект ({hint})"
    if git:
        gl = git.lower()
        for owner in FOREIGN_GIT:
            if owner in gl:
                return f"клонирован чужой репозиторий ({gl.rsplit('/', 1)[0].rsplit('/', 1)[-1]})"
    return ""


def build(conn, log=print):
    roots = _roots(conn)
    projects = _assign(conn, roots)
    now = dt.datetime.now().timestamp()
    result = []
    foreign = []
    foreign_paths = set()
    for root, items in projects.items():
        name = os.path.basename(root.rstrip("\\")) or root
        py_items = [(p, s, m) for p, s, m in items if p.lower().endswith((".py", ".pyw"))]
        if not py_items:
            continue
        readme_name, readme = _find_readme(root)
        imports, loc = _scan_files(root, py_items)
        reqs = _requirements(root)
        stack = _stack(imports, reqs)
        last = max(m for _, _, m in py_items)
        git = _git_remote(root)
        has_git = os.path.isdir(os.path.join(root, ".git"))
        reason = _foreign_reason(root, name, git)
        if not reason:
            d = parent_of(root)
            steps = 0
            while d and steps < 20:
                if any(d.lower().rstrip("\\") == f.lower().rstrip("\\") for f in foreign_paths):
                    reason = "внутри чужого проекта"
                    break
                d = parent_of(d)
                steps += 1
        if reason:
            foreign.append({"name": name, "path": root, "reason": reason, "stack": stack,
                            "files": len(py_items), "loc": loc})
            foreign_paths.add(root)
            continue
        entries = [
            n for n in ("main.py", "app.py", "run.py", "bot.py", "manage.py", "server.py",
                        "start.py", "__main__.py")
            if os.path.isfile(os.path.join(root, n))
        ]
        tests = any("\\test" in p.lower() or os.path.basename(p).lower().startswith("test_")
                    for p, _, _ in py_items)
        images = any(
            os.path.isdir(os.path.join(root, n)) for n in ("screenshots", "screens", "static", "assets", "images")
        ) or any(
            f.lower().endswith((".png", ".jpg", ".jpeg", ".gif"))
            for f in _safe_listdir(root)
        )
        flags = [f for f in NAME_FLAGS if f in name.lower()]
        score = 0
        score += 2 if has_git else 0
        score += 2 if readme else 0
        score += 1 if (reqs or os.path.isfile(os.path.join(root, "pyproject.toml"))) else 0
        score += 1 if tests else 0
        score += 1 if entries else 0
        score += 1 if len(py_items) >= 5 else 0
        score += 1 if loc >= 200 else 0
        score += 1 if last > now - 180 * 86400 else 0
        score += 1 if images else 0
        if flags:
            score -= 2
        score = max(0, min(10, score))

        todo = []
        if not readme:
            todo.append("README")
        if not has_git:
            todo.append("git и публикация")
        if not reqs and not os.path.isfile(os.path.join(root, "pyproject.toml")):
            todo.append("requirements.txt")
        if not entries:
            todo.append("точка входа (main.py)")
        if not tests:
            todo.append("тесты")
        if not images:
            todo.append("скриншоты")
        potential = score + 2 * (not readme and 1 or 0) + 2 * (not has_git and 1 or 0)
        potential += (not reqs) + (not entries) + (not tests) + (not images)
        if flags:
            potential -= 2
        potential = max(score, min(10, potential))

        if score >= 7:
            verdict = "сильный кандидат"
        elif potential >= 7 and not flags:
            verdict = "хорошая база: добавить " + ", ".join(todo[:3])
        elif score >= 5:
            verdict = "можно показать, если дополнить"
        elif score >= 3:
            verdict = "сырой/учебный"
        else:
            verdict = "скорее черновик"
        if flags and score < 5:
            verdict = "похоже на учебный/черновой"

        result.append(
            {
                "path": root,
                "name": name,
                "score": score,
                "potential": potential,
                "verdict": verdict,
                "todo": todo,
                "stack": stack,
                "files": len(py_items),
                "loc": loc,
                "bytes": sum(s for _, s, _ in py_items),
                "last": last,
                "git": git,
                "has_git": has_git,
                "readme": readme_name or "",
                "description": _describe(readme),
                "entry": entries,
                "tests": tests,
                "images": images,
                "flags": flags,
                "requirements": reqs[:20],
                "imports": [k for k, _ in sorted(imports.items(), key=lambda x: -x[1])][:15],
            }
        )

    result.sort(key=lambda p: (p["score"], p["last"]), reverse=True)
    foreign.sort(key=lambda p: -p["loc"])
    log(f"  твоих проектов: {len(result)}, кандидатов в портфолио (>=7): "
        f"{sum(1 for p in result if p['score'] >= 7)}")
    log(f"  чужих/скачанных репозиториев отложено: {len(foreign)}")
    return result, foreign


def _safe_listdir(path):
    try:
        return os.listdir(path)
    except OSError:
        return []


def write_files(projects, out_dir, foreign=()):
    os.makedirs(out_dir, exist_ok=True)
    csv_path = os.path.join(out_dir, "portfolio.csv")
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["score", "potential", "verdict", "project", "path", "stack", "py_files", "loc",
                    "last_activity", "git", "readme", "entry", "tests", "images", "todo"])
        for p in projects:
            w.writerow([
                p["score"], p["potential"], p["verdict"], p["name"], p["path"], ", ".join(p["stack"]),
                p["files"], p["loc"], dt.datetime.fromtimestamp(p["last"]).strftime("%Y-%m-%d"),
                p["git"], p["readme"], ", ".join(p["entry"]),
                "да" if p["tests"] else "", "да" if p["images"] else "", ", ".join(p["todo"]),
            ])

    md_path = os.path.join(out_dir, "portfolio.md")
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write("# Кандидаты в портфолио\n\n")
        fh.write(f"Сгенерировано pysort · {dt.datetime.now():%d.%m.%Y %H:%M} · "
                 f"проектов: {len(projects)}\n\n")
        fh.write("Оценка 0–10 складывается из git, README, зависимостей, тестов, точки входа, "
                 "размера, свежести и скриншотов.\n\n")
        fh.write("| # | Оценка | Потенциал | Проект | Стек | .py | Строк | Активность | Git |\n")
        fh.write("|---|--------|-----------|--------|------|-----|-------|------------|-----|\n")
        for i, p in enumerate(projects[:80], 1):
            active = dt.datetime.fromtimestamp(p["last"]).strftime("%m.%Y")
            git = f"[repo]({p['git']})" if p["git"] else ("есть" if p["has_git"] else "—")
            fh.write(f"| {i} | **{p['score']}** | {p['potential']} | {p['name']} | {', '.join(p['stack']) or '—'} | "
                     f"{p['files']} | {p['loc']} | {active} | {git} |\n")
        fh.write("\n## Детали\n\n")
        for i, p in enumerate(projects[:40], 1):
            fh.write(f"### {i}. {p['name']} — {p['score']}/10 · {p['verdict']}\n\n")
            fh.write(f"`{p['path']}`\n\n")
            if p["description"]:
                fh.write(f"> {p['description']}\n\n")
            fh.write(f"- **Потенциал:** {p['potential']}/10")
            if p["todo"]:
                fh.write(f" · что добавить: {', '.join(p['todo'])}")
            fh.write("\n")
            fh.write(f"- **Стек:** {', '.join(p['stack']) or 'не определён'}\n")
            fh.write(f"- **Код:** {p['files']} файлов, {p['loc']} строк\n")
            fh.write(f"- **Активность:** {dt.datetime.fromtimestamp(p['last']):%d.%m.%Y}\n")
            if p["entry"]:
                fh.write(f"- **Запуск:** {', '.join(p['entry'])}\n")
            if p["git"]:
                fh.write(f"- **Git:** {p['git']}\n")
            elif p["has_git"]:
                fh.write("- **Git:** локальный репозиторий без remote\n")
            if p["readme"]:
                fh.write(f"- **README:** {p['readme']}\n")
            if p["tests"]:
                fh.write("- **Тесты:** есть\n")
            if p["images"]:
                fh.write("- **Скриншоты/ассеты:** есть\n")
            if p["flags"]:
                fh.write(f"- **Похоже на:** {', '.join(p['flags'])} (учебное/черновое)\n")
            if p["requirements"]:
                fh.write(f"- **Зависимости:** {', '.join(p['requirements'][:12])}\n")
            fh.write("\n")
        if foreign:
            fh.write("\n## Отложено: чужие/скачанные репозитории\n\n")
            fh.write("Они не являются твоими проектами, поэтому не попали в основную таблицу.\n\n")
            for f in foreign[:60]:
                fh.write(f"- `{f['name']}` — {f['reason']}; `{f['path']}`\n")
    return md_path, csv_path


def main(conn, out_dir, log=print):
    projects, foreign = build(conn, log=log)
    md, csv_path = write_files(projects, out_dir, foreign)
    log(f"  портфолио: {md}")
    log(f"             {csv_path}")
    return projects
