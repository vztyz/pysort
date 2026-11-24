"""Builds the offline HTML dashboard + CSV exports."""
import csv
import datetime as dt
import hashlib
import json
import os

from classify import CATEGORIES
from db import get_meta
from scanner import depth_of, parent_of

CACHE_NAMES = (
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".ipynb_checkpoints",
    ".tox",
    ".eggs",
)

USER_CATS = ("user_project", "user_loose", "unknown")


# --------------------------------------------------------------------------- data
def _dir_size(conn, path):
    row = conn.execute("SELECT size FROM dirs WHERE path=?", (path,)).fetchone()
    if row:
        return row[0]
    # venv may be deeper than the aggregation depth -> use closest stored ancestor
    row = conn.execute(
        "SELECT size FROM dirs WHERE path >= ? AND path < ? ORDER BY depth DESC LIMIT 1",
        (path + "\\", path + "\\\uffff"),
    ).fetchone()
    return row[0] if row else None


def _hash_dupes(conn, log, max_files=8000, max_bytes=1024**3):
    rows = conn.execute(
        "SELECT path,size,mtime FROM files WHERE size>=8192 AND size<=2097152 "
        "AND category IN ('user_project','user_loose','unknown') ORDER BY size DESC"
    ).fetchall()
    # only files whose size repeats can possibly be duplicates
    by_size = {}
    for row in rows:
        by_size.setdefault(row[1], []).append(row)
    candidates = [r for group in by_size.values() if len(group) > 1 for r in group]
    candidates.sort(key=lambda r: -r[1])
    known = {
        p: (s, m, h) for p, s, m, h in conn.execute("SELECT path,size,mtime,sha1 FROM hashes")
    }
    groups = {}
    done_bytes = 0
    processed = 0
    for path, size, mtime in candidates:
        if processed >= max_files or done_bytes > max_bytes:
            break
        cached = known.get(path)
        if cached and cached[0] == size and cached[1] == mtime:
            digest = cached[2]
        else:
            try:
                h = hashlib.sha1()
                with open(path, "rb") as fh:
                    for chunk in iter(lambda: fh.read(1 << 20), b""):
                        h.update(chunk)
                digest = h.hexdigest()
            except OSError:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO hashes(path,size,mtime,sha1) VALUES(?,?,?,?)",
                (path, size, mtime, digest),
            )
            done_bytes += size
        processed += 1
        groups.setdefault((digest, size), []).append(path)
    conn.commit()
    dupes = [
        {"size": size, "paths": sorted(paths)}
        for (_, size), paths in groups.items()
        if len(paths) > 1
    ]
    dupes.sort(key=lambda d: d["size"] * (len(d["paths"]) - 1), reverse=True)
    return dupes[:300], sum(d["size"] * (len(d["paths"]) - 1) for d in dupes)


def _cleanup(conn, projects, log):
    items = []
    seen_roots = []
    rows = conn.execute("SELECT path FROM markers WHERE kind='venv'").fetchall()
    venv_dirs = sorted({os.path.dirname(p) for (p,) in rows}, key=len)
    for path in venv_dirs:
        if any(path.lower().startswith(r.lower() + "\\") for r in seen_roots):
            continue
        seen_roots.append(path)
        size = _dir_size(conn, path)
        items.append(
            {
                "kind": "venv",
                "path": path,
                "size": size or 0,
                "note": "пересоздаётся: python -m venv .venv && pip install -r requirements.txt",
            }
        )

    cached_dirs = []
    for name in CACHE_NAMES:
        for (path,) in conn.execute(
            "SELECT path FROM dirs WHERE path LIKE ? AND depth>=2", ("%\\" + name,)
        ):
            cached_dirs.append((path, name))
    for path, name in cached_dirs:
        if any(path.lower().startswith(r.lower() + "\\") for r in seen_roots):
            continue
        size = _dir_size(conn, path)
        cached_dirs_size = size or 0
        if cached_dirs_size == 0:
            continue
        items.append(
            {
                "kind": "cache",
                "path": path,
                "size": cached_dirs_size,
                "note": "кэш/сгенерированные файлы, удаляются безопасно",
            }
        )

    dupes, dupe_bytes = _hash_dupes(conn, log)
    stale = [
        p for p in projects
        if p["kind"] == "project" and p["last"] and p["last"] < dt.datetime.now().timestamp() - 365 * 86400
    ]
    total = sum(i["size"] for i in items) + dupe_bytes
    return {"items": items, "dupes": dupes, "dupe_bytes": dupe_bytes, "stale": stale[:100], "total": total}


def _files_for_report(conn):
    cols = "path,size,mtime,category,confidence,project_root,reasons"
    user = conn.execute(
        f"SELECT {cols} FROM files WHERE category IN ('user_project','user_loose','unknown') "
        "ORDER BY size DESC LIMIT 25000"
    ).fetchall()
    sysrows = conn.execute(
        f"SELECT {cols} FROM files WHERE category NOT IN ('user_project','user_loose','unknown') "
        "ORDER BY size DESC LIMIT 4000"
    ).fetchall()
    out = []
    for r in user + sysrows:
        out.append([r[0], r[1], r[2], r[3], round(r[4] or 0, 2), r[5], r[6]])
    return out


def _refresh_dir_py(conn):
    """Recompute per-folder .py counters from the files table (cheap, keeps DBs consistent)."""
    dirs = {p: [0, 0] for (p,) in conn.execute("SELECT path FROM dirs")}
    for path, size in conn.execute("SELECT path,size FROM files"):
        parent = parent_of(path)
        steps = 0
        while parent and steps < 14:
            row = dirs.get(parent)
            if row is not None:
                row[0] += 1
                row[1] += size
            parent = parent_of(parent)
            steps += 1
    conn.executemany(
        "UPDATE dirs SET py_files=?, py_size=? WHERE path=?",
        [(v[0], v[1], p) for p, v in dirs.items()],
    )
    conn.commit()


