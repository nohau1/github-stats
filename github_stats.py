#!/usr/bin/env python3
import argparse
import getpass
import json
import os
import re
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API = "https://api.github.com"
UA = "github-stats/1.0"

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  account TEXT,
  repos INTEGER DEFAULT 0,
  errors INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS repos(
  id INTEGER PRIMARY KEY,
  owner TEXT NOT NULL,
  name TEXT NOT NULL,
  full_name TEXT NOT NULL UNIQUE,
  private INTEGER NOT NULL DEFAULT 0,
  fork INTEGER NOT NULL DEFAULT 0,
  archived INTEGER NOT NULL DEFAULT 0,
  description TEXT,
  homepage TEXT,
  license TEXT,
  default_branch TEXT,
  topics TEXT,
  created_at TEXT,
  first_seen TEXT
);
CREATE TABLE IF NOT EXISTS repo_snapshots(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL,
  repo_id INTEGER NOT NULL,
  ts TEXT NOT NULL,
  stars INTEGER,
  forks INTEGER,
  watchers INTEGER,
  open_issues INTEGER,
  open_prs INTEGER,
  subscribers INTEGER,
  network INTEGER,
  size_kb INTEGER,
  language TEXT,
  pushed_at TEXT,
  updated_at TEXT,
  total_downloads INTEGER
);
CREATE INDEX IF NOT EXISTS idx_snap_repo_ts ON repo_snapshots(repo_id, ts);
CREATE TABLE IF NOT EXISTS traffic_views(
  repo_id INTEGER NOT NULL,
  date TEXT NOT NULL,
  count INTEGER NOT NULL DEFAULT 0,
  uniques INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(repo_id, date)
);
CREATE TABLE IF NOT EXISTS traffic_clones(
  repo_id INTEGER NOT NULL,
  date TEXT NOT NULL,
  count INTEGER NOT NULL DEFAULT 0,
  uniques INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(repo_id, date)
);
CREATE TABLE IF NOT EXISTS traffic_referrers(
  repo_id INTEGER NOT NULL,
  run_id INTEGER NOT NULL,
  referrer TEXT NOT NULL,
  count INTEGER NOT NULL DEFAULT 0,
  uniques INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(repo_id, run_id, referrer)
);
CREATE TABLE IF NOT EXISTS traffic_paths(
  repo_id INTEGER NOT NULL,
  run_id INTEGER NOT NULL,
  path TEXT NOT NULL,
  title TEXT,
  count INTEGER NOT NULL DEFAULT 0,
  uniques INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(repo_id, run_id, path)
);
CREATE TABLE IF NOT EXISTS traffic_totals(
  repo_id INTEGER NOT NULL,
  run_id INTEGER NOT NULL,
  kind TEXT NOT NULL,
  count INTEGER NOT NULL DEFAULT 0,
  uniques INTEGER NOT NULL DEFAULT 0,
  window_days INTEGER NOT NULL DEFAULT 14,
  PRIMARY KEY(run_id, repo_id, kind)
);
CREATE TABLE IF NOT EXISTS releases(
  id INTEGER PRIMARY KEY,
  repo_id INTEGER NOT NULL,
  tag TEXT,
  name TEXT,
  published_at TEXT,
  prerelease INTEGER DEFAULT 0,
  draft INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS release_downloads(
  run_id INTEGER NOT NULL,
  release_id INTEGER NOT NULL,
  downloads INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(run_id, release_id)
);
CREATE TABLE IF NOT EXISTS commit_activity(
  repo_id INTEGER NOT NULL,
  week TEXT NOT NULL,
  total INTEGER NOT NULL DEFAULT 0,
  days TEXT,
  PRIMARY KEY(repo_id, week)
);
CREATE TABLE IF NOT EXISTS languages(
  repo_id INTEGER NOT NULL,
  run_id INTEGER NOT NULL,
  language TEXT NOT NULL,
  bytes INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(repo_id, run_id, language)
);
CREATE TABLE IF NOT EXISTS contributors(
  repo_id INTEGER NOT NULL,
  run_id INTEGER NOT NULL,
  count INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(repo_id, run_id)
);
"""


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def data_home():
    return os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")


def config_home():
    return os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")


def default_db_path():
    return os.path.join(data_home(), "github-stats", "stats.db")


def default_token_path():
    return os.path.join(config_home(), "github-stats", "token")


def resolve_token(args):
    if getattr(args, "token", None):
        return args.token.strip()
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        value = os.environ.get(var)
        if value and value.strip():
            return value.strip()
    path = getattr(args, "token_file", None) or default_token_path()
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            value = fh.read().strip()
        if value:
            return value
    return None


def connect(path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    return db


class GitHub:
    def __init__(self, token=None):
        self.token = token
        self.calls = 0

    def _headers(self):
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": UA,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def get(self, path, params=None, retries=2, retry_404=0, soft_404=False):
        url = API + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        for attempt in range(retries + 1):
            req = urllib.request.Request(url, headers=self._headers())
            try:
                self.calls += 1
                with urllib.request.urlopen(req, timeout=30) as resp:
                    body = resp.read()
                    data = json.loads(body) if body else None
                    return resp.status, data, dict(resp.headers)
            except urllib.error.HTTPError as err:
                status = err.code
                headers = dict(err.headers)
                if status == 202:
                    return 202, None, headers
                if status == 404 and retry_404 > 0 and attempt < retries:
                    retry_404 -= 1
                    time.sleep(3)
                    continue
                if status == 404 and soft_404:
                    return 404, None, headers
                if status in (403, 429) and attempt < retries:
                    retry_after = headers.get("Retry-After")
                    if retry_after:
                        time.sleep(min(int(retry_after), 30))
                        continue
                    if headers.get("X-RateLimit-Remaining") == "0" or status == 429:
                        reset = headers.get("X-RateLimit-Reset")
                        wait = max(1, min(int(reset) - int(time.time()) + 1, 60)) if reset else 5
                        time.sleep(wait)
                        continue
                detail = ""
                try:
                    detail = err.read().decode("utf-8", "replace")[:300]
                except Exception:
                    pass
                raise RuntimeError(f"GET {path} -> {status}: {detail}") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as err:
                if attempt < retries:
                    time.sleep(2)
                    continue
                raise RuntimeError(f"GET {path} failed: {err}") from None
        raise RuntimeError(f"GET {path} failed")

    def paginate(self, path, params=None, limit=50):
        params = dict(params or {})
        params.setdefault("per_page", 100)
        page = 1
        fetched = 0
        while page <= limit:
            params["page"] = page
            status, data, headers = self.get(path, params)
            if status != 200 or not data:
                break
            for item in data:
                yield item
            if 'rel="next"' not in headers.get("Link", ""):
                break
            page += 1

    def count_last_page(self, path, params=None):
        params = dict(params or {})
        params["per_page"] = 1
        status, data, headers = self.get(path, params)
        if status in (204, 404):
            return 0
        if status != 200:
            return None
        match = re.search(r'[?&]page=(\d+)>; rel="last"', headers.get("Link", ""))
        if match:
            return int(match.group(1))
        return len(data or [])


def sum_release_assets(release):
    total = 0
    for asset in release.get("assets", []) or []:
        total += asset.get("download_count", 0) or 0
    return total


def collect_repo(gh, db, run_id, repo, warnings):
    full = repo["full_name"]
    detail = repo
    try:
        status, data, _ = gh.get(f"/repos/{full}")
        if status == 200 and data:
            detail = data
    except RuntimeError as err:
        warnings.append(f"{full}: repo detail: {err}")

    repo_id = detail.get("id") or repo.get("id")
    owner, name = full.split("/", 1)
    db.execute(
        """
        INSERT INTO repos(id, owner, name, full_name, private, fork, archived, description,
                          homepage, license, default_branch, topics, created_at, first_seen)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
          full_name=excluded.full_name,
          private=excluded.private,
          fork=excluded.fork,
          archived=excluded.archived,
          description=excluded.description,
          homepage=excluded.homepage,
          license=excluded.license,
          default_branch=excluded.default_branch,
          topics=excluded.topics
        """,
        (
            repo_id,
            owner,
            name,
            full,
            1 if detail.get("private") else 0,
            1 if detail.get("fork") else 0,
            1 if detail.get("archived") else 0,
            detail.get("description"),
            detail.get("homepage"),
            (detail.get("license") or {}).get("spdx_id") if detail.get("license") else None,
            detail.get("default_branch"),
            json.dumps(detail.get("topics") or [], ensure_ascii=False),
            detail.get("created_at"),
            now_iso(),
        ),
    )

    open_prs = None
    try:
        open_prs = gh.count_last_page(f"/repos/{full}/pulls", {"state": "open"})
    except RuntimeError as err:
        warnings.append(f"{full}: pulls: {err}")

    total_downloads = 0
    try:
        for release in gh.paginate(f"/repos/{full}/releases"):
            rid = release.get("id")
            if rid is None:
                continue
            downloads = sum_release_assets(release)
            total_downloads += downloads
            db.execute(
                """
                INSERT INTO releases(id, repo_id, tag, name, published_at, prerelease, draft)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                  tag=excluded.tag, name=excluded.name, published_at=excluded.published_at,
                  prerelease=excluded.prerelease, draft=excluded.draft
                """,
                (
                    rid,
                    repo_id,
                    release.get("tag_name"),
                    release.get("name"),
                    release.get("published_at") or release.get("created_at"),
                    1 if release.get("prerelease") else 0,
                    1 if release.get("draft") else 0,
                ),
            )
            db.execute(
                "INSERT OR REPLACE INTO release_downloads(run_id, release_id, downloads) VALUES (?,?,?)",
                (run_id, rid, downloads),
            )
    except RuntimeError as err:
        warnings.append(f"{full}: releases: {err}")

    try:
        contributors = gh.count_last_page(f"/repos/{full}/contributors", {"anon": "false"})
        if contributors is not None:
            db.execute(
                "INSERT OR REPLACE INTO contributors(repo_id, run_id, count) VALUES (?,?,?)",
                (repo_id, run_id, contributors),
            )
    except RuntimeError as err:
        warnings.append(f"{full}: contributors: {err}")

    try:
        status, langs, _ = gh.get(f"/repos/{full}/languages")
        if status == 200 and langs:
            for lang, count in langs.items():
                db.execute(
                    "INSERT OR REPLACE INTO languages(repo_id, run_id, language, bytes) VALUES (?,?,?,?)",
                    (repo_id, run_id, lang, int(count or 0)),
                )
    except RuntimeError as err:
        warnings.append(f"{full}: languages: {err}")

    try:
        status, activity, _ = gh.get(f"/repos/{full}/stats/commit_activity")
        tries = 0
        while status == 202 and tries < 3:
            time.sleep(5)
            tries += 1
            status, activity, _ = gh.get(f"/repos/{full}/stats/commit_activity")
        if status == 200 and isinstance(activity, list):
            for week in activity:
                stamp = week.get("week")
                if stamp is None:
                    continue
                day = datetime.fromtimestamp(stamp, tz=timezone.utc).strftime("%Y-%m-%d")
                db.execute(
                    "INSERT OR REPLACE INTO commit_activity(repo_id, week, total, days) VALUES (?,?,?,?)",
                    (repo_id, day, int(week.get("total") or 0), json.dumps(week.get("days") or [])),
                )
    except RuntimeError as err:
        warnings.append(f"{full}: commit activity: {err}")

    if gh.token:
        try:
            status, views, _ = gh.get(f"/repos/{full}/traffic/views", {"per": "day"}, retry_404=1, soft_404=True)
            if status == 200:
                views = views or {}
                db.execute(
                    """INSERT OR REPLACE INTO traffic_totals(repo_id, run_id, kind, count, uniques, window_days)
                       VALUES (?,?,?,?,?,14)""",
                    (repo_id, run_id, "views", views.get("count", 0), views.get("uniques", 0)),
                )
                for item in views.get("views") or []:
                    day = (item.get("timestamp") or "")[:10]
                    if day:
                        db.execute(
                            """INSERT INTO traffic_views(repo_id, date, count, uniques) VALUES (?,?,?,?)
                               ON CONFLICT(repo_id, date) DO UPDATE SET count=excluded.count, uniques=excluded.uniques""",
                            (repo_id, day, item.get("count", 0), item.get("uniques", 0)),
                        )
        except RuntimeError as err:
            warnings.append(f"{full}: traffic/views: {err}")
        try:
            status, clones, _ = gh.get(f"/repos/{full}/traffic/clones", {"per": "day"}, retry_404=1, soft_404=True)
            if status == 200:
                clones = clones or {}
                db.execute(
                    """INSERT OR REPLACE INTO traffic_totals(repo_id, run_id, kind, count, uniques, window_days)
                       VALUES (?,?,?,?,?,14)""",
                    (repo_id, run_id, "clones", clones.get("count", 0), clones.get("uniques", 0)),
                )
                for item in clones.get("clones") or []:
                    day = (item.get("timestamp") or "")[:10]
                    if day:
                        db.execute(
                            """INSERT INTO traffic_clones(repo_id, date, count, uniques) VALUES (?,?,?,?)
                               ON CONFLICT(repo_id, date) DO UPDATE SET count=excluded.count, uniques=excluded.uniques""",
                            (repo_id, day, item.get("count", 0), item.get("uniques", 0)),
                        )
        except RuntimeError as err:
            warnings.append(f"{full}: traffic/clones: {err}")
        try:
            status, referrers, _ = gh.get(f"/repos/{full}/traffic/popular/referrers", retry_404=1, soft_404=True)
            if status == 200 and referrers:
                for item in referrers:
                    db.execute(
                        """INSERT OR REPLACE INTO traffic_referrers(repo_id, run_id, referrer, count, uniques)
                           VALUES (?,?,?,?,?)""",
                        (repo_id, run_id, item.get("referrer", ""), item.get("count", 0), item.get("uniques", 0)),
                    )
        except RuntimeError as err:
            warnings.append(f"{full}: traffic/referrers: {err}")
        try:
            status, paths, _ = gh.get(f"/repos/{full}/traffic/popular/paths", retry_404=1, soft_404=True)
            if status == 200 and paths:
                for item in paths:
                    db.execute(
                        """INSERT OR REPLACE INTO traffic_paths(repo_id, run_id, path, title, count, uniques)
                           VALUES (?,?,?,?,?,?)""",
                        (
                            repo_id,
                            run_id,
                            item.get("path", ""),
                            item.get("title"),
                            item.get("count", 0),
                            item.get("uniques", 0),
                        ),
                    )
        except RuntimeError as err:
            warnings.append(f"{full}: traffic/paths: {err}")

    db.execute(
        """
        INSERT INTO repo_snapshots(run_id, repo_id, ts, stars, forks, watchers, open_issues, open_prs,
                                   subscribers, network, size_kb, language, pushed_at, updated_at, total_downloads)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            run_id,
            repo_id,
            now_iso(),
            detail.get("stargazers_count"),
            detail.get("forks_count"),
            detail.get("watchers_count"),
            detail.get("open_issues_count"),
            open_prs,
            detail.get("subscribers_count"),
            detail.get("network_count"),
            detail.get("size"),
            detail.get("language"),
            detail.get("pushed_at"),
            detail.get("updated_at"),
            total_downloads,
        ),
    )


