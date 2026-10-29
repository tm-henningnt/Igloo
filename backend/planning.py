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
            "last": O.load(O.STATE / "wizard-last.json"),
            "thawLast": O.load(thaw_last_path())}

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

def execute(identifier, run_id=None, run_kind="wizard"):
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
    space_run_id = run_id or identifier
    mirror_paths = [Path("plans") / (identifier + ".json")]
    plan["spaceRunID"] = space_run_id
    plan.update(status="running", acceptedAt=O.now())
    persist(plan)
    O.record_space_run(space_run_id, run_kind, started=plan.get("created"), status="running",
                       label="Wizard" if run_kind == "wizard" else plan.get("task", {}).get("name", "Task"),
                       estimate_bytes=plan.get("outcome", {}).get("bytesFreedEstimate", 0),
                       mirror_paths=mirror_paths)
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
                reply = O.execute_cleanup(fresh["id"], run_id=space_run_id, run_kind=run_kind)
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
                result = life.execute_freeze(fresh["id"], mode, run_id=space_run_id, run_kind=run_kind)
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
        O.record_space_run(space_run_id, run_kind, started=plan.get("created"), status="running",
                           label="Wizard" if run_kind == "wizard" else plan.get("task", {}).get("name", "Task"),
                           estimate_bytes=plan.get("outcome", {}).get("bytesFreedEstimate", 0),
                           observed=plan.get("outcome", {}).get("observedSpace"), mirror_paths=mirror_paths)
    plan.update(status="finished", finishedAt=O.now(),
                message="Wizard actions completed. Changed or blocked projects were held; completed actions remain recorded.")
    persist(plan)
    final_status = "partial" if plan.get("held") or any(row["status"] in {"failed", "partial", "running", "blocked"}
                                                          for row in plan["rows"]) else "completed"
    O.record_space_run(space_run_id, run_kind, started=plan.get("created"), status=final_status,
                       label="Wizard" if run_kind == "wizard" else plan.get("task", {}).get("name", "Task"),
                       estimate_bytes=plan.get("outcome", {}).get("bytesFreedEstimate", 0),
                       observed=plan.get("outcome", {}).get("observedSpace"), mirror_paths=mirror_paths)
    O.progress("Refreshing the catalog after the review…")
    try:
        O.scan()
    except Exception:
        plan["message"] += " Catalog refresh failed; run Analyze Projects. Action results are preserved."
        persist(plan)
    return plan


def thaw_location(identifier):
    if not re.fullmatch(r"[0-9a-f]{32}", identifier):
        raise O.Refusal("Invalid Thaw plan.")
    return O.STATE / "wizard-thaw" / (identifier + ".json")


def thaw_last_path():
    return O.STATE / "wizard-thaw-last.json"


def thaw_path_state(path, identifier, primary):
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        return {"path": str(path), "exists": False}
    try:
        info = path.lstat()
    except OSError:
        return {"path": str(path), "exists": True, "unavailable": True}
    life = O.lifecycle_module()
    frozen_marker = str(path) == primary and life.is_stub(path, identifier)
    state = {"path": str(path), "exists": True, "device": info.st_dev, "inode": info.st_ino,
             "mode": info.st_mode, "size": info.st_size, "mtime": info.st_mtime_ns,
             "symlink": path.is_symlink(), "frozenMarker": frozen_marker}
    if frozen_marker:
        marker = path / life.MARKER
        marker_info = marker.lstat()
        state["marker"] = {"device": marker_info.st_dev, "inode": marker_info.st_ino,
                           "size": marker_info.st_size, "mtime": marker_info.st_mtime_ns,
                           "fingerprint": life.stable(O.load(marker, {}))}
    return state


def thaw_destinations(manifest, identifier):
    primary = manifest["path"]
    return [thaw_path_state(path, identifier, primary)
            for path in sorted(set(manifest["roots"]))]