def collect(conn, log=print):
    _refresh_dir_py(conn)
    py_files, py_bytes = conn.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM files").fetchone()
    by_cat = {}
    for cat, n, b in conn.execute(
        "SELECT category, COUNT(*), COALESCE(SUM(size),0) FROM files GROUP BY category"
    ):
        by_cat[cat] = {"files": n, "bytes": b}
    for key, meta in CATEGORIES.items():
        row = by_cat.setdefault(key, {"files": 0, "bytes": 0})
        row["label"] = meta["label"]
        row["color"] = meta["color"]
        row["user"] = meta["user"]

    projects = []
    for root, n, b, last in conn.execute(
        "SELECT project_root, COUNT(*), SUM(size), MAX(mtime) FROM files "
        "WHERE project_root IS NOT NULL GROUP BY project_root"
    ):
        kind = "folder"
        candidates = [
            root,
            os.path.join(root, ".git"),
            os.path.join(root, ".idea"),
            os.path.join(root, "pyproject.toml"),
            os.path.join(root, "requirements.txt"),
            os.path.join(root, "setup.py"),
        ]
        qmarks = ",".join("?" * len(candidates))
        if conn.execute(
            f"SELECT 1 FROM markers WHERE path IN ({qmarks}) AND "
            "kind IN ('git','pyproject','requirements','setup','idea') LIMIT 1",
            candidates,
        ).fetchone():
            kind = "project"
        projects.append(
            {"path": root, "files": n, "bytes": b, "last": last, "kind": kind}
        )
    projects.sort(key=lambda p: p["bytes"], reverse=True)

    dirs = [
        {
            "path": p,
            "parent": parent_of(p),
            "depth": depth_of(p),
            "size": s,
            "files": f,
            "py": pf,
            "pysize": ps,
        }
        for p, s, f, pf, ps in conn.execute(
            "SELECT path,size,files,py_files,py_size FROM dirs WHERE depth>=1 "
            "ORDER BY size DESC LIMIT 1500"
        )
    ]

    drives = []
    for root in (get_meta(conn, "scan_roots", "") or "").split(","):
        if not root:
            continue
        row = conn.execute(
            "SELECT size, files, py_files FROM dirs WHERE path=?", (root,)
        ).fetchone()
        if row:
            drives.append({"path": root, "size": row[0], "files": row[1], "py": row[2]})

    cleanup = _cleanup(conn, projects, log)

    # reuse the project discovery engine so the dashboard shows portfolio candidates too
    import portfolio

    own, foreign = portfolio.build(conn, log=lambda *a, **k: None)

    data = {
        "generated": dt.datetime.now().strftime("%d.%m.%Y %H:%M"),
        "scan": {
            "roots": get_meta(conn, "scan_roots", ""),
            "files": int(get_meta(conn, "scan_total_files", 0)),
            "bytes": int(get_meta(conn, "scan_total_bytes", 0)),
            "seconds": float(get_meta(conn, "scan_seconds", 0)),
            "errors": int(get_meta(conn, "scan_errors", 0)),
            "py_files": py_files,
            "py_bytes": py_bytes,
        },
        "cats": by_cat,
        "files": _files_for_report(conn),
        "projects": projects,
        "dirs": dirs,
        "drives": drives,
        "cleanup": cleanup,
        "portfolio": own,
        "foreign": foreign,
    }
    stats = {
        "py_files": py_files,
        "py_bytes": py_bytes,
        "user_files": sum(by_cat[c]["files"] for c in USER_CATS),
        "user_bytes": sum(by_cat[c]["bytes"] for c in USER_CATS),
    }
    return data, stats


# --------------------------------------------------------------------------- csv
def write_csvs(data, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    files_csv = os.path.join(out_dir, "files.csv")
    with open(files_csv, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["path", "size_bytes", "modified", "category", "confidence", "project", "reasons"])
        for path, size, mtime, cat, conf, project, reasons in data["files"]:
            ts = dt.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
            w.writerow([path, size, ts, cat, conf, project or "", reasons or ""])

    projects_csv = os.path.join(out_dir, "projects.csv")
    with open(projects_csv, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["folder", "kind", "py_files", "py_bytes", "last_activity"])
        for p in data["projects"]:
            ts = dt.datetime.fromtimestamp(p["last"]).strftime("%Y-%m-%d") if p["last"] else ""
            w.writerow([p["path"], p["kind"], p["files"], p["bytes"], ts])

    cleanup_csv = os.path.join(out_dir, "cleanup.csv")
    with open(cleanup_csv, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["kind", "path", "size_bytes", "note"])
        for item in data["cleanup"]["items"]:
            w.writerow([item["kind"], item["path"], item["size"], item["note"]])
        for group in data["cleanup"]["dupes"]:
            for path in group["paths"]:
                w.writerow(["duplicate", path, group["size"], "копия файла"])

    _write_cleanup_script(data, out_dir)
    return files_csv, projects_csv, cleanup_csv


def _write_cleanup_script(data, out_dir):
    lines = [
        "# Сгенерировано pysort. Сначала выполните с -WhatIf:",
        "#   powershell -ExecutionPolicy Bypass -File cleanup.ps1",
        "# Когда список верный — удалите -WhatIf из строк ниже.",
        '$ErrorActionPreference = "Continue"',
        "",
    ]
    for item in data["cleanup"]["items"]:
        if item["size"] < 1024 * 1024:
            continue
        lines.append(
            f'Remove-Item -LiteralPath "{item["path"].replace(chr(34), chr(39))}" '
            "-Recurse -Force -WhatIf  # " + item["note"]
        )
    txt = os.path.join(out_dir, "cleanup-plan.txt")
    with open(txt, "w", encoding="utf-8") as fh:
        fh.write("Кандидаты на удаление (проверьте перед запуском)\n\n")
        for item in sorted(data["cleanup"]["items"], key=lambda i: -i["size"]):
            fh.write(f'{item["size"] / 1024**3:8.2f} GB  {item["kind"]:6s}  {item["path"]}\n')
        fh.write(f'\nВсего: {data["cleanup"]["total"] / 1024**3:.2f} GB\n')
    with open(os.path.join(out_dir, "cleanup.ps1"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


# --------------------------------------------------------------------------- html
HTML_TEMPLATE = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>pysort — карта Python-файлов</title>
<style>
:root{
  --bg:#0d1015; --panel:#151922; --panel2:#1b2130; --line:#242c3b;
  --text:#e8ecf4; --muted:#8b96aa; --accent:#35c48e; --warn:#e6b23c; --bad:#ef6a7b;
  --up:#4e8ff7;
}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--text);
  font:14px/1.5 "Segoe UI",system-ui,-apple-system,sans-serif}
a{color:var(--accent);text-decoration:none}
header{position:sticky;top:0;z-index:20;background:rgba(13,16,21,.92);
  backdrop-filter:blur(10px);border-bottom:1px solid var(--line);padding:14px 24px}
.hrow{display:flex;align-items:center;gap:18px;flex-wrap:wrap;max-width:1500px;margin:0 auto}
.brand{font-size:19px;font-weight:700;letter-spacing:.3px}
.brand span{color:var(--muted);font-weight:400;font-size:12px;margin-left:8px}
.scanline{color:var(--muted);font-size:12.5px}
.scanline b{color:var(--text);font-weight:600}
main{max-width:1500px;margin:0 auto;padding:20px 24px 60px}
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:20px}
.tab{border:1px solid var(--line);background:var(--panel);color:var(--muted);
  padding:8px 14px;border-radius:9px;cursor:pointer;font-size:13.5px}
