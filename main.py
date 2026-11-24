#!/usr/bin/env python
"""pysort — найти все Python-файлы на диске и отделить свои проекты от системных.

Примеры:
    python main.py all --open                 # полный проход по всем дискам
    python main.py scan --roots C:\\ D:\\      # только скан
    python main.py classify                   # только сортировка
    python main.py report --open              # пересобрать отчёт
    python main.py all --ai auto --open       # + LLM для спорных файлов
"""
import argparse
import os
import sys
import time
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(BASE, "pysort.db")
DEFAULT_OUT = os.path.join(BASE, "out")


def setup_console():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def log(msg=""):
    print(msg, flush=True)


def human(n):
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if n < 1024 or unit == "ТБ":
            return f"{n:,.1f} {unit}".replace(",", " ")
        n /= 1024


def ollama_alive():
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=2) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001
        return False


def build_ai(args):
    pref = (args.ai or "auto").lower()
    if pref == "off":
        return None
    if pref == "auto":
        if os.environ.get("OPENAI_API_KEY"):
            pref = "openai"
        elif os.environ.get("DEEPSEEK_API_KEY"):
            pref = "deepseek"
        elif ollama_alive():
            pref = "ollama"
        else:
            log("  AI: ключа нет и Ollama не запущена — работаю только на правилах.")
            log("      (можно задать OPENAI_API_KEY / DEEPSEEK_API_KEY или запустить ollama)")
            return None

    key = args.ai_key
    if not key:
        key = {
            "openai": os.environ.get("OPENAI_API_KEY"),
            "deepseek": os.environ.get("DEEPSEEK_API_KEY"),
        }.get(pref, "")

    from ai import AIClient

    log(f"  AI: {pref}, модель {args.ai_model or 'по умолчанию'}")
    return AIClient(pref, model=args.ai_model, api_key=key, base_url=args.ai_url)


def add_scan_args(p):
    p.add_argument("--roots", nargs="*", default=None, help="папки/диски (по умолчанию все диски)")
    p.add_argument("--dir-depth", type=int, default=7, help="глубина агрегации размеров папок")
    p.add_argument("--exclude", action="append", default=[], help="исключить папку (можно несколько)")
    p.add_argument("--include-sxs", action="store_true", help="не пропускать служебные папки Windows")
    p.add_argument(
        "--backend",
        default="auto",
        choices=["auto", "wiztree", "python"],
        help="wiztree = быстрый скан через установленный WizTree, python = встроенный обход",
    )
    p.add_argument("--keep-export", action="store_true", help="не удалять промежуточный CSV WizTree")


def add_ai_args(p):
    p.add_argument("--ai", default="auto", choices=["auto", "off", "openai", "deepseek", "ollama"])
    p.add_argument("--ai-model", default=None)
    p.add_argument("--ai-key", default=None, help="API-ключ (иначе из переменных окружения)")
    p.add_argument("--ai-url", default=None, help="свой base URL, OpenAI-совместимый")
    p.add_argument("--ai-limit", type=int, default=500, help="сколько спорных файлов отдать модели (0 = все)")
    p.add_argument("--no-ai-content", action="store_true", help="не отправлять фрагменты кода, только пути")


def cmd_scan(args):
    from db import connect, set_meta

    roots = args.roots or _fixed_drives()
    if not roots:
        log("Не нашёл дисков для сканирования.")
        return 1

    backend = args.backend
    if backend == "auto":
        from wiztree import find_wiztree

        backend = "wiztree" if find_wiztree() else "python"
        if backend == "wiztree":
            log("  найден WizTree — использую быстрый скан (--backend python чтобы отключить)")

    log(f"Скан: {', '.join(roots)}  →  {args.db}")
    conn = connect(args.db)
    set_meta(conn, "scan_started_at", int(time.time()))

    if backend == "wiztree":
        from wiztree import scan

        try:
            info = scan(
                conn,
                roots,
                dir_depth=args.dir_depth,
                excludes=args.exclude,
                log=log,
                keep_export=args.keep_export,
            )
        except Exception as exc:  # noqa: BLE001
            log(f"  ! WizTree не сработал ({exc}) — переключаюсь на встроенный обход")
            from scanner import scan as py_scan

            info = py_scan(
                conn,
                roots,
                dir_depth=args.dir_depth,
                excludes=args.exclude,
                log=log,
                include_sxs=args.include_sxs,
            )
    else:
        from scanner import scan

        info = scan(
            conn,
            roots,
            dir_depth=args.dir_depth,
            excludes=args.exclude,
            log=log,
            include_sxs=args.include_sxs,
        )
    log(f"Итого: {info['py_files']:,} python-файлов, {human(info['bytes'])} данных.")
    return 0


