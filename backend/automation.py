"""Saved cache-maintenance tasks, durable runs and user LaunchAgents."""
import hashlib
import datetime as dt
import json
import os
from pathlib import Path
import plistlib
import platform
import re
import shlex
import shutil
import subprocess
import tempfile
import uuid
import planning
import classification

O = None
COMMANDS = {"task-list", "task-save", "task-delete", "task-run", "task-apply", "task-history",
            "task-install-cli", "task-schedule", "task-unschedule", "task-plan",
            "remind-check", "remind-agent"}
MODULES = ("organizer.py", "preferences.py", "lifecycle.py", "planning.py", "classification.py", "automation.py", "task_cli.py", "LICENSE")

def configure(organizer):
    global O
    O = organizer
    planning.configure(O)

def fail(message):
    raise O.Refusal(message)

def task_path():
    return O.preferences.config_path().with_name("tasks.json")

def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", value):
        fail("Task IDs use lowercase letters, numbers, hyphens and underscores.")
    return value

def config():
    value = O.load(task_path(), {"version": 1, "tasks": []})
    if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("tasks"), list):
        fail("The tasks configuration is invalid; existing data was preserved.")
    return value

def scope_path(value):
    if not isinstance(value, str):
        fail("Task scope must contain project folder paths.")
    p = Path(value).expanduser()
    if not p.is_absolute() or ".." in p.parts or not O.under(p, O.ROOT) or not O.under(p.resolve(), O.ROOT):
        fail("Task scope must stay inside the configured Projects root.")
    return str(p)

def validate(value):
    identifier(value.get("id"))
    raw_name = value.get("name", "")
    if not isinstance(raw_name, str): fail("Give the task a text name.")
    name = raw_name.strip()
    if not name or len(name) > 120:
        fail("Give the task a name of at most 120 characters.")
    if value.get("mode") not in {"review", "cleanup"} or type(value.get("unattended")) is not bool:
        fail("Choose review-only or cache-cleanup mode and its unattended permission.")
    policy = planning.rules(value["rules"])
    project_limit, byte_limit = value.get("maxProjects"), value.get("maxBytes")
    if type(project_limit) is not int or not 1 <= project_limit <= 1000:
        fail("The project limit must be between 1 and 1000.")
    if type(byte_limit) is not int or not 1 <= byte_limit <= 100 * 1024**4:
        fail("Choose a positive per-run byte limit no greater than 100 TiB.")
    for key in ("include", "exclude", "projectTypes"):
        if not isinstance(value.get(key), list) or any(not isinstance(x, str) for x in value[key]):
            fail("Task scopes and project types must be lists.")
    if any(kind not in classification.TYPES for kind in value["projectTypes"]):
        fail("Choose recognized project types.")
    if value.get("root") != str(O.ROOT) or value.get("vault") != str(O.VAULT):
        fail("This task belongs to another workspace or Freezer. Save a new task for the current settings.")
    return {"id": value["id"], "name": name, "root": str(O.ROOT), "vault": str(O.VAULT),
            "mode": value["mode"], "unattended": value["unattended"], "rules": policy,
            "include": sorted(set(map(scope_path, value["include"]))),
            "exclude": sorted(set(map(scope_path, value["exclude"]))),
            "projectTypes": sorted(set(value["projectTypes"])), "maxProjects": project_limit, "maxBytes": byte_limit}

def policy_hash(task):
    return hashlib.sha256(json.dumps({k:v for k,v in task.items() if k != "name"}, sort_keys=True).encode()).hexdigest()

def get(task_id):
    identifier(task_id)
    value = next((v for v in config()["tasks"] if v.get("id") == task_id), None)
    if value is None:
        fail("Task not found.")
    return validate(value)

def save(value):
    value = validate(value)
    catalog = config()
    catalog["tasks"] = [v for v in catalog["tasks"] if v["id"] != value["id"]] + [value]
    task_path().parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    O.save(task_path(), catalog)
    return {"message": "Task saved. Changed rules invalidate an existing schedule until it is enabled again."}

