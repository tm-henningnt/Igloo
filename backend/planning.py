"""Policy suggestions and one-acceptance batch execution using existing guarded actions."""
import json
from pathlib import Path
import re
import uuid

O = None
ACTIONS = {"keep", "cleanup", "archive", "freeze"}

def configure(organizer):
    global O
    O = organizer

def defaults():
    return {"cacheDays": 30, "freezeDays": 180, "ignoreActive": True, "ageBasis": "git",
            "cacheKinds": sorted(O.CACHE_KINDS)}

def rules(value):
    if not isinstance(value, dict):
        raise O.Refusal("Choose wizard rules first.")
    result = dict(defaults(), **value)
    if any(type(result[k]) is not int or not 1 <= result[k] <= 3650 for k in ("cacheDays", "freezeDays")):
        raise O.Refusal("Age thresholds must be whole days between 1 and 3650.")
    if result["freezeDays"] <= result["cacheDays"]:
        raise O.Refusal("The freeze threshold must be later than the cleanup threshold.")
    if type(result["ignoreActive"]) is not bool or result["ageBasis"] not in {"git", "files"}:
        raise O.Refusal("Choose a valid activity policy and age measure.")
    kinds = result["cacheKinds"]
    if not isinstance(kinds, list) or any(not isinstance(k, str) or k not in O.CACHE_KINDS for k in kinds):
        raise O.Refusal("Choose recognized cache and dependency types.")
    result["cacheKinds"] = sorted(set(kinds))
    return result

def location(identifier):
    if not re.fullmatch(r"[0-9a-f]{32}", identifier):
        raise O.Refusal("Invalid wizard review.")
    return O.STATE / "wizard" / (identifier + ".json")

def context(plan):
    if plan.get("root") != str(O.ROOT) or plan.get("vault") != str(O.VAULT):
        raise O.Refusal("Workspace or Freezer storage changed. Build a new wizard review.")

def rules_path():
    return O.preferences.config_path().with_name("wizard.json")

def state():
    return {"rules": rules(O.load(rules_path(), defaults())),
            "cacheKinds": sorted(O.CACHE_KINDS),
            "last": O.load(O.STATE / "wizard-last.json")}

def suggest(snapshot, policy):
    policy = rules(policy)
    rows = []
    for p in snapshot["projects"]:
        age = p.get("ageDays" if policy["ageBasis"] == "git" else "fileAgeDays")
        caches = [c for c in p["candidates"] if c["kind"] in policy["cacheKinds"] and c["eligible"]]
        action, reason = "keep", "Below the cleanup threshold."
        if p.get("hold"):
            reason = "On hold."
        elif O.under(O.APP, Path(p["path"])):
            reason = "The running organizer is protected."
        elif not p.get("activityKnown"):
            reason = "Activity information is unavailable; kept for review."
        elif p.get("active") and policy["ignoreActive"]:
            reason = "Active project excluded."
        elif age is None:
            reason = "Age is unknown; no automatic action proposed."
        elif age >= policy["freezeDays"]:
            if not p["git"]:
                reason = "Old enough to freeze, but no root Git repository is available."
            else:
                action, reason = "freeze", "Past the freeze threshold. Preserve local files, push reviewed source, then remove verified checkouts."
                if p["changes"]:
                    reason = "Freeze proposed; commit or resolve local source changes before execution."
                elif not p["remotes"]:
                    reason = "Freeze proposed; connect a remote before execution."
        elif age >= policy["cacheDays"]:
            action, reason = ("cleanup", "In the cleanup age band.") if caches else ("keep", "In the cleanup age band, but no eligible selected cache types were found.")
        if p.get("active") and action != "keep":
            reason += " Running project processes must close before changes."
        rows.append({"id": p["path"], "path": p["path"], "name": p["name"], "ageDays": age,
                     "action": action, "reason": reason, "active": p["active"], "activityKnown": p["activityKnown"],
                     "size": p["size"], "cacheSize": sum(c["size"] for c in caches), "cacheCount": len(caches),
                     "changedCount": len(p["changes"]), "localCount": len(p["localFiles"]),
                     "branchCount": p["uncovered"] + p["unknown"], "stashCount": len(p["stashes"]),
                     "remotes": p["remotes"], "held": p.get("hold", False),
                     "remote": next((r["name"] for r in p["remotes"] if r["name"] == "origin"), p["remotes"][0]["name"] if p["remotes"] else "")})
    return sorted(rows, key=lambda r: (r["action"] == "keep", -r["size"], r["path"]))

