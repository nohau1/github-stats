#!/usr/bin/env python3
"""Local web dashboard for github_stats.py (stdlib only)."""
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "github_stats.py")


def open_db(path):
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=15)
    db.row_factory = sqlite3.Row
    return db


def one(db, sql, params=()):
    row = db.execute(sql, params).fetchone()
    return row


def repo_totals(db, repo_id):
    totals = {}
    for kind, count, uniques in db.execute(
        "SELECT kind, count, uniques FROM traffic_totals WHERE repo_id=? ORDER BY run_id DESC",
        (repo_id,),
    ):
        totals.setdefault(kind, {"count": count or 0, "uniques": uniques or 0})
    if "views" not in totals:
        row = one(
            db,
            """SELECT COALESCE(SUM(count),0), COALESCE(SUM(uniques),0) FROM traffic_views
               WHERE repo_id=? AND date >= date('now','-13 day')""",
            (repo_id,),
        )
        totals["views"] = {"count": row[0], "uniques": row[1]}
    if "clones" not in totals:
        row = one(
            db,
            """SELECT COALESCE(SUM(count),0), COALESCE(SUM(uniques),0) FROM traffic_clones
               WHERE repo_id=? AND date >= date('now','-13 day')""",
            (repo_id,),
        )
        totals["clones"] = {"count": row[0], "uniques": row[1]}
    return totals


def traffic_series(db, table, repo_id, days=14):
    rows = db.execute(
        f"SELECT date, count, uniques FROM {table} WHERE repo_id=? ORDER BY date DESC LIMIT ?",
        (repo_id, days),
    ).fetchall()
    return [{"date": r["date"], "count": r["count"] or 0, "uniques": r["uniques"] or 0} for r in reversed(rows)]


def latest_snapshot_sql(where=""):
    return f"""
        SELECT r.id, r.full_name, r.owner, r.name, r.private, r.fork, r.archived,
               r.description, r.homepage, r.license, r.default_branch, r.topics,
               r.created_at, s.ts, s.stars, s.forks, s.watchers, s.open_issues,
               s.open_prs, s.subscribers, s.network, s.size_kb, s.language,
               s.pushed_at, s.updated_at, s.total_downloads
        FROM repos r
        JOIN repo_snapshots s ON s.id = (
            SELECT s2.id FROM repo_snapshots s2
            WHERE s2.repo_id = r.id ORDER BY s2.ts DESC, s2.id DESC LIMIT 1
        )
        {where}
    """


def repo_dict(row, totals):
    issues = row["open_issues"]
    prs = row["open_prs"]
    if issues is not None and prs is not None:
        issues = max(issues - prs, 0)
    return {
        "id": row["id"],
        "full_name": row["full_name"],
        "owner": row["owner"],
        "name": row["name"],
        "private": bool(row["private"]),
        "fork": bool(row["fork"]),
        "archived": bool(row["archived"]),
        "description": row["description"],
        "homepage": row["homepage"],
        "license": row["license"],
        "default_branch": row["default_branch"],
        "topics": json.loads(row["topics"]) if row["topics"] else [],
        "created_at": row["created_at"],
        "ts": row["ts"],
        "stars": row["stars"] or 0,
        "forks": row["forks"] or 0,
        "watchers": row["watchers"] or 0,
        "issues": issues or 0,
        "prs": prs or 0,
        "subscribers": row["subscribers"] or 0,
        "network": row["network"] or 0,
        "size_kb": row["size_kb"] or 0,
        "language": row["language"],
        "pushed_at": row["pushed_at"],
        "updated_at": row["updated_at"],
        "downloads": row["total_downloads"] or 0,
        "views": totals["views"]["count"],
        "visitors": totals["views"]["uniques"],
        "clones": totals["clones"]["count"],
        "cloners": totals["clones"]["uniques"],
    }


def build_summary(db):
    last = one(db, "SELECT * FROM runs ORDER BY id DESC LIMIT 1")
    repos = db.execute(latest_snapshot_sql()).fetchall()
    totals = {"stars": 0, "forks": 0, "issues": 0, "prs": 0, "subscribers": 0,
              "views": 0, "visitors": 0, "clones": 0, "cloners": 0, "downloads": 0}
    for row in repos:
        t = repo_totals(db, row["id"])
        d = repo_dict(row, t)
        for key in totals:
            totals[key] += d.get(key) or 0
    return {
        "account": last["account"] if last else None,
        "last_run": dict(last) if last else None,
        "runs": one(db, "SELECT COUNT(*) FROM runs")[0],
        "repo_count": len(repos),
        "private_count": sum(1 for row in repos if row["private"]),
        "totals": totals,
    }


def build_repos(db):
    out = []
    for row in db.execute(latest_snapshot_sql("ORDER BY r.full_name")):
        t = repo_totals(db, row["id"])
        d = repo_dict(row, t)
        views = traffic_series(db, "traffic_views", row["id"])
        clones = traffic_series(db, "traffic_clones", row["id"])
        d["views_series"] = [p["count"] for p in views]
        d["clones_series"] = [p["count"] for p in clones]
        out.append(d)
    return out