def build_name(name):
    """Igloo Dev keeps its schedules, logs and CLI apart from the release in the shared state root."""
    return name + ".dev" if O.preferences.is_dev() else name

def schedules_path():
    return O.STATE / (build_name("task-schedules") + ".json")

def schedules():
    return O.load(schedules_path(), {})

def label(task_id):
    return O.preferences.app_id() + ".task." + identifier(task_id)

def cli_path():
    return Path.home() / ".local/bin" / build_name("igloo")

def loaded(task_id):
    try:
        r = subprocess.run(["/bin/launchctl", "print", f"gui/{os.getuid()}/" + label(task_id)],
                           capture_output=True, timeout=10)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False

def history(task_id=None):
    if task_id:
        identifier(task_id)
    rows = []
    for p in (O.STATE / "task-runs").glob("*/*.json"):
        try:
            value = O.load(p)
            if value and (not task_id or value.get("taskID") == task_id):
                rows.append(value)
        except (OSError, ValueError):
            continue
    return {"runs": sorted(rows, key=lambda r:r["started"], reverse=True)[:100]}

def our_cli(path):
    try:
        if path.is_symlink() or not path.is_file(): return False
        with path.open("rb") as f:
            marker = f.read(100)
            return b"Igloo CLI" in marker
    except OSError: return False


def listing():
    registered = schedules()
    all_runs = history()["runs"]
    tasks = []
    for raw in config()["tasks"]:
        task = raw
        schedule = registered.get(task["id"])
        if schedule:
            schedule = dict(schedule, loaded=loaded(task["id"]), stale=schedule["policyHash"] != policy_hash(task))
        tasks.append({"definition": task, "schedule": schedule,
                      "lastRun": next((r for r in all_runs if r["taskID"] == task["id"]), None),
                      "compatible": task.get("root") == str(O.ROOT) and task.get("vault") == str(O.VAULT)})
    cli = cli_path()
    return {"tasks": sorted(tasks, key=lambda t:t["definition"]["name"].casefold()), "cliPath": str(cli),
            "cliInstalled": our_cli(cli)}

def new_run(task, kind):
    return {"id": uuid.uuid4().hex, "taskID": task["id"], "taskName": task["name"], "started": O.now(),
            "status": "running", "kind": kind, "message": "Starting…", "planID": None,
            "ready": 0, "blocked": 0, "cacheBytes": 0}

def record(run):
    location = O.STATE / "task-runs" / run["taskID"] / (run["id"] + ".json")
    O.save(location, run)
    outcome = run.get("outcome") if isinstance(run.get("outcome"), dict) else {}
    O.record_space_run(run["id"], "task", started=run.get("started"), status=run.get("status"),
                       label=run.get("taskName", "Task"),
                       estimate_bytes=outcome.get("bytesFreedEstimate"),
                       observed=run.get("observed", outcome.get("observedSpace")),
                       mirror_paths=[location.relative_to(O.STATE)])

def record_busy(task_id):
    run = new_run(get(task_id), "scheduled")
    run.update(status="skipped-busy", finished=O.now(), message="Another catalog operation was running; no changes were made.")
    record(run)
    return run

def scoped(project, task):
    p = Path(project["path"])
    return ((not task["include"] or any(O.under(p, Path(q)) for q in task["include"])) and
            not any(O.under(p, Path(q)) for q in task["exclude"]) and
            (not task["projectTypes"] or project.get("projectType", "Unclassified") in task["projectTypes"]))

def held(row, reason):
    return {"id": row["path"], "path": row["path"], "name": row["name"], "action": row["action"],
            "status": "blocked", "message": reason, "cleanup": None, "freeze": None}