def cmd_collect(args):
    token = resolve_token(args)
    if not token:
        print("warning: no token found, collecting public data only (traffic unavailable)", file=sys.stderr)
    gh = GitHub(token)
    db = connect(args.db)

    account = None
    if token:
        try:
            _, user, _ = gh.get("/user")
            account = (user or {}).get("login")
        except RuntimeError as err:
            print(f"warning: /user failed: {err}", file=sys.stderr)

    if not account and not args.user:
        print("error: pass --user or configure a token so the account can be detected", file=sys.stderr)
        return 1

    started = now_iso()
    run_id = db.execute(
        "INSERT INTO runs(started_at, account) VALUES (?, ?)", (started, account)
    ).lastrowid

    if token:
        repos = list(
            gh.paginate(
                "/user/repos",
                {"affiliation": "owner,collaborator,organization_member", "sort": "updated"},
            )
        )
    else:
        repos = list(gh.paginate(f"/users/{args.user}/repos", {"sort": "updated"}))

    if args.repo:
        wanted = {value.lower() for value in args.repo}
        repos = [
            repo
            for repo in repos
            if repo["full_name"].lower() in wanted or repo["name"].lower() in wanted
        ]

    warnings = []
    errors = 0
    done = 0
    for repo in repos:
        try:
            collect_repo(gh, db, run_id, repo, warnings)
            done += 1
            print(f"  {repo['full_name']}", file=sys.stderr)
        except Exception as err:
            errors += 1
            print(f"  {repo.get('full_name', '?')}: {err}", file=sys.stderr)
        db.commit()

    db.execute(
        "UPDATE runs SET finished_at=?, repos=?, errors=? WHERE id=?",
        (now_iso(), done, errors, run_id),
    )
    db.commit()

    for line in warnings:
        print(f"warning: {line}", file=sys.stderr)
    print(
        f"run #{run_id}: {done} repos collected, {errors} errors, {gh.calls} API calls -> {args.db}",
        file=sys.stderr,
    )
    return 0 if errors == 0 else 2