def thaw_blocker(manifest, identifier):
    primary = manifest["path"]
    life = O.lifecycle_module()
    for root in manifest["roots"]:
        path = Path(root)
        exists = path.exists() or path.is_symlink()
        if exists and not (str(path) == primary and life.is_stub(path, identifier)):
            return "The original path is occupied. Inspect its contents in Finder before Thaw."
        if not path.parent.is_dir():
            return "The original parent folder is missing. Use Show path in Finder to inspect its location."
    return ""


def thaw_details(manifest):
    refs, local_files = [], []
    for role in manifest["capture"]["roles"]:
        branch = role.get("branch")
        refs.append({"path": role["path"], "ref": "refs/heads/" + branch if branch else "detached",
                     "sha": role["head"]})
        local_files.extend(str(Path(role["path"]) / name) for name, entry in role["entries"].items()
                            if entry.get("local", True))
    return refs, sorted(set(local_files))


def thaw_manifest_signature(manifest):
    life = O.lifecycle_module()
    fields = {key: manifest.get(key) for key in ("id", "state", "path", "roots", "destination", "capture", "checksums")}
    return life.stable(fields)


def thaw_remote_ready(location, destination):
    life = O.lifecycle_module()
    try:
        safe = life.safe_destination(destination)
    except (O.Refusal, OSError, ValueError, TypeError):
        return False
    result, _ = life.git(location, "ls-remote", safe, timeout=30)
    return result == 0


def thaw_row(identifier):
    life = O.lifecycle_module()
    row = {"id": identifier, "archiveID": identifier, "name": "Frozen project", "path": "",
           "status": "blocked", "message": "This Freezer entry could not be verified.",
           "destination": "", "refs": [], "checkouts": [], "localFiles": [],
           "restoredRoots": [], "archiveAttempted": False,
           "archiveLocation": "", "checksums": {}, "manifestFingerprint": "", "destinationStates": []}
    try:
        location, manifest = life.archive_record(identifier)
        row.update(name=manifest.get("name", "Frozen project"), path=manifest["path"],
                   archiveLocation=str(location), state=manifest.get("state", ""),
                   restoredRoots=list(manifest.get("restoredRoots", [])))
        refs, local_files = thaw_details(manifest)
        row.update(refs=refs, checkouts=[role["path"] for role in manifest["capture"]["roles"]],
                   localFiles=local_files, checksums=dict(manifest.get("checksums", {})),
                   manifestFingerprint=thaw_manifest_signature(manifest),
                   destinationStates=thaw_destinations(manifest, identifier))
        try:
            row["destination"] = life.safe_destination(manifest.get("destination", ""))
        except (O.Refusal, OSError, ValueError, TypeError):
            row["destination"] = "Unavailable"
        if manifest.get("state") == "rehydrating":
            row["message"] = "A previous Thaw was interrupted. Use Open Freezer to inspect restored roots and finish this project."
            return row
        if manifest.get("state") != "frozen":
            row["message"] = "This project is no longer Frozen. Its Freezer entry changed after you selected it. Use Open Freezer to inspect it."
            return row
        life.verify_archives(location, manifest)
        destination = life.safe_destination(manifest["destination"])
        row["destination"] = destination
        blocker = thaw_blocker(manifest, identifier)
        if blocker:
            row["message"] = blocker
            return row
        if not thaw_remote_ready(location, destination):
            row["message"] = "The remote is unreachable. Restore remote access, then use Check remote again."
            return row
        row.update(status="ready", message="Ready. The remote is reachable; the verified Freezer archive copy is the recovery source for Frozen refs missing from it.")
    except O.Refusal as error:
        message = str(error).lower()
        if "archive" in message or "checksum" in message:
            row["message"] = "The archive failed its checksum check. Keep the Freezer copy and use Open Freezer to inspect it."
        elif "destination" in message or "remote" in message:
            row["message"] = "The remote is unavailable or invalid. Restore remote access, then use Check remote again."
    except Exception:
        row["message"] = "The archive could not be verified. Keep the Freezer copy and use Open Freezer to inspect it."
    return row