def scan(policy):
    policy = rules(policy)
    rules_path().parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    O.save(rules_path(), policy)
    snapshot = O.scan()
    return {"rules": policy, "generated": snapshot["generated"], "rows": suggest(O.snapshot(), policy),
            "scanIssues": len(snapshot.get("errors", []))}

def persist(plan):
    update_outcome(plan)
    O.save(location(plan["id"]), plan)
    O.save(O.STATE / "wizard-last.json", plan)

def update_outcome(plan):
    paths = {r["path"] for r in plan["rows"]}
    held = [h for h in plan.get("held", []) if h["path"] not in paths]
    held += [{"path": r["path"], "reason": r["message"]} for r in plan["rows"]
             if r["status"] in {"blocked", "failed", "partial"}]
    observed = plan["status"] != "review"
    if observed:
        outcomes = [r.get("outcome") for r in plan["rows"]]
    else:
        outcomes = [(r.get("cleanup") or r.get("freeze") or {}).get("outcome")
                    for r in plan["rows"] if r["status"] == "ready"]
    summary = O.combine_outcomes(outcomes, held=held, observed=observed)
    if observed:
        summary["bytesFreedEstimate"] = plan.get("outcome", {}).get("bytesFreedEstimate", 0)
        summary["bytesUsedEstimate"] = plan.get("outcome", {}).get("bytesUsedEstimate", 0)
    plan["held"], plan["outcome"] = held, summary

def prepare(request):
    policy = rules(request["rules"])
    snapshot = O.snapshot()
    known = {p["path"]: p for p in snapshot["projects"]}
    rows, seen = [], set()
    life = O.lifecycle_module()
    for choice in request["choices"]:
        path, action = choice.get("path"), choice.get("action")
        if path not in known or action not in ACTIONS or path in seen:
            raise O.Refusal("A project selection is invalid or duplicated. Scan again.")
        seen.add(path)
        if action == "keep":
            continue
        p = known[path]
        row = {"id": path, "path": path, "name": p["name"], "action": action, "status": "blocked",
               "message": "", "cleanup": None, "freeze": None}
        O.progress("Preparing " + p["name"] + "…")
        try:
            if p.get("hold"):
                raise O.Refusal("On hold.")
            if action == "cleanup":
                candidates = [c["path"] for c in p["candidates"]
                              if c["kind"] in policy["cacheKinds"] and c["eligible"]]
                review = O.preview_cleanup(candidates)
                row["cleanup"] = review
                if not review["items"]:
                    raise O.Refusal("No selected cache directories passed fresh validation.")
            else:
                remote = choice.get("remote", "")
                if not any(r["name"] == remote for r in p["remotes"]):
                    raise O.Refusal("Choose this project's publication remote.")
                row["freeze"] = life.preview_freeze(path, remote, "freeze" if action == "freeze" else "preserve")
            row.update(status="ready", message="Ready for your approval.")
        except O.Refusal as e:
            row["message"] = str(e)
        except (OSError, ValueError):
            row["message"] = "Preparation failed. Check permissions and build a new review."
        rows.append(row)
    # One project can contain another project or a registered worktree. Avoid
    # accepting two operations whose filesystem footprints overlap.
    def footprint(row):
        return [i["path"] for i in row["cleanup"]["items"]] if row["cleanup"] else row["freeze"]["checkouts"] if row["freeze"] else []
    for i, row in enumerate(rows):
        if row["status"] != "ready":
            continue
        for other in rows[:i]:
            if other["status"] != "ready":
                continue
            if any(O.under(Path(a), Path(b)) or O.under(Path(b), Path(a))
                   for a in footprint(row) for b in footprint(other)):
                row.update(status="blocked", message="Overlaps another selected operation. Keep one and rebuild the review.")
                break
    plan = {"id": uuid.uuid4().hex, "created": O.now(), "root": str(O.ROOT), "vault": str(O.VAULT),
            "rules": policy, "status": "review", "rows": rows,
            "message": "Only ready rows will run. Blocked rows stay untouched.",
            "held": [{"path": r["path"], "reason": r["message"]} for r in rows if r["status"] == "blocked"]}
    persist(plan)
    return plan

