"""WizTree backend: MFT/Win32 scan in C++ + CSV import. 20-100x faster than pure Python."""
import csv
import ctypes
import datetime as dt
import os
import shutil
import subprocess
import sys
import tempfile
import time

from db import set_meta
from scanner import (
    DEFAULT_EXCLUDES,
    INTERESTING_EXT,
    MARKERS,
    depth_of,
    parent_of,
    norm_root,
)

CANDIDATES = [
    r"C:\Program Files\WizTree\WizTree64.exe",
    r"C:\Program Files (x86)\WizTree\WizTree64.exe",
    r"C:\Program Files\WizTree\WizTree.exe",
]


def find_wiztree():
    for path in CANDIDATES:
        if os.path.isfile(path):
            return path
    return shutil.which("WizTree64.exe") or shutil.which("WizTree.exe")


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def _detect_encoding(path):
    with open(path, "rb") as fh:
        head = fh.read(65536)
    if head.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    try:
        head.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        acp = ctypes.windll.kernel32.GetACP()
        if acp:
            return f"cp{acp}"
    except Exception:  # noqa: BLE001
        pass
    return "cp1251"


def _parse_time(text):
    for fmt in ("%Y.%m.%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return int(dt.datetime.strptime(text.strip(), fmt).timestamp())
        except ValueError:
            continue
    return 0


def export(root, csv_path, exe, log=print, timeout=3600):
    admin = "1" if is_admin() else "0"
    args = [exe, root, f"/export={csv_path}", f"/admin={admin}"]
    log(f"    WizTree: {root} (admin={admin})")
    started = time.time()
    creation = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
    proc = subprocess.run(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=timeout,
        creationflags=creation,
    )
    if proc.returncode != 0 or not os.path.exists(csv_path):
        raise RuntimeError(f"WizTree не смог экспортировать {root} (код {proc.returncode})")
    log(f"    готово за {time.time() - started:.0f} с, {os.path.getsize(csv_path) / 1024**2:.0f} МБ CSV")
    return csv_path


def _under_excludes(low, excludes):
    return any(low.startswith(e) for e in excludes)


def import_csv(conn, csv_path, root, dir_depth, excludes, log=print):
    enc = _detect_encoding(csv_path)
    dirs = {}
    markers = []
    file_batch = []
    py_rows = []
    stats = {"files": 0, "bytes": 0, "py": 0, "errors": 0}

    with open(csv_path, "r", encoding=enc, errors="replace", newline="") as fh:
        reader = csv.reader(fh)
        for row in reader:
            if len(row) < 7 or len(row[0]) < 3 or row[0][1] != ":":
                continue
            name = row[0]
            try:
                size = int(row[1] or 0)
                nfiles = int(row[5] or 0)
            except ValueError:
                stats["errors"] += 1
                continue
            low = name.lower()
            if _under_excludes(low, excludes):
                continue
            is_dir = name.endswith("\\")
            clean = name.rstrip("\\")
            if is_dir:
                base = os.path.basename(clean).lower()
                if base in ("site-packages", "dist-packages"):
                    markers.append((clean, "site-packages"))
                elif base in MARKERS:
                    markers.append((clean, MARKERS[base]))
                elif base == "conda-meta":
                    markers.append((clean, "conda_env"))
                d = depth_of(clean)
                if d <= dir_depth:
                    dirs[clean] = [size, nfiles, 0, 0]
                continue

            stats["files"] += 1
            stats["bytes"] += size
            base = os.path.basename(clean)
            lbase = base.lower()
            ext = os.path.splitext(lbase)[1]
            if lbase in MARKERS:
                markers.append((clean, MARKERS[lbase]))
            elif lbase == "python.exe":
                markers.append((os.path.dirname(clean), "python-install"))
            if ext not in INTERESTING_EXT:
                continue
            stats["py"] += 1
            file_batch.append(
                (clean, base, ext, size, _parse_time(row[3]), clean[:2].upper())
            )
            py_rows.append((clean, size))
            if len(file_batch) >= 5000:
                conn.executemany(
                    "INSERT OR REPLACE INTO files(path,name,ext,size,mtime,drive) VALUES(?,?,?,?,?,?)",
                    file_batch,
                )
                file_batch = []

    if file_batch:
        conn.executemany(
            "INSERT OR REPLACE INTO files(path,name,ext,size,mtime,drive) VALUES(?,?,?,?,?,?)",
            file_batch,
        )

    # aggregate .py counts only after all folder rows are known
    for path, size in py_rows:
        parent = parent_of(path)
        steps = 0
        while parent and steps <= dir_depth + 3:
            row = dirs.get(parent)
            if row is not None:
                row[2] += 1
                row[3] += size
            parent = parent_of(parent)
            steps += 1

    root_row = dirs.get(norm_root(root).rstrip("\\")) or dirs.get(norm_root(root))
    if root_row:
        stats["bytes"] = max(stats["bytes"], root_row[0])
        stats["files"] = max(stats["files"], root_row[1])

    dir_rows = [(p, depth_of(p), v[0], v[0], v[1], v[2], v[3]) for p, v in dirs.items()]
    conn.executemany(
        "INSERT OR REPLACE INTO dirs(path,depth,size,own_size,files,py_files,py_size) "
        "VALUES(?,?,?,?,?,?,?)",
        dir_rows,
    )
    conn.executemany("INSERT OR REPLACE INTO markers(path,kind) VALUES(?,?)", markers)
    conn.commit()
    return stats


def scan(conn, roots, dir_depth=7, excludes=None, log=print, keep_export=False, exe=None):
    exe = exe or find_wiztree()
    if not exe:
        raise RuntimeError("WizTree не найден")

    excludes = [e.rstrip("\\").lower() for e in (excludes or [])] + [
        e.lower() for e in DEFAULT_EXCLUDES
    ]

    conn.execute("DELETE FROM files")
    conn.execute("DELETE FROM dirs")
    conn.execute("DELETE FROM markers")
    conn.commit()

    # drop roots nested inside other roots
    norm = sorted({norm_root(r) for r in roots}, key=len)
    picked = []
    for r in norm:
        if any(r.lower().startswith(p.lower() + "\\") for p in picked):
            continue
        picked.append(r)

    tmp_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    started = time.time()
    total = {"files": 0, "bytes": 0, "py": 0, "errors": 0}
    used = []

    for root in picked:
        if not os.path.isdir(root):
            log(f"  ! {root} недоступен, пропускаю")
            continue
        safe = root[:2].replace(":", "") or "root"
        csv_path = os.path.join(tmp_dir, f"wiztree_{safe}_{len(used)}.csv")
        used.append(csv_path)
        export(root, csv_path, exe, log=log)
        log(f"  импортирую {os.path.basename(csv_path)}…")
        st = import_csv(conn, csv_path, root, dir_depth, excludes, log=log)
        for k in total:
            total[k] += st[k]
        log(
            f"    {st['files']:,} файлов, {st['py']:,} py, "
            f"{st['bytes'] / 1024**3:.1f} ГБ"
        )
        if not keep_export:
            try:
                os.remove(csv_path)
            except OSError:
                pass

    elapsed = time.time() - started
    set_meta(conn, "scan_finished_at", int(time.time()))
    set_meta(conn, "scan_seconds", round(elapsed, 1))
    set_meta(conn, "scan_roots", ",".join(picked))
    set_meta(conn, "scan_total_files", total["files"])
    set_meta(conn, "scan_total_bytes", total["bytes"])
    set_meta(conn, "scan_py_files", total["py"])
    set_meta(conn, "scan_errors", total["errors"])
    set_meta(conn, "scan_skipped_links", 0)
    set_meta(conn, "scan_backend", "wiztree")
    conn.commit()

    log(
        f"  готово: {total['files']:,} файлов ({total['bytes'] / 1024**3:.1f} ГБ), "
        f"из них {total['py']:,} python-файлов за {elapsed:.0f} с (бэкенд WizTree)"
    )
    return {
        "files": total["files"],
        "bytes": total["bytes"],
        "py_files": total["py"],
        "errors": total["errors"],
        "seconds": elapsed,
    }