def update_thaw_outcome(plan):
    ready = [row for row in plan["rows"] if row["status"] == "ready"]
    if plan["status"] == "review":
        checkouts_restored = [path for row in ready for path in row["checkouts"]]
        archives_used = [row["archiveLocation"] for row in ready]
    else:
        checkouts_restored = [path for row in plan["rows"]
                              for path in thaw_restored_checkouts(row)]
        archives_used = [row["archiveLocation"] for row in plan["rows"]
                         if row["status"] == "done" or row.get("archiveAttempted")]
    held = [{"path": row["path"], "reason": row["message"]} for row in plan["rows"]
            if row["status"] in {"blocked", "failed", "partial", "running"}]
    refs_restored = []
    for row in plan["rows"]:
        restored_roots = row["checkouts"] if row["status"] == "done" else row.get("restoredRoots", [])
        for ref in row.get("refs", []):
            if any(O.under(Path(ref["path"]), Path(root)) for root in restored_roots):
                refs_restored.append({key: ref[key] for key in ("path", "ref", "sha")})
    plan["held"] = held
    plan["outcome"] = O.outcome_summary(
        checkouts_restored=checkouts_restored, archives_used=archives_used,
        archives_used_count=len(archives_used), refs_restored=refs_restored,
        held=held, observed=plan["status"] != "review")


def thaw_restored_checkouts(row):
    if row["status"] == "done":
        return row["checkouts"]
    restored_roots = row.get("restoredRoots", [])
    return [checkout for checkout in row["checkouts"]
            if any(O.under(Path(checkout), Path(root)) for root in restored_roots)]


def record_thaw_restored_roots(row):
    life = O.lifecycle_module()
    try:
        _, manifest = life.archive_record(row["archiveID"])
        roots = set(manifest.get("roots", []))
        restored = manifest.get("restoredRoots", []) if manifest.get("state") == "rehydrating" else []
        row["restoredRoots"] = sorted({path for path in restored if path in roots})
    except Exception:
        row.setdefault("restoredRoots", [])
    return row["restoredRoots"]


def fail_thaw_row(row, message):
    row.update(status="failed", message=message)
    restored = record_thaw_restored_roots(row)
    if restored:
        row["message"] += " Already restored roots: " + ", ".join(restored) + ". Use Open Freezer to finish this project."


def persist_thaw(plan):
    update_thaw_outcome(plan)
    O.save(thaw_location(plan["id"]), plan)
    O.save(thaw_last_path(), plan)


def prepare_thaw(archive_ids):
    if not isinstance(archive_ids, list) or not archive_ids:
        raise O.Refusal("Select one or more Frozen projects first.")
    if any(not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{32}", identifier)
           for identifier in archive_ids) or len(set(archive_ids)) != len(archive_ids):
        raise O.Refusal("The Frozen project selection is invalid or duplicated. Select the projects again.")
    rows = [thaw_row(identifier) for identifier in archive_ids]
    plan = {"id": uuid.uuid4().hex, "created": O.now(), "root": str(O.ROOT), "vault": str(O.VAULT),
            "status": "review", "rows": rows, "message": "Only ready projects will run. Blocked projects stay untouched."}
    update_thaw_outcome(plan)
    # Projects that restore into overlapping destinations cannot share one acceptance.
    for index, row in enumerate(rows):
        if row["status"] != "ready":
            continue
        for other in rows[:index]:
            if other["status"] != "ready":
                continue
            if any(O.under(Path(left), Path(right)) or O.under(Path(right), Path(left))
                   for left in row["checkouts"] for right in other["checkouts"]):
                row.update(status="blocked", message="Overlaps another selected Thaw destination. Use Change selection to keep one project.")
                break
    persist_thaw(plan)
    return plan


def thaw_review_matches(row):
    life = O.lifecycle_module()
    location, manifest = life.archive_record(row["archiveID"])
    if manifest.get("state") != "frozen":
        return False
    life.verify_archives(location, manifest)
    if manifest.get("destination") != row["destination"] or manifest.get("checksums") != row["checksums"]:
        return False
    if thaw_manifest_signature(manifest) != row["manifestFingerprint"]:
        return False
    return thaw_destinations(manifest, row["archiveID"]) == row["destinationStates"]


