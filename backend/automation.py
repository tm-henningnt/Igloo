"""Saved cache-maintenance tasks, durable runs and user LaunchAgents."""
import hashlib
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
            "task-install-cli", "task-schedule", "task-unschedule", "task-plan"}
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

def schedules():
    return O.load(O.STATE / "task-schedules.json", {})

def label(task_id):
    return "local.igloo.task." + identifier(task_id)

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
    cli = Path.home() / ".local/bin/igloo"
    return {"tasks": sorted(tasks, key=lambda t:t["definition"]["name"].casefold()), "cliPath": str(cli),
            "cliInstalled": our_cli(cli)}

def new_run(task, kind):
    return {"id": uuid.uuid4().hex, "taskID": task["id"], "taskName": task["name"], "started": O.now(),
            "status": "running", "kind": kind, "message": "Starting…", "planID": None,
            "ready": 0, "blocked": 0, "cacheBytes": 0}

def record(run):
    O.save(O.STATE / "task-runs" / run["taskID"] / (run["id"] + ".json"), run)

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
            result = planning.execute(plan["id"])
            run["outcome"] = result["outcome"]
            run["blocked"] = len(result["held"])
            bad = any(r["status"] in {"failed","partial","running"} for r in result["rows"])
            run.update(status="partial" if bad or run["blocked"] else "completed",
                       message="Task actions finished. Review held or failed rows in its saved review.")
        elif apply:
            run["outcome"] = O.combine_outcomes([], held=plan["held"], observed=True)
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
        result = planning.execute(plan_id)
        run["outcome"] = result["outcome"]
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
    cli = Path.home()/".local/bin/igloo"
    cli.parent.mkdir(parents=True, exist_ok=True)
    if cli.is_symlink() or cli.exists() and not our_cli(cli):
        fail("A different igloo executable already exists; it was not replaced.")
    script = "#!/bin/sh\n# Igloo CLI\nexec " + shlex.quote(str(python)) + " -E -s -B " + shlex.quote(str(runtime/"task_cli.py")) + ' "$@"\n'
    fd, temporary = tempfile.mkstemp(prefix=".igloo-", dir=cli.parent)
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
    logs=O.STATE/"task-logs";logs.mkdir(mode=0o700,parents=True,exist_ok=True)
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
    registered=schedules();registered.pop(task_id,None);O.save(O.STATE/"task-schedules.json",registered)
    result=subprocess.run(["/bin/launchctl","bootstrap",f"gui/{os.getuid()}",str(plist)],capture_output=True)
    if result.returncode:fail("The LaunchAgent could not load. Its file is saved; check the current GUI login session.")
    registered[task_id]={"frequency":value["frequency"],"time":value["time"],"weekday":value.get("weekday",0),
                         "policyHash":policy_hash(task),"label":label(task_id),"plist":str(plist)}
    O.save(O.STATE/"task-schedules.json",registered)
    return {"message":"Schedule enabled. It runs while this user is logged in; no task was started now."}

def unschedule(task_id):
    identifier(task_id)
    result=subprocess.run(["/bin/launchctl","bootout",f"gui/{os.getuid()}/"+label(task_id)],capture_output=True)
    if result.returncode and loaded(task_id):
        fail("The LaunchAgent is still loaded; it was not removed.")
    plist=Path.home()/"Library/LaunchAgents"/(label(task_id)+".plist")
    if plist.is_symlink():fail("The LaunchAgent path is a symbolic link.")
    if plist.exists():plist.unlink()
    value=schedules();value.pop(task_id,None);O.save(O.STATE/"task-schedules.json",value)
    return {"message":"Schedule disabled. Run history and task definition were preserved."}

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
    if command=="task-run":return run_task(args[0],apply=len(args)>1 and args[1]=="apply")
    if command=="task-apply":return apply_plan(args[0])
    fail("Unknown task command.")