.tab:hover{color:var(--text);border-color:#36415a}
.tab.on{background:var(--accent);color:#03251b;border-color:var(--accent);font-weight:600}
section.pane{display:none;animation:fade .18s ease}
section.pane.on{display:block}
@keyframes fade{from{opacity:0;transform:translateY(3px)}to{opacity:1;transform:none}}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin-bottom:22px}
.card{background:linear-gradient(160deg,var(--panel2),var(--panel));border:1px solid var(--line);
  border-radius:13px;padding:15px 17px}
.card .k{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.7px}
.card .v{font-size:24px;font-weight:700;margin-top:6px;line-height:1.15}
.card .s{color:var(--muted);font-size:12.5px;margin-top:4px}
.card.green .v{color:var(--accent)} .card.blue .v{color:var(--up)}
.card.warn .v{color:var(--warn)} .card.pink .v{color:#e07fb4}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:13px;padding:16px 18px;margin-bottom:16px}
.panel h3{margin:0 0 14px;font-size:14px;font-weight:600;color:#c9d2e2}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:1000px){.grid2{grid-template-columns:1fr}}
.bar{display:flex;align-items:center;gap:10px;margin:9px 0}
.bar .nm{width:230px;font-size:12.5px;color:#c3ccdc;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.bar .tr{flex:1;height:7px;background:#0b0e14;border-radius:4px;overflow:hidden}
.bar .fl{height:100%;border-radius:4px;min-width:2px}
.bar .vl{width:110px;text-align:right;font-variant-numeric:tabular-nums;font-size:12px;color:var(--muted)}
table{width:100%;border-collapse:collapse;font-size:12.8px}
th{position:sticky;top:0;background:#11151d;color:var(--muted);text-align:left;font-weight:500;
  padding:9px 10px;border-bottom:1px solid var(--line);cursor:pointer;white-space:nowrap;user-select:none}
th:hover{color:var(--text)}
th.sorted{color:var(--accent)}
td{padding:7px 10px;border-bottom:1px solid #1c2230;vertical-align:top}
tr.r:hover td{background:#171d29}
.path{word-break:break-all}
.path .dir{color:var(--muted)}
.mono{font-family:"Cascadia Mono",Consolas,monospace;font-size:12px}
.badge{display:inline-block;padding:2px 8px;border-radius:20px;font-size:11px;white-space:nowrap}
.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.toolbar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:12px}
input[type=search],select{background:var(--panel2);border:1px solid var(--line);color:var(--text);
  border-radius:9px;padding:8px 11px;font-size:13px;outline:none}
input[type=search]{min-width:300px;flex:1;max-width:520px}
input[type=search]:focus{border-color:var(--accent)}
.chips{display:flex;gap:6px;flex-wrap:wrap}
.chip{border:1px solid var(--line);background:var(--panel2);border-radius:20px;padding:5px 11px;
  font-size:12px;cursor:pointer;color:var(--muted);display:flex;gap:6px;align-items:center}
.chip .dot{width:8px;height:8px;border-radius:50%}
.chip.on{background:#20303f;color:var(--text);border-color:#35506b}
.btn{border:1px solid var(--line);background:var(--panel2);color:var(--text);border-radius:8px;
  padding:6px 11px;font-size:12.5px;cursor:pointer}
.btn:hover{border-color:#3c4a63}
.btn.primary{background:var(--accent);color:#04241a;border-color:var(--accent);font-weight:600}
.pager{display:flex;gap:8px;align-items:center;justify-content:flex-end;margin-top:12px;color:var(--muted);font-size:12.5px}
.hint{color:var(--muted);font-size:12px}
.empty{color:var(--muted);padding:24px 6px;text-align:center}
.modal{position:fixed;inset:0;background:rgba(4,6,9,.72);display:none;align-items:center;
  justify-content:center;z-index:50;padding:20px}
.modal.on{display:flex}
.modal .box{background:var(--panel);border:1px solid #2b3548;border-radius:14px;max-width:860px;width:100%;
  max-height:82vh;overflow:auto;padding:22px}
.modal h2{margin:0 0 4px;font-size:16px;word-break:break-all}
.kv{display:grid;grid-template-columns:130px 1fr;gap:6px 14px;font-size:13px;margin:14px 0}
.kv .k{color:var(--muted)}
.muted{color:var(--muted)}
.tag{font-size:11px;color:var(--muted);border:1px solid var(--line);border-radius:6px;padding:1px 6px;margin-left:6px}
.legend{display:flex;gap:14px;flex-wrap:wrap;color:var(--muted);font-size:12px;margin-top:10px}
.legend i{display:inline-block;width:9px;height:9px;border-radius:3px;margin-right:5px}
.copyline{display:flex;gap:8px;align-items:center;margin-top:10px}
.copyline input{flex:1;background:#0b0e14;border:1px solid var(--line);color:var(--muted);
  border-radius:8px;padding:7px 10px;font:12px/1.4 Consolas,monospace}
.warnbox{background:#2a2213;border:1px solid #4d3d17;color:#e8cd8c;border-radius:10px;
  padding:11px 14px;font-size:12.5px;margin-bottom:14px}
</style>
</head>
<body>
<header>
  <div class="hrow">
    <div class="brand">pysort <span>карта Python-файлов на диске</span></div>
    <div class="scanline" id="scanline"></div>
  </div>
</header>
<main>
  <div class="tabs" id="tabs"></div>
  <section class="pane on" id="pane-overview"></section>
  <section class="pane" id="pane-projects"></section>
  <section class="pane" id="pane-files"></section>
  <section class="pane" id="pane-portfolio"></section>
  <section class="pane" id="pane-disk"></section>
  <section class="pane" id="pane-cleanup"></section>
</main>
<div class="modal" id="modal"><div class="box" id="modalbox"></div></div>
<script>
const DATA = __DATA__;
const USER_CATS = ["user_project","user_loose","unknown"];
const state = {tab:"overview", q:"", pq:"", cats:new Set(), minSize:0, sortKey:"size", sortDir:-1, page:0, pageSize:100};

const $ = (s,el=document)=>el.querySelector(s);
const esc = s => String(s).replace(/[&<>"']/g, c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
function fmtBytes(n){
  if(n==null) return "—";
  const u=["Б","КБ","МБ","ГБ","ТБ"]; let i=0; n=Number(n);
  while(n>=1024 && i<u.length-1){n/=1024;i++;}
  return (n>=100||i===0? Math.round(n) : n.toFixed(n>=10?1:2))+" "+u[i];
}
function fmtDate(ts){
  if(!ts) return "—";
  const d=new Date(ts*1000), p=x=>String(x).padStart(2,"0");
  return `${p(d.getDate())}.${p(d.getMonth()+1)}.${d.getFullYear()}`;
}
function fmtAgo(ts){
  if(!ts) return "—";
  const days=Math.floor((Date.now()/1000-ts)/86400);
  if(days<=1) return "сегодня";
  if(days<30) return days+" дн. назад";
  if(days<365) return Math.round(days/30)+" мес. назад";
  return (days/365).toFixed(1)+" г. назад";
}
const cat = k => DATA.cats[k] || {label:k,color:"#8a94a6",files:0,bytes:0};
function badge(k){
  const c=cat(k);
  return `<span class="badge" style="background:${c.color}22;color:${c.color};border:1px solid ${c.color}44">${esc(c.label)}</span>`;
}
function pathHtml(p){
  const i=p.lastIndexOf("\\");
  return `<span class="dir">${esc(p.slice(0,i+1))}</span><b>${esc(p.slice(i+1))}</b>`;
}
function copyText(t){
  navigator.clipboard?.writeText(t).catch(()=>{});
}

/* ---------- tabs ---------- */
const TABS=[["overview","Обзор"],["projects","Проекты"],["portfolio","Портфолио"],["files","Все файлы"],["disk","Диск"],["cleanup","Очистка"]];
function renderTabs(){
  $("#tabs").innerHTML = TABS.map(([k,l])=>{
    const n = k==="files"?DATA.files.length:k==="projects"?DATA.projects.length:k==="portfolio"?DATA.portfolio.length:k==="cleanup"?DATA.cleanup.items.length:"";
    return `<button class="tab ${state.tab===k?'on':''}" data-tab="${k}">${l}${n?` <span class="muted">${n}</span>`:""}</button>`;
  }).join("");
  $("#tabs").querySelectorAll(".tab").forEach(b=>b.onclick=()=>{
    state.tab=b.dataset.tab;
    document.querySelectorAll(".pane").forEach(p=>p.classList.toggle("on",p.id==="pane-"+state.tab));
    renderTabs(); renderTab();
  });
}

/* ---------- overview ---------- */
function renderOverview(){
  const s=DATA.scan, u=USER_CATS.reduce((a,c)=>({f:a.f+cat(c).files,b:a.b+cat(c).bytes}),{f:0,b:0});
  const libs=["venv","third_party","system_python","system_other"].reduce((a,c)=>({f:a.f+cat(c).files,b:a.b+cat(c).bytes}),{f:0,b:0});
  const cache=cat("cache");
  const cats=Object.entries(DATA.cats).sort((a,b)=>b[1].bytes-a[1].bytes);
  const maxB=Math.max(...cats.map(([,v])=>v.bytes),1);
  const topProj=DATA.projects.filter(p=>p.kind==="project").slice(0,8);
  const dirPaths=new Set(DATA.dirs.map(d=>d.path));
  const topDirs=DATA.dirs.filter(d=>!dirPaths.has(d.parent)).sort((a,b)=>b.size-a.size).slice(0,8);
  const maxD=Math.max(...topDirs.map(d=>d.size),1);
  $("#pane-overview").innerHTML=`
    <div class="cards">
      <div class="card green"><div class="k">Твой код</div><div class="v">${u.f.toLocaleString("ru")}</div>
        <div class="s">${fmtBytes(u.b)} · проекты и отдельные скрипты</div></div>
      <div class="card blue"><div class="k">Библиотеки и окружения</div><div class="v">${libs.f.toLocaleString("ru")}</div>
        <div class="s">${fmtBytes(libs.b)} · venv, pip, stdlib</div></div>
      <div class="card pink"><div class="k">Кэши и мусор</div><div class="v">${cache.files.toLocaleString("ru")}</div>
        <div class="s">${fmtBytes(cache.bytes)}</div></div>
      <div class="card warn"><div class="k">Можно освободить</div><div class="v">${fmtBytes(DATA.cleanup.total)}</div>
        <div class="s">venv + кэши + дубликаты</div></div>
      <div class="card"><div class="k">Просканировано</div><div class="v">${fmtBytes(s.bytes)}</div>
        <div class="s">${Number(s.files).toLocaleString("ru")} файлов за ${Math.round(s.seconds)} с</div></div>
    </div>
    <div class="grid2">
      <div class="panel"><h3>Что за файлы</h3>
        ${cats.map(([k,v])=>`<div class="bar"><div class="nm" title="${esc(cat(k).label)}">${esc(cat(k).label)}</div>
          <div class="tr"><div class="fl" style="width:${(v.bytes/maxB*100).toFixed(1)}%;background:${v.color}"></div></div>
          <div class="vl">${fmtBytes(v.bytes)} · ${v.files.toLocaleString("ru")}</div></div>`).join("")}
        <div class="legend">${Object.entries(DATA.cats).map(([k,v])=>`<span><i style="background:${v.color}"></i>${esc(v.label)}</span>`).join("")}</div>
      </div>
      <div class="panel"><h3>Самые большие папки</h3>
        ${topDirs.map(d=>`<div class="bar"><div class="nm" title="${esc(d.path)}">${esc(d.path)}</div>
          <div class="tr"><div class="fl" style="width:${(d.size/maxD*100).toFixed(1)}%;background:#4e8ff7"></div></div>
          <div class="vl">${fmtBytes(d.size)}</div></div>`).join("")}
        <div class="hint" style="margin-top:10px">Вкладка «Диск» — топ-300 папок.</div>
      </div>
    </div>
    <div class="panel"><h3>Активные проекты</h3>
      ${topProj.length?`<table><thead><tr><th>Папка</th><th class="num">.py</th><th class="num">Размер</th><th>Активность</th><th></th></tr></thead>
      <tbody>${topProj.map(p=>`<tr class="r"><td class="path">${esc(p.path)}</td><td class="num">${p.files}</td>
      <td class="num">${fmtBytes(p.bytes)}</td><td>${fmtAgo(p.last)}</td>
      <td><button class="btn" data-show="${esc(p.path)}">файлы</button></td></tr>`).join("")}</tbody></table>`
      :'<div class="empty">Проектов не найдено — только отдельные файлы.</div>'}
    </div>`;
  $("#pane-overview").querySelectorAll("button[data-show]").forEach(b=>b.onclick=()=>showProject(b.dataset.show));
}

/* ---------- projects ---------- */
function renderProjects(){
  const rows=DATA.projects;
  $("#pane-projects").innerHTML=`
    <div class="toolbar"><input type="search" id="pq" placeholder="Фильтр по папке проекта…" value="${esc(state.pq||"")}">
      <span class="hint">Всего: ${rows.length} папок · ${fmtBytes(rows.reduce((a,p)=>a+p.bytes,0))}</span></div>
    <div class="panel" style="padding:0">
      <table><thead><tr><th>Папка</th><th class="num">Файлов</th><th class="num">Размер</th><th>Последнее изменение</th><th>Тип</th><th></th></tr></thead>
      <tbody id="pbody"></tbody></table>
    </div>`;
  const draw=()=>{
    const q=(state.pq||"").toLowerCase();
    const list=rows.filter(p=>!q||p.path.toLowerCase().includes(q));
    $("#pbody").innerHTML=list.slice(0,1500).map(p=>`<tr class="r">
      <td class="path">${esc(p.path)}</td><td class="num">${p.files}</td><td class="num">${fmtBytes(p.bytes)}</td>
      <td>${fmtDate(p.last)} <span class="muted">· ${fmtAgo(p.last)}</span></td>
      <td>${p.kind==="project"?'<span class="badge" style="background:#35c48e22;color:#35c48e">проект</span>':'<span class="badge" style="background:#8a94a622;color:#8a94a6">папка</span>'}</td>
      <td><button class="btn" data-p="${esc(p.path)}">показать .py</button></td></tr>`).join("")
      || '<tr><td colspan="6" class="empty">Ничего не найдено</td></tr>';
    $("#pbody").querySelectorAll("button[data-p]").forEach(b=>b.onclick=()=>{
      state.q=b.dataset.p; state.page=0; state.cats.clear(); state.tab="files";
      document.querySelectorAll(".pane").forEach(p=>p.classList.toggle("on",p.id==="pane-files"));
      renderTabs(); renderFiles();
    });
  };
  $("#pq").oninput=e=>{state.pq=e.target.value; draw();};
  draw();
}
function showProject(path){
  state.q=path; state.page=0; state.cats.clear(); state.tab="files";
  document.querySelectorAll(".pane").forEach(p=>p.classList.toggle("on",p.id==="pane-files"));
  renderTabs(); renderFiles();
}

/* ---------- files ---------- */
function filteredFiles(){
  const q=(state.q||"").toLowerCase();
  return DATA.files.filter(f=>{
    if(state.cats.size && !state.cats.has(f[3])) return false;
    if(state.minSize && f[1]<state.minSize) return false;
    if(q && !f[0].toLowerCase().includes(q)) return false;
    return true;
  }).sort((a,b)=>{
    const k=state.sortKey, d=state.sortDir;
    let va,vb;
    if(k==="size"){va=a[1];vb=b[1];}
    else if(k==="mtime"){va=a[2];vb=b[2];}
    else if(k==="cat"){va=cat(a[3]).label;vb=cat(b[3]).label;}
    else {va=a[0].toLowerCase();vb=b[0].toLowerCase();}
    return (va<vb?-1:va>vb?1:0)*d;
  });
}
function renderFiles(){
  const cats=Object.keys(DATA.cats).filter(k=>DATA.cats[k].files>0);
  $("#pane-files").innerHTML=`
    <div class="toolbar">
      <input type="search" id="fq" placeholder="Поиск по пути: имя файла, папка, проект…" value="${esc(state.q)}">
      <select id="fsize">
        <option value="0">любой размер</option><option value="1024">от 1 КБ</option>
        <option value="10240">от 10 КБ</option><option value="102400">от 100 КБ</option>
      </select>
      <button class="btn" id="freset">сбросить</button>
    </div>
    <div class="chips" style="margin-bottom:12px">
      ${cats.map(k=>`<span class="chip ${state.cats.has(k)?"on":""}" data-c="${k}">
        <i class="dot" style="background:${cat(k).color}"></i>${esc(cat(k).label)}
        <span class="muted">${DATA.cats[k].files.toLocaleString("ru")}</span></span>`).join("")}
    </div>
    <div class="panel" style="padding:0;overflow:auto;max-height:70vh">
      <table><thead><tr>
        <th data-s="path" class="${state.sortKey==="path"?"sorted":""}">Путь</th>
        <th data-s="size" class="num ${state.sortKey==="size"?"sorted":""}">Размер</th>
        <th data-s="mtime" class="${state.sortKey==="mtime"?"sorted":""}">Изменён</th>
        <th data-s="cat" class="${state.sortKey==="cat"?"sorted":""}">Категория</th>
        <th class="num">Уверенность</th>
      </tr></thead><tbody id="fbody"></tbody></table>
    </div>
    <div class="pager">
      <span id="pinfo"></span>
      <button class="btn" id="pprev">←</button>
      <button class="btn" id="pnext">→</button>
      <select id="psize"><option>50</option><option selected>100</option><option>250</option><option>500</option></select>
    </div>
    <div class="hint" style="margin-top:8px">В отчёт встроены до 29 000 крупнейших файлов; полный список — в files.csv. Клик по строке открывает детали.</div>`;

  const draw=()=>{
    const list=filteredFiles();
    const total=list.length, pages=Math.max(1,Math.ceil(total/state.pageSize));
    if(state.page>=pages) state.page=pages-1;
    const start=state.page*state.pageSize;
    const slice=list.slice(start,start+state.pageSize);
    $("#fbody").innerHTML=slice.map(f=>`<tr class="r" data-i="${DATA.files.indexOf(f)}">
      <td class="path">${pathHtml(f[0])}</td>
      <td class="num">${fmtBytes(f[1])}</td>
      <td>${fmtDate(f[2])}</td>
      <td>${badge(f[3])}</td>
      <td class="num muted">${f[4]?Math.round(f[4]*100)+"%":""}</td></tr>`).join("")
      || '<tr><td colspan="5" class="empty">Ничего не найдено</td></tr>';
    $("#pinfo").textContent=`${total.toLocaleString("ru")} файлов · стр. ${state.page+1} из ${pages}`;
    $("#pprev").disabled=state.page===0; $("#pnext").disabled=state.page>=pages-1;
    $("#fbody").querySelectorAll("tr[data-i]").forEach(tr=>tr.onclick=()=>openFile(+tr.dataset.i));
  };
  $("#fq").oninput=e=>{state.q=e.target.value; state.page=0; draw();};
  $("#fsize").onchange=e=>{state.minSize=+e.target.value; state.page=0; draw();};
  $("#freset").onclick=()=>{state.q="";state.cats.clear();state.page=0;state.minSize=0;$("#fq").value="";renderFiles();};
  $("#pane-files").querySelectorAll(".chip").forEach(c=>c.onclick=()=>{
    const k=c.dataset.c; state.cats.has(k)?state.cats.delete(k):state.cats.add(k);
    state.page=0; renderFiles();
  });
  $("#pane-files").querySelectorAll("th[data-s]").forEach(th=>th.onclick=()=>{
    const k=th.dataset.s;
    if(state.sortKey===k) state.sortDir*=-1; else {state.sortKey=k;state.sortDir=k==="size"||k==="mtime"?-1:1;}
    draw();
  });
  $("#pprev").onclick=()=>{state.page--;draw();};
  $("#pnext").onclick=()=>{state.page++;draw();};
  $("#psize").onchange=e=>{state.pageSize=+e.target.value;state.page=0;draw();};
  draw();
}
function openFile(i){
  const f=DATA.files[i]; if(!f) return;
  const [path,size,mtime,c,conf,project,reasons]=f;
  $("#modalbox").innerHTML=`
    <h2>${esc(path.split("\\").pop())}</h2>
    <div class="muted" style="word-break:break-all">${esc(path)}</div>
    <div class="kv">
      <div class="k">Категория</div><div>${badge(c)}</div>
      <div class="k">Уверенность</div><div>${conf?Math.round(conf*100)+"%":"—"}</div>
      <div class="k">Размер</div><div>${fmtBytes(size)}</div>
      <div class="k">Изменён</div><div>${fmtDate(mtime)} · ${fmtAgo(mtime)}</div>
      <div class="k">Проект</div><div class="mono">${project?esc(project):"—"}</div>
      <div class="k">Почему</div><div>${esc(reasons||"—")}</div>
    </div>
    <div class="copyline"><input readonly value="${esc(path)}"><button class="btn primary" id="copybtn">Копировать путь</button></div>
    <div style="margin-top:14px;text-align:right"><button class="btn" onclick="document.getElementById('modal').classList.remove('on')">Закрыть</button></div>`;
  $("#modal").classList.add("on");
  $("#copybtn").onclick=()=>{copyText(path); $("#copybtn").textContent="Скопировано";};
}
$("#modal").onclick=e=>{ if(e.target.id==="modal") e.target.classList.remove("on"); };
document.addEventListener("keydown",e=>{ if(e.key==="Escape") $("#modal").classList.remove("on"); });

/* ---------- portfolio ---------- */
function renderPortfolio(){
  const all=DATA.portfolio||[], foreign=DATA.foreign||[];
  const strong=all.filter(p=>p.score>=7).length;
  const good=all.filter(p=>p.score>=5).length;
  const mode=state.pfMode||"strong";
  const list=mode==="all"?all:mode==="good"?all.filter(p=>p.score>=5):all.filter(p=>p.score>=7);
  const rows=list.map(p=>`<tr class="r" data-path="${esc(p.path)}" title="${esc(p.path)}">
    <td class="num"><b style="font-size:16px;color:${p.score>=7?'#4ade80':p.score>=5?'#fbbf24':'#8ea3c0'}">${p.score}</b><span class="muted">/${p.potential}</span></td>
    <td><b>${esc(p.name)}</b><div class="path" style="white-space:normal">${esc(p.path)}</div></td>
    <td>${esc(p.stack.join(", ")||"—")}</td>
    <td class="num">${p.files}</td><td class="num">${p.loc.toLocaleString("ru")}</td>
    <td class="num">${fmtDate(p.last)}</td><td>${esc(p.verdict)}</td></tr>`).join("");
  $("#pane-portfolio").innerHTML=`
    <div class="cards">
      <div class="card"><div class="k">Твоих проектов найдено</div><div class="v">${all.length}</div><div class="s">без учёта скачанных репозиториев</div></div>
      <div class="card"><div class="k">Сильных кандидатов</div><div class="v">${strong}</div><div class="s">оценка 7–10</div></div>
      <div class="card"><div class="k">С хорошей базой</div><div class="v">${good}</div><div class="s">оценка от 5</div></div>
      <div class="card"><div class="k">Отложено как чужие</div><div class="v">${foreign.length}</div><div class="s">скачанные open-source репозитории</div></div>
    </div>
    <div class="panel">
      <div class="toolbar" style="margin-bottom:10px"><h3 style="margin:0">Проекты</h3>
        <button class="btn ${mode==='strong'?'on':''}" data-pf="strong">сильные (${strong})</button>
        <button class="btn ${mode==='good'?'on':''}" data-pf="good">от 5 (${good})</button>
        <button class="btn ${mode==='all'?'on':''}" data-pf="all">все (${all.length})</button>
        <span class="hint">клик по проекту — детали и путь</span></div>
      <table><thead><tr><th>Оценка</th><th>Проект / путь</th><th>Стек</th><th class="num">.py</th><th class="num">Строк</th><th class="num">Активность</th><th>Вердикт</th></tr></thead>
      <tbody>${rows||'<tr><td colspan="7" class="muted">Проекты не найдены</td></tr>'}</tbody></table>
    </div>
    <details class="panel"><summary>Скачанные/чужие репозитории (${foreign.length}) — не включать в портфолио</summary>
      <table style="margin-top:10px"><tbody>${foreign.map(f=>`<tr><td><b>${esc(f.name)}</b><div class="path">${esc(f.path)}</div></td><td>${esc(f.reason)}</td></tr>`).join("")}</tbody></table></details>`;
  $("#pane-portfolio").querySelectorAll("button[data-pf]").forEach(b=>b.onclick=()=>{state.pfMode=b.dataset.pf;renderPortfolio();});
  $("#pane-portfolio").querySelectorAll("tr[data-path]").forEach(tr=>tr.onclick=()=>{
    const p=all.find(x=>x.path===tr.dataset.path); if(!p) return;
    $("#modalbox").innerHTML=`<h3>${esc(p.name)} — ${p.score}/10</h3>
      <div class="copyline"><input readonly value="${esc(p.path)}"><button class="btn primary" id="pfcopy">Копировать путь</button></div>
      <p><b>Вердикт:</b> ${esc(p.verdict)}</p>
      ${p.description?`<p><b>Описание:</b> ${esc(p.description)}</p>`:""}
      <p><b>Стек:</b> ${esc(p.stack.join(", ")||"не определён")}</p>
      <p><b>Код:</b> ${p.files} .py файлов, ${p.loc.toLocaleString("ru")} строк</p>
      <p><b>Последняя активность:</b> ${fmtDate(p.last)}</p>
      ${p.entry.length?`<p><b>Точка входа:</b> ${esc(p.entry.join(", "))}</p>`:""}
      ${p.git?`<p><b>Git:</b> <a href="${esc(p.git)}" target="_blank">${esc(p.git)}</a></p>`:(p.has_git?"<p><b>Git:</b> локальный репозиторий без remote</p>":"")}
      ${p.readme?`<p><b>README:</b> ${esc(p.readme)}</p>`:""}
      ${p.tests?"<p><b>Тесты:</b> есть</p>":""}
      ${p.todo.length?`<p><b>Что добавить:</b> ${esc(p.todo.join(", "))}</p>`:""}
      ${p.images?"<p><b>Скриншоты/ассеты:</b> есть</p>":""}
      <div style="margin-top:14px;text-align:right"><button class="btn" id="pfclose">Закрыть</button></div>`;
    $("#modal").classList.add("on");
    $("#pfcopy").onclick=()=>{copyText(p.path);$("#pfcopy").textContent="Скопировано";};
    $("#pfclose").onclick=()=>$("#modal").classList.remove("on");
  });
}

/* ---------- disk ---------- */
function renderDisk(){
  const nodes=DATA.dirs;
  const byPath=new Map(nodes.map(n=>[n.path,n]));
  const kids=new Map();
  nodes.forEach(n=>{
    if(n.parent && byPath.has(n.parent)){
      if(!kids.has(n.parent)) kids.set(n.parent,[]);
      kids.get(n.parent).push(n);
    }
  });
  kids.forEach(a=>a.sort((x,y)=>y.size-x.size));
  const roots=nodes.filter(n=>!(n.parent && byPath.has(n.parent))).sort((a,b)=>b.size-a.size);
  if(!state.expanded) state.expanded=new Set();
  if(!state.diskInit){ if(roots[0]) state.expanded.add(roots[0].path); state.diskInit=true; }
  const rows=[];
  const walk=(n,level)=>{
    const has=kids.has(n.path), open=state.expanded.has(n.path);
    const parent=byPath.get(n.parent);
    const share=parent&&parent.size? Math.min(100, n.size/parent.size*100) : 100;
    rows.push(`<tr class="r" data-p="${esc(n.path)}" title="${esc(n.path)}">
      <td class="path" style="padding-left:${10+level*16}px">
        <span style="display:inline-block;width:15px;color:${has?'#8ea3c0':'#39424f'}">${has?(open?'▾':'▸'):'·'}</span>${esc(n.path.split("\\").filter(Boolean).pop()||n.path)}
      </td>
      <td class="num">${fmtBytes(n.size)}</td>
      <td class="num muted">${parent?Math.round(share)+'%':''}</td>
      <td class="num">${n.py.toLocaleString("ru")}</td>
      <td class="num">${n.pysize?fmtBytes(n.pysize):'—'}</td>
      <td style="width:22%"><div style="height:7px;background:#0b0e14;border-radius:4px">
        <div style="height:100%;width:${share.toFixed(1)}%;background:#4e8ff7;border-radius:4px"></div></div></td>
    </tr>`);
    if(open) (kids.get(n.path)||[]).forEach(c=>walk(c,level+1));
  };
  roots.forEach(r=>walk(r,0));
  const drives=DATA.drives||[];
  $("#pane-disk").innerHTML=`
    <div class="cards">
      ${drives.map(r=>`<div class="card"><div class="k">${esc(r.path)}</div><div class="v">${fmtBytes(r.size)}</div>
        <div class="s">${r.py.toLocaleString("ru")} python-файлов · ${Number(r.files).toLocaleString("ru")} файлов</div></div>`).join("")}
      <div class="card"><div class="k">Всего просканировано</div><div class="v">${fmtBytes(DATA.scan.bytes)}</div>
        <div class="s">${Number(DATA.scan.files).toLocaleString("ru")} файлов · ${DATA.scan.errors} ошибок доступа</div></div>
    </div>
    <div class="panel">
      <div class="toolbar" style="margin-bottom:10px">
        <h3 style="margin:0">Дерево папок</h3>
        <button class="btn" id="dexpand">развернуть топ</button>
        <button class="btn" id="dcollapse">свернуть</button>
        <span class="hint">клик по строке раскрывает папку; «доля» — от размера родителя</span>
      </div>
      <table><thead><tr><th>Папка</th><th class="num">Размер</th><th class="num">Доля</th><th class="num">.py</th><th class="num">Размер .py</th><th></th></tr></thead>
      <tbody>${rows.join("")}</tbody></table>
    </div>`;
  $("#pane-disk").querySelectorAll("tr[data-p]").forEach(tr=>tr.onclick=()=>{
    const p=tr.dataset.p;
    state.expanded.has(p)?state.expanded.delete(p):state.expanded.add(p);
    renderDisk();
  });
  $("#dexpand").onclick=()=>{ nodes.forEach(n=>{ if(n.depth<=2) state.expanded.add(n.path); }); renderDisk(); };
  $("#dcollapse").onclick=()=>{ state.expanded.clear(); renderDisk(); };
}


/* ---------- cleanup ---------- */
function renderCleanup(){
  const c=DATA.cleanup;
  const groups={venv:[],cache:[]};
  c.items.forEach(i=>groups[i.kind].push(i));
  const sum=a=>a.reduce((x,i)=>x+i.size,0);
  const table=(rows,note)=>rows.length?`<table><thead><tr><th>Путь</th><th class="num">Размер</th><th></th></tr></thead>
    <tbody>${rows.sort((a,b)=>b.size-a.size).slice(0,400).map(i=>`<tr class="r"><td class="path">${esc(i.path)}</td>
    <td class="num">${fmtBytes(i.size)}</td><td><button class="btn" data-copy="${esc(i.path)}">копировать</button></td></tr>`).join("")}</tbody></table>
    <div class="hint" style="margin-top:8px">${note}</div>`:'<div class="empty">Не найдено</div>';
  $("#pane-cleanup").innerHTML=`
    <div class="warnbox">pysort ничего не удаляет сам. Список и безопасный скрипт лежат рядом с отчётом:
      <b>cleanup-plan.txt</b> и <b>cleanup.ps1</b> (в нём стоит <span class="mono">-WhatIf</span> — сначала покажет, что удалил бы).</div>
    <div class="cards">
      <div class="card warn"><div class="k">Потенциал очистки</div><div class="v">${fmtBytes(c.total)}</div>
        <div class="s">с учётом дубликатов</div></div>
      <div class="card blue"><div class="k">Виртуальные окружения</div><div class="v">${fmtBytes(sum(groups.venv))}</div>
        <div class="s">${groups.venv.length} шт. · пересоздаются из requirements</div></div>
      <div class="card pink"><div class="k">Кэши</div><div class="v">${fmtBytes(sum(groups.cache))}</div>
        <div class="s">__pycache__, .pytest_cache и др.</div></div>
      <div class="card"><div class="k">Дубликаты .py</div><div class="v">${fmtBytes(c.dupe_bytes)}</div>
        <div class="s">${c.dupes.length} групп одинаковых файлов</div></div>
    </div>
    <div class="panel"><h3>Виртуальные окружения (${groups.venv.length})</h3>${table(groups.venv,"Удалять только вместе с проектом или если окружение не нужно: зависимости из requirements.txt ставятся заново.")}</div>
    <div class="panel"><h3>Кэши (${groups.cache.length})</h3>${table(groups.cache,"Безопасно удалять: Python создаст их снова при запуске.")}</div>
    <div class="panel"><h3>Дубликаты файлов</h3>${c.dupes.length?`<table><thead><tr><th>Размер</th><th>Копии</th><th></th></tr></thead><tbody>
      ${c.dupes.slice(0,150).map((d,i)=>`<tr class="r"><td class="num">${fmtBytes(d.size)}</td>
      <td class="mono" style="word-break:break-all">${d.paths.map(esc).join("<br>")}</td>
      <td><button class="btn" data-dup="${i}">пути</button></td></tr>`).join("")}</tbody></table>`:'<div class="empty">Дубликатов не найдено</div>'}</div>
    <div class="panel"><h3>Давно не трогал (${c.stale.length})</h3>${c.stale.length?`<table><thead><tr><th>Проект</th><th class="num">.py</th><th class="num">Размер</th><th>Активность</th></tr></thead><tbody>
      ${c.stale.slice(0,100).map(p=>`<tr class="r"><td class="path">${esc(p.path)}</td><td class="num">${p.files}</td>
      <td class="num">${fmtBytes(p.bytes)}</td><td>${fmtAgo(p.last)}</td></tr>`).join("")}</tbody></table>`:'<div class="empty">Нет</div>'}</div>`;

  $("#pane-cleanup").querySelectorAll("button[data-copy]").forEach(b=>b.onclick=()=>{
    copyText(b.dataset.copy); b.textContent="ok";
  });
  $("#pane-cleanup").querySelectorAll("button[data-dup]").forEach(b=>b.onclick=()=>{
    const d=c.dupes[+b.dataset.dup]; copyText(d.paths.join("\n")); b.textContent="ok";
  });
}

/* ---------- boot ---------- */
function renderTab(){
  if(state.tab==="overview") renderOverview();
  else if(state.tab==="projects") renderProjects();
  else if(state.tab==="files") renderFiles();
  else if(state.tab==="portfolio") renderPortfolio();
  else if(state.tab==="disk") renderDisk();
  else renderCleanup();
}
const s=DATA.scan;
$("#scanline").innerHTML=`<b>${(s.roots||"—")}</b> · ${Number(s.files).toLocaleString("ru")} файлов · ${fmtBytes(s.bytes)} · ${s.py_files.toLocaleString("ru")} python-файлов · скан ${Math.round(s.seconds)} с`;
renderTabs(); renderTab();
</script>
</body>
</html>
"""


def write_html(data, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    blob = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = HTML_TEMPLATE.replace("__DATA__", blob)
    path = os.path.join(out_dir, "report.html")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html)
    return path