def _fixed_drives():
    from scanner import fixed_drives

    return fixed_drives()


def cmd_classify(args):
    from classify import apply_ai, classify
    from db import connect

    conn = connect(args.db)
    if not conn.execute("SELECT 1 FROM files LIMIT 1").fetchone():
        log("База пуста — сначала выполни: python main.py scan")
        return 1
    log("Сортирую по правилам…")
    classify(conn, log=log)
    client = build_ai(args)
    if client:
        apply_ai(
            conn,
            client,
            limit=args.ai_limit,
            with_content=not args.no_ai_content,
            log=log,
        )
    return 0


def cmd_report(args):
    from db import connect
    from report import collect, write_csvs, write_html

    conn = connect(args.db)
    if not conn.execute("SELECT 1 FROM files LIMIT 1").fetchone():
        log("База пуста — сначала выполни: python main.py scan")
        return 1
    log("Собираю отчёт…")
    data, stats = collect(conn, log=log)
    files_csv, projects_csv, cleanup_csv = write_csvs(data, args.out)
    html = write_html(data, args.out)
    log("")
    log(f"  python-файлов:        {stats['py_files']:,} ({human(stats['py_bytes'])})")
    log(f"  твои проекты/файлы:   {stats['user_files']:,} ({human(stats['user_bytes'])})")
    log(f"  можно освободить:     {human(data['cleanup']['total'])}")
    log("")
    log(f"  отчёт:    {html}")
    log(f"  csv:      {files_csv}")
    log(f"            {projects_csv}")
    log(f"            {cleanup_csv}")
    log(f"  план:     {os.path.join(args.out, 'cleanup-plan.txt')}")
    if args.open:
        os.startfile(html)  # noqa: S606
    return 0


def cmd_portfolio(args):
    from db import connect
    import portfolio

    conn = connect(args.db)
    if not conn.execute("SELECT 1 FROM files LIMIT 1").fetchone():
        log("База пуста — сначала выполни: python main.py scan")
        return 1
    portfolio.main(conn, args.out, log=log)
    return 0


def cmd_all(args):
    rc = cmd_scan(args)
    if rc:
        return rc
    rc = cmd_classify(args)
    if rc:
        return rc
    rc = cmd_report(args)
    if rc:
        return rc
    return cmd_portfolio(args)


def cmd_open(args):
    path = os.path.join(args.out, "report.html")
    if not os.path.exists(path):
        log("Отчёта ещё нет — выполни: python main.py all")
        return 1
    os.startfile(path)  # noqa: S606
    return 0


def main(argv=None):
    setup_console()
    parser = argparse.ArgumentParser(
        prog="pysort",
        description="Сканирует диски и раскладывает Python-файлы: свои проекты vs библиотеки/система.",
    )
    parser.add_argument("--db", default=DEFAULT_DB, help="файл базы (по умолчанию pysort.db)")
    parser.add_argument("--out", default=DEFAULT_OUT, help="папка для отчёта")
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("scan", help="просканировать диски")
    add_scan_args(p)
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("classify", help="разложить файлы по категориям")
    add_ai_args(p)
    p.set_defaults(func=cmd_classify)

    p = sub.add_parser("report", help="собрать HTML-отчёт и CSV")
    p.add_argument("--open", action="store_true")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("portfolio", help="найти проекты и оценить готовность для портфолио")
    p.set_defaults(func=cmd_portfolio)

    p = sub.add_parser("all", help="scan + classify + report")
    add_scan_args(p)
    add_ai_args(p)
    p.add_argument("--open", action="store_true")
    p.set_defaults(func=cmd_all)

    p = sub.add_parser("open", help="открыть готовый отчёт")
    p.set_defaults(func=cmd_open)

    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        parser.print_help()
        return 0
    try:
        return args.func(args) or 0
    except KeyboardInterrupt:
        log("\nОстановлено.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