def build_plan(task):
    scan = O.scan()
    if scan.get("errors"):
        fail("Analysis reported unreadable or failed projects. Resolve scan issues before automated cleanup.")
    snapshot = O.snapshot()
    projects = [p for p in snapshot["projects"] if scoped(p, task)]
    proposals = planning.suggest({"projects":projects}, task["rules"])
    choices, holds, excluded = [], [], []
    for row in proposals:
        if row.get("held"):
            excluded.append({"path": row["path"], "reason": "On hold."})
        elif row["action"] == "cleanup":
            if len(choices) < task["maxProjects"]:
                choices.append({"path":row["path"], "action":"cleanup", "remote":""})
            else:
                holds.append(held(row, "Per-run project limit reached."))
        elif row["action"] == "freeze":
            holds.append(held(row, "Past the freeze threshold. Review freezing in the wizard; tasks never push or remove checkouts."))
    plan = planning.prepare({"rules":task["rules"], "choices":choices})
    plan["rows"].extend(holds)
    plan["task"] = {"id":task["id"], "policyHash":policy_hash(task)}
    total = sum(r["cleanup"]["totalSize"] for r in plan["rows"] if r["status"] == "ready" and r["cleanup"])
    if total > task["maxBytes"]:
        for row in plan["rows"]:
            if row["status"] == "ready":
                row.update(status="blocked", message="The review exceeds this task's per-run byte limit. No cleanup will run.")
    plan["held"] = excluded + [{"path": r["path"], "reason": r["message"]} for r in plan["rows"] if r["status"] == "blocked"]
    planning.persist(plan)
    return plan, total

def run_task(task_id, apply=False, scheduled=False, expected_hash=""):
    task = get(task_id)
    run = new_run(task, "scheduled" if scheduled else "manual")
    record(run)
    try:
        if scheduled:
            if expected_hash != policy_hash(task):
                fail("Task rules changed. Re-enable the schedule to authorize the current policy.")
            if task["mode"] == "cleanup" and not task["unattended"]:
                fail("Unattended cache cleanup is not enabled for this task.")
            apply = task["mode"] == "cleanup"
        if apply and task["mode"] != "cleanup":
            fail("Review-only tasks cannot apply changes. Choose cache-cleanup mode explicitly.")
        plan, total = build_plan(task)
        run.update(planID=plan["id"], ready=sum(r["status"]=="ready" for r in plan["rows"]),
                   blocked=len(plan["held"]), cacheBytes=total, outcome=plan["outcome"])
        record(run)
        if apply and run["ready"]:
            result = planning.execute(plan["id"], run_id=run["id"], run_kind="task")
            run["outcome"] = result["outcome"]
            if isinstance(result["outcome"].get("observedSpace"), dict):
                run["observed"] = result["outcome"]["observedSpace"]
            run["blocked"] = len(result["held"])
            bad = any(r["status"] in {"failed","partial","running"} for r in result["rows"])
            run.update(status="partial" if bad or run["blocked"] else "completed",
                       message="Task actions finished. Review held or failed rows in its saved review.")
        elif apply:
            run["outcome"] = O.combine_outcomes([], held=plan["held"], observed=True)
            run["observed"] = run["outcome"]["observedSpace"]
            run.update(status="held" if run["blocked"] else "nothing-to-do",
                       message="No ready cleanup actions. Held projects were not changed.")
        else:
            run.update(status="planned", message="Fresh review saved. No project files were changed.")
    except O.Refusal as e:
        run.update(status="failed", message=str(e))
    except Exception:
        run.update(status="failed", message="Task stopped unexpectedly. Existing reviews and recovery history were preserved.")
    run["finished"] = O.now()
    record(run)
    return run

