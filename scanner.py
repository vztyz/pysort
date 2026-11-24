"""Fast(ish) whole-disk walker. Collects Python-ish files, directory sizes and project markers."""
import os
import string
import time

from db import set_meta

LONG_PREFIX = "\\\\?\\"

# extensions we actually index (everything else is only counted for folder sizes)
INTERESTING_EXT = {".py", ".pyw", ".pyi", ".ipynb", ".pyx"}

MARKERS = {
    ".git": "git",
    "pyproject.toml": "pyproject",
    "requirements.txt": "requirements",
    "setup.py": "setup",
    "setup.cfg": "setup",
    "pipfile": "pipfile",
    "poetry.lock": "poetry",
    "pyvenv.cfg": "venv",
    ".idea": "idea",
    "environment.yml": "conda_env",
    "conda-meta": "conda_env",
    "manage.py": "django",
}

REPARSE_POINT = 0x400

# never descend into VCS internals: huge, never holds user .py files
SKIP_DESCEND = {".git", ".hg", ".svn", "$recycle.bin"}

DEFAULT_EXCLUDES = [
    r"c:\windows\winsxs",
    r"c:\windows\servicing",
    r"c:\windows\softwaredistribution\download",
    r"c:\windows\csc",
    r"c:\$recycle.bin",
    r"c:\system volume information",
    r"c:\$winreagent",
    r"c:\config.msi",
]


def to_long(path):
    path = os.path.abspath(path)
    if path.startswith(LONG_PREFIX):
        return path
    return LONG_PREFIX + path


def from_long(path):
    return path[4:] if path.startswith(LONG_PREFIX) else path


def norm_root(root):
    root = os.path.abspath(root)
    if len(root) == 2 and root[1] == ":":
        root += "\\"
    return root


def depth_of(path):
    return path.rstrip("\\").count("\\")


def parent_of(path):
    p = path.rstrip("\\")
    i = p.rfind("\\")
    if i < 0:
        return None
    if i == 2 and p[1] == ":":
        return p[:3]
    return p[:i]


def fixed_drives():
    drives = []
    for letter in string.ascii_uppercase:
        root = f"{letter}:\\"
        if os.path.exists(root):
            try:
                if os.stat(root).st_dev:
                    drives.append(root)
            except OSError:
                pass
    return drives


def _truncate_dir(path, max_depth):
    d = depth_of(path)
    if d <= max_depth:
        return path
    parts = path.rstrip("\\").split("\\")
    # parts[0] == 'C:'
    return "\\".join(parts[: max_depth + 1]) + "\\"


def _is_reparse(entry):
    try:
        st = entry.stat(follow_symlinks=False)
    except OSError:
        return True
    return bool(getattr(st, "st_file_attributes", 0) & REPARSE_POINT)


