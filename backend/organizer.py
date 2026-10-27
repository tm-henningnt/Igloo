#!/usr/bin/env python3
"""Local inventory and guarded cleanup backend. No third-party dependencies."""
from __future__ import annotations
import argparse
import concurrent.futures
import datetime as dt
import fcntl
import json
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
if sys.version_info < (3, 11):
    print(json.dumps({"error": "Igloo needs Python 3.11 or later. Choose it in Settings, or install the standard Igloo download with Python included."}))
    raise SystemExit(1)
import tempfile
import uuid

import preferences
import classification

APP = Path(__file__).resolve().parent
if APP.name == "backend" and (APP.parent / "build.command").is_file():
    APP = APP.parent
CONFIG = preferences.load()
ROOT = Path(CONFIG["projectsRoot"]).resolve()
STATE = Path(CONFIG["stateRoot"]).expanduser()
VAULT = Path(CONFIG["archiveRoot"]).expanduser()
GIT = CONFIG["gitExecutable"]
GITHUB = CONFIG["githubExecutable"]
CACHE_KINDS = {"node_modules", ".venv", "venv", ".turbo", "__pycache__", ".pytest_cache",
               ".mypy_cache", ".ruff_cache", ".next", ".nuxt", ".svelte-kit", ".parcel-cache"}
REVIEW_KINDS = {".build", "build", "dist", "target", ".worker", "artifacts", "coverage",
                "test-results", "playwright-report", ".scratch", ".tmp", "tmp", "temp",
                ".cache", ".playwright-cli", "_site", ".orchestration", ".vercel"}
SKIP_DISCOVERY = CACHE_KINDS | REVIEW_KINDS | {".git"}
PRIVATE_REF = re.compile(r"(^|[/_.-])(private|archive|backup|pre-squash|pre-publication|before-publication)([/_.-]|$)", re.I)
ENV = dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0",
           GCM_INTERACTIVE="Never", GIT_SSH_COMMAND="ssh -o BatchMode=yes -o ConnectTimeout=8")