def apply_plan(plan_id):
    plan = O.load(planning.location(plan_id))
    if not plan or not plan.get("task"):
        fail("Choose a saved task review.")
    task = get(plan["task"]["id"])
    if task["mode"] != "cleanup" or plan["task"]["policyHash"] != policy_hash(task):
        fail("Task rules changed or cleanup is disabled. Generate a new review.")
    if any(r["action"] != "cleanup" for r in plan["rows"] if r["status"] == "ready"):
        fail("Tasks only apply cache cleanup.")
    ready = [r for r in plan["rows"] if r["status"] == "ready"]
    total = sum(r["cleanup"]["totalSize"] for r in ready)
    if len(ready) > task["maxProjects"] or total > task["maxBytes"]:
        fail("The review exceeds its saved task limits.")
    run = new_run(task, "reviewed")
    run.update(planID=plan_id, ready=len(ready), cacheBytes=total,
               blocked=len(plan.get("held", [])), outcome=plan.get("outcome"))
    record(run)
    try:
        result = planning.execute(plan_id, run_id=run["id"], run_kind="task")
        run["outcome"] = result["outcome"]
        if isinstance(result["outcome"].get("observedSpace"), dict):
            run["observed"] = result["outcome"]["observedSpace"]
        run.update(status="partial" if result["held"] or any(r["status"] in {"failed","partial","running","blocked"} for r in result["rows"]) else "completed",
                   message="Reviewed task review finished. Inspect its saved results.")
    except O.Refusal as e:
        run.update(status="failed", message=str(e))
    except Exception:
        run.update(status="failed", message="Task stopped. Inspect the saved review and Trash history.")
    run["finished"] = O.now()
    record(run)
    return run

def install_cli():
    source = O.APP / "backend" if (O.APP / "backend/organizer.py").is_file() else O.APP
    bundled = source.parent / "python" / platform.machine() / "bin/python3"
    python = bundled if bundled.is_file() else Path(O.CONFIG["pythonExecutable"])
    if not python.is_file() or not os.access(python, os.X_OK):
        fail("Configure an installed Python executable first.")
    sources = [(name, ((O.APP if name == "LICENSE" else source) / name).read_bytes()) for name in MODULES]
    digest = hashlib.sha256(b"".join(name.encode()+data for name,data in sources)).hexdigest()[:16]
    runtime = O.STATE / "task-runtime" / digest
    if runtime.is_symlink() or runtime.parent.is_symlink():
        fail("The task runtime location is a symbolic link.")
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name, data in sources:
        target = runtime/name
        if target.is_symlink():
            fail("The runtime contains a symbolic link.")
        if not target.exists():
            fd, temporary = tempfile.mkstemp(dir=runtime)
            with os.fdopen(fd,"wb") as f:f.write(data)
            os.replace(temporary,target)
        if target.read_bytes() != data:
            fail("The installed task runtime changed. Remove that runtime version before reinstalling.")
    cli = cli_path()
    cli.parent.mkdir(parents=True, exist_ok=True)
    if cli.is_symlink() or cli.exists() and not our_cli(cli):
        fail("A different " + cli.name + " executable already exists; it was not replaced.")
    identity = "export IGLOO_APP_ID=" + O.preferences.DEV_ID + "\n" if O.preferences.is_dev() else ""
    script = "#!/bin/sh\n# Igloo CLI\n" + identity + "exec " + shlex.quote(str(python)) + " -E -s -B " + shlex.quote(str(runtime/"task_cli.py")) + ' "$@"\n'
    fd, temporary = tempfile.mkstemp(prefix="." + cli.name + "-", dir=cli.parent)
    with os.fdopen(fd, "w") as f: f.write(script)
    os.chmod(temporary, 0o700)
    os.replace(temporary, cli)
    return {"message": "CLI installed at " + str(cli) + ". Add ~/.local/bin to PATH if needed.", "path": str(cli)}

def calendar(value):
    frequency = value.get("frequency")
    clock = value.get("time","")
    if frequency not in {"daily","weekly"} or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d",clock):
        fail("Choose a daily or weekly schedule and a 24-hour HH:MM time.")
    hour,minute=map(int,clock.split(":"))
    interval={"Hour":hour,"Minute":minute}
    weekday=value.get("weekday",0)
    if frequency=="weekly":
        if type(weekday) is not int or not 0<=weekday<=6:fail("Choose a weekday from Sunday (0) through Saturday (6).")
        interval["Weekday"]=weekday
    return interval