def fmt_delta(value):
    if value is None:
        return ""
    if value > 0:
        return f" +{value}"
    if value < 0:
        return f" {value}"
    return " ="


def fmt_int(value):
    if value is None:
        return "-"
    return str(value)


SPARK_BLOCKS = "▁▂▃▄▅▆▇█"


def sparkline(values, width=24):
    vals = list(values)[-width:]
    if not vals:
        return ""
    lo, hi = min(vals), max(vals)
    if hi == lo:
        return SPARK_BLOCKS[0] * len(vals)
    return "".join(
        SPARK_BLOCKS[round((value - lo) / (hi - lo) * (len(SPARK_BLOCKS) - 1))] for value in vals
    )


def fmt_number(value):
    if value is None:
        return "-"
    return f"{value:,}".replace(",", " ")


def render_hbars(title, rows):
    if not rows:
        print("no data")
        return
    term = shutil.get_terminal_size((100, 24)).columns
    name_width = min(max(len(name) for name, _ in rows), 45)
    bar_width = max(20, term - name_width - 12)
    top = max((value for _, value in rows), default=0) or 1
    print(title)
    for name, value in rows:
        label = name if len(name) <= name_width else name[: name_width - 1] + "…"
        length = max(1, round((value or 0) / top * bar_width)) if value else 0
        print(f"  {label:<{name_width}}  {'█' * length} {fmt_number(value)}")