class Refusal(Exception):
    pass


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def load(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(value, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def progress(message):
    save(STATE / "progress.json", {"message": message, "at": now()})


def run_git(path, *args, timeout=30, input_text=None):
    try:
        r = subprocess.run([GIT, "-c", "core.fsmonitor=false", "-C", str(path), *args],
                           capture_output=True, text=True, input=input_text, env=ENV, timeout=timeout)
        return r.returncode, r.stdout
    except subprocess.TimeoutExpired:
        return 124, ""


def safe_url(value):
    return re.sub(r"(?<=://)[^/@]+@", "[redacted]@", value).split("?")[0].split("#")[0]


def under(path, parent):
    return path == parent or parent in path.parents


def checked_path(value, root=ROOT, allow_missing=False):
    """Reject traversal and every symlink component, including links inside Projects."""
    p = Path(value)
    if not p.is_absolute() or ".." in p.parts:
        raise Refusal("Use an absolute path inside Projects.")
    if not under(p, root) or p == root or under(p, APP) or under(APP, p) or under(p, VAULT):
        raise Refusal("This path is outside the allowed project area.")
    current = root
    for component in p.relative_to(root).parts:
        current = current / component
        if current.is_symlink():
            raise Refusal("Symbolic links cannot be cleaned or restored by this tool.")
    if not allow_missing and not p.exists():
        raise Refusal("The directory no longer exists. Run a new analysis.")
    return p


def common_dir(path):
    rc, out = run_git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if rc:
        raise Refusal("Git metadata is unavailable.")
    return out.strip()


def worktree_list(path):
    rc, out = run_git(path, "worktree", "list", "--porcelain")
    if rc:
        raise Refusal("Cannot read the worktree registrations.")
    rows = []
    for block in out.strip().split("\n\n"):
        row = {}
        for line in block.splitlines():
            key, _, value = line.partition(" ")
            row[key] = value or True
        if row:
            rows.append(row)
    return rows


def process_cwds():
    """Cheap cwd-only activity observation; full open-file checks belong to Freeze reviews."""
    try:
        r = subprocess.run(["/usr/sbin/lsof", "-nP", "-a", "-d", "cwd", "-Fpcn"],
                           capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        raise Refusal("Activity check failed. Close project sessions and try again.")
    if r.returncode not in (0, 1) or not r.stdout:
        raise Refusal("Activity information is unavailable.")
    results, row = [], {}
    for line in r.stdout.splitlines():
        if line.startswith("p"):
            row = {"pid": line[1:]}
        elif line.startswith("c"):
            row["command"] = line[1:]
        elif line.startswith("n") and line[1:].startswith("/"):
            if row.get("pid") != str(os.getpid()):
                results.append(dict(row, path=line[1:]))
    return results


def activity(path, cwds):
    trees = [Path(w["worktree"]) for w in worktree_list(path) if isinstance(w.get("worktree"), str)]
    return [p for p in cwds if any(under(Path(p["path"]), t) for t in trees)]


def parse_changes(value):
    entries, fields, i = [], value.split("\0"), 0
    while i < len(fields):
        line = fields[i]
        i += 1
        if not line:
            continue
        entries.append({"status": line[:2], "path": line[3:]})
        if "R" in line[:2] or "C" in line[:2]:
            i += 1
    return entries


def config_name(path):
    n = path.name.lower()
    return (n.startswith(".env") or n in {".npmrc", ".pypirc", ".netrc", "tenant.json",
            "credentials.json", "providers.json", ".cline-profiles.json", "id_rsa", "id_ed25519"}
            or n.endswith((".pem", ".key", ".p12", ".pfx", ".sqlite", ".sqlite3", ".db", ".qvf", ".qvd"))
            or any(t in path.parts for t in ("secrets", ".auth")))


def is_template(path):
    return any(t in path.name.lower() for t in ("example", "sample", "template"))


def physical_size(path):
    path = Path(path)
    if path.is_symlink():
        return 0
    total = 0
    for base, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = [d for d in dirs if not (Path(base) / d).is_symlink()]
        for name in files:
            try:
                total += (Path(base) / name).lstat().st_blocks * 512
            except OSError:
                pass
    return total


def outcome_summary(checkouts=(), estimate=0, freed=None, archives=(), refs=(), trash=0, held=(), observed=False,
                    archive_count=None, used_estimate=0):
    """One JSON shape for projected and observed maintenance outcomes."""
    paths = list(dict.fromkeys(map(str, checkouts)))
    archives = list(dict.fromkeys(map(str, archives)))
    count = len(archives) if archive_count is None else archive_count
    return {"checkoutsRemoved": {"count": len(paths), "paths": paths},
            "bytesFreedEstimate": estimate, "bytesUsedEstimate": used_estimate, "bytesFreed": freed,
            "archivesWritten": {"count": count, "location": str(VAULT),
                                "paths": None if count and not observed else archives},
            "refsPushed": list(refs), "movedToTrash": trash, "held": list(held), "observed": observed}


def combine_outcomes(outcomes, held=(), observed=False):
    values = [v for v in outcomes if v]
    return outcome_summary(
        checkouts=[p for v in values for p in v["checkoutsRemoved"]["paths"]],
        estimate=sum(v["bytesFreedEstimate"] for v in values),
        used_estimate=sum(v.get("bytesUsedEstimate", 0) for v in values),
        freed=sum(v.get("bytesFreed") or 0 for v in values) if observed else None,
        archives=[p for v in values for p in (v["archivesWritten"]["paths"] or [])],
        archive_count=sum(v["archivesWritten"]["count"] for v in values),
        refs=[r for v in values for r in v["refsPushed"]],
        trash=sum(v["movedToTrash"] for v in values), held=held, observed=observed)


def observe_files(root=ROOT):
    """One traversal; dependency contents are measured, never inspected for secrets."""
    sizes, repos, candidates, local, errors, modified = {}, [], [], [], [], {}
    source_dirs = []
    def visit(p, suppressed=False, config_suppressed=False, age_suppressed=False):
        total, latest = 0, 0
        try:
            entries = list(os.scandir(p))
        except OSError:
            errors.append(str(p))
            return 0
        names = {e.name for e in entries}
        if not suppressed and ".git" in names:
            repos.append(str(p))
        for e in entries:
            ep = Path(e.path)
            if p == root and (ep == APP or e.name == "Igloo.app"
                              or e.name.startswith("project-cleanup-audit-") or ep == VAULT):
                continue
            try:
                st = e.stat(follow_symlinks=False)
                n = st.st_blocks * 512
                if e.is_dir(follow_symlinks=False):
                    n += visit(ep, suppressed or e.name in SKIP_DISCOVERY or e.name.startswith(".freeze-removing-"),
                               config_suppressed or e.name in CACHE_KINDS | {".git", ".worker", "build", "dist", "target", ".vercel", "artifacts"},
                               age_suppressed or e.name in SKIP_DISCOVERY or e.name.endswith(".app"))
                    latest = max(latest, modified.get(str(ep), 0))
                    if not suppressed and e.name in CACHE_KINDS | REVIEW_KINDS:
                        candidates.append({"path": str(ep), "size": n, "kind": e.name})
                    # Repositories nested in .claude worktrees remain discoverable.
                elif not config_suppressed and config_name(ep):
                    local.append({"path": str(ep), "size": n, "template": is_template(ep)})
                if not age_suppressed and not e.is_dir(follow_symlinks=False):
                    latest = max(latest, st.st_mtime)
                total += n
            except OSError:
                errors.append(str(ep))
        sizes[str(p)] = total
        modified[str(p)] = latest
        return total
    total = visit(root)
    for category in ("Privat", "Tallmaker", "Others", "Formue"):
        parent = root / category
        if parent.exists():
            source_dirs.extend(str(p) for p in parent.iterdir() if p.is_dir() and not p.is_symlink())
    return {"sizes": sizes, "modified": modified, "repos": repos, "candidates": candidates, "local": local,
            "errors": errors, "total": total, "top": source_dirs}


def git_state(path):
    rc, top = run_git(path, "rev-parse", "--show-toplevel")
    if rc or Path(top.strip()) != Path(path):
        raise Refusal("Not a standalone Git checkout.")
    common = common_dir(path)
    rc, raw = run_git(path, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if rc:
        raise Refusal("Git status failed; this checkout needs manual review.")
    rc, tracked = run_git(path, "ls-files", "-z")
    if rc:
        raise Refusal("Cannot read tracked files.")
    rc, ignored = run_git(path, "ls-files", "--others", "--ignored", "--exclude-standard",
                          "--directory", "--no-empty-directory", "-z")
    if rc:
        raise Refusal("Cannot read ignored files.")
    rc, branch = run_git(path, "symbolic-ref", "--short", "-q", "HEAD")
    rc, head = run_git(path, "rev-parse", "--verify", "HEAD")
    rc, out = run_git(path, "for-each-ref",
                      "--format=%(refname:short)%09%(objectname)%09%(upstream:short)%09%(upstream:track)%09%(committerdate:iso-strict)",
                      "refs/heads")
    branches = []
    for line in out.splitlines():
        cells = line.split("\t")
        if len(cells) == 5:
            name, sha, upstream, tracking, date = cells
            branches.append({"name": name, "sha": sha, "upstream": upstream, "tracking": tracking,
                             "date": date, "uncovered": None, "privateHistory": bool(PRIVATE_REF.search(name))})
    rc, out = run_git(path, "remote")
    remotes = []
    for name in out.splitlines():
        rc, url = run_git(path, "remote", "get-url", name)
        rc, push_url = run_git(path, "remote", "get-url", "--push", name)
        remotes.append({"name": name, "url": safe_url(url.strip()), "pushURL": safe_url(push_url.strip())})
    rc, out = run_git(path, "reflog", "show", "--format=%H", "refs/stash")
    stashes = out.splitlines()
    rc, out = run_git(path, "log", "-1", "--format=%cI")
    last_commit = out.strip()
    rc, out = run_git(path, "reflog", "-1", "--format=%gD", "--date=iso-strict", "HEAD")
    match = re.search(r"@\{(.+)\}", out)
    last_reflog = match.group(1) if match else ""
    rc, out = run_git(path, "for-each-ref", "--format=%(refname)%09%(objectname)", "refs/tags", "refs/notes")
    refs = [{"name": line.split("\t")[0], "sha": line.split("\t")[1], "verified": False}
            for line in out.splitlines() if "\t" in line]
    return {"path": str(path), "common": common, "branch": branch.strip(), "head": head.strip(),
            "changes": parse_changes(raw), "tracked": set(filter(None, tracked.split("\0"))),
            "ignored": list(filter(None, ignored.split("\0"))), "branches": branches,
            "remotes": remotes, "stashes": stashes, "lastCommit": last_commit,
            "lastReflog": last_reflog, "refs": refs, "registrations": worktree_list(path)}


def verify_remote(state):
    tips, advertised, failures = set(), set(), []
    for remote in state["remotes"]:
        name = remote["name"]
        if name.startswith("-"):
            failures.append(name)
            continue
        rc, out = run_git(state["path"], "ls-remote", "--heads", "--tags", name, timeout=25)
        if rc:
            failures.append(name)
            continue
        for line in out.splitlines():
            sha, _, ref = line.partition("\t")
            if re.fullmatch(r"[0-9a-f]{40,64}", sha) and ref:
                tips.add(sha)
                advertised.add((ref, sha))
    available = []
    # A single batch avoids one subprocess per remote tag.
    if tips:
        rc, out = run_git(state["path"], "cat-file", "--batch-check=%(objectname) %(objecttype)",
                          input_text="".join(t + "^{commit}\n" for t in tips))
        if rc == 0:
            available = list({line.split()[0] for line in out.splitlines()
                              if len(line.split()) == 2 and line.split()[1] == "commit"})
    for branch in state["branches"]:
        if available:
            rc, out = run_git(state["path"], "rev-list", "--count", branch["sha"], "--not", *available)
            if rc == 0 and out.strip().isdigit():
                branch["uncovered"] = int(out.strip())
    for ref in state["refs"]:
        ref["verified"] = (ref["name"], ref["sha"]) in advertised
    detached = []
    if not state["branch"] and state["head"]:
        count = None
        if available:
            rc, out = run_git(state["path"], "rev-list", "--count", state["head"], "--not", *available)
            if not rc and out.strip().isdigit():
                count = int(out.strip())
        detached.append({"sha": state["head"], "uncovered": count})
    return {"status": "No remote" if not state["remotes"] else "Check failed" if failures else "Verified",
            "verifiedAt": now(), "failed": failures, "detached": detached, "commitTips": available}


def age_days(dates):
    values = []
    for value in dates:
        try:
            parsed = dt.datetime.fromisoformat(value)
            if parsed.tzinfo:
                values.append(parsed)
        except (TypeError, ValueError):
            pass
    return max(0, (dt.datetime.now(dt.timezone.utc) - max(values)).days) if values else None


def project_record(states, observation, cwds, live=True):
    main = min(states, key=lambda s: len(s["path"]))
    active = activity(main["path"], cwds) if cwds is not None else []
    status = verify_remote(main) if live else {"status": "Not checked", "verifiedAt": "", "failed": [], "detached": [], "commitTips": []}
    detached = []
    for state in states:
        if state["branch"] or not state["head"]:
            continue
        count = None
        if status.get("commitTips"):
            rc, out = run_git(state["path"], "rev-list", "--count", state["head"], "--not", *status["commitTips"])
            if not rc and out.strip().isdigit():
                count = int(out.strip())
        detached.append({"path": state["path"], "sha": state["head"], "uncovered": count})
    def own(p):
        candidate = next((s for s in sorted(observation.get("owners", states), key=lambda s: -len(s["path"]))
                          if under(Path(p), Path(s["path"]))), None)
        return candidate if candidate and candidate["common"] == main["common"] else None
    roots = [s for s in states if not any(s["path"].startswith(t["path"] + "/") for t in states if s != t)]
    # All worktrees of a repository share one branch inventory but keep their own status.
    candidates, locals_ = [], []
    for c in observation["candidates"]:
        s = own(c["path"])
        if not s:
            continue
        rel = os.path.relpath(c["path"], s["path"])
        rc, _ = run_git(s["path"], "check-ignore", "-q", rel + "/")
        count = sum(t == rel or t.startswith(rel + "/") for t in s["tracked"])
        eligible = c["kind"] in CACHE_KINDS and not count and rc == 0 and not active and cwds is not None and Path(main["path"]) != APP
        reason = ("Igloo runtime is kept in place" if Path(main["path"]) == APP else "Contains tracked files" if count else "Active project session" if active
                  else "Activity check unavailable" if cwds is None else "Review local evidence/data" if c["kind"] not in CACHE_KINDS
                  else "No Git ignore rule" if rc != 0 else "Ignored cache or dependency; review custom edits")
        candidates.append(dict(c, id=c["path"], owner=s["path"], eligible=eligible, reason=reason))
    for f in observation["local"]:
        s = own(f["path"])
        if s:
            rel = os.path.relpath(f["path"], s["path"])
            rc, _ = run_git(s["path"], "check-ignore", "-q", rel)
            locals_.append(dict(f, tracked=rel in s["tracked"], ignored=rc == 0))
    worktrees = []
    for w in main["registrations"]:
        path = w.get("worktree", "")
        s = next((x for x in states if x["path"] == path), None)
        worktrees.append({"path": str(path), "branch": str(w.get("branch", "DETACHED")).removeprefix("refs/heads/"),
                          "exists": Path(path).is_dir() if path else False, "changes": len(s["changes"]) if s else 0,
                          "locked": bool(w.get("locked")), "prunable": str(w.get("prunable", ""))})
    changes = [{"checkout": s["path"], **c} for s in states for c in s["changes"]]
    days = age_days([s[k] for s in states for k in ("lastCommit", "lastReflog")]
                    + [b["date"] for b in main["branches"]])
    unknown = sum(b["uncovered"] is None for b in main["branches"])
    uncovered = sum((b["uncovered"] or 0) > 0 for b in main["branches"])
    holds = []
    detached_to_preserve = [h for h in detached if h["uncovered"] is None or h["uncovered"] > 0]
    if detached_to_preserve: holds.append(f"{len(detached_to_preserve)} detached HEADs need preservation review")
    if not main["remotes"]: holds.append("No remote configured")
    if status["status"] == "Check failed": holds.append("Remote access could not be verified")
    if uncovered: holds.append(f"{uncovered} branches with history outside verified remote tips")
    if unknown: holds.append(f"{unknown} branches need remote verification or a fetch")
    if changes: holds.append(f"{len(changes)} changed or untracked entries")
    if main["stashes"]: holds.append(f'{len(main["stashes"])} stashes to preserve')
    if active: holds.append("Running process has a working directory in this repository")
    if cwds is None: holds.append("Activity check unavailable")
    non_templates = [f for f in locals_ if not f["template"]]
    if non_templates: holds.append(f"{len(non_templates)} config, key or data filename matches")
    private_refs = [r for r in main["refs"] if not r["verified"]]
    if private_refs: holds.append(f"{len(private_refs)} tags or notes not verified remotely")
    name = Path(main["path"]).name
    return {"id": main["path"], "name": name, "path": main["path"], "common": main["common"],
            "category": str(Path(main["path"]).relative_to(ROOT)).split("/")[0] if under(Path(main["path"]), ROOT) else "Linked outside Projects",
            "size": sum(observation["sizes"].get(s["path"], 0) for s in roots), "git": True,
            **classification.detect(main["path"]),
            "ageDays": days, "fileAgeDays": file_age([observation.get("modified", {}).get(s["path"], 0) for s in roots]), "lastCommit": main["lastCommit"], "remoteStatus": status["status"],
            "verifiedAt": status["verifiedAt"], "remotes": main["remotes"],
            "active": bool(active), "processes": list(dict.fromkeys(f'{p.get("command", "Process")} (PID {p.get("pid", "?")})' for p in active)),
            "branches": main["branches"], "detachedHeads": detached, "worktrees": worktrees, "changes": changes,
            "stashes": main["stashes"], "refs": main["refs"], "localFiles": locals_,
            "ignoredFiles": [{"checkout": s["path"], "path": p} for s in states for p in s["ignored"]],
            "candidates": candidates, "holds": holds, "uncovered": uncovered, "unknown": unknown,
            "error": "", "activityKnown": cwds is not None,
            "blockers": lifecycle_module().blockers_for_states(states, active if cwds is not None else None)}


def file_age(timestamps):
    latest = max(timestamps, default=0)
    return max(0, int((dt.datetime.now().timestamp() - latest) / 86400)) if latest > 0 else None


def scan():
    progress("Measuring project folders and discovering Git checkouts…")
    observation = observe_files()
    progress(f'Inspecting {len(observation["repos"])} Git checkouts…')
    errors = list(observation["errors"])
    states = []
    def inspect(p):
        try: return git_state(p)
        except Refusal: return {"failed": p}
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        for result in pool.map(inspect, observation["repos"]):
            if "failed" in result: errors.append(result["failed"])
            else: states.append(result)
    observation["owners"] = states
    groups = {}
    for s in states:
        groups.setdefault(s["common"], []).append(s)
    try: cwds = process_cwds()
    except Refusal: cwds = None
    progress("Verifying remotes and checking cleanup opportunities…")
    projects = []
    def build(ss):
        return project_record(ss, observation, cwds)
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        projects = list(pool.map(build, groups.values()))
    known = {s["path"] for s in states}
    # Include non-Git top-level project folders and worktree residue, without pretending they are backed up.
    residue = list(observation["top"])
    wt_root = ROOT / ".herdr-wt"
    if wt_root.exists():
        for group in wt_root.iterdir():
            if group.is_dir() and not group.is_symlink():
                residue.extend(str(p) for p in group.iterdir() if p.is_dir() and not p.is_symlink())
    for p in set(residue) - known:
        if any(p.startswith(k + "/") for k in known):
            continue
        projects.append({"id": p, "name": Path(p).name, "path": p, "common": "", "git": False,
                         "category": "No Git metadata", "size": observation["sizes"].get(p, 0),
                         **classification.detect(p),
                         "ageDays": None, "fileAgeDays": file_age([observation.get("modified", {}).get(p, 0)]), "lastCommit": "", "remoteStatus": "No Git metadata",
                         "verifiedAt": "", "remotes": [], "active": False, "activityKnown": cwds is not None,
                         "processes": [], "branches": [], "worktrees": [], "changes": [], "stashes": [],
                         "refs": [], "localFiles": [f | {"tracked": False, "ignored": False} for f in observation["local"] if f["path"].startswith(p + "/")],
                         "ignoredFiles": [], "candidates": [], "holds": ["No root Git metadata. Review local files and nested content."],
                         "uncovered": 0, "unknown": 0, "error": ""})
    disk = shutil.disk_usage(ROOT)
    result = {"version": 1, "generated": now(), "root": str(ROOT), "source": "Live analysis",
              "totalSize": observation["total"], "freeSpace": disk.free,
              "projects": sorted(projects, key=lambda p: -p["size"]), "errors": errors}
    save(STATE / "snapshot.json", result)
    progress("Analysis complete.")
    return result


def import_audit():
    choices = sorted(ROOT.glob("project-cleanup-audit-*/catalog.json"), reverse=True)
    if not choices:
        return {"version": 1, "generated": "", "root": str(ROOT), "source": "Run your first analysis",
                "totalSize": 0, "freeSpace": shutil.disk_usage(ROOT).free, "projects": [], "errors": []}
    audit = load(choices[0])
    projects = []
    for g in audit["groups"]:
        checkouts = [r for r in audit["checkouts"] if r["common_dir"] == next(
            x["common_dir"] for x in audit["checkouts"] if x["path"] == g["path"])]
        r = next(x for x in checkouts if x["path"] == g["path"])
        lg = r["live"]
        branches = [{"name": b["name"], "sha": b["sha"], "upstream": b["upstream"], "tracking": b["tracking"],
                     "date": b["date"], "uncovered": b["commits_not_reachable_from_verified_live_tips"],
                     "privateHistory": bool(PRIVATE_REF.search(b["name"]))} for b in lg["branches"]]
        candidates = [{"id": c["path"], "path": c["path"], "size": c["bytes"], "kind": c["kind"],
                       "owner": c["owner"] or "", "eligible": c["eligible_first_pass"], "reason": "Historical audit; recheck before cleanup"}
                      for c in audit["candidates"] if c["owner"] in {s["path"] for s in checkouts}]
        projects.append({"id": g["path"], "name": Path(g["path"]).name, "path": g["path"], "common": r["common_dir"],
                         "category": str(Path(g["path"]).relative_to(ROOT)).split("/")[0], "size": g["bytes"],
                         "git": True, "ageDays": age_days([x["last_commit"] for x in checkouts]), "lastCommit": r["last_commit"],
                         "remoteStatus": "No remote" if not r["remotes"] else "Check failed" if any(not m["ok"] for m in lg["remotes"]) else "Verified",
                         "verifiedAt": audit["generated"], "remotes": [{"name": x["name"], "url": x["url"], "pushURL": x["push_url"]} for x in r["remotes"]],
                         "active": g["active"], "activityKnown": True, "processes": [], "branches": branches,
                         "worktrees": [{"path": str(w.get("worktree", "")), "branch": str(w.get("branch", "DETACHED")).removeprefix("refs/heads/"),
                                       "exists": Path(w.get("worktree", "")).is_dir(), "changes": next((len(x["changes"]) for x in checkouts if x["path"] == w.get("worktree")), 0),
                                       "locked": bool(w.get("locked")), "prunable": str(w.get("prunable", ""))} for w in r["worktrees"]],
                         "changes": [{"checkout": x["path"], **c} for x in checkouts for c in x["changes"]],
                         "stashes": r["stashes"], "refs": [], "localFiles": [{"path": f["path"], "size": f["bytes"], "template": f["template"],
                                                                          "tracked": f["tracked"], "ignored": f["ignored"]}
                                                                         for f in audit["local_files"] if f["owner"] in {x["path"] for x in checkouts}],
                         "ignoredFiles": [{"checkout": x["path"], "path": p} for x in checkouts for p in x["ignored"]],
                         "candidates": candidates, "holds": g["holds"], "uncovered": g["uncovered"], "unknown": g["unknown"],
                         "error": ""})
    for t in audit["top_directories"]:
        if not t["git"]:
            projects.append({"id": t["path"], "name": Path(t["path"]).name, "path": t["path"], "common": "",
                             "category": "No Git metadata", "git": False, "size": t["bytes"] or 0, "ageDays": None,
                             "lastCommit": "", "remoteStatus": "No Git metadata", "verifiedAt": "", "remotes": [],
                             "active": False, "activityKnown": False, "processes": [], "branches": [], "worktrees": [],
                             "changes": [], "stashes": [], "refs": [], "localFiles": [], "ignoredFiles": [], "candidates": [],
                             "holds": ["Review local files before removing this folder."], "uncovered": 0, "unknown": 0, "error": ""})
    result = {"version": 1, "generated": audit["generated"], "root": str(ROOT), "source": "Imported audit — run a fresh analysis",
              "totalSize": load(choices[0].parent / "filesystem.json")["total"],
              "freeSpace": shutil.disk_usage(ROOT).free, "projects": projects, "errors": []}
    return result


def lifecycle_module():
    import lifecycle
    import types
    lifecycle.configure(sys.modules.get(__name__) or types.SimpleNamespace(**globals()))
    return lifecycle


def snapshot():
    result = load(STATE / "snapshot.json") or import_audit()
    if result.get("root") != str(ROOT):
        result = {"version":1,"generated":"","root":str(ROOT),"source":"Run an analysis for this workspace",
                  "totalSize":0,"freeSpace":shutil.disk_usage(ROOT).free,"projects":[],"errors":[]}
    decisions = project_settings()
    types = load(STATE / "classifications.json", {})
    for p in result["projects"]:
        p["detectedType"] = p.get("projectType", "Unclassified")
        if types.get(p["id"]) in classification.TYPES:
            p["projectType"] = types[p["id"]]
        p["hold"] = decisions.get(p["id"], {}).get("hold", False)
        p.pop("decision", None)
        p["note"] = decisions.get(p["id"], {}).get("note", "")
    result["history"] = load(STATE / "trash.json", [])
    result["archives"] = lifecycle_module().vault_entries()
    dormant = {a["path"] for a in result["archives"] if a["state"] in {"frozen", "retiring", "removing", "rehydrating"}}
    result["projects"] = [p for p in result["projects"] if p["path"] not in dormant]
    return result


def on_hold(path, data=None):
    data = snapshot() if data is None else data
    return any(p.get("hold", False) and (p.get("path") == str(path) or p.get("id") == str(path)
               or any(w.get("path") == str(path) for w in p.get("worktrees", []))) for p in data["projects"])


def require_project(path):
    if Path(path) != ROOT and under(APP, Path(path)):
        lifecycle_module().known_checkout(path, allow_app=True)
    else:
        checked_path(path)
    record = next((p for p in snapshot()["projects"] if p["id"] == path), None)
    if record is None or not record["git"]:
        raise Refusal("Select a Git project from the latest analysis.")
    return record


def validate_cache(path, owner, check_processes=True):
    """Revalidate every mutation rather than trusting the saved eligibility flag."""
    p = checked_path(path)
    base = checked_path(owner)
    if p.name not in CACHE_KINDS or not under(p, base) or p == base or not p.is_dir():
        raise Refusal("Only recognized cache/dependency directories can be moved.")
    rc, out = run_git(base, "rev-parse", "--show-toplevel")
    if rc or Path(out.strip()) != base:
        raise Refusal("The owning checkout has moved or lost its Git metadata.")
    rc, inside = run_git(p, "rev-parse", "--show-toplevel")
    if rc or Path(inside.strip()) != base:
        raise Refusal("The candidate is a nested repository.")
    rel = str(p.relative_to(base))
    rc, tracked = run_git(base, "ls-files", "-z", "--", ":(literal)" + rel)
    if rc or tracked:
        raise Refusal("Tracked files exist here, or tracked-file verification failed.")
    rc, _ = run_git(base, "check-ignore", "-q", rel + "/")
    if rc:
        raise Refusal("The candidate is no longer ignored by Git.")
    if check_processes and activity(base, process_cwds()):
        raise Refusal("This repository has an active process or shell. Close it before cleanup.")
    # A cache can itself contain a manually cloned project or important local configuration.
    fingerprint = hashlib.sha256()
    for directory, dirs, files in os.walk(p, followlinks=False):
        here = Path(directory)
        for name in sorted(dirs + files):
            child = here / name
            st = child.lstat()
            fingerprint.update(str(child.relative_to(p)).encode("utf-8", errors="surrogateescape"))
            fingerprint.update(f":{st.st_ino}:{st.st_size}:{st.st_mtime_ns}".encode())
        if ".git" in dirs or ".git" in files:
            raise Refusal("A nested Git repository exists inside this directory.")
        for name in files:
            fp = here / name
            if config_name(fp) and not is_template(fp):
                # Dependency distributions often ship public certificates and fixtures.
                # Preserve any env/credential/key material conservatively; review manually.
                if name.lower().startswith(".env") or name.lower() in {
                    ".npmrc", ".pypirc", ".netrc", "credentials.json", "providers.json", "id_rsa", "id_ed25519"
                }:
                    raise Refusal("Local configuration or environment files were found inside. Review this directory manually.")
        dirs[:] = [d for d in dirs if not (here / d).is_symlink()]
    st = p.stat()
    return {"path": str(p), "owner": str(base), "size": physical_size(p),
            "device": st.st_dev, "inode": st.st_ino, "mtime": st.st_mtime_ns, "fingerprint": fingerprint.hexdigest()}


def preview_cleanup(paths):
    snap = snapshot()
    known = {c["id"]: c for p in snap["projects"] for c in p["candidates"]}
    valid, blocked = [], []
    for path in dict.fromkeys(paths):
        c = known.get(path)
        if not c:
            blocked.append({"path": path, "reason": "Not in the current analysis."})
            continue
        try:
            valid.append(validate_cache(path, c["owner"]))
        except Refusal as e:
            blocked.append({"path": path, "reason": str(e)})
    valid = [v for v in valid if not any(v["path"].startswith(w["path"] + "/") for w in valid if v != w)]
    plan = {"id": uuid.uuid4().hex, "created": now(), "items": valid, "blocked": blocked,
            "totalSize": sum(v["size"] for v in valid),
            "held": any(on_hold(v["owner"], snap) for v in valid)}
    # Moving to Trash on the same volume frees no allocated blocks yet.
    plan["outcome"] = outcome_summary(trash=len(valid),
                                      held=[{"path": b["path"], "reason": b["reason"]} for b in blocked])
    save(STATE / "plans" / (plan["id"] + ".json"), plan)
    return plan


def execute_cleanup(plan_id):
    if not re.fullmatch("[0-9a-f]{32}", plan_id):
        raise Refusal("Invalid cleanup review.")
    plan = load(STATE / "plans" / (plan_id + ".json"))
    if not plan or age_seconds(plan["created"]) > 600:
        raise Refusal("The review expired. Review the cleanup again.")
    if plan.get("executed"):
        raise Refusal("This cleanup review was already used.")
    trash = Path.home() / ".Trash"
    if trash.is_symlink():
        raise Refusal("Trash is a symbolic link; cleanup was refused.")
    trash.mkdir(mode=0o700, exist_ok=True)
    results = []
    for item in plan["items"]:
        try:
            fresh = validate_cache(item["path"], item["owner"])
            if any(fresh[k] != item[k] for k in ("device", "inode", "mtime", "fingerprint")):
                raise Refusal("This directory changed since the review. Run a new review.")
            if os.stat(trash).st_dev != fresh["device"]:
                raise Refusal("Trash is on another volume. Use Finder to review this directory.")
            token = uuid.uuid4().hex
            destination = trash / (Path(item["path"]).name + " — " + Path(item["owner"]).name + " — " + token[:8])
            row = {"id": token, "original": item["path"], "destination": str(destination),
                   "owner": item["owner"], "size": fresh["size"], "at": now(), "status": "prepared"}
            history = load(STATE / "trash.json", [])
            history.append(row)
            save(STATE / "trash.json", history)  # Write recoverable intent before the atomic rename.
            os.rename(item["path"], destination)
            row["status"] = "trashed"
            history[-1] = row
            save(STATE / "trash.json", history)
            results.append({"path": item["path"], "ok": True, "message": "Moved to Trash. Restore from History."})
        except (Refusal, OSError) as e:
            results.append({"path": item["path"], "ok": False, "message": str(e) if isinstance(e, Refusal) else "Filesystem move failed. Nothing was permanently deleted; inspect History."})
    plan["executed"] = now()
    save(STATE / "plans" / (plan_id + ".json"), plan)
    outcome = outcome_summary(trash=sum(r["ok"] for r in results), freed=0, observed=True,
                              held=[{"path": r["path"], "reason": r["message"]} for r in results if not r["ok"]]
                                   + [{"path": b["path"], "reason": b["reason"]} for b in plan["blocked"]])
    return {"results": results, "outcome": outcome,
            "message": "Trash keeps these files until you empty it. Reanalyse to update disk estimates."}


def age_seconds(value):
    return (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(value)).total_seconds()


def restore(item_id):
    history = load(STATE / "trash.json", [])
    item = next((r for r in history if r["id"] == item_id), None)
    if not item or item["status"] not in ("trashed", "prepared"):
        raise Refusal("This entry cannot be restored.")
    original = checked_path(item["original"], allow_missing=True)
    destination = Path(item["destination"])
    trash = Path.home() / ".Trash"
    if trash.is_symlink() or destination.parent != trash or destination.is_symlink():
        raise Refusal("The Trash path changed; restore it manually in Finder.")
    if original.exists() or original.is_symlink():
        raise Refusal("The original path already exists. Nothing will be overwritten.")
    if not original.parent.is_dir() or not destination.is_dir():
        raise Refusal("The original parent or Trash item is missing. Review it in Finder.")
    os.rename(destination, original)
    item["status"] = "restored"
    item["restoredAt"] = now()
    save(STATE / "trash.json", history)
    return {"message": "Restored to " + str(original)}


def refresh_project(path, fetch=False):
    record = require_project(path)
    if fetch:
        remotes = record["remotes"]
        if not remotes:
            raise Refusal("No remote is configured.")
        for remote in remotes:
            name = remote["name"]
            if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", name):
                raise Refusal("Use Terminal to fetch this unusually named remote.")
            rc, _ = run_git(path, "fetch", "--no-tags", "--no-prune", "--no-prune-tags", "--", name, timeout=90)
            if rc:
                raise Refusal("Fetch failed. Check access in Terminal. Your checkout was not changed.")
    # A targeted reanalysis measures only the currently known worktree roots.
    paths = [w["path"] for w in record["worktrees"] if w["exists"] and Path(w["path"]).exists()]
    if path not in paths: paths.append(path)
    observation = {"sizes": {}, "modified": {}, "candidates": [], "local": []}
    states = []
    nested_paths = set()
    for p in paths:
        try: states.append(git_state(p))
        except Refusal: continue
        # Use the same complete observation logic with the checkout as root.
        obs = observe_files(Path(p))
        observation["sizes"].update(obs["sizes"])
        observation["modified"].update(obs["modified"])
        observation["candidates"].extend(obs["candidates"])
        observation["local"].extend(obs["local"])
        nested_paths.update(obs["repos"])
    owners = list(states)
    for nested in nested_paths - {s["path"] for s in states}:
        try: owners.append(git_state(nested))
        except Refusal: continue
    observation["owners"] = owners
    if not states:
        raise Refusal("No readable checkout remains.")
    try: cwds = process_cwds()
    except Refusal: cwds = None
    project = project_record(states, observation, cwds)
    data = load(STATE / "snapshot.json") or import_audit()
    data["projects"] = [project if p["id"] == path else p for p in data["projects"]]
    data["source"] = "Mixed snapshot — selected project refreshed"
    save(STATE / "snapshot.json", data)
    return {"message": "Project refreshed. Other projects retain their previous timestamps."}


def push_branch(path, name, expected_sha, remote_name, expected_url):
    if under(APP, Path(path)):
        lifecycle_module().known_checkout(path, allow_app=True)
    else:
        require_project(path)
    if PRIVATE_REF.search(name):
        raise Refusal("This branch is labeled private, archive or backup history. Review and publish manually.")
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", remote_name):
        raise Refusal("Invalid remote name.")
    rc, _ = run_git(path, "check-ref-format", "refs/heads/" + name)
    if rc:
        raise Refusal("Invalid branch ref.")
    rc, out = run_git(path, "rev-parse", "--verify", "refs/heads/" + name)
    if rc or out.strip() != expected_sha:
        raise Refusal("The branch changed since review. Refresh and review it again.")
    rc, urls = run_git(path, "remote", "get-url", "--push", "--all", remote_name)
    destinations = urls.splitlines()
    if rc or len(destinations) != 1:
        raise Refusal("This remote has no single push destination. Review its push URLs manually.")
    destination = destinations[0]
    if safe_url(destination) != expected_url:
        raise Refusal("The push destination changed since review. Refresh and review it again.")
    if safe_url(destination) != destination:
        raise Refusal("This destination embeds authentication or query data. Push it manually in Terminal.")
    # Freeze both the reviewed commit and destination. Explicit options override followTags
    # and submodule-push settings, without bypassing pre-push hooks.
    rc, _ = run_git(path, "push", "--porcelain", "--no-follow-tags", "--recurse-submodules=no", "--",
                    destination, expected_sha + ":refs/heads/" + name, timeout=120)
    if rc:
        raise Refusal("Push failed or was rejected. Review access and branch state in Terminal. No force push was attempted.")
    if under(APP, Path(path)):
        lifecycle_module().quick_refresh(path)
    else:
        refresh_project(path)
    return {"message": "Pushed only " + name + " to " + remote_name + ". Uncommitted files were not included."}


def project_settings():
    location = STATE / "decisions.json"
    values = load(location, {})
    changed = False
    for path, row in list(values.items()):
        if "decision" in row or "hold" not in row:
            values[path] = {"hold": row.get("decision") == "Keep", "note": row.get("note", ""),
                            "at": row.get("at", now())}
            changed = True
        elif type(row["hold"]) is not bool:
            raise Refusal("A saved Hold setting is invalid. Review the project settings.")
    if changed:
        save(location, values)
    return values


def update_project_setting(path, **change):
    if path not in {p["id"] for p in snapshot()["projects"]}:
        raise Refusal("Unknown project.")
    values = project_settings()
    values[path] = dict(values.get(path, {"hold": False, "note": ""}), **change, at=now())
    save(STATE / "decisions.json", values)
    return {"message": "Project setting saved locally."}


def set_hold(path, value):
    if value not in {"true", "false"}:
        raise Refusal("Choose true or false for Hold.")
    return update_project_setting(path, hold=value == "true")


def set_note(path, text):
    return update_project_setting(path, note=text[:20000])


def categorize(path, kind):
    if path not in {p["id"] for p in snapshot()["projects"]} or kind not in classification.TYPES + ["Automatic"]:
        raise Refusal("Choose a catalog project and a recognized type.")
    values = load(STATE / "classifications.json", {})
    if kind == "Automatic":
        values.pop(path, None)
    else:
        values[path] = kind
    save(STATE / "classifications.json", values)
    return {"message": "Project type saved locally."}


def main():
    parser = argparse.ArgumentParser()
    commands = ["snapshot", "scan", "refresh", "fetch", "preview", "trash", "restore", "hold", "note", "push", "categorize"] + sorted(lifecycle_module().COMMANDS) + ["wizard-state", "wizard-scan", "wizard-prepare", "wizard-execute", "task-list", "task-save", "task-delete", "task-run", "task-apply", "task-history", "task-install-cli", "task-schedule", "task-unschedule", "task-plan"]
    parser.add_argument("command", choices=commands, metavar="{" + ",".join(commands) + "}")
    parser.add_argument("args", nargs="*")
    args = parser.parse_args()
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Serialize all application commands across multiple windows/processes.
    with open(STATE / "backend.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"error": "Another analysis or action is running. Wait for it to finish."}))
            return 1
        try:
            if args.command == "snapshot": result = snapshot()
            elif args.command == "scan": result = scan()
            elif args.command == "refresh": result = refresh_project(args.args[0])
            elif args.command == "fetch": result = refresh_project(args.args[0], fetch=True)
            elif args.command == "preview": result = preview_cleanup(args.args)
            elif args.command == "trash": result = execute_cleanup(args.args[0])
            elif args.command == "restore": result = restore(args.args[0])
            elif args.command == "hold": result = set_hold(*args.args)
            elif args.command == "note": result = set_note(*args.args)
            elif args.command == "push": result = push_branch(*args.args)
            elif args.command == "categorize": result = categorize(*args.args)
            elif args.command.startswith("task-"):
                import automation
                automation.configure(sys.modules[__name__])
                result = automation.dispatch(args.command, args.args)
            elif args.command.startswith("wizard-"):
                import planning
                planning.configure(sys.modules[__name__])
                result = planning.dispatch(args.command, args.args)
            else: result = lifecycle_module().dispatch(args.command, args.args)
            print(json.dumps(result))
            return 0
        except Refusal as e:
            result = {"error": str(e)}
            if hasattr(e, "outcome"):
                result["outcome"] = e.outcome
            print(json.dumps(result))
            return 1
        except Exception as e:
            # Do not echo credentials, file contents or arbitrary subprocess output.
            result = {"error": "The action could not complete. Existing snapshot and project files were preserved. Check permissions and reanalyse."}
            if hasattr(e, "outcome"):
                result["outcome"] = e.outcome
            print(json.dumps(result))
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