def schedule_task(task_id, value):
    task=get(task_id)
    interval=calendar(value)
    if task["mode"]=="cleanup" and not task["unattended"]:
        fail("Enable unattended cache cleanup in the saved task before scheduling execution.")
    cli=install_cli()["path"]
    folder=Path.home()/"Library/LaunchAgents";folder.mkdir(parents=True,exist_ok=True)
    plist=folder/(label(task_id)+".plist")
    if plist.is_symlink():fail("The LaunchAgent path is a symbolic link.")
    logs=O.STATE/build_name("task-logs");logs.mkdir(mode=0o700,parents=True,exist_ok=True)
    out,err=logs/(task_id+".out.log"),logs/(task_id+".err.log")
    for path in (out,err):
        if path.is_symlink():fail("The task log path is a symbolic link.")
        path.touch(mode=0o600,exist_ok=True)
    definition={"Label":label(task_id),"ProgramArguments":[cli,"run",task_id,"--scheduled","--policy-hash",policy_hash(task),"--json"],
                "StartCalendarInterval":interval,"RunAtLoad":False,"ProcessType":"Background",
                "WorkingDirectory":str(O.ROOT),"StandardOutPath":str(out),"StandardErrorPath":str(err)}
    fd,tmp=tempfile.mkstemp(prefix=".organizer-",dir=folder)
    with os.fdopen(fd,"wb") as f:f.write(plistlib.dumps(definition))
    subprocess.run(["/bin/launchctl","bootout",f"gui/{os.getuid()}/"+label(task_id)],capture_output=True)
    os.replace(tmp,plist)
    registered=schedules();registered.pop(task_id,None);O.save(schedules_path(),registered)
    result=subprocess.run(["/bin/launchctl","bootstrap",f"gui/{os.getuid()}",str(plist)],capture_output=True)
    if result.returncode:fail("The LaunchAgent could not load. Its file is saved; check the current GUI login session.")
    registered[task_id]={"frequency":value["frequency"],"time":value["time"],"weekday":value.get("weekday",0),
                         "policyHash":policy_hash(task),"label":label(task_id),"plist":str(plist)}
    O.save(schedules_path(),registered)
    return {"message":"Schedule enabled. It runs while this user is logged in; no task was started now."}

def unschedule(task_id):
    identifier(task_id)
    result=subprocess.run(["/bin/launchctl","bootout",f"gui/{os.getuid()}/"+label(task_id)],capture_output=True)
    if result.returncode and loaded(task_id):
        fail("The LaunchAgent is still loaded; it was not removed.")
    plist=Path.home()/"Library/LaunchAgents"/(label(task_id)+".plist")
    if plist.is_symlink():fail("The LaunchAgent path is a symbolic link.")
    if plist.exists():plist.unlink()
    value=schedules();value.pop(task_id,None);O.save(schedules_path(),value)
    return {"message":"Schedule disabled. Run history and task definition were preserved."}

def reminder_history_path():
    return O.STATE / "reminder-history.json"