def execute(identifier):
    plan = O.load(location(identifier))
    if not plan:
        raise O.Refusal("The wizard review is missing.")
    context(plan)
    if plan["status"] != "review":
        raise O.Refusal("This review was already accepted. Review its results and build a new review for remaining work.")
    # Expiry applies to acceptance. Each action gets a fresh comparison against the
    # exact accepted state when its turn arrives, including in a long-running batch.
    if O.age_seconds(plan["created"]) > 600:
        raise O.Refusal("This wizard review expired. Prepare a fresh review.")
    plan.update(status="running", acceptedAt=O.now())
    persist(plan)
    life = O.lifecycle_module()
    for row in plan["rows"]:
        if row["status"] != "ready":
            continue
        if O.on_hold(row["path"]):
            row.update(status="blocked", message="On hold.")
            persist(plan)
            continue
        row.update(status="running", message="Checking accepted state…")
        persist(plan)
        action_name = {"cleanup": "Clean up", "archive": "Preserve", "freeze": "Freeze"}[row["action"]]
        O.progress("Running " + action_name + " for " + row["name"] + "…")
        try:
            if row["action"] == "cleanup":
                original = row["cleanup"]
                fresh = O.preview_cleanup([i["path"] for i in original["items"]])
                fields = ("path", "owner", "device", "inode", "mtime", "fingerprint", "size")
                def signatures(review):
                    return sorted(tuple(item[k] for k in fields) for item in review["items"])
                if fresh["blocked"] or signatures(fresh) != signatures(original):
                    raise O.Refusal("Cache contents changed since acceptance. Nothing in this row was moved; prepare again.")
                reply = O.execute_cleanup(fresh["id"])
                row["results"] = reply["results"]
                row["outcome"] = reply["outcome"]
                row["status"] = "done" if all(r["ok"] for r in reply["results"]) else "partial"
                row["message"] = reply["message"]
            else:
                original = O.load(life.plan_path("freeze", row["freeze"]["id"]))
                if not original:
                    raise O.Refusal("The accepted freeze review is missing.")
                life.current_matches(original)
                mode = "freeze" if row["action"] == "freeze" else "preserve"
                if original.get("mode", "preserve") != mode:
                    raise O.Refusal("The action mode changed since this review. Prepare a new review.")
                fresh = life.preview_freeze(row["path"], original["remote"], mode)
                captured = O.load(life.plan_path("freeze", fresh["id"]))
                if any(life.stable(captured[k]) != life.stable(original[k])
                       for k in ("capture", "publish", "localRefs", "roots", "destination")):
                    raise O.Refusal("The freeze scope changed. Prepare a new review.")
                row["archiveID"] = fresh["id"]
                row["message"] = "Preserving local files and publishing reviewed refs…"
                persist(plan)
                result = life.execute_freeze(fresh["id"], mode)
                row["outcome"] = result["outcome"]
                row["message"] = result["message"]
                row["state"] = result["state"]
                row["status"] = "done" if result["state"] == ("frozen" if mode == "freeze" else "ready") else "partial"
        except O.Refusal as e:
            row.update(status="failed", message=str(e))
            if hasattr(e, "outcome"):
                row["outcome"] = e.outcome
        except Exception as e:
            row.update(status="failed", message="The action stopped. Inspect its archive or Trash history before making another review.")
            if hasattr(e, "outcome"):
                row["outcome"] = e.outcome
        persist(plan)
    plan.update(status="finished", finishedAt=O.now(),
                message="Review finished. Review each result. Changed or blocked projects were held; completed actions remain recorded.")
    persist(plan)
    O.progress("Refreshing the catalog after the review…")
    try:
        O.scan()
    except Exception:
        plan["message"] += " Catalog refresh failed; run Analyse Projects. Action results are preserved."
        persist(plan)
    return plan

def dispatch(command, args):
    if command == "wizard-state": return state()
    if command == "wizard-scan": return scan(json.loads(args[0]))
    if command == "wizard-prepare": return prepare(json.loads(args[0]))
    if command == "wizard-execute": return execute(args[0])
    raise O.Refusal("Unknown wizard action.")