def render_vbars(title, points, height=12, agg="sum"):
    points = [(label, value or 0) for label, value in points]
    if not points:
        print("no data")
        return
    term = shutil.get_terminal_size((100, 24)).columns
    available = max(10, term - 10)
    if len(points) > available:
        step = len(points) / available
        merged = []
        for bucket in range(available):
            lo = int(bucket * step)
            hi = max(lo + 1, int((bucket + 1) * step))
            chunk = points[lo:hi]
            if not chunk:
                continue
            value = sum(item[1] for item in chunk) if agg == "sum" else chunk[-1][1]
            merged.append((chunk[-1][0], value))
        points = merged

    top = max((value for _, value in points), default=0) or 1
    scale = top / height
    decimals = 0 if scale >= 1 else min(3, len(str(int(round(1 / scale)))))
    label_width = len(f"{top:.{decimals}f}")
    grid = [[" "] * len(points) for _ in range(height)]
    for column, (_, value) in enumerate(points):
        if value <= 0:
            continue
        cells = min(height, max(1, round(value / scale)))
        for row in range(cells):
            grid[height - 1 - row][column] = "█"

    print(title)
    for index, row in enumerate(grid):
        value = top - index * scale
        label = f"{value:.{decimals}f}"
        print(f"  {label:>{label_width}} │" + "".join(row))
    print(" " * (label_width + 3) + "└" + "─" * len(points))

    if len(points) < 5:
        return
    label_line = [" "] * len(points)
    occupied = [False] * len(points)
    count = min(6, len(points))
    positions = sorted({round(i * (len(points) - 1) / (count - 1)) for i in range(count)})

    def shorten(pos):
        text = points[pos][0]
        if re.match(r"^\d{4}-\d{2}-\d{2}$", text):
            return text[8:10] if len(points) < 15 else text[5:10]
        return text[-5:] if len(text) > 5 else text

    def place(pos):
        text = shorten(pos)
        if len(text) <= 2:
            start = min(pos, len(points) - len(text))
        else:
            start = max(0, min(pos - len(text) // 2, len(points) - len(text)))
        if any(occupied[start : start + len(text)]):
            return
        for offset, char in enumerate(text):
            occupied[start + offset] = True
            label_line[start + offset] = char

    order = []
    for pos in [len(points) - 1, 0] + positions:
        if pos not in order:
            order.append(pos)
    for pos in order:
        place(pos)
    print(" " * (label_width + 3) + "".join(label_line).rstrip())


SNAPSHOT_METRICS = {
    "stars": "stars",
    "forks": "forks",
    "watchers": "watchers",
    "issues": "open_issues",
    "prs": "open_prs",
    "subs": "subscribers",
    "network": "network",
    "size": "size_kb",
    "downloads": "total_downloads",
}

TRAFFIC_METRICS = {
    "views": ("traffic_views", "count"),
    "visitors": ("traffic_views", "uniques"),
    "clones": ("traffic_clones", "count"),
    "cloners": ("traffic_clones", "uniques"),
}


def resolve_repo(db, pattern):
    like = f"%{pattern}%"
    repos = db.execute(
        "SELECT id, full_name FROM repos WHERE full_name LIKE ? OR name LIKE ? ORDER BY full_name",
        (like, like),
    ).fetchall()
    if not repos:
        print(f"no repos matching {pattern!r}")
        return None
    if len(repos) > 1:
        print("matching repos:")
        for _, full in repos:
            print(f"  {full}")
        print("specify more precisely")
        return None
    return repos[0]


def cmd_report(args):
    db = connect(args.db)
    last_run = db.execute("SELECT id, finished_at, started_at FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    if not last_run:
        print("no runs yet, execute: github_stats.py collect")
        return 1

    rows = []
    for rid, full_name, private, archived in db.execute(
        "SELECT id, full_name, private, archived FROM repos ORDER BY full_name"
    ):
        snaps = db.execute(
            """SELECT ts, stars, forks, open_issues, open_prs, subscribers, size_kb, language,
                      pushed_at, total_downloads, network
               FROM repo_snapshots WHERE repo_id=? ORDER BY ts DESC LIMIT 2""",
            (rid,),
        ).fetchall()
        if not snaps:
            continue
        cur = snaps[0]
        prev = snaps[1] if len(snaps) > 1 else None
        views = db.execute(
            """SELECT COALESCE(SUM(count),0), COALESCE(SUM(uniques),0) FROM traffic_views
               WHERE repo_id=? AND date >= date('now','-13 day')""",
            (rid,),
        ).fetchone()
        clones = db.execute(
            """SELECT COALESCE(SUM(count),0), COALESCE(SUM(uniques),0) FROM traffic_clones
               WHERE repo_id=? AND date >= date('now','-13 day')""",
            (rid,),
        ).fetchone()
        totals = {}
        for kind, count, uniques in db.execute(
            """SELECT kind, count, uniques FROM traffic_totals
               WHERE repo_id=? ORDER BY run_id DESC""",
            (rid,),
        ):
            totals.setdefault(kind, (count, uniques))
        if "views" in totals:
            views = totals["views"]
        if "clones" in totals:
            clones = totals["clones"]
        rows.append(
            {
                "repo": full_name + (" (private)" if private else "") + (" (archived)" if archived else ""),
                "stars": cur[1],
                "d_stars": None if not prev else (cur[1] or 0) - (prev[1] or 0),
                "forks": cur[2],
                "d_forks": None if not prev else (cur[2] or 0) - (prev[2] or 0),
                "issues": None if cur[3] is None else max((cur[3] or 0) - (cur[4] or 0), 0) if cur[4] is not None else cur[3],
                "prs": cur[4],
                "subs": cur[5],
                "lang": cur[7] or "-",
                "views": views[0],
                "views_u": views[1],
                "clones": clones[0],
                "clones_u": clones[1],
                "dl": cur[9] or 0,
                "pushed": (cur[8] or "")[:10],
            }
        )

    keys = {
        "name": lambda row: row["repo"].lower(),
        "stars": lambda row: row["stars"] or 0,
        "forks": lambda row: row["forks"] or 0,
        "issues": lambda row: row["issues"] or 0,
        "views": lambda row: row["views"] or 0,
        "visitors": lambda row: row["views_u"] or 0,
        "clones": lambda row: row["clones"] or 0,
        "cloners": lambda row: row["clones_u"] or 0,
        "downloads": lambda row: row["dl"] or 0,
        "pushed": lambda row: row["pushed"],
    }
    if args.sort not in keys:
        print(f"error: unknown sort key {args.sort!r}, use one of: {', '.join(keys)}", file=sys.stderr)
        return 1
    rows.sort(key=keys[args.sort], reverse=args.sort not in ("name", "pushed"))
    if args.limit:
        rows = rows[: args.limit]

    header = ["REPO", "STARS", "FORKS", "ISSUES", "PRS", "SUBS", "VIEWS14", "CLONES14", "DL", "LANG", "PUSHED"]
    table = []
    for row in rows:
        table.append(
            [
                row["repo"],
                f"{fmt_int(row['stars'])}{fmt_delta(row['d_stars'])}",
                f"{fmt_int(row['forks'])}{fmt_delta(row['d_forks'])}",
                fmt_int(row["issues"]),
                fmt_int(row["prs"]),
                fmt_int(row["subs"]),
                f"{row['views']} ({row['views_u']})",
                f"{row['clones']} ({row['clones_u']})",
                fmt_int(row["dl"]),
                row["lang"],
                row["pushed"],
            ]
        )

    widths = [max(len(header[i]), *(len(str(row[i])) for row in table)) for i in range(len(header))] if table else [len(h) for h in header]
    print("run #{} at {}  ({} repos)".format(last_run[0], last_run[1] or last_run[2], len(rows)))
    print("  ".join(header[i].ljust(widths[i]) for i in range(len(header))))
    print("  ".join("-" * widths[i] for i in range(len(header))))
    for row in table:
        print("  ".join(str(row[i]).ljust(widths[i]) for i in range(len(header))))
    print()
    print("delta vs previous run; VIEWS14/CLONES14 = last 14 days (unique visitors/cloners in parentheses); DL = release asset downloads")
    return 0


def cmd_history(args):
    db = connect(args.db)
    found = resolve_repo(db, args.repo)
    if not found:
        return 1
    repo_id, full_name = found
    rows = db.execute(
        """SELECT ts, stars, forks, open_issues, open_prs, subscribers, size_kb, language,
                  pushed_at, total_downloads FROM repo_snapshots
           WHERE repo_id=? ORDER BY ts ASC""",
        (repo_id,),
    ).fetchall()
    header = ["DATE", "STARS", "TREND", "FORKS", "ISSUES", "PRS", "SUBS", "SIZE_KB", "DL", "LANG", "PUSHED"]
    stars = [row[1] or 0 for row in rows]
    table = []
    for index, row in enumerate(rows):
        table.append([row[0][:19], fmt_int(row[1]), sparkline(stars[: index + 1]), fmt_int(row[2]), fmt_int(row[3]),
                      fmt_int(row[4]), fmt_int(row[5]), fmt_int(row[6]), fmt_int(row[9]), row[7] or "-", (row[8] or "")[:10]])
    widths = [max(len(header[i]), *(len(str(r[i])) for r in table)) for i in range(len(header))]
    print(full_name)
    print("  ".join(header[i].ljust(widths[i]) for i in range(len(header))))
    print("  ".join("-" * widths[i] for i in range(len(header))))
    for row in table:
        print("  ".join(str(row[i]).ljust(widths[i]) for i in range(len(header))))

    traffic = db.execute(
        """SELECT d.date, v.count, v.uniques, c.count, c.uniques
           FROM (SELECT DISTINCT date FROM traffic_views WHERE repo_id=?
                 UNION SELECT DISTINCT date FROM traffic_clones WHERE repo_id=?) d
           LEFT JOIN traffic_views v ON v.repo_id=? AND v.date=d.date
           LEFT JOIN traffic_clones c ON c.repo_id=? AND c.date=d.date
           ORDER BY d.date DESC LIMIT ?""",
        (repo_id, repo_id, repo_id, repo_id, args.days),
    ).fetchall()
    if traffic:
        print()
        print("traffic (day, views, unique, clones, unique)")
        for row in traffic:
            print(f"  {row[0]}  {row[1] or 0:>6}  {row[2] or 0:>6}  {row[3] or 0:>6}  {row[4] or 0:>6}")

    referrers = db.execute(
        """SELECT referrer, count, uniques FROM traffic_referrers
           WHERE repo_id=? AND run_id=(SELECT MAX(run_id) FROM traffic_referrers WHERE repo_id=?)
           ORDER BY count DESC LIMIT 10""",
        (repo_id, repo_id),
    ).fetchall()
    if referrers:
        print()
        print("top referrers (last snapshot)")
        for ref, count, uniques in referrers:
            print(f"  {count:>6}  {uniques:>6}  {ref}")

    weeks = db.execute(
        "SELECT week, total FROM commit_activity WHERE repo_id=? ORDER BY week DESC LIMIT 12",
        (repo_id,),
    ).fetchall()
    if weeks:
        print()
        print("commits per week (latest 12)")
        for week, total in weeks:
            print(f"  {week}  {total:>4}")
    return 0


def latest_traffic_totals(db, repo_id):
    totals = {}
    for kind, count, uniques, run_id in db.execute(
        "SELECT kind, count, uniques, run_id FROM traffic_totals WHERE repo_id=? ORDER BY run_id DESC",
        (repo_id,),
    ):
        totals.setdefault(kind, (count, uniques, run_id))
    return totals


def cmd_traffic(args):
    db = connect(args.db)
    found = resolve_repo(db, args.repo)
    if not found:
        return 1
    repo_id, full_name = found
    totals = latest_traffic_totals(db, repo_id)

    rows = db.execute(
        """SELECT d.date, v.count, v.uniques, c.count, c.uniques
           FROM (SELECT DISTINCT date FROM traffic_views WHERE repo_id=?
                 UNION SELECT DISTINCT date FROM traffic_clones WHERE repo_id=?) d
           LEFT JOIN traffic_views v ON v.repo_id=? AND v.date=d.date
           LEFT JOIN traffic_clones c ON c.repo_id=? AND c.date=d.date
           ORDER BY d.date DESC LIMIT ?""",
        (repo_id, repo_id, repo_id, repo_id, args.days),
    ).fetchall()
    rows.reverse()

    print(full_name)
    period = f"last {args.days} days"
    if rows:
        period += f" ({rows[0][0]} .. {rows[-1][0]})"
    if totals:
        run_id = next(iter(totals.values()))[2]
        finished = db.execute("SELECT COALESCE(finished_at, started_at) FROM runs WHERE id=?", (run_id,)).fetchone()
        if finished:
            period += f", snapshot {finished[0]}"
    print(f"traffic — {period}")
    print()

    labels = {
        "clones": ("CLONES", "Clones", "Unique cloners"),
        "views": ("VIEWS", "Views", "Unique visitors"),
    }
    for kind in ("clones", "views"):
        if kind not in totals:
            continue
        count, uniques, _ = totals[kind]
        title, total_label, unique_label = labels[kind]
        print(title)
        print(f"  {total_label:<18}{count:>8}")
        print(f"  {unique_label:<18}{uniques:>8}")
        print()

    if not totals:
        print("no period totals stored yet — run collect with a token")
        views_sum = sum(row[1] or 0 for row in rows)
        clones_sum = sum(row[3] or 0 for row in rows)
        print(f"  daily sum fallback: {views_sum} views, {clones_sum} clones")
        print()

    if rows:
        header = ["DATE", "VIEWS", "UNIQUE", "CLONES", "UNIQUE"]
        table = [[row[0], row[1] or 0, row[2] or 0, row[3] or 0, row[4] or 0] for row in rows]
        widths = [max(len(header[i]), *(len(str(r[i])) for r in table)) for i in range(len(header))]
        print("DAILY")
        print("  " + "  ".join(header[i].ljust(widths[i]) for i in range(len(header))))
        print("  " + "  ".join("-" * widths[i] for i in range(len(header))))
        for row in table:
            print("  " + "  ".join(str(row[i]).ljust(widths[i]) for i in range(len(header))))
        print()

    ref_run = db.execute(
        "SELECT MAX(run_id) FROM traffic_referrers WHERE repo_id=?", (repo_id,)
    ).fetchone()[0]
    if ref_run:
        refs = db.execute(
            """SELECT referrer, count, uniques FROM traffic_referrers
               WHERE repo_id=? AND run_id=? ORDER BY count DESC, uniques DESC""",
            (repo_id, ref_run),
        ).fetchall()
        if refs:
            print("REFERRING SITES")
            width = max(len(ref) for ref, _, _ in refs)
            print(f"  {'VIEWS':>6}  {'UNIQUE':>6}  {'SITE':<{width}}")
            for ref, count, uniques in refs:
                print(f"  {count:>6}  {uniques:>6}  {ref:<{width}}")
            print()

    path_run = db.execute(
        "SELECT MAX(run_id) FROM traffic_paths WHERE repo_id=?", (repo_id,)
    ).fetchone()[0]
    if path_run:
        paths = db.execute(
            """SELECT path, title, count, uniques FROM traffic_paths
               WHERE repo_id=? AND run_id=? ORDER BY count DESC, uniques DESC""",
            (repo_id, path_run),
        ).fetchall()
        if paths:
            print("POPULAR CONTENT")
            labels = [f"{title}  {path}" if title else path for path, title, _, _ in paths]
            width = max(len(label) for label in labels)
            print(f"  {'VIEWS':>6}  {'UNIQUE':>6}  {'CONTENT':<{width}}")
            for label, (_, _, count, uniques) in zip(labels, paths):
                print(f"  {count:>6}  {uniques:>6}  {label:<{width}}")
            print()

    return 0


def cmd_chart(args):
    db = connect(args.db)
    found = resolve_repo(db, args.repo)
    if not found:
        return 1
    repo_id, full_name = found

    metric = args.metric
    points = []
    agg = "sum"
    window = f"-{max(args.days - 1, 0)} day"

    if metric in TRAFFIC_METRICS:
        table, column = TRAFFIC_METRICS[metric]
        query = f"SELECT date, {column} FROM {table} WHERE repo_id=?"
        params = [repo_id]
        if args.days:
            query += " ORDER BY date DESC LIMIT ?"
            params.append(args.days)
        else:
            query += " ORDER BY date"
        points = [(row[0], row[1] or 0) for row in db.execute(query, params)]
        if args.days:
            points.reverse()
    elif metric == "commits":
        query = "SELECT week, total FROM commit_activity WHERE repo_id=?"
        params = [repo_id]
        if args.days:
            query += " AND week >= date('now', ?)"
            params.append(window)
        query += " ORDER BY week"
        points = [(row[0], row[1] or 0) for row in db.execute(query, params)]
    elif metric in SNAPSHOT_METRICS:
        query = f"SELECT ts, {SNAPSHOT_METRICS[metric]} FROM repo_snapshots WHERE repo_id=?"
        params = [repo_id]
        if args.days:
            query += " AND ts >= date('now', ?)"
            params.append(window)
        query += " ORDER BY ts"
        points = [(row[0][:10], row[1] or 0) for row in db.execute(query, params)]
        agg = "last"
    else:
        print(f"error: unknown metric {metric!r}", file=sys.stderr)
        print(metric_help(), file=sys.stderr)
        return 1

    if not points:
        print(f"no data for {metric!r} in {full_name}, run: github_stats.py collect")
        return 1

    render_vbars(f"{metric.upper()}  {full_name}", points, agg=agg)
    values = [value for _, value in points]
    print()
    print(f"  period: {points[0][0]} .. {points[-1][0]}   points: {len(points)}")
    print(
        f"  last: {fmt_number(values[-1])}   change: {fmt_number(values[-1] - values[0])}"
        f"   min: {fmt_number(min(values))}   max: {fmt_number(max(values))}"
        f"   avg: {fmt_number(round(sum(values) / len(values)))}"
    )
    return 0


def cmd_top(args):
    db = connect(args.db)
    metric = args.metric

    if metric in ("visitors", "cloners"):
        kind = "views" if metric == "visitors" else "clones"
        data = db.execute(
            """SELECT r.full_name, t.uniques FROM repos r
               JOIN traffic_totals t ON t.repo_id=r.id AND t.kind=?
               WHERE t.run_id=(SELECT MAX(t2.run_id) FROM traffic_totals t2
                               WHERE t2.repo_id=r.id AND t2.kind=?)
               ORDER BY t.uniques DESC""",
            (kind, kind),
        ).fetchall()
        title = f"{metric.upper()} (last 14 days, unique)"
    elif metric in TRAFFIC_METRICS:
        table, column = TRAFFIC_METRICS[metric]
        days = args.days or 14
        data = db.execute(
            f"""SELECT r.full_name, COALESCE(SUM(t.{column}), 0) FROM repos r
                LEFT JOIN {table} t ON t.repo_id=r.id AND t.date >= date('now', ?)
                GROUP BY r.id ORDER BY 2 DESC""",
            (f"-{max(days - 1, 0)} day",),
        ).fetchall()
        title = f"{metric.upper()} (last {days} days)"
    elif metric == "commits":
        data = db.execute(
            """SELECT r.full_name, COALESCE(SUM(c.total), 0) FROM repos r
               LEFT JOIN commit_activity c ON c.repo_id=r.id
               GROUP BY r.id ORDER BY 2 DESC"""
        ).fetchall()
        title = "COMMITS (total tracked)"
    elif metric in SNAPSHOT_METRICS:
        column = SNAPSHOT_METRICS[metric]
        data = db.execute(
            f"""SELECT r.full_name, s.{column} FROM repos r
                JOIN repo_snapshots s ON s.repo_id=r.id
                WHERE s.ts=(SELECT MAX(s2.ts) FROM repo_snapshots s2 WHERE s2.repo_id=r.id)
                ORDER BY s.{column} DESC"""
        ).fetchall()
        title = f"{metric.upper()} (current)"
    else:
        print(f"error: unknown metric {metric!r}", file=sys.stderr)
        print(metric_help(), file=sys.stderr)
        return 1

    rows = [(name, value or 0) for name, value in data if value]
    if args.limit:
        rows = rows[: args.limit]
    if not rows:
        print("no data yet, run: github_stats.py collect")
        return 1
    render_hbars(title, rows)
    return 0


def metric_help():
    metrics = ["commits"] + sorted(set(SNAPSHOT_METRICS) | set(TRAFFIC_METRICS))
    return "available metrics: " + ", ".join(metrics)


def cmd_token(args):
    path = args.token_file or default_token_path()
    token = getpass.getpass("GitHub token: ").strip()
    if not token:
        print("empty token, nothing saved", file=sys.stderr)
        return 1
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(token + "\n")
    os.chmod(path, 0o600)
    print(f"token saved to {path}")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="github_stats.py",
        description="Collect and store GitHub repository statistics locally (SQLite).",
    )
    parser.add_argument("--db", default=default_db_path(), help=f"SQLite database path (default: {default_db_path()})")
    parser.add_argument("--token", help="GitHub token (default: GITHUB_TOKEN / GH_TOKEN env or token file)")
    parser.add_argument("--token-file", help=f"token file path (default: {default_token_path()})")
    sub = parser.add_subparsers(dest="command", required=True)

    collect = sub.add_parser("collect", help="fetch a new snapshot of all repositories")
    collect.add_argument("--user", help="GitHub username (used only without a token)")
    collect.add_argument("--repo", action="append", help="limit to specific repo (name or owner/name), repeatable")
    collect.set_defaults(func=cmd_collect)

    report = sub.add_parser("report", help="print the latest snapshot with deltas")
    report.add_argument("--sort", default="stars", help="name | stars | forks | issues | views | clones | downloads | pushed")
    report.add_argument("--limit", type=int, default=0, help="show only first N rows")
    report.set_defaults(func=cmd_report)

    history = sub.add_parser("history", help="show history for one repository as a table")
    history.add_argument("repo", help="repo name or owner/name (substring match)")
    history.add_argument("--days", type=int, default=30, help="traffic days to show, default 30")
    history.set_defaults(func=cmd_history)

    traffic = sub.add_parser("traffic", help="show all GitHub traffic metrics for one repository")
    traffic.add_argument("repo", help="repo name or owner/name (substring match)")
    traffic.add_argument("--days", type=int, default=14, help="traffic days to show, default 14")
    traffic.set_defaults(func=cmd_traffic)

    chart = sub.add_parser("chart", help="plot a metric history for one repository as a terminal chart")
    chart.add_argument("repo", help="repo name or owner/name (substring match)")
    chart.add_argument("--metric", default="stars", help=metric_help())
    chart.add_argument("--days", type=int, default=0, help="limit to last N days, 0 = all history")
    chart.set_defaults(func=cmd_chart)

    top = sub.add_parser("top", help="bar chart comparing all repositories")
    top.add_argument("--metric", default="stars", help=metric_help())
    top.add_argument("--days", type=int, default=0, help="window for views/clones, default 14 days")
    top.add_argument("--limit", type=int, default=0, help="show only first N rows")
    top.set_defaults(func=cmd_top)

    token = sub.add_parser("token", help="save a GitHub token to the local token file")
    token.set_defaults(func=cmd_token)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except RuntimeError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