def reminder_datetime(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    except ValueError:
        return None

def reminder_age_days(timestamp, checked):
    earlier = reminder_datetime(timestamp)
    if earlier is None or checked is None:
        return None
    return max(0, (checked - earlier).days)

def reminder_projects(snapshot, policy, checked):
    """Refresh the age used by reminders without scanning or writing the catalog."""
    projects = []
    for original in snapshot.get("projects", []):
        if not isinstance(original, dict):
            continue
        project = dict(original)
        if policy["ageBasis"] == "git":
            path = project.get("path")
            if project.get("git") is True and isinstance(path, str) and path:
                result, output = O.run_git(path, "log", "--all", "-1", "--format=%ct", timeout=10)
                commit = output.strip()
                if result == 0 and re.fullmatch(r"\d+", commit):
                    project["ageDays"] = max(0, int((checked.timestamp() - int(commit)) // 86400)) if checked else None
                else:
                    project["ageDays"] = None
            else:
                project["ageDays"] = None
        else:
            scanned_at = project.get("verifiedAt") or snapshot.get("generated")
            elapsed = reminder_age_days(scanned_at, checked)
            age = project.get("fileAgeDays")
            project["fileAgeDays"] = age + elapsed if type(age) is int and age >= 0 and elapsed is not None else None
        projects.append(project)
    return projects

def reminder_size(size):
    if size == 0:
        return "0 bytes"
    units = ("bytes", "KB", "MB", "GB", "TB", "PB")
    value, unit = float(size), 0
    while value >= 1024 and unit < len(units) - 1:
        value /= 1024
        unit += 1
    if unit == 0:
        whole = int(value)
        return f"{whole} byte" if whole == 1 else f"{whole} bytes"
    return f"{value:.1f} {units[unit]}"

def reminder_notification_text(count, freeze_days, size):
    noun, verb = ("project", "has") if count == 1 else ("projects", "have")
    return f"{count} {noun} {verb} been quiet for more than {freeze_days} days ({reminder_size(size)})"

def remind_check():
    """Read the saved catalog and record one reminder check without changing projects."""
    snapshot = O.snapshot()
    policy = planning.state()["rules"]
    checked = reminder_datetime(O.now())
    frozen = {row.get("path") for row in snapshot.get("archives", [])
              if row.get("state") in {"frozen", "retiring", "removing", "rehydrating"}}
    fresh_snapshot = dict(snapshot, projects=reminder_projects(snapshot, policy, checked))
    proposals = planning.suggest(fresh_snapshot, policy)
    candidate_paths = {row["path"] for row in proposals if row["action"] == "freeze" and not row.get("held")
                       and type(row.get("ageDays")) is int and row["ageDays"] > policy["freezeDays"]}
    projects = {row["path"]: row for row in snapshot.get("projects", [])
                if isinstance(row, dict) and isinstance(row.get("path"), str)}
    ids = sorted(path for path in candidate_paths if path not in frozen and path in projects
                 and not projects[path].get("hold", False))
    size = sum(projects[path]["size"] for path in ids
               if type(projects[path].get("size")) is int and projects[path]["size"] >= 0)
    entry = {"checked": O.now(), "count": len(ids), "size": size, "ids": ids,
             "freezeDays": policy["freezeDays"],
             "message": reminder_notification_text(len(ids), policy["freezeDays"], size)}
    history_path = reminder_history_path()
    history = O.load(history_path, [])
    if not isinstance(history, list) or any(not isinstance(row, dict) for row in history):
        fail("The reminder history is invalid; existing data was preserved.")
    history.append(entry)
    O.save(history_path, history[-52:])
    return entry

def reminder_program_arguments(app_path):
    # launchd starts the app executable itself, so Background activity names the app.
    return [str(Path(app_path) / "Contents/MacOS/Igloo"), "--remind"]

def reminder_label(bundle_id):
    if bundle_id == "local.igloo":
        return "local.igloo.remind"
    if bundle_id == "local.igloo.dev":
        return "local.igloo.dev.remind"
    fail("The reminder belongs to an installed Igloo or Igloo Dev app.")

def reminder_loaded(agent_label):
    try:
        result = subprocess.run(["/bin/launchctl", "print", f"gui/{os.getuid()}/{agent_label}"],
                                capture_output=True, timeout=10)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False

def configure_reminder(value):
    if not isinstance(value, dict) or type(value.get("enabled")) is not bool:
        fail("Choose whether the quiet-project reminder is enabled.")
    weekday, clock = value.get("weekday"), value.get("time")
    if type(weekday) is not int or not 0 <= weekday <= 6 or not isinstance(clock, str) or not re.fullmatch(
            r"(?:[01]\d|2[0-3]):[0-5]\d", clock):
        fail("Choose a weekday and a 24-hour HH:MM time.")
    raw_path = value.get("appPath")
    if not isinstance(raw_path, str) or not raw_path:
        fail("The installed Igloo app path is missing.")
    app_path = Path(raw_path).expanduser()
    info_path = app_path / "Contents/Info.plist"
    if not app_path.is_absolute() or app_path.suffix != ".app" or app_path.is_symlink() \
            or not app_path.is_dir() or info_path.is_symlink() or not info_path.is_file():
        fail("Choose the installed Igloo app bundle.")
    try:
        with info_path.open("rb") as stream:
            bundle_id = plistlib.load(stream).get("CFBundleIdentifier")
    except (OSError, ValueError, plistlib.InvalidFileException):
        fail("The installed app identity could not be read.")
    agent_label = reminder_label(bundle_id)
    executable = Path(reminder_program_arguments(app_path)[0])
    if value["enabled"] and (executable.is_symlink() or not executable.is_file()):
        fail("The installed Igloo app has no executable.")
    folder = Path.home() / "Library/LaunchAgents"
    plist = folder / (agent_label + ".plist")
    if folder.is_symlink() or plist.is_symlink():
        fail("The reminder LaunchAgent location is a symbolic link.")
    if not value["enabled"] and not plist.exists():
        return {"message": "Quiet-project reminder disabled."}

    stopped = subprocess.run(["/bin/launchctl", "bootout", f"gui/{os.getuid()}/{agent_label}"],
                             capture_output=True)
    if not value["enabled"]:
        if stopped.returncode and reminder_loaded(agent_label):
            fail("The reminder LaunchAgent is still loaded; it was not removed.")
        if plist.exists():
            plist.unlink()
        return {"message": "Quiet-project reminder disabled."}
    if stopped.returncode and reminder_loaded(agent_label):
        fail("The reminder LaunchAgent is still loaded; its app path was not changed.")

    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    definition = {
        "Label": agent_label,
        "ProgramArguments": reminder_program_arguments(app_path),
        "StartCalendarInterval": {"Weekday": weekday, "Hour": int(clock[:2]), "Minute": int(clock[3:])},
        "RunAtLoad": False,
        "ProcessType": "Background",
        "AssociatedBundleIdentifiers": [bundle_id],
    }
    fd, temporary = tempfile.mkstemp(prefix=".igloo-remind-", dir=folder)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(plistlib.dumps(definition))
        os.replace(temporary, plist)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    result = subprocess.run(["/bin/launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)],
                           capture_output=True)
    if result.returncode:
        fail("The reminder LaunchAgent could not load. Its file is saved; check the current GUI login session.")
    return {"message": "Quiet-project reminder enabled."}

def delete(task_id):
    get(task_id)
    if task_id in schedules():fail("Disable this task's schedule before deleting it.")
    value=config();value["tasks"]=[v for v in value["tasks"] if v["id"]!=task_id];O.save(task_path(),value)
    return {"message":"Task deleted. Run history was kept."}

def dispatch(command,args):
    if command=="task-list":return listing()
    if command=="task-plan":
        plan=O.load(planning.location(args[0]))
        if not plan or not plan.get("task"):fail("Task review not found.")
        return plan
    if command=="task-save":return save(json.loads(args[0]))
    if command=="task-delete":return delete(args[0])
    if command=="task-history":return history(args[0] if args else None)
    if command=="task-install-cli":return install_cli()
    if command=="task-schedule":return schedule_task(args[0],json.loads(args[1]))
    if command=="task-unschedule":return unschedule(args[0])
    if command=="remind-check":return remind_check()
    if command=="remind-agent":return configure_reminder(json.loads(args[0]))
    if command=="task-run":return run_task(args[0],apply=len(args)>1 and args[1]=="apply")
    if command=="task-apply":return apply_plan(args[0])
    fail("Unknown task command.")
