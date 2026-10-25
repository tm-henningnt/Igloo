#!/usr/bin/env python3
"""Human-friendly CLI for Igloo saved tasks."""
import argparse
import fcntl
import json
from pathlib import Path
import sys
import organizer as O
import automation as tasks

def parser():
    p=argparse.ArgumentParser(prog="igloo",description="Plan, execute and schedule saved project-maintenance tasks.")
    sub=p.add_subparsers(dest="command",required=True)
    for name in ("tasks","install-cli"):
        q=sub.add_parser(name);q.add_argument("--json",action="store_true")
    new=sub.add_parser("create",help="Save a task; this never enables a schedule.")
    new.add_argument("id");new.add_argument("--name");new.add_argument("--mode",choices=["review","cleanup"],default="review")
    new.add_argument("--cache-days",type=int,default=30);new.add_argument("--freeze-days",type=int,default=180)
    new.add_argument("--age",choices=["git","files"],default="git")
    new.add_argument("--cache-kind",action="append")
    new.add_argument("--include",action="append",default=[]);new.add_argument("--exclude",action="append",default=[])
    new.add_argument("--type",action="append",default=[],dest="types")
    new.add_argument("--max-projects",type=int,default=5);new.add_argument("--max-gib",type=float,default=10)
    new.add_argument("--allow-unattended",action="store_true")
    new.add_argument("--json",action="store_true")
    for name in ("show","delete","plan","run","unschedule"):
        q=sub.add_parser(name);q.add_argument("id");q.add_argument("--json",action="store_true")
        if name=="run":
            q.add_argument("--apply",action="store_true",help="Execute fresh matching cache cleanup under this task's saved limits.")
            q.add_argument("--scheduled",action="store_true",help=argparse.SUPPRESS)
            q.add_argument("--policy-hash",default="",help=argparse.SUPPRESS)
    q=sub.add_parser("apply",help="Accept an existing reviewed task plan.")
    q.add_argument("plan");q.add_argument("--accept",action="store_true",required=True);q.add_argument("--json",action="store_true")
    q=sub.add_parser("history");q.add_argument("id",nargs="?");q.add_argument("--json",action="store_true")
    q=sub.add_parser("schedule");q.add_argument("id")
    q.add_argument("--frequency",choices=["daily","weekly"],default="weekly")
    q.add_argument("--day",choices=["sun","mon","tue","wed","thu","fri","sat"],default="sun")
    q.add_argument("--at",default="09:00");q.add_argument("--json",action="store_true")
    return p

def dispatch(a):
    if a.command=="tasks":return tasks.listing()
    if a.command=="show":return tasks.get(a.id)
    if a.command=="install-cli":return tasks.install_cli()
    if a.command=="create":
        return tasks.save({"id":a.id,"name":a.name or a.id.replace("-"," ").title(),"root":str(O.ROOT),"vault":str(O.VAULT),
             "mode":a.mode,"rules":dict(tasks.planning.defaults(),cacheDays=a.cache_days,freezeDays=a.freeze_days,
                                      ageBasis=a.age,cacheKinds=a.cache_kind or sorted(O.CACHE_KINDS)),
             "include":a.include,"exclude":a.exclude,"projectTypes":a.types,"maxProjects":a.max_projects,
             "maxBytes":int(a.max_gib*1024**3),"unattended":a.allow_unattended})
    if a.command in {"plan","run"}:
        return tasks.run_task(a.id,apply=getattr(a,"apply",False),scheduled=getattr(a,"scheduled",False),expected_hash=getattr(a,"policy_hash",""))
    if a.command=="apply":return tasks.apply_plan(a.plan)
    if a.command=="history":return tasks.history(a.id)
    if a.command=="schedule":return tasks.schedule_task(a.id,{"frequency":a.frequency,"time":a.at,"weekday":["sun","mon","tue","wed","thu","fri","sat"].index(a.day)})
    if a.command=="unschedule":return tasks.unschedule(a.id)
    if a.command=="delete":return tasks.delete(a.id)
    raise O.Refusal("Unknown command.")

def main():
    a=parser().parse_args()
    tasks.configure(O)
    O.STATE.mkdir(mode=0o700,parents=True,exist_ok=True)
    try:
        with (O.STATE/"backend.lock").open("a") as lock:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                if a.command=="run":result=tasks.record_busy(a.id)
                else:raise O.Refusal("Another analysis or action is running. Try again when it finishes.")
                emit(a,result);return 75
            result=dispatch(a)
        emit(a,result)
        return 1 if result.get("status") in {"failed","partial","held"} else 0
    except O.Refusal as e:
        emit(a,{"error":str(e)});return 1
    except Exception:
        emit(a,{"error":"The command stopped. Existing project files and history were preserved."});return 1

def emit(a,value):
    if getattr(a,"json",False):
        print(json.dumps(value));return
    if "error" in value:print("Stopped: "+value["error"],file=sys.stderr)
    elif "tasks" in value:
        for item in value["tasks"]:
            t=item["definition"];s=item["schedule"]
            print(t["id"]+"  "+t["name"]+"  ["+t["mode"]+"]"+("  schedule: "+("needs re-enabling" if s["stale"] else s["frequency"]+" "+s["time"]) if s else ""))
        if not value["tasks"]:print("No saved tasks. Use igloo create --help.")
    elif "runs" in value:
        for run in value["runs"]:print(run["started"][:19]+"  "+run["taskID"]+"  "+run["status"]+"  "+run["message"])
    elif "status" in value:
        print(value["taskName"]+": "+value["status"]+"\n"+value["message"])
        if value.get("planID"):print("Plan: "+value["planID"])
        print(str(value["ready"])+" ready, "+str(value["blocked"])+" held")
    elif "message" in value:print(value["message"])
    else:print(json.dumps(value,indent=2))

if __name__=="__main__":raise SystemExit(main())