def scan(conn, roots, dir_depth=6, excludes=None, log=print, include_sxs=False):
    excludes = list(excludes or [])
    if not include_sxs:
        excludes += DEFAULT_EXCLUDES
    excludes = [e.rstrip("\\").lower() for e in excludes]

    conn.execute("DELETE FROM files")
    conn.execute("DELETE FROM dirs")
    conn.execute("DELETE FROM markers")
    conn.commit()

    started = time.time()
    dirs = {}  # path -> [own_size, files, py_files, py_size]
    file_rows = []
    marker_rows = []
    errors = 0
    skipped_links = 0
    total_files = 0
    total_bytes = 0
    interesting = 0

    def flush_files():
        nonlocal file_rows
        if file_rows:
            conn.executemany(
                "INSERT OR REPLACE INTO files(path,name,ext,size,mtime,drive) "
                "VALUES(?,?,?,?,?,?)",
                file_rows,
            )
            file_rows = []

    def excluded(path):
        low = path.lower()
        return any(low == e or low.startswith(e + "\\") for e in excludes)

    next_report = 25000

    for root in roots:
        root = norm_root(root)
        if not os.path.isdir(root):
            log(f"  ! {root} недоступен, пропускаю")
            continue
        log(f"  сканирую {root}")
        stack = [root]
        root_name = root[:2].upper()
        while stack:
            d = stack.pop()
            ddepth = depth_of(d)
            try:
                it = os.scandir(to_long(d))
            except OSError:
                errors += 1
                continue

            key = _truncate_dir(d, dir_depth)
            bucket = dirs.setdefault(key, [0, 0, 0, 0])
            if os.path.basename(d).lower() in ("site-packages", "dist-packages"):
                marker_rows.append((d, "site-packages"))
            if ddepth <= dir_depth:
                dirs.setdefault(d, [0, 0, 0, 0])

            try:
                with it:
                    for entry in it:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                name = entry.name
                                if _is_reparse(entry):
                                    skipped_links += 1
                                    continue
                                child = d if d.endswith("\\") else d + "\\"
                                child += name
                                if excluded(child):
                                    continue
                                low = name.lower()
                                if low in MARKERS:
                                    marker_rows.append((child, MARKERS[low]))
                                if low in SKIP_DESCEND:
                                    skipped_links += 1
                                    continue
                                stack.append(child)
                            elif entry.is_file(follow_symlinks=False):
                                st = entry.stat(follow_symlinks=False)
                                low = entry.name.lower()
                                total_files += 1
                                total_bytes += st.st_size
                                bucket[0] += st.st_size
                                bucket[1] += 1
                                ext = os.path.splitext(low)[1]
                                if ext in INTERESTING_EXT:
                                    bucket[2] += 1
                                    bucket[3] += st.st_size
                                    interesting += 1
                                    child = d if d.endswith("\\") else d + "\\"
                                    file_rows.append(
                                        (
                                            child + entry.name,
                                            entry.name,
                                            ext,
                                            st.st_size,
                                            int(st.st_mtime),
                                            root_name,
                                        )
                                    )
                                if low in MARKERS:
                                    child = d if d.endswith("\\") else d + "\\"
                                    marker_rows.append((child + entry.name, MARKERS[low]))
                                elif low == "python.exe":
                                    marker_rows.append((d, "python-install"))
                        except OSError:
                            errors += 1
                        if interesting and interesting % 4000 == 0:
                            flush_files()
                        if total_files >= next_report:
                            next_report += 25000
                            rate = total_files / max(time.time() - started, 0.1)
                            log(
                                f"    {total_files:,} файлов, {interesting:,} py-файлов, "
                                f"{rate:,.0f} файлов/с"
                            )
            except OSError:
                errors += 1

            if len(file_rows) >= 4000:
                flush_files()

    flush_files()

    # bottom-up subtree aggregation (parents get created on the fly, so bucket by depth)
    by_depth = {}
    for p in dirs:
        by_depth.setdefault(depth_of(p), []).append(p)
    for depth in sorted(by_depth, reverse=True):
        for p in by_depth[depth]:
            parent = parent_of(p)
            if not parent:
                continue
            if parent not in dirs:
                dirs[parent] = [0, 0, 0, 0]
                by_depth.setdefault(depth - 1, []).append(parent)
            dirs[parent][0] += dirs[p][0]
            dirs[parent][1] += dirs[p][1]
            dirs[parent][2] += dirs[p][2]
            dirs[parent][3] += dirs[p][3]

    dir_rows = [
        (p, depth_of(p), v[0], v[0], v[1], v[2], v[3]) for p, v in dirs.items()
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO dirs(path,depth,size,own_size,files,py_files,py_size) "
        "VALUES(?,?,?,?,?,?,?)",
        dir_rows,
    )
    conn.executemany("INSERT OR REPLACE INTO markers(path,kind) VALUES(?,?)", marker_rows)
    conn.commit()

    elapsed = time.time() - started
    set_meta(conn, "scan_finished_at", int(time.time()))
    set_meta(conn, "scan_seconds", round(elapsed, 1))
    set_meta(conn, "scan_roots", ",".join(roots))
    set_meta(conn, "scan_total_files", total_files)
    set_meta(conn, "scan_total_bytes", total_bytes)
    set_meta(conn, "scan_py_files", interesting)
    set_meta(conn, "scan_errors", errors)
    set_meta(conn, "scan_skipped_links", skipped_links)
    conn.commit()

    log(
        f"  готово: {total_files:,} файлов ({total_bytes / 1024**3:.1f} ГБ), "
        f"из них {interesting:,} python-файлов за {elapsed:.0f} с "
        f"(ошибок доступа: {errors}, junction/symlink пропущено: {skipped_links})"
    )
    return {
        "files": total_files,
        "bytes": total_bytes,
        "py_files": interesting,
        "errors": errors,
        "seconds": elapsed,
    }