def build_repo(db, repo_id):
    row = one(db, latest_snapshot_sql("WHERE r.id = ?"), (repo_id,))
    if not row:
        return None
    d = repo_dict(row, repo_totals(db, repo_id))

    snapshots = db.execute(
        """SELECT ts, stars, forks, open_issues, open_prs, subscribers, size_kb,
                  language, total_downloads
           FROM repo_snapshots WHERE repo_id=? ORDER BY ts ASC""",
        (repo_id,),
    ).fetchall()
    d["snapshots"] = [
        {
            "ts": s["ts"],
            "stars": s["stars"] or 0,
            "forks": s["forks"] or 0,
            "issues": max((s["open_issues"] or 0) - (s["open_prs"] or 0), 0),
            "subscribers": s["subscribers"] or 0,
            "size_kb": s["size_kb"] or 0,
            "downloads": s["total_downloads"] or 0,
        }
        for s in snapshots
    ]

    d["views_daily"] = traffic_series(db, "traffic_views", repo_id)
    d["clones_daily"] = traffic_series(db, "traffic_clones", repo_id)

    ref_run = one(db, "SELECT MAX(run_id) FROM traffic_referrers WHERE repo_id=?", (repo_id,))[0]
    d["referrers"] = [
        {"referrer": r["referrer"], "count": r["count"] or 0, "uniques": r["uniques"] or 0}
        for r in db.execute(
            """SELECT referrer, count, uniques FROM traffic_referrers
               WHERE repo_id=? AND run_id=? ORDER BY count DESC, uniques DESC""",
            (repo_id, ref_run),
        )
    ] if ref_run else []

    path_run = one(db, "SELECT MAX(run_id) FROM traffic_paths WHERE repo_id=?", (repo_id,))[0]
    d["paths"] = [
        {"path": r["path"], "title": r["title"], "count": r["count"] or 0, "uniques": r["uniques"] or 0}
        for r in db.execute(
            """SELECT path, title, count, uniques FROM traffic_paths
               WHERE repo_id=? AND run_id=? ORDER BY count DESC, uniques DESC LIMIT 25""",
            (repo_id, path_run),
        )
    ] if path_run else []

    lang_run = one(db, "SELECT MAX(run_id) FROM languages WHERE repo_id=?", (repo_id,))[0]
    d["languages"] = [
        {"language": r["language"], "bytes": r["bytes"] or 0}
        for r in db.execute(
            """SELECT language, bytes FROM languages WHERE repo_id=? AND run_id=?
               ORDER BY bytes DESC""",
            (repo_id, lang_run),
        )
    ] if lang_run else []

    d["commits"] = [
        {"week": r["week"], "total": r["total"] or 0}
        for r in reversed(db.execute(
            "SELECT week, total FROM commit_activity WHERE repo_id=? ORDER BY week DESC LIMIT 26",
            (repo_id,),
        ).fetchall())
    ]

    row = one(db, "SELECT count FROM contributors WHERE repo_id=? ORDER BY run_id DESC LIMIT 1", (repo_id,))
    d["contributors"] = row[0] if row else None
    return d


class CollectJob:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.output = ""
        self.code = None
        self.started_at = None
        self.finished_at = None

    def start(self, db_path):
        with self.lock:
            if self.running:
                return False
            self.running = True
            self.output = ""
            self.code = None
            self.started_at = time.time()
            self.finished_at = None
        threading.Thread(target=self._run, args=(db_path,), daemon=True).start()
        return True

    def _run(self, db_path):
        cmd = [sys.executable, SCRIPT, "--db", db_path, "collect"]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
            output = (proc.stdout or "") + (proc.stderr or "")
            code = proc.returncode
        except Exception as err:  # noqa: BLE001
            output = f"collect failed: {err}"
            code = -1
        with self.lock:
            self.output = output[-20000:]
            self.code = code
            self.running = False
            self.finished_at = time.time()

    def status(self):
        with self.lock:
            return {
                "running": self.running,
                "code": self.code,
                "output": self.output,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
            }


JOB = CollectJob()


class Handler(BaseHTTPRequestHandler):
    db_path = None
    server_version = "GhStatsWeb/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("  %s\n" % (fmt % args))

    def _json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _html(self):
        body = HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path in ("/", "/index.html"):
                return self._html()
            if path == "/api/summary":
                db = open_db(self.db_path)
                try:
                    payload = build_summary(db)
                finally:
                    db.close()
                payload["collecting"] = JOB.status()
                return self._json(payload)
            if path == "/api/repos":
                db = open_db(self.db_path)
                try:
                    payload = build_repos(db)
                finally:
                    db.close()
                return self._json(payload)
            if path.startswith("/api/repo/"):
                try:
                    repo_id = int(path.rsplit("/", 1)[1])
                except ValueError:
                    return self._json({"error": "bad repo id"}, 400)
                db = open_db(self.db_path)
                try:
                    payload = build_repo(db, repo_id)
                finally:
                    db.close()
                if payload is None:
                    return self._json({"error": "repo not found"}, 404)
                return self._json(payload)
            if path == "/api/collect":
                return self._json(JOB.status())
            if path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
                return
            return self._json({"error": "not found"}, 404)
        except Exception as err:  # noqa: BLE001
            return self._json({"error": str(err)}, 500)

    def do_POST(self):
        if urlparse(self.path).path == "/api/collect":
            started = JOB.start(self.db_path)
            if not started:
                return self._json({"error": "already running", **JOB.status()}, 409)
            return self._json(JOB.status(), 202)
        return self._json({"error": "not found"}, 404)


def serve(db_path, host="127.0.0.1", port=8765, open_browser=True):
    db_path = os.path.abspath(os.path.expanduser(db_path))
    if not os.path.exists(db_path):
        print(f"database not found: {db_path}", file=sys.stderr)
        print("run: github_stats.py collect", file=sys.stderr)
        return 1

    handler = type("BoundHandler", (Handler,), {"db_path": db_path})
    httpd = ThreadingHTTPServer((host, port), handler)
    url = f"http://{host}:{port}/"
    print(f"GitHub Stats dashboard: {url}")
    print(f"database: {db_path}")
    print("Ctrl+C to stop")

    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        httpd.server_close()
    return 0