def execute_thaw(identifier):
    plan = O.load(thaw_location(identifier))
    if not plan:
        raise O.Refusal("The Thaw plan is missing.")
    context(plan)
    if plan["status"] != "review":
        raise O.Refusal("This Thaw plan was already accepted. Inspect its results and prepare a new Thaw plan for remaining work.")
    if O.age_seconds(plan["created"]) > 600:
        raise O.Refusal("This Thaw plan expired. Prepare a fresh Thaw plan.")
    # Reject a stale batch before changing any project. Remote reachability is
    # checked per row after acceptance so one remote failure does not stop peers.
    try:
        for row in plan["rows"]:
            if row["status"] == "ready" and not thaw_review_matches(row):
                plan.update(status="stale", finishedAt=O.now(),
                            message="The Thaw plan is stale. No project was changed; prepare a fresh Thaw plan.")
                row.update(status="blocked", message="The accepted archive or destination changed. Prepare a fresh Thaw plan.")
                for other in plan["rows"]:
                    if other["status"] == "ready":
                        other.update(status="blocked", message="Not run because the accepted Thaw plan became stale.")
                persist_thaw(plan)
                raise O.Refusal(plan["message"])
    except Exception as error:
        if plan["status"] != "stale":
            plan.update(status="stale", finishedAt=O.now(),
                        message="The Thaw plan is stale. No project was changed; prepare a fresh Thaw plan.")
            for row in plan["rows"]:
                if row["status"] == "ready":
                    row.update(status="blocked", message="Not run because the accepted Thaw plan became stale.")
            persist_thaw(plan)
        raise O.Refusal(plan["message"]) from error

    plan.update(status="running", acceptedAt=O.now(),
                message="Thawing every ready project. Each result is recorded separately.")
    persist_thaw(plan)
    life = O.lifecycle_module()
    for row in plan["rows"]:
        if row["status"] != "ready":
            continue
        row.update(status="running", message="Checking the remote and accepted destination state…")
        persist_thaw(plan)
        O.progress("Thawing " + row["name"] + "…")
        try:
            location, manifest = life.archive_record(row["archiveID"])
            if not thaw_review_matches(row):
                raise O.Refusal("The accepted archive or destination changed. Prepare a fresh Thaw plan.")
            if not thaw_remote_ready(location, row["destination"]):
                raise O.Refusal("The remote is unreachable. The archive is intact; restore remote access, then use Check remote again.")
            row["archiveAttempted"] = True
            persist_thaw(plan)
            reply = life.rehydrate(row["archiveID"], offline=False)
            row.update(status="done", restoredRoots=list(row["checkouts"]),
                       message=reply["message"] + " Checkout and remote refs were restored. Dependencies remain uninstalled.")
        except O.Refusal as error:
            fail_thaw_row(row, str(error))
        except Exception:
            fail_thaw_row(row, "Thaw stopped for this project. Use Open Freezer to inspect its state before preparing another Thaw plan.")
        except KeyboardInterrupt:
            row["message"] = "Thaw was interrupted. Use Open Freezer to inspect this project before continuing."
            restored = record_thaw_restored_roots(row)
            if restored:
                row["message"] += " Already restored roots: " + ", ".join(restored) + ". Use Freezer recovery to finish this project."
            persist_thaw(plan)
            raise
        persist_thaw(plan)
    plan.update(status="finished", finishedAt=O.now(),
                message="Thaw completed. Inspect each project result; completed projects remain restored.")
    persist_thaw(plan)
    O.progress("Refreshing the catalog after Thaw…")
    try:
        O.scan()
    except Exception:
        plan["message"] += " Catalog refresh failed; run Analyze Projects. Thaw results are preserved."
        persist_thaw(plan)
    return plan

def dispatch(command, args):
    if command == "wizard-state": return state()
    if command == "wizard-scan": return scan(json.loads(args[0]))
    if command == "wizard-prepare": return prepare(json.loads(args[0]))
    if command == "wizard-execute": return execute(args[0])
    if command == "wizard-thaw-prepare": return prepare_thaw(json.loads(args[0]))
    if command == "wizard-thaw-execute": return execute_thaw(args[0])
    raise O.Refusal("Unknown wizard action.")