HTML = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GitHub Stats</title>
<style>
  :root{
    --bg:#0a0c11; --bg2:#0e1117; --panel:#141924; --panel2:#1a2030;
    --line:#232b3b; --text:#e8ebf2; --muted:#8d97ad; --dim:#5f6a80;
    --accent:#7c5cff; --accent2:#a78bfa; --green:#3ddc97; --blue:#4cb8ff;
    --amber:#ffb454; --red:#ff6b81; --radius:14px;
  }
  *{box-sizing:border-box}
  html,body{margin:0;padding:0}
  body{
    background:
      radial-gradient(1100px 600px at 12% -10%, rgba(124,92,255,.16), transparent 60%),
      radial-gradient(900px 500px at 100% 0%, rgba(76,184,255,.10), transparent 55%),
      var(--bg);
    color:var(--text);
    font:14px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,Inter,Arial,sans-serif;
    min-height:100vh;
  }
  a{color:inherit;text-decoration:none}
  .wrap{width:min(1240px,calc(100% - 40px));margin:0 auto}
  .topbar{
    position:sticky;top:0;z-index:20;
    backdrop-filter:blur(14px);
    background:rgba(10,12,17,.78);
    border-bottom:1px solid var(--line);
  }
  .topbar .wrap{display:flex;align-items:center;gap:18px;height:62px}
  .brand{display:flex;align-items:center;gap:9px;font-weight:650;letter-spacing:.2px;font-size:15px}
  .logo{
    width:26px;height:26px;border-radius:8px;display:grid;place-items:center;
    background:linear-gradient(135deg,var(--accent),#3f2bd6);
    box-shadow:0 6px 18px rgba(124,92,255,.35);font-size:13px;color:#fff;
  }
  .meta{display:flex;gap:14px;color:var(--muted);font-size:12.5px;margin-left:auto;white-space:nowrap}
  .actions{display:flex;gap:8px}
  .btn{
    border:1px solid var(--line);background:var(--panel);color:var(--text);
    padding:8px 14px;border-radius:10px;font:inherit;font-size:13px;cursor:pointer;
    transition:.15s ease;display:inline-flex;align-items:center;gap:7px;white-space:nowrap;
  }
  .btn:hover{background:var(--panel2);border-color:#33405a}
  .btn.primary{background:linear-gradient(135deg,var(--accent),#5b3fe0);border-color:transparent;box-shadow:0 8px 20px rgba(124,92,255,.28)}
  .btn.primary:hover{filter:brightness(1.08)}
  .btn:disabled{opacity:.55;cursor:default}
  .btn .spin{
    width:12px;height:12px;border-radius:50%;border:2px solid rgba(255,255,255,.35);
    border-top-color:#fff;animation:spin .8s linear infinite;display:none
  }
  .btn.busy .spin{display:inline-block}
  @keyframes spin{to{transform:rotate(360deg)}}
  main{padding:30px 0 70px}
  .hero{display:flex;align-items:flex-end;justify-content:space-between;gap:20px;margin-bottom:22px}
  h1{font-size:26px;line-height:1.2;margin:0 0 6px;letter-spacing:-.4px}
  h2{font-size:15px;margin:0;letter-spacing:.2px}
  h3{font-size:13px;color:var(--muted);font-weight:550;margin:0 0 10px;text-transform:uppercase;letter-spacing:.9px}
  .sub{color:var(--muted);font-size:13px}
  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:22px}
  .card{
    background:linear-gradient(180deg,rgba(26,32,48,.9),rgba(20,25,36,.9));
    border:1px solid var(--line);border-radius:var(--radius);padding:14px 16px;
    position:relative;overflow:hidden;
  }
  .card::after{content:"";position:absolute;inset:0;background:linear-gradient(120deg,rgba(124,92,255,.10),transparent 45%);pointer-events:none}
  .card .k{color:var(--muted);font-size:11.5px;text-transform:uppercase;letter-spacing:.8px;margin-bottom:7px}
  .card .v{font-size:24px;font-weight:650;letter-spacing:-.6px;font-variant-numeric:tabular-nums}
  .card .s{color:var(--dim);font-size:11.5px;margin-top:3px}
  .panel{
    background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
    padding:18px 18px 16px;margin-bottom:16px;
  }
  .panel-head{display:flex;align-items:center;justify-content:space-between;gap:14px;margin-bottom:14px}
  .toolbar{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
  input[type=search]{
    background:var(--bg2);border:1px solid var(--line);color:var(--text);
    border-radius:10px;padding:8px 12px;font:inherit;font-size:13px;min-width:240px;outline:none;
  }
  input[type=search]:focus{border-color:#3d4a66;box-shadow:0 0 0 3px rgba(124,92,255,.12)}
  .tablewrap{overflow:auto;border-radius:10px;border:1px solid var(--line)}
  table{width:100%;border-collapse:collapse;font-size:13px;min-width:900px}
  th{
    text-align:left;padding:9px 12px;background:var(--bg2);color:var(--muted);
    font-weight:550;font-size:11.5px;text-transform:uppercase;letter-spacing:.6px;
    border-bottom:1px solid var(--line);cursor:pointer;user-select:none;white-space:nowrap;
  }
  th:hover{color:var(--text)}
  th.sorted{color:var(--accent2)}
  td{padding:10px 12px;border-bottom:1px solid rgba(35,43,59,.65);vertical-align:middle;white-space:nowrap}
  tbody tr{cursor:pointer;transition:background .12s}
  tbody tr:hover{background:rgba(124,92,255,.07)}
  tbody tr:last-child td{border-bottom:0}
  .num{font-variant-numeric:tabular-nums;text-align:right}
  .repo-name{font-weight:560;color:var(--text)}
  .repo-desc{color:var(--dim);font-size:12px;margin-top:2px;max-width:360px;overflow:hidden;text-overflow:ellipsis}
  .badge{display:inline-block;padding:1px 7px;border-radius:20px;font-size:10.5px;font-weight:600;letter-spacing:.3px;vertical-align:1px}
  .badge.private{background:rgba(255,180,84,.14);color:var(--amber);border:1px solid rgba(255,180,84,.25)}
  .badge.archived{background:rgba(141,151,173,.12);color:var(--muted);border:1px solid var(--line)}
  .badge.fork{background:rgba(76,184,255,.12);color:var(--blue);border:1px solid rgba(76,184,255,.22)}
  .spark{display:block;margin-top:2px}
  .muted{color:var(--muted)}
  .dim{color:var(--dim)}
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
  @media (max-width:900px){.grid2{grid-template-columns:1fr}.meta{display:none}}
  .chart svg{width:100%;height:auto;display:block;overflow:visible}
  .chart text{fill:var(--dim);font-size:10px;font-family:inherit}
  .legend{display:flex;gap:16px;margin-top:8px;color:var(--muted);font-size:12px}
  .legend i{width:9px;height:9px;border-radius:50%;display:inline-block;margin-right:6px;vertical-align:0}
  .seg{display:inline-flex;background:var(--bg2);border:1px solid var(--line);border-radius:10px;padding:3px}
  .seg button{
    background:transparent;border:0;color:var(--muted);font:inherit;font-size:12.5px;
    padding:6px 12px;border-radius:7px;cursor:pointer;transition:.15s;
  }
  .seg button.active{background:var(--panel2);color:var(--text);box-shadow:0 1px 0 rgba(255,255,255,.04)}
  .list{display:flex;flex-direction:column;gap:2px}
  .row{display:flex;align-items:center;gap:10px;padding:7px 0;border-bottom:1px solid rgba(35,43,59,.5);font-size:13px}
  .row:last-child{border-bottom:0}
  .row .grow{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .row .n{font-variant-numeric:tabular-nums;color:var(--muted);font-size:12.5px;min-width:64px;text-align:right}
  .bar{height:5px;border-radius:4px;background:var(--panel2);overflow:hidden;margin-top:5px}
  .bar i{display:block;height:100%;border-radius:4px;background:linear-gradient(90deg,var(--accent),var(--accent2))}
  .empty{padding:36px 18px;text-align:center;color:var(--muted)}
  .empty b{color:var(--text);display:block;margin-bottom:6px;font-size:15px}
  .back{display:inline-flex;align-items:center;gap:7px;color:var(--muted);font-size:13px;margin-bottom:14px}
  .back:hover{color:var(--text)}
  .repo-head{display:flex;align-items:flex-start;justify-content:space-between;gap:20px;margin-bottom:20px;flex-wrap:wrap}
  .repo-head h1{display:flex;align-items:center;gap:10px;flex-wrap:wrap;font-size:25px}
  .repo-links{display:flex;gap:14px;color:var(--muted);font-size:12.5px;margin-top:6px;flex-wrap:wrap}
  .repo-links a:hover{color:var(--accent2)}
  .kpis{display:flex;gap:22px;flex-wrap:wrap;margin-top:10px;color:var(--muted);font-size:12.5px}
  .kpis b{color:var(--text);font-weight:600}
  .toast{
    position:fixed;right:20px;bottom:20px;background:var(--panel);border:1px solid var(--line);
    border-radius:12px;padding:12px 16px;max-width:420px;font-size:13px;color:var(--text);
    box-shadow:0 18px 45px rgba(0,0,0,.45);opacity:0;transform:translateY(8px);
    pointer-events:none;transition:.2s;z-index:40;white-space:pre-wrap;
  }
  .toast.show{opacity:1;transform:none}
  .toast.err{border-color:rgba(255,107,129,.4)}
  .foot{color:var(--dim);font-size:12px;margin-top:26px;display:flex;gap:14px;flex-wrap:wrap}
  .loading{padding:70px;text-align:center;color:var(--muted)}
  .pill{background:var(--bg2);border:1px solid var(--line);border-radius:20px;padding:3px 10px;font-size:11.5px;color:var(--muted)}
  .scroll-x{overflow:auto}
</style>
</head>
<body>
<header class="topbar">
  <div class="wrap">
    <a class="brand" href="#/"><span class="logo">◈</span> GitHub Stats</a>
    <div class="meta">
      <span id="account"></span>
      <span id="lastrun"></span>
    </div>
    <div class="actions">
      <button class="btn" id="refreshBtn">Обновить</button>
      <button class="btn primary" id="collectBtn"><span class="spin"></span>Собрать сейчас</button>
    </div>
  </div>
</header>
<main class="wrap" id="app"><div class="loading">Загрузка…</div></main>
<div class="toast" id="toast"></div>
<script>
"use strict";
const app = document.getElementById("app");
const toastEl = document.getElementById("toast");
const accountEl = document.getElementById("account");
const lastrunEl = document.getElementById("lastrun");
const collectBtn = document.getElementById("collectBtn");
const refreshBtn = document.getElementById("refreshBtn");

let COLLECTING = false;
let REPOS = [];
let SORT = { key: "stars", dir: -1 };
let FILTER = "";

const COLORS = {
  accent:"#7c5cff", accent2:"#a78bfa", green:"#3ddc97", blue:"#4cb8ff",
  amber:"#ffb454", red:"#ff6b81", muted:"#8d97ad"
};

function esc(v){
  return String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}
function fmt(v){
  if(v === null || v === undefined) return "—";
  return new Intl.NumberFormat("ru-RU").format(v);
}
function fmtShort(v){
  if(v === null || v === undefined) return "—";
  if(v >= 1000000) return (v/1000000).toFixed(v >= 10000000 ? 0 : 1).replace(".",",") + "M";
  if(v >= 1000) return (v/1000).toFixed(v >= 10000 ? 0 : 1).replace(".",",") + "k";
  return String(v);
}
function fmtTick(v){
  return Number.isInteger(v) ? fmtShort(v) : v.toFixed(1).replace(".", ",");
}
function fmtDate(s, withTime){
  if(!s) return "—";
  const d = new Date(s);
  if(isNaN(d)) return s;
  return d.toLocaleDateString("ru-RU", { day:"2-digit", month:"short", year:"numeric" }) +
    (withTime ? " " + d.toLocaleTimeString("ru-RU", { hour:"2-digit", minute:"2-digit" }) : "");
}
function fmtDay(s){
  const d = new Date(s && s.indexOf("T") >= 0 ? s : s + "T00:00:00Z");
  if(isNaN(d)) return s;
  return d.toLocaleDateString("ru-RU", { day:"2-digit", month:"short" });
}
function fmtStamp(s){
  const d = new Date(s);
  if(isNaN(d)) return s;
  return d.toLocaleDateString("ru-RU", { day:"2-digit", month:"short" }) + " " +
    d.toLocaleTimeString("ru-RU", { hour:"2-digit", minute:"2-digit" });
}
function toast(msg, err){
  toastEl.textContent = msg;
  toastEl.classList.toggle("err", !!err);
  toastEl.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => toastEl.classList.remove("show"), 5200);
}

async function api(path, opts){
  const res = await fetch(path, opts || {});
  const data = await res.json().catch(() => ({}));
  if(!res.ok) throw new Error(data.error || ("HTTP " + res.status));
  return data;
}

function sparkline(values, color){
  if(!values || values.length === 0) return "";
  const W = 86, H = 22, max = Math.max(1, ...values);
  const step = values.length > 1 ? W/(values.length-1) : W;
  const pts = values.map((v,i) => `${(i*step).toFixed(1)},${(H - 2 - (v/max)*(H-5)).toFixed(1)}`).join(" ");
  return `<svg class="spark" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" aria-hidden="true">
    <polyline points="${pts}" fill="none" stroke="${color}" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/></svg>`;
}

function lineChart(labels, series, opts){
  opts = opts || {};
  if(!labels || !labels.length || !series.some(s => s.values.length)){
    return `<div class="empty" style="padding:30px">Нет данных за период</div>`;
  }
  const W = 820, H = opts.height || 250;
  const P = { l: 44, r: 14, t: 14, b: 28 };
  const iw = W - P.l - P.r, ih = H - P.t - P.b;
  const max = Math.max(1, ...series.flatMap(s => s.values));
  const x = i => labels.length <= 1 ? P.l + iw/2 : P.l + i*(iw/(labels.length-1));
  const y = v => P.t + ih - (v/max)*ih;
  const labelText = s => opts.time ? fmtStamp(s) : fmtDay(s);
  let g = "";
  const ticks = Math.max(1, Math.min(4, Math.round(max)));
  for(let k=0; k<=ticks; k++){
    const gy = P.t + ih*k/ticks;
    const val = max*(1 - k/ticks);
    g += `<line x1="${P.l}" y1="${gy}" x2="${W-P.r}" y2="${gy}" stroke="#232b3b" stroke-dasharray="3 4" stroke-width="1"/>`;
    g += `<text x="${P.l-8}" y="${gy+3.5}" text-anchor="end">${fmtTick(val)}</text>`;
  }
  const xt = [];
  let idxs;
  if(opts.time){
    const n = labels.length;
    idxs = n <= 1 ? [0] : [0, Math.floor((n-1)/2), n-1];
  } else {
    idxs = labels.length <= 1 ? [0] : labels.length <= 8 ? labels.map((_,i)=>i) : [0, Math.floor((labels.length-1)/3), Math.floor(2*(labels.length-1)/3), labels.length-1];
  }
  [...new Set(idxs)].forEach(i => {
    xt.push(`<text x="${x(i)}" y="${H-7}" text-anchor="${i===0?"start":i===labels.length-1?"end":"middle"}">${esc(labelText(labels[i]))}</text>`);
  });
  let paths = "";
  series.forEach(s => {
    const pts = s.values.map((v,i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`);
    const area = `M ${x(0)},${P.t+ih} L ${pts.join(" L ")} L ${x(s.values.length-1)},${P.t+ih} Z`;
    paths += `<path d="${area}" fill="${s.color}" opacity="0.07"/>`;
    paths += `<polyline points="${pts.join(" ")}" fill="none" stroke="${s.color}" stroke-width="2.2" stroke-linejoin="round" stroke-linecap="round"/>`;
    s.values.forEach((v,i) => {
      paths += `<circle cx="${x(i).toFixed(1)}" cy="${y(v).toFixed(1)}" r="2.6" fill="${s.color}"><title>${esc(labels[i])}: ${fmt(v)}</title></circle>`;
    });
  });
  const legend = series.map(s => `<span><i style="background:${s.color}"></i>${esc(s.name)}</span>`).join("");
  return `<svg viewBox="0 0 ${W} ${H}" role="img">${g}${xt.join("")}${paths}</svg><div class="legend">${legend}</div>`;
}

function barChart(labels, values, color, opts){
  opts = opts || {};
  const W = 820, H = opts.height || 210;
  const P = { l: 38, r: 10, t: 12, b: 30 };
  const iw = W - P.l - P.r, ih = H - P.t - P.b;
  const max = Math.max(1, ...values);
  const step = iw / Math.max(values.length, 1);
  const bw = Math.max(4, step*0.62);
  let g = "";
  for(let k=0; k<=3; k++){
    const gy = P.t + ih*k/3;
    g += `<line x1="${P.l}" y1="${gy}" x2="${W-P.r}" y2="${gy}" stroke="#232b3b" stroke-dasharray="3 4"/>`;
    g += `<text x="${P.l-7}" y="${gy+3.5}" text-anchor="end">${fmtTick(max*(1-k/3))}</text>`;
  }
  let bars = "";
  const every = Math.ceil(labels.length/8);
  values.forEach((v,i) => {
    const h = (v/max)*ih;
    const bx = P.l + step*i + (step-bw)/2;
    bars += `<rect x="${bx.toFixed(1)}" y="${(P.t+ih-h).toFixed(1)}" width="${bw.toFixed(1)}" height="${Math.max(h,0).toFixed(1)}" rx="3" fill="${color}" opacity="0.85"><title>${esc(labels[i])}: ${fmt(v)}</title></rect>`;
    if(i % every === 0){
      bars += `<text x="${(bx+bw/2).toFixed(1)}" y="${H-8}" text-anchor="middle">${esc(fmtDay(labels[i]))}</text>`;
    }
  });
  return `<svg viewBox="0 0 ${W} ${H}" role="img">${g}${bars}</svg>`;
}

function card(k, v, s){
  return `<div class="card"><div class="k">${esc(k)}</div><div class="v">${v}</div>${s ? `<div class="s">${esc(s)}</div>` : ""}</div>`;
}

function setHeader(summary){
  const run = summary.last_run;
  accountEl.innerHTML = summary.account ? `Аккаунт: <b style="color:var(--text)">${esc(summary.account)}</b>` : "";
  lastrunEl.innerHTML = run
    ? `Обновлено: ${esc(fmtDate(run.finished_at || run.started_at, true))} <span class="dim">· run #${run.id}</span>`
    : "";
}

async function loadOverview(){
  app.innerHTML = `<div class="loading">Загрузка…</div>`;
  const [summary, repos] = await Promise.all([api("/api/summary"), api("/api/repos")]);
  setHeader(summary);
  REPOS = repos;
  COLLECTING = !!(summary.collecting && summary.collecting.running);
  updateCollectButton();

  if(!repos.length){
    app.innerHTML = `<div class="empty"><b>Данных пока нет</b>Запустите сбор: <code>./github_stats.py collect</code> или кнопку «Собрать сейчас».</div>`;
    return;
  }

  const t = summary.totals;
  app.innerHTML = `
    <div class="hero">
      <div>
        <h1>Обзор репозиториев</h1>
        <div class="sub">${summary.repo_count} репозиториев${summary.private_count ? `, из них ${summary.private_count} приватных` : ""} · ${summary.runs} сборов</div>
      </div>
      <div class="toolbar">
        <input type="search" id="filter" placeholder="Поиск по названию или описанию…" value="${esc(FILTER)}">
      </div>
    </div>
    <div class="cards">
      ${card("Репозиториев", fmt(summary.repo_count), summary.private_count ? summary.private_count + " приватных" : "все публичные")}
      ${card("Звёзды", fmt(t.stars))}
      ${card("Просмотры · 14д", fmt(t.views), fmt(t.visitors) + " уникальных")}
      ${card("Клоны · 14д", fmt(t.clones), fmt(t.cloners) + " уникальных")}
      ${card("Issues", fmt(t.issues), fmt(t.prs) + " открытых PR")}
      ${card("Загрузки релизов", fmt(t.downloads))}
    </div>
    <div class="panel">
      <div class="panel-head">
        <h2>Репозитории</h2>
        <div class="muted" id="tableCount"></div>
      </div>
      <div class="tablewrap"><table id="repoTable"></table></div>
    </div>
    <div class="foot">
      <span>Клик по строке — детали и трафик</span>
      <span>Данные: ${esc(fmtDate((summary.last_run || {}).finished_at || (summary.last_run || {}).started_at, true))}</span>
    </div>`;

  document.getElementById("filter").addEventListener("input", e => {
    FILTER = e.target.value;
    renderRepoTable();
  });
  renderRepoTable();
}

function sortRepos(list){
  const key = SORT.key, dir = SORT.dir;
  return list.slice().sort((a,b) => {
    let av = a[key], bv = b[key];
    if(typeof av === "string" || typeof bv === "string"){
      av = String(av ?? "").toLowerCase();
      bv = String(bv ?? "").toLowerCase();
      return av < bv ? -dir : av > bv ? dir : 0;
    }
    return ((av || 0) - (bv || 0)) * dir;
  });
}

function renderRepoTable(){
  const q = FILTER.trim().toLowerCase();
  const rows = REPOS.filter(r => !q || r.full_name.toLowerCase().includes(q) || (r.description || "").toLowerCase().includes(q));
  const sorted = sortRepos(rows);
  const cols = [
    ["full_name","Репозиторий"], ["stars","★"], ["forks","Форки"], ["issues","Issues"], ["prs","PR"],
    ["views","Просмотры"], ["visitors","Уники"], ["clones","Клоны"], ["cloners","Уники"],
    ["downloads","Загрузки"], ["language","Язык"], ["pushed_at","Push"]
  ];
  const head = cols.map(([k,label]) => {
    const cls = SORT.key === k ? "sorted" : "";
    return `<th data-key="${k}" class="${cls}">${label}${SORT.key === k ? (SORT.dir < 0 ? " ↓" : " ↑") : ""}</th>`;
  }).join("");
  const body = sorted.map(r => {
    const badges = (r.private ? `<span class="badge private">private</span>` : "") +
                   (r.archived ? `<span class="badge archived">archived</span>` : "");
    return `<tr data-id="${r.id}">
      <td><div class="repo-name">${esc(r.full_name)} ${badges}</div>${r.description ? `<div class="repo-desc">${esc(r.description)}</div>` : ""}</td>
      <td class="num">${fmt(r.stars)}</td>
      <td class="num">${fmt(r.forks)}</td>
      <td class="num">${fmt(r.issues)}</td>
      <td class="num">${fmt(r.prs)}</td>
      <td class="num">${fmt(r.views)}${sparkline(r.views_series, COLORS.accent)}</td>
      <td class="num">${fmt(r.visitors)}</td>
      <td class="num">${fmt(r.clones)}${sparkline(r.clones_series, COLORS.green)}</td>
      <td class="num">${fmt(r.cloners)}</td>
      <td class="num">${fmt(r.downloads)}</td>
      <td>${esc(r.language || "—")}</td>
      <td class="dim">${esc(fmtDate(r.pushed_at))}</td>
    </tr>`;
  }).join("");
  const table = document.getElementById("repoTable");
  table.innerHTML = `<thead><tr>${head}</tr></thead><tbody>${body || `<tr><td colspan="${cols.length}" class="empty">Ничего не найдено</td></tr>`}</tbody>`;
  document.getElementById("tableCount").textContent = `${rows.length} из ${REPOS.length}`;
  table.querySelectorAll("th").forEach(th => th.addEventListener("click", () => {
    const k = th.dataset.key;
    if(SORT.key === k) SORT.dir *= -1;
    else { SORT.key = k; SORT.dir = (k === "full_name" || k === "language" || k === "pushed_at") ? 1 : -1; }
    renderRepoTable();
  }));
  table.querySelectorAll("tbody tr[data-id]").forEach(tr => {
    tr.addEventListener("click", () => { location.hash = "#/repo/" + tr.dataset.id; });
  });
}

async function loadRepo(id){
  app.innerHTML = `<div class="loading">Загрузка…</div>`;
  const [d, summary] = await Promise.all([api("/api/repo/" + id), api("/api/summary")]);
  setHeader(summary);
  const badges = (d.private ? `<span class="badge private">private</span>` : "") +
                 (d.archived ? `<span class="badge archived">archived</span>` : "") +
                 (d.fork ? `<span class="badge fork">fork</span>` : "");
  const ghUrl = "https://github.com/" + d.full_name;
  const topicHtml = (d.topics || []).map(t => `<span class="pill">${esc(t)}</span>`).join(" ");
  const lastViews = d.views_daily.reduce((s,p) => s + p.count, 0);
  const lastVisitors = d.views; // period unique from API
  const lastClones = d.clones_daily.reduce((s,p) => s + p.count, 0);

  app.innerHTML = `
    <a class="back" href="#/">← Все репозитории</a>
    <div class="repo-head">
      <div>
        <h1>${esc(d.full_name)} ${badges}</h1>
        ${d.description ? `<div class="sub">${esc(d.description)}</div>` : ""}
        <div class="repo-links">
          <a href="${ghUrl}" target="_blank" rel="noopener">Открыть на GitHub ↗</a>
          ${d.homepage ? `<a href="${esc(d.homepage)}" target="_blank" rel="noopener">${esc(d.homepage)}</a>` : ""}
          <span>${esc(d.language || "—")}</span>
          ${d.license ? `<span>${esc(d.license)}</span>` : ""}
          <span>создан ${esc(fmtDate(d.created_at))}</span>
          ${d.contributors !== null && d.contributors !== undefined ? `<span>${fmt(d.contributors)} контрибьюторов</span>` : ""}
        </div>
        ${topicHtml ? `<div class="repo-links">${topicHtml}</div>` : ""}
      </div>
    </div>

    <div class="cards">
      ${card("Звёзды", fmt(d.stars))}
      ${card("Форки", fmt(d.forks))}
      ${card("Issues", fmt(d.issues), fmt(d.prs) + " PR")}
      ${card("Просмотры · 14д", fmt(d.views), fmt(d.visitors) + " уникальных")}
      ${card("Клоны · 14д", fmt(d.clones), fmt(d.cloners) + " уникальных")}
      ${card("Загрузки", fmt(d.downloads))}
    </div>

    <div class="panel">
      <div class="panel-head">
        <h2>Трафик за 14 дней</h2>
        <div class="seg" id="trafficToggle">
          <button data-m="views" class="active">Просмотры</button>
          <button data-m="clones">Клоны</button>
        </div>
      </div>
      <div class="chart" id="trafficChart"></div>
    </div>

    <div class="grid2">
      <div class="panel">
        <div class="panel-head"><h2>История звёзд и форков</h2></div>
        <div class="chart" id="historyChart"></div>
      </div>
      <div class="panel">
        <div class="panel-head"><h2>Коммиты по неделям</h2></div>
        <div class="chart" id="commitChart"></div>
      </div>
    </div>

    <div class="grid2">
      <div class="panel">
        <div class="panel-head"><h2>Referring sites</h2><span class="muted">за 14 дней</span></div>
        <div id="referrers"></div>
      </div>
      <div class="panel">
        <div class="panel-head"><h2>Popular content</h2><span class="muted">за 14 дней</span></div>
        <div id="paths"></div>
      </div>
    </div>

    <div class="panel">
      <div class="panel-head"><h2>Языки</h2></div>
      <div id="languages"></div>
    </div>

    <div class="foot">
      <span>Снимок: ${esc(fmtDate(d.ts, true))}</span>
      <span>Push: ${esc(fmtDate(d.pushed_at))}</span>
      <span>Размер: ${fmt(d.size_kb)} KB</span>
    </div>`;

  let mode = "views";
  const drawTraffic = () => {
    const daily = mode === "views" ? d.views_daily : d.clones_daily;
    const labels = daily.map(p => p.date);
    const counts = daily.map(p => p.count);
    const uniques = daily.map(p => p.uniques);
    const color = mode === "views" ? COLORS.accent : COLORS.green;
    document.getElementById("trafficChart").innerHTML = lineChart(labels, [
      { name: mode === "views" ? "Просмотры" : "Клоны", values: counts, color },
      { name: mode === "views" ? "Уникальные посетители" : "Уникальные клонеры", values: uniques, color: COLORS.blue }
    ], { height: 260 });
  };
  drawTraffic();
  document.querySelectorAll("#trafficToggle button").forEach(btn => btn.addEventListener("click", () => {
    mode = btn.dataset.m;
    document.querySelectorAll("#trafficToggle button").forEach(b => b.classList.toggle("active", b === btn));
    drawTraffic();
  }));

  const snaps = d.snapshots || [];
  if(snaps.length){
    document.getElementById("historyChart").innerHTML = lineChart(
      snaps.map(s => s.ts),
      [
        { name:"Звёзды", values: snaps.map(s => s.stars), color: COLORS.amber },
        { name:"Форки", values: snaps.map(s => s.forks), color: COLORS.blue }
      ],
      { height: 220, time: true }
    );
  } else {
    document.getElementById("historyChart").innerHTML = `<div class="empty">Нет истории</div>`;
  }

  const commits = d.commits || [];
  if(commits.length){
    document.getElementById("commitChart").innerHTML = barChart(
      commits.map(c => c.week), commits.map(c => c.total), COLORS.accent2, { height: 220 }
    );
  } else {
    document.getElementById("commitChart").innerHTML = `<div class="empty">Нет данных по коммитам</div>`;
  }

  const listHtml = (items, render) => items.length
    ? `<div class="list">${items.map(render).join("")}</div>`
    : `<div class="empty" style="padding:24px">Нет данных</div>`;

  document.getElementById("referrers").innerHTML = listHtml(d.referrers, r => `
    <div class="row">
      <span class="grow">${esc(r.referrer)}</span>
      <span class="n">${fmt(r.count)} / ${fmt(r.uniques)}</span>
    </div>`);

  document.getElementById("paths").innerHTML = listHtml(d.paths, p => `
    <div class="row">
      <span class="grow" title="${esc(p.path)}">${esc(p.title || p.path)}<div class="dim" style="font-size:11px;overflow:hidden;text-overflow:ellipsis">${esc(p.path)}</div></span>
      <span class="n">${fmt(p.count)} / ${fmt(p.uniques)}</span>
    </div>`);

  const langs = d.languages || [];
  if(langs.length){
    const total = langs.reduce((s,l) => s + l.bytes, 0) || 1;
    document.getElementById("languages").innerHTML = `<div class="list">${langs.map(l => `
      <div class="row" style="display:block">
        <div style="display:flex;justify-content:space-between;gap:12px">
          <span>${esc(l.language)}</span>
          <span class="n">${(l.bytes/total*100).toFixed(1)}%</span>
        </div>
        <div class="bar"><i style="width:${(l.bytes/total*100).toFixed(2)}%"></i></div>
      </div>`).join("")}</div>`;
  } else {
    document.getElementById("languages").innerHTML = `<div class="empty" style="padding:24px">Нет данных</div>`;
  }
}

function updateCollectButton(){
  collectBtn.classList.toggle("busy", COLLECTING);
  collectBtn.disabled = COLLECTING;
  collectBtn.innerHTML = `<span class="spin"></span>${COLLECTING ? "Собираю…" : "Собрать сейчас"}`;
}

async function pollCollect(){
  try{
    const s = await api("/api/collect");
    if(s.running){
      COLLECTING = true;
      updateCollectButton();
      setTimeout(pollCollect, 1500);
      return;
    }
    if(COLLECTING){
      COLLECTING = false;
      updateCollectButton();
      const ok = s.code === 0;
      toast(ok ? "Сбор завершён" : "Сбор завершился с ошибкой (код " + s.code + ")", !ok);
      if(ok) await route(true);
    }
  }catch(err){
    COLLECTING = false;
    updateCollectButton();
    toast(err.message, true);
  }
}

collectBtn.addEventListener("click", async () => {
  if(COLLECTING) return;
  try{
    await api("/api/collect", { method: "POST" });
    COLLECTING = true;
    updateCollectButton();
    toast("Запустил сбор. Это может занять пару минут.");
    pollCollect();
  }catch(err){
    toast(err.message, true);
  }
});

refreshBtn.addEventListener("click", () => route(true));

async function route(force){
  const m = location.hash.match(/^#\\/repo\\/(\\d+)/);
  try{
    if(m){
      await loadRepo(m[1]);
    } else {
      await loadOverview();
    }
  }catch(err){
    app.innerHTML = `<div class="empty"><b>Ошибка</b>${esc(err.message)}</div>`;
  }
}

window.addEventListener("hashchange", () => route());
route();

// resume progress if a collection was already running when the page opened
api("/api/collect").then(s => {
  if(s.running){ COLLECTING = true; updateCollectButton(); pollCollect(); }
}).catch(() => {});
</script>
</body>
</html>
"""
