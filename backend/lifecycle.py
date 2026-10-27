"""Reviewed Git setup, commits and a local freeze-dry/rehydrate vault."""
from __future__ import annotations
import hashlib
import ctypes
import sys
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import uuid
import preferences

O = None
REBUILD = {"node_modules", ".venv", "venv", "__pycache__", ".turbo", ".next", ".nuxt",
           ".svelte-kit", ".parcel-cache", ".pytest_cache", ".mypy_cache", ".ruff_cache",
           ".build", "build", "dist", "target", "coverage", "playwright-report", "test-results"}
COMMANDS = {"github-status", "remote-add", "github-create", "commit-preview", "commit-execute",
            "freeze-preview", "freeze-blockers", "freeze-execute", "freeze-retire", "rehydrate", "vault-list",
            "github-repos", "clone-preview", "clone-execute",
            "backup-run", "backup-verify", "backup-status", "backup-remove"}
BACKUP_FILES = {"source.bundle", "git-metadata.tar.gz", "local-files.tar.gz"}
BACKUP_FOLDER = "Igloo-Freezer-Backup"
MARKER = ".freeze-dried.json"
BLOCKERS = {
    "no-remote": ("Connect a remote before preserving or freezing this project.", "connect-remote"),
    "no-files": ("Add files to this project, then commit them.", None),
    "no-commit": ("Make an initial commit before freezing this project.", "commit"),
    "changed-source": ("Commit or resolve changed/untracked source first. Ignored local-only files will be preserved separately.", "commit"),
    "open-processes": ("A process still has files open in this project. Close it before freezing.", None),
    "own-source": ("The running Igloo app is protected from freezing. Its Git setup and source commits are available.", None),
}


def configure(organizer):
    global O
    O = organizer


def refuse(message):
    raise O.Refusal(message)


def vault():
    p = O.VAULT
    if p.is_symlink():
        refuse("The Freezer is a symbolic link. Choose a normal project folder.")
    p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    p.mkdir(mode=0o700, exist_ok=True)
    p.chmod(0o700)
    return p


def rename_exclusive(source, destination):
    # Native no-replace rename closes the check/rename race for live project paths.
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        function = libc.renamex_np
        function.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        result = function(os.fsencode(source), os.fsencode(destination), 0x00000004)
    elif hasattr(libc, "renameat2"):
        function = libc.renameat2
        function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        result = function(-100, os.fsencode(source), -100, os.fsencode(destination), 1)
    else:
        refuse("This platform cannot guarantee a restore without overwriting paths.")
    if result != 0:
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number), str(destination))


def quick_refresh(path):
    # Update Git state after our own actions without traversing dependency trees again.
    try:
        data = O.load(O.STATE / "snapshot.json") or O.import_audit()
        record = next((p for p in data["projects"] if p.get("path") == str(path) or
                       any(w.get("path") == str(path) for w in p.get("worktrees", []))), None)
        if not record or not record.get("git"):
            return
        states = [O.git_state(w["path"]) for w in O.worktree_list(record["path"])
                  if isinstance(w.get("worktree"), str) and Path(w["worktree"]).is_dir()]
        try: cwds = O.process_cwds()
        except O.Refusal: cwds = None
        observation = {"sizes": {}, "candidates": record.get("candidates", []),
                       "local": record.get("localFiles", []), "owners": states}
        fresh = O.project_record(states, observation, cwds)
        fresh["size"] = record["size"]
        data["projects"] = [fresh if p.get("id") == record["id"] else p for p in data["projects"]]
        data["source"] = "Git state updated; disk sizes retain their scan timestamps"
        O.save(O.STATE / "snapshot.json", data)
    except Exception:
        # A successful commit/remote change must not be reported as a failed mutation.
        return

def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def signature(path):
    st = path.lstat()
    if stat.S_ISLNK(st.st_mode):
        kind, value = "link", os.readlink(path)
    elif stat.S_ISREG(st.st_mode):
        kind, value = "file", digest(path)
    elif stat.S_ISDIR(st.st_mode):
        kind, value = "dir", ""
    else:
        refuse("A socket, pipe or special device needs manual preservation: " + str(path))
    after = path.lstat()
    if (st.st_ino, st.st_size, st.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        refuse("A file changed while it was being read. Close the project and retry.")
    return {"kind": kind, "hash": value, "size": st.st_size if kind == "file" else 0,
            "mode": stat.S_IMODE(st.st_mode), "mtime": st.st_mtime_ns}


def stable(entries):
    return hashlib.sha256(json.dumps(entries, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def checked(value, missing=False):
    p = O.checked_path(str(value), allow_missing=missing)
    if O.under(p, O.VAULT):
        refuse("The Freezer is not a project checkout.")
    return p


def known_checkout(path, allow_app=False):
    candidate = Path(path)
    if allow_app and candidate != O.ROOT and O.under(candidate, O.ROOT) and O.under(O.APP, candidate) and candidate.is_dir() and not candidate.is_symlink():
        rc, top = git(candidate, "rev-parse", "--show-toplevel")
        if rc or Path(top.strip()) != candidate:
            refuse("The organizer source is not a normal Git checkout.")
        return candidate
    p = checked(path)
    for project in O.snapshot()["projects"]:
        if not project.get("git"):
            continue
        if project["path"] == str(p) or any(w["path"] == str(p) and w["exists"] for w in project["worktrees"]):
            return p
    refuse("Select a known project checkout and refresh the analysis.")


def git(path, *args, timeout=60):
    return O.run_git(path, "-c", "gc.auto=0", "-c", "maintenance.auto=false", *args, timeout=timeout)


def gh(*args):
    executable = O.GITHUB if Path(O.GITHUB).is_file() else shutil.which("gh")
    if not executable:
        refuse("GitHub CLI is not installed. Install gh or connect an existing Git remote.")
    try:
        result = subprocess.run([executable, *args], capture_output=True, text=True,
                                env=dict(O.ENV, GH_PROMPT_DISABLED="1"), timeout=90)
    except subprocess.TimeoutExpired:
        refuse("GitHub CLI timed out. Source files were not removed.")
    return result.returncode, result.stdout


def github_status():
    rc, login = gh("api", "user", "--jq", ".login")
    owners = [login.strip()] if rc == 0 else []
    if rc == 0:
        org_rc, orgs = gh("api", "user/orgs", "--paginate", "--slurp", "--jq", "[.[][].login]")
        if not org_rc:
            try: owners += json.loads(orgs)
            except ValueError: pass
    return {"authenticated": rc == 0, "login": login.strip() if rc == 0 else "", "owners": sorted(set(owners)),
            "message": "GitHub CLI is ready." if rc == 0 else "Sign into GitHub CLI in Terminal first."}


def remote_name(value):
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", value):
        refuse("Use a simple remote name such as origin.")
    return value


def safe_destination(value):
    if not value or O.safe_url(value) != value or any(ord(c) < 32 for c in value):
        refuse("Use a remote URL without embedded credentials, query data or control characters.")
    if re.fullmatch(r"https://[A-Za-z0-9.-]+(?::[0-9]+)?/[A-Za-z0-9_./~-]+", value):
        return value
    if re.fullmatch(r"git@[A-Za-z0-9.-]+:[A-Za-z0-9_./~-]+", value):
        return value
    # Local paths are useful for deliberate local remotes and isolated tests.
    p = Path(value)
    if p.is_absolute() and O.under(p, O.ROOT) and not p.is_symlink() and p.is_dir():
        return str(p)
    refuse("Use an HTTPS, git@host:path, or existing local Git remote.")


def selected_destination(path, name):
    remote_name(name)
    rc, raw = git(path, "remote", "get-url", "--push", "--all", name)
    urls = raw.splitlines()
    if rc or len(urls) != 1:
        refuse("Choose a remote with exactly one push destination.")
    return safe_destination(urls[0])


def connect_remote(path, name, url):
    p = known_checkout(path, allow_app=True)
    remote_name(name)
    safe_destination(url)
    rc, _ = git(p, "remote", "get-url", name)
    if not rc:
        refuse("That remote already exists. Its configuration was not overwritten.")
    rc, _ = git(p, "remote", "add", name, url)
    if rc:
        refuse("Could not add the remote. Check Git configuration permissions.")
    quick_refresh(p)
    return {"message": "Remote added. Nothing was pushed; refresh the project if its actions still show older state."}


def create_private_repo(path, slug, name):
    p = known_checkout(path, allow_app=True)
    remote_name(name)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]{1,100}", slug) or slug.split("/")[1] in {".", ".."}:
        refuse("Enter the exact GitHub owner/repository, such as your-account/my-project.")
    rc, _ = git(p, "remote", "get-url", name)
    if not rc:
        refuse("That remote name already exists. Choose another name.")
    # Owner is the explicit slug, never a local folder label. Creation never pushes.
    rc, _ = gh("repo", "create", slug, "--private", "--source", str(p), "--remote", name)
    if rc:
        refuse("Repository creation did not complete. Check GitHub and local remotes before retrying; no source was removed.")
    quick_refresh(p)
    return {"message": "Private repository created and connected. Source has not been pushed yet."}


def plan_path(kind, identifier):
    if not re.fullmatch(r"[0-9a-f]{32}", identifier):
        refuse("Invalid review identifier.")
    return O.STATE / "plans" / (kind + "-" + identifier + ".json")


def reviewed_plan(kind, identifier):
    p = plan_path(kind, identifier)
    value = O.load(p)
    if not value or O.age_seconds(value["created"]) > 900 or value.get("executed"):
        refuse("This review expired or was already used. Make a new review.")
    return p, value


def preview_commit(path, files):
    p = known_checkout(path, allow_app=True)
    state = O.git_state(str(p))
    if not files:
        refuse("Select specific changed source files.")
    changes = {c["path"]: c for c in state["changes"]}
    chosen = sorted(set(files))
    for name in chosen:
        rel = PurePosixPath(name)
        if rel.is_absolute() or ".." in rel.parts or name not in changes or name.endswith("/"):
            refuse("Choose individual changed files from this checkout.")
        candidate = p / name
        if candidate.is_dir() and not candidate.is_symlink():
            refuse("Nested repositories and directories need separate review.")
        if O.config_name(candidate) and not O.is_template(candidate):
            refuse("Environment, key and data files stay local. Exclude them from this commit.")
        # A symlink itself can be committed, but never dereference a symlink parent.
        for parent in candidate.parents:
            if parent == p:
                break
            if parent.is_symlink():
                refuse("A selected file is beneath a symbolic link.")
    rc, cached = git(p, "diff", "--cached", "--name-only", "-z")
    if rc or not set(filter(None, cached.split("\0"))).issubset(chosen):
        refuse("Other files are already staged. Include them in the review or unstage them in Terminal first.")
    entries = {name: signature(p / name) if (p / name).exists() or (p / name).is_symlink() else {"kind": "deleted"}
               for name in chosen}
    value = {"id": uuid.uuid4().hex, "created": O.now(), "path": str(p), "head": state["head"],
             "files": chosen, "entries": entries}
    O.save(plan_path("commit", value["id"]), value)
    return {k: value[k] for k in ("id", "created", "path", "files")}


def execute_commit(identifier, message):
    location, plan = reviewed_plan("commit", identifier)
    if not message.strip() or len(message) > 4000:
        refuse("Enter a commit message of up to 4,000 characters.")
    p = known_checkout(plan["path"], allow_app=True)
    fresh = preview_commit(str(p), plan["files"])
    fresh_path = plan_path("commit", fresh["id"])
    comparison = O.load(fresh_path)
    fresh_path.unlink()
    if comparison["head"] != plan["head"] or comparison["entries"] != plan["entries"]:
        refuse("The selected files or branch changed. Review the commit again.")
    rc, _ = git(p, "--literal-pathspecs", "add", "-A", "--", *plan["files"])
    if rc:
        refuse("Staging failed. Review Git status before retrying.")
    rc, cached = git(p, "diff", "--cached", "--name-only", "-z")
    if rc or not set(filter(None, cached.split("\0"))).issubset(plan["files"]):
        refuse("The staged selection changed. Review it in Terminal; no commit was made.")
    rc, _ = git(p, "commit", "-m", message, timeout=120)
    if rc:
        refuse("Commit failed, possibly because of identity, signing or hooks. Reviewed files may remain staged; inspect Git status.")
    plan["executed"] = O.now()
    O.save(location, plan)
    quick_refresh(p)
    return {"message": "Committed only the reviewed selection. Nothing was pushed."}


def read_refs(path):
    rc, raw = git(path, "for-each-ref", "--format=%(refname)%09%(objectname)")
    if rc:
        refuse("Git refs could not be read.")
    return [{"ref": line.split("\t")[0], "sha": line.split("\t")[1]} for line in raw.splitlines() if "\t" in line]


def open_files(roots):
    """Freeze needs a stronger activity check than the catalog's cwd estimate."""
    for root in roots:
        try:
            result = subprocess.run(["/usr/sbin/lsof", "-nP", "-Fpn", "+D", str(root)],
                                    capture_output=True, text=True, timeout=45)
        except (OSError, subprocess.TimeoutExpired):
            refuse("The open-file check did not finish. Close project applications before freezing.")
        if result.returncode not in (0, 1) or result.stderr.strip():
            refuse("The open-file check was incomplete. Close applications and check folder permissions.")
        pid = ""
        for line in result.stdout.splitlines():
            if line.startswith("p"): pid = line[1:]
            if line.startswith("n") and pid != str(os.getpid()):
                refuse(BLOCKERS["open-processes"][0])


def initial_commit_has_files(state):
    """Inspect collected Git status only; do not hash, query Git or scan files."""
    root = Path(state["path"])
    for change in state["changes"]:
        name, status = change["path"], change["status"]
        if name.endswith("/") or "D" in status:
            continue
        path = root / name
        if O.config_name(path) and not O.is_template(path):
            continue
        return True
    return False


def blockers_for_states(states, processes):
    """Use already collected local facts; no hashing, Git calls or network access."""
    main = next((s for s in states if Path(s["common"]).parent == Path(s["path"])), states[0])
    roots = [Path(s["path"]) for s in states]
    changed = False
    for state in states:
        nested = [p for p in roots if p != Path(state["path"]) and O.under(p, Path(state["path"]))]
        changed |= any(not any(O.under(Path(state["path"]) / c["path"].rstrip("/"), p) for p in nested)
                       for c in state["changes"])
    unborn = [state for state in states if not state["head"]]
    no_files = any(not initial_commit_has_files(state) for state in unborn)
    facts = {"no-remote": not main["remotes"], "no-files": no_files,
             "no-commit": bool(unborn) and not no_files,
             "changed-source": changed and not unborn,
             "open-processes": processes is not None and any(O.under(Path(p["path"]), root)
                                                             for p in processes for root in roots),
             "own-source": any(O.under(O.APP, root) for root in roots)}
    return [{"code": code, "message": message, "fix": fix}
            for code, (message, fix) in BLOCKERS.items() if facts[code]]


def freeze_blockers(path):
    p = known_checkout(path, allow_app=True)
    states = [O.git_state(w["worktree"]) for w in O.worktree_list(str(p))
              if isinstance(w.get("worktree"), str) and Path(w["worktree"]).is_dir()]
    if not states:
        refuse("The main checkout could not be verified.")
    try:
        processes = O.process_cwds()
    except O.Refusal:
        processes = None
    return {"path": str(p), "blockers": blockers_for_states(states, processes)}


def group_state(path):
    if O.under(O.APP, Path(path)):
        refuse(BLOCKERS["own-source"][0])
    p = known_checkout(path)
    common = Path(O.common_dir(p))
    primary = common.parent
    if common.name != ".git" or not common.is_dir() or common.is_symlink() or not (primary / ".git").is_dir():
        refuse("Freezing requires the main checkout with a normal .git directory.")
    checked(primary)
    if (common / "objects/info/alternates").exists():
        refuse("This repository uses external Git object storage. Make it self-contained before freezing.")
    states = []
    for w in O.worktree_list(str(primary)):
        value = w.get("worktree")
        if isinstance(value, str) and Path(value).exists():
            candidate = checked(value)
            state = O.git_state(str(candidate))
            if state["common"] != str(common):
                refuse("A worktree points to different Git metadata.")
            if not state["head"]:
                code = "no-commit" if initial_commit_has_files(state) else "no-files"
                refuse(BLOCKERS[code][0])
            states.append(state)
    if not states or not any(s["path"] == str(primary) for s in states):
        refuse("The main checkout could not be verified.")
    cwds = O.process_cwds()
    if O.activity(str(primary), cwds):
        refuse(BLOCKERS["open-processes"][0])
    paths = [Path(s["path"]) for s in states]
    for state in states:
        nested = [r for r in paths if r != Path(state["path"]) and O.under(r, Path(state["path"]))]
        remaining = [c for c in state["changes"]
                     if not any(O.under(Path(state["path"]) / c["path"].rstrip("/"), n) for n in nested)]
        if remaining:
            refuse(BLOCKERS["changed-source"][0])
    for name in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-apply", "rebase-merge", "index.lock"):
        if (common / name).exists() or any((Path(s["path"]) / ".git" / name).exists() for s in states if (Path(s["path"]) / ".git").is_dir()):
            refuse("Finish the active Git operation before freezing.")
    return primary, common, states


def inventory(root, tracked, nested=(), excluded_override=None):
    """Hash retained content; exclude only ignored rebuild directories without tracked descendants."""
    entries, excluded = {}, []
    nested = set(map(str, nested))
    def visit(base, inherited_exclusion=False):
        try: children = sorted(base.iterdir(), key=lambda p: p.name)
        except OSError: refuse("A project directory could not be read: " + str(base))
        if not children and base != root and not inherited_exclusion:
            entries[str(base.relative_to(root))] = signature(base)
        for p in children:
            rel = str(p.relative_to(root))
            if base == root and p.name == ".git":
                continue
            if str(p) in nested:
                continue
            if p.is_dir() and not p.is_symlink():
                skip = inherited_exclusion
                if not skip and p.name in REBUILD and not any(t == rel or t.startswith(rel + "/") for t in tracked):
                    if excluded_override is None:
                        rc, _ = git(root, "check-ignore", "-q", rel + "/")
                    else:
                        rc = 0 if rel in excluded_override else 1
                    if rc == 0:
                        skip = True
                        excluded.append(rel)
                visit(p, skip)
            else:
                # A secret/config filename inside a rebuild cache is still kept locally.
                if inherited_exclusion and not (O.config_name(p) and not O.is_template(p)):
                    continue
                data = signature(p)
                data["local"] = rel not in tracked
                entries[rel] = data
    visit(root)
    return entries, excluded


def metadata_inventory(common):
    entries = {}
    for base, dirs, files in os.walk(common, followlinks=False):
        for name in sorted(dirs + files):
            p = Path(base) / name
            if p.is_symlink():
                refuse("Git metadata contains a symbolic link. Preserve this repository manually.")
        for name in sorted(files):
            p = Path(base) / name
            entries[str(p.relative_to(common))] = signature(p)
    return entries


def capture(primary, common, states):
    roles = []
    paths = [Path(s["path"]) for s in states]
    for index, state in enumerate(sorted(states, key=lambda s: (s["path"] != str(primary), s["path"]))):
        root = Path(state["path"])
        entries, excluded = inventory(root, state["tracked"], [p for p in paths if p != root and O.under(p, root)])
        roles.append({"id": str(index), "path": str(root), "relative": str(root.relative_to(O.ROOT)),
                      "branch": state["branch"], "head": state["head"],
                      "pointer": (root / ".git").read_text() if (root / ".git").is_file() else "", "entries": entries,
                      "excluded": excluded, "tracked": sorted(state["tracked"])})
    return {"roles": roles, "git": metadata_inventory(common), "refs": read_refs(primary)}


def preview_freeze(path, remote, mode="preserve"):
    if mode not in {"preserve", "freeze"}:
        refuse("Choose preserve or freeze mode for this review.")
    primary, common, states = group_state(path)
    if not states[0]["remotes"]:
        refuse(BLOCKERS["no-remote"][0])
    destination = selected_destination(primary, remote)
    roots = [Path(s["path"]) for s in states]
    roots = [p for p in roots if not any(p != q and O.under(p, q) for q in roots)]
    open_files(roots)
    O.progress("Hashing source and local-only files for the freeze review…")
    captured = capture(primary, common, states)
    publish, local_refs = [], []
    for ref in captured["refs"]:
        normal = ref["ref"].startswith(("refs/heads/", "refs/tags/"))
        if normal and not O.PRIVATE_REF.search(ref["ref"]):
            publish.append(ref)
        elif not ref["ref"].startswith("refs/remotes/"):
            local_refs.append(ref)
    for role in captured["roles"]:
        if not role["branch"]:
            local_refs.append({"ref": "detached:" + role["relative"], "sha": role["head"]})
    if not publish:
        refuse("No ordinary branches or tags are available to publish. Preserve this repository manually.")
    local = [(r, n, e) for r in captured["roles"] for n, e in r["entries"].items() if e.get("local", True)]
    plan = {"id": uuid.uuid4().hex, "mode": mode, "created": O.now(), "name": primary.name, "path": str(primary),
            "remote": remote, "destination": destination, "roots": [str(p) for p in roots],
            "capture": captured, "publish": publish, "localRefs": local_refs, "held": O.on_hold(str(primary))}
    plan["checkoutBytes"] = sum(O.physical_size(p) for p in roots)
    # Allocation is an estimate input, not part of the preservation identity.
    # Keep signatures compatible with existing Preserved manifests.
    archive_estimate = sum((Path(r["path"]) / name).lstat().st_blocks * 512
                           for r in captured["roles"] for name, e in r["entries"].items() if e["kind"] == "file")
    archive_estimate += sum((common / name).lstat().st_blocks * 512
                            for name, e in captured["git"].items() if e["kind"] == "file")
    net_estimate = (plan["checkoutBytes"] if mode == "freeze" else 0) - archive_estimate
    plan["outcome"] = O.outcome_summary(
        checkouts=[r["path"] for r in captured["roles"]] if mode == "freeze" else [],
        estimate=max(0, net_estimate), used_estimate=max(0, -net_estimate),
        archive_count=1,
        refs=[dict(r, destination=destination) for r in publish])
    O.save(plan_path("freeze", plan["id"]), plan)
    return {"id": plan["id"], "mode": mode, "held": plan["held"], "created": plan["created"], "name": plan["name"], "path": plan["path"],
            "destination": destination, "publish": publish, "localRefs": local_refs,
            "checkouts": [r["path"] for r in captured["roles"]],
            "localCount": len(local), "localSize": sum(e["size"] for _, _, e in local),
            "localFiles": [str(Path(r["path"]) / n) for r, n, _ in local],
            "excluded": [str(Path(r["path"]) / n) for r in captured["roles"] for n in r["excluded"]],
            "archiveLocation": str(O.VAULT), "outcome": plan["outcome"],
            "message": "Local archives are private-permission files on this Mac, not encrypted and never uploaded."}


def add_tar(archive, path, name):
    archive.add(path, arcname=name, recursive=False)


def write_local_archive(location, roles, overrides):
    output = location / "local-files.tar.gz"
    with tarfile.open(output, "w:gz", dereference=False) as archive:
        for role in roles:
            for name, entry in role["entries"].items():
                if entry.get("local", True) or name in overrides.get(role["id"], []):
                    add_tar(archive, Path(role["path"]) / name, role["id"] + "/" + name)
    output.chmod(0o600)


def write_git_archive(location, common, entries):
    output = location / "git-metadata.tar.gz"
    with tarfile.open(output, "w:gz", dereference=False) as archive:
        for name in entries:
            add_tar(archive, common / name, name)
    output.chmod(0o600)


def safe_member(name):
    p = PurePosixPath(name)
    if p.is_absolute() or not p.parts or ".." in p.parts or "\\" in name or "\x00" in name:
        refuse("An archive contains an unsafe path.")
    return p


def parent_chain(destination, relative):
    current = destination
    for part in relative.parts[:-1]:
        current = current / part
        if current.is_symlink() or current.exists() and not current.is_dir():
            refuse("An archive path would traverse a link or file.")
        current.mkdir(mode=0o700, exist_ok=True)
    return destination.joinpath(*relative.parts)


def extract_safe(archive_path, destination, prefix=None):
    """No extractall, hard links, devices, absolute paths or writes through symlinks."""
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    seen = set()
    with tarfile.open(archive_path, "r:*") as archive:
        for member in archive:
            relative = safe_member(member.name)
            if prefix is not None:
                if relative.parts[0] != prefix:
                    continue
                relative = PurePosixPath(*relative.parts[1:])
                if not relative.parts:
                    continue
            if str(relative) in seen:
                refuse("An archive contains duplicate file paths.")
            seen.add(str(relative))
            target = parent_chain(destination, relative)
            if member.isdir():
                if target.is_symlink() or target.exists() and not target.is_dir():
                    refuse("An archive directory conflicts with an existing path.")
                target.mkdir(mode=0o700, exist_ok=True)
                continue
            if not (member.isfile() or member.issym()):
                refuse("An archive contains an unsupported hard link or special file.")
            if target.exists() or target.is_symlink():
                if target.is_dir() and not target.is_symlink():
                    refuse("An archive file conflicts with a directory.")
                target.unlink()  # Only a generated staging file, never a live checkout.
            if member.issym():
                os.symlink(member.linkname, target)
            else:
                source = archive.extractfile(member)
                if source is None:
                    refuse("An archive file could not be read.")
                with source, open(target, "xb") as out:
                    shutil.copyfileobj(source, out)
                target.chmod(member.mode & 0o777)


def materialize(git_dir, head, destination, scratch):
    archive_path = scratch / (uuid.uuid4().hex + ".tar")
    rc, _ = git(scratch, "--git-dir=" + str(git_dir), "archive", "--format=tar",
                "--output=" + str(archive_path), head, timeout=300)
    if rc:
        refuse("The frozen source tree could not be reconstructed from Git.")
    try: extract_safe(archive_path, destination)
    finally: archive_path.unlink(missing_ok=True)


def content_matches(path, expected):
    if not path.exists() and not path.is_symlink():
        return False
    actual = signature(path)
    return actual["kind"] == expected["kind"] and actual["hash"] == expected["hash"]


def trim_generated_tree(path, entries):
    # git archive expands sparse checkouts; retain the exact set of frozen paths.
    expected = set(entries)
    for base, dirs, files in os.walk(path, topdown=False, followlinks=False):
        for name in files:
            p = Path(base) / name
            if str(p.relative_to(path)) not in expected:
                p.unlink()
        for name in dirs:
            p = Path(base) / name
            rel = str(p.relative_to(path))
            if p.is_symlink():
                if rel not in expected: p.unlink()
            elif rel not in expected and not any(n.startswith(rel + "/") for n in expected):
                if not any(p.iterdir()): p.rmdir()


def verify_tree(path, entries):
    for name, entry in entries.items():
        if not content_matches(path / name, entry):
            refuse("Reconstruction verification failed for a retained file: " + name)


def apply_metadata(path, entries):
    for name, entry in entries.items():
        p = path / name
        if not p.is_symlink():
            p.chmod(entry["mode"])
            os.utime(p, ns=(entry["mtime"], entry["mtime"]))


def verify_archives(location, manifest):
    for name, expected in manifest["checksums"].items():
        if name not in {"git-metadata.tar.gz", "local-files.tar.gz", "source.bundle"}:
            refuse("An archive manifest contains an unknown file.")
        p = location / name
        if p.is_symlink() or not p.is_file() or digest(p) != expected:
            refuse("An archive is missing or has changed. No source will be removed or overwritten.")


def prove_recovery(location, captured):
    O.progress("Testing recovery from the local archive…")
    with tempfile.TemporaryDirectory(prefix=".verify-", dir=location) as directory:
        stage = Path(directory)
        meta = stage / "git"
        extract_safe(location / "git-metadata.tar.gz", meta)
        for role in captured["roles"]:
            tree = stage / "tree"
            materialize(meta, role["head"], tree, stage)
            trim_generated_tree(tree, role["entries"])
            extract_safe(location / "local-files.tar.gz", tree, prefix=role["id"])
            verify_tree(tree, role["entries"])
            shutil.rmtree(tree)


def current_matches(plan):
    primary, common, states = group_state(plan["path"])
    if selected_destination(primary, plan["remote"]) != plan["destination"]:
        refuse("The remote destination changed since review.")
    fresh = capture(primary, common, states)
    if stable(fresh) != stable(plan["capture"]):
        refuse("Source, local files or Git metadata changed since review. Make a new freeze review.")
    return primary, common, states


def record_freeze_outcome(plan, manifest, location, refs):
    removed = manifest["state"] == "frozen"
    wrote_archive = any((location / name).is_file() for name in
                        ("source.bundle", "git-metadata.tar.gz", "local-files.tar.gz"))
    outcome = O.outcome_summary(
        checkouts=[r["path"] for r in manifest["capture"]["roles"]] if removed else [],
        estimate=plan.get("outcome", {}).get("bytesFreedEstimate", 0),
        used_estimate=plan.get("outcome", {}).get("bytesUsedEstimate", 0),
        archives=[location] if wrote_archive else [], refs=refs, observed=True)
    if manifest["state"] != ("frozen" if plan.get("mode") == "freeze" else "ready"):
        outcome["held"] = [{"path": plan["path"], "reason": manifest["message"]}]
    manifest["outcome"] = outcome
    O.save(location / "manifest.json", manifest)
    removed_bytes = (plan.get("checkoutBytes", 0) - sum(O.physical_size(p) for p in plan["roots"])) if removed else 0
    # Include the durable summary itself in allocated recovery bytes. A second
    # write normally keeps the same allocation; recalculate if it crosses a block.
    for _ in range(3):
        freed = removed_bytes - O.physical_size(location)
        if outcome["bytesFreed"] == freed:
            break
        outcome["bytesFreed"] = freed
        O.save(location / "manifest.json", manifest)
    return outcome


def execute_freeze(identifier, mode=None):
    plan_location, plan = reviewed_plan("freeze", identifier)
    accepted_mode = plan.get("mode", "preserve")
    if accepted_mode not in {"preserve", "freeze"} or mode is not None and mode != accepted_mode:
        refuse("The requested mode does not match this review. Make a new review.")
    primary, common, states = current_matches(plan)
    open_files([Path(p) for p in plan["roots"]])
    captured = plan["capture"]
    # Space for archives and one proof checkout. Dependencies are not duplicated.
    retained = sum(e["size"] for r in captured["roles"] for e in r["entries"].values())
    metadata = sum(e["size"] for e in captured["git"].values())
    maximum = max((sum(e["size"] for e in r["entries"].values()) for r in captured["roles"]), default=0)
    if shutil.disk_usage(O.ROOT).free < retained + 2 * metadata + maximum + 256 * 1024 * 1024:
        refuse("Not enough free space to build and verify the recovery archive safely.")
    location = vault() / identifier
    location.mkdir(mode=0o700)
    manifest = {"version": 1, "id": identifier, "name": plan["name"], "path": plan["path"],
                "created": O.now(), "state": "preparing", "destination": plan["destination"],
                "remote": plan["remote"], "roots": plan["roots"], "capture": captured,
                "publish": plan["publish"], "localRefs": plan["localRefs"], "checksums": {},
                "moves": [], "message": "Archive preparation is incomplete; source was not removed."}
    O.save(location / "manifest.json", manifest)
    confirmed_refs = []
    try:
        O.progress("Building a local Git recovery copy…")
        rc, _ = git(primary, "bundle", "create", str(location / "source.bundle"), "--all", "HEAD", timeout=600)
        if rc:
            refuse("Git recovery bundle creation failed. Source was not removed.")
        (location / "source.bundle").chmod(0o600)
        rc, _ = git(primary, "bundle", "verify", str(location / "source.bundle"))
        if rc:
            refuse("The Git recovery bundle could not be verified.")
        write_git_archive(location, common, captured["git"])
        # Preserve filtered/export-ignored tracked files as local overlays when needed.
        overrides = {}
        with tempfile.TemporaryDirectory(prefix=".representation-", dir=location) as directory:
            scratch = Path(directory)
            for role in captured["roles"]:
                tree = scratch / "tree"
                materialize(common, role["head"], tree, scratch)
                overrides[role["id"]] = [name for name, e in role["entries"].items()
                                         if not e.get("local", True) and not content_matches(tree / name, e)]
                shutil.rmtree(tree)
        write_local_archive(location, captured["roles"], overrides)
        manifest["checksums"] = {name: digest(location / name) for name in
                                 ("git-metadata.tar.gz", "local-files.tar.gz", "source.bundle")}
        verify_archives(location, manifest)
        prove_recovery(location, captured)
        current_matches(plan)  # Archiving must not race new work.
        O.progress("Pushing only the reviewed branches and tags…")
        specs = [r["sha"] + ":" + r["ref"] for r in plan["publish"]]
        rc, _ = git(primary, "push", "--atomic", "--porcelain", "--no-follow-tags",
                    "--recurse-submodules=no", "--", plan["destination"], *specs, timeout=300)
        if rc:
            refuse("Push was rejected or failed. The local archive exists, and all source remains in place.")
        rc, raw = git(primary, "ls-remote", "--heads", "--tags", plan["destination"], timeout=60)
        advertised = {(line.split("\t")[1], line.split("\t")[0]) for line in raw.splitlines() if "\t" in line}
        if rc or any((r["ref"], r["sha"]) not in advertised for r in plan["publish"]):
            refuse("The published refs could not be verified. Source remains in place.")
        confirmed_refs = [dict(r, destination=plan["destination"]) for r in plan["publish"]]
        # Push may update tracking refs; recapture and preserve that metadata too.
        fresh_primary, fresh_common, fresh_states = group_state(str(primary))
        after = capture(fresh_primary, fresh_common, fresh_states)
        before_local = {r["id"]: r["entries"] for r in captured["roles"]}
        if before_local != {r["id"]: r["entries"] for r in after["roles"]}:
            refuse("Working files changed during publication. Source remains in place.")
        valuable = lambda refs: [r for r in refs if not r["ref"].startswith("refs/remotes/")]
        if valuable(after["refs"]) != valuable(captured["refs"]):
            refuse("Local branches, tags or stashes changed during publication. Make a new archive.")
        write_git_archive(location, fresh_common, after["git"])
        manifest["capture"] = after
        manifest["checksums"]["git-metadata.tar.gz"] = digest(location / "git-metadata.tar.gz")
        verify_archives(location, manifest)
        prove_recovery(location, after)
        # Capture once more to catch changes while testing the archive.
        if stable(capture(fresh_primary, fresh_common, fresh_states)) != stable(after):
            refuse("The project changed during recovery verification. Source remains in place.")
        manifest.update(state="ready", verifiedAt=O.now(),
                        message="Preserved. Recovery and published refs verified. Checkouts are still present.")
        O.save(location / "manifest.json", manifest)
        plan["executed"] = O.now()
        O.save(plan_location, plan)
    except Exception as e:
        manifest["state"] = "incomplete"
        manifest["message"] = str(e) if isinstance(e, O.Refusal) else "Preservation did not finish. Source checkouts were not removed."
        O.save(location / "manifest.json", manifest)
        e.outcome = record_freeze_outcome(plan, manifest, location, confirmed_refs)
        raise
    if accepted_mode == "freeze":
        try:
            freeze_retire(identifier)
        except (O.Refusal, OSError) as e:
            # Preserve has completed. A refused removal keeps its exact recovery
            # state and reason, including an interrupted move journal if present.
            manifest = O.load(location / "manifest.json")
            reason = str(e) if isinstance(e, O.Refusal) else "Checkout removal stopped. Review the recovery history."
            manifest["message"] = ("Preserved. Freeze stopped: " if manifest["state"] == "ready"
                                   else "Freeze was interrupted: ") + reason
            O.save(location / "manifest.json", manifest)
    manifest = O.load(location / "manifest.json")
    outcome = record_freeze_outcome(plan, manifest, location, confirmed_refs)
    result = {"archiveID": identifier, "mode": accepted_mode, "state": manifest["state"],
              "outcome": outcome, "message": manifest["message"]}
    if O.CONFIG.get("freezerBackupRoot"):
        try:
            backup_run([identifier])
            result["backup"] = next(r for r in backup_status()["archives"] if r["id"] == identifier)
        except Exception as error:
            # The preservation result is durable already. Backup failure must
            # never turn it into a failed Preserve or invite a duplicate push.
            reason = backup_failures().get(identifier) or backup_reason(error)
            result["backup"] = backup_row(identifier, reason=reason)
            try:
                failures = backup_failures()
                failures[identifier] = reason
                save_backup_failures(failures)
            except Exception:
                pass
    return result


def archive_record(identifier):
    if not re.fullmatch(r"[0-9a-f]{32}", identifier):
        refuse("Invalid archive identifier.")
    location = vault() / identifier
    if location.is_symlink():
        refuse("The archive path is a symbolic link.")
    manifest = O.load(location / "manifest.json")
    if not manifest or manifest.get("id") != identifier:
        refuse("The archive manifest is missing.")
    checked(manifest["path"], missing=True)
    for root in manifest["roots"]:
        checked(root, missing=True)
    for role in manifest["capture"]["roles"]:
        expected = O.ROOT / role["relative"]
        if str(expected) != role["path"]:
            refuse("This archive belongs to a different Projects location. Do not relocate its paths manually.")
        checked(expected, missing=True)
        for name in role["entries"]:
            safe_member(name)
    return location, manifest


def is_stub(path, identifier):
    return (path.is_dir() and not path.is_symlink()
            and {p.name for p in path.iterdir()} == {MARKER}
            and not (path / MARKER).is_symlink()
            and O.load(path / MARKER, {}).get("id") == identifier)


def freeze_retire(identifier):
    location, manifest = archive_record(identifier)
    if manifest["state"] != "ready":
        refuse("Only a fully verified archive is ready for checkout removal.")
    checkout_bytes = sum(O.physical_size(p) for p in manifest["roots"])
    verify_archives(location, manifest)
    primary, common, states = group_state(manifest["path"])
    open_files([Path(p) for p in manifest["roots"]])
    if stable(capture(primary, common, states)) != stable(manifest["capture"]):
        refuse("This project changed after Preserve. Preserve it again before freezing.")
    # Verify recovery again immediately before any source leaves its original path.
    prove_recovery(location, manifest["capture"])
    if stable(capture(primary, common, states)) != stable(manifest["capture"]):
        refuse("The project changed during verification. Nothing was removed.")
    manifest["moves"] = [{"original": root,
                          "temporary": str(Path(root).parent / (".freeze-removing-" + identifier + "-" + str(i)))}
                         for i, root in enumerate(manifest["roots"])]
    manifest["state"] = "retiring"
    O.save(location / "manifest.json", manifest)
    # All checkouts move before deletion. A crash leaves an explicit recovery journal.
    renamed = []
    def mapped(original):
        for move in manifest["moves"]:
            if O.under(original, Path(move["original"])):
                return Path(move["temporary"]) / original.relative_to(move["original"])
        refuse("A source root is outside the reviewed move review.")
    try:
        for move in manifest["moves"]:
            original, temporary = Path(move["original"]), Path(move["temporary"])
            if temporary.exists() or temporary.is_symlink():
                refuse("A checkout removal staging path already exists. Source was not overwritten.")
            rename_exclusive(original, temporary)
            renamed.append(move)
        for role in manifest["capture"]["roles"]:
            root = mapped(Path(role["path"]))
            nested = [mapped(Path(r["path"])) for r in manifest["capture"]["roles"]
                      if r["path"] != role["path"] and O.under(Path(r["path"]), Path(role["path"]))]
            entries, _ = inventory(root, set(role["tracked"]), nested, excluded_override=set(role["excluded"]))
            if role.get("pointer") and (root / ".git").read_text() != role["pointer"]:
                refuse("A worktree pointer changed during checkout removal.")
            if entries != role["entries"]:
                refuse("A file changed during checkout removal. Original checkouts were preserved.")
        if metadata_inventory(mapped(common)) != manifest["capture"]["git"]:
            refuse("Git metadata changed during checkout removal. Original checkouts were preserved.")
        open_files([Path(m["temporary"]) for m in manifest["moves"]])
    except Exception:
        for move in reversed(renamed):
            original, temporary = Path(move["original"]), Path(move["temporary"])
            if not original.exists() and temporary.exists():
                rename_exclusive(temporary, original)
        manifest["state"] = "ready" if all(Path(m["original"]).exists() for m in manifest["moves"]) else "retiring"
        O.save(location / "manifest.json", manifest)
        raise
    primary.mkdir(mode=0o700)
    O.save(primary / MARKER, {"id": identifier, "vault": str(location), "name": manifest["name"]})
    manifest["state"] = "removing"
    O.save(location / "manifest.json", manifest)
    # Only directories this operation just renamed can be removed.
    for move in manifest["moves"]:
        temporary = Path(move["temporary"])
        if temporary.is_symlink() or not temporary.is_dir():
            refuse("A removal staging directory changed. Keep the verified archive and use recovery.")
        shutil.rmtree(temporary)
    manifest.update(state="frozen", frozenAt=O.now(), message="Checkouts removed; local archives and a project marker remain.")
    O.save(location / "manifest.json", manifest)
    remaining = sum(O.physical_size(p) for p in manifest["roots"])
    return {"outcome": O.outcome_summary(checkouts=[r["path"] for r in manifest["capture"]["roles"]],
                                         estimate=checkout_bytes, freed=checkout_bytes - remaining, observed=True),
            "message": "Safely frozen. Local-only files and Git recovery are in the Freezer; the marker can be thawed whenever you are ready."}


def rehydrate(identifier, offline=False):
    location, manifest = archive_record(identifier)
    if manifest["state"] not in {"frozen", "retiring", "removing", "rehydrating"}:
        refuse("Choose a Frozen project or an interrupted checkout removal.")
    verify_archives(location, manifest)
    # Recover interruptions before removal, without overwriting anything the user added.
    if manifest["state"] == "retiring":
        for move in reversed(manifest["moves"]):
            original, temporary = Path(move["original"]), Path(move["temporary"])
            if temporary.exists():
                if original.exists():
                    if str(original) == manifest["path"] and is_stub(original, identifier):
                        (original / MARKER).unlink()
                        original.rmdir()
                    else:
                        refuse("A project path is occupied during recovery. Keep both copies and review it manually.")
                rename_exclusive(temporary, original)
        manifest.update(state="ready", message="Interrupted checkout removal rolled back. Checkouts are present.")
        O.save(location / "manifest.json", manifest)
        return {"message": "Interrupted removal rolled back. All moved checkouts are back in place."}
    resumed = set()
    if manifest["state"] == "rehydrating":
        for root in manifest["roots"]:
            p = Path(root)
            if p.is_dir() and not is_stub(p, identifier):
                for role in manifest["capture"]["roles"]:
                    if O.under(Path(role["path"]), p):
                        verify_tree(Path(role["path"]), role["entries"])
                        if role.get("pointer") and (Path(role["path"]) / ".git").read_text() != role["pointer"]:
                            refuse("A restored worktree pointer changed after interruption.")
                if str(p) == manifest["path"]:
                    verify_tree(p / ".git", manifest["capture"]["git"])
                resumed.add(root)
    for root in manifest["roots"]:
        p = Path(root)
        if p.exists() and root not in resumed and not (str(p) == manifest["path"] and is_stub(p, identifier)):
            refuse("A restore destination is occupied. Nothing will be overwritten.")
        if not p.parent.is_dir():
            refuse("A parent folder is missing. Recreate the original parent before restoring.")
    required = sum(e["size"] for r in manifest["capture"]["roles"] for e in r["entries"].values())
    required += sum(e["size"] for e in manifest["capture"]["git"].values())
    if shutil.disk_usage(O.ROOT).free < required + 256 * 1024 * 1024:
        refuse("Not enough free space to thaw all retained files safely.")
    O.progress("Thawing the frozen project…")
    with tempfile.TemporaryDirectory(prefix=".rehydrate-", dir=location) as directory:
        stage = Path(directory)
        pool = stage / "network"
        if not offline:
            rc, _ = git(stage, "clone", "--no-checkout", "--no-hardlinks", "--", manifest["destination"], str(pool), timeout=600)
            if rc:
                refuse("Remote clone failed. The archive is intact; choose offline recovery or fix remote access.")
        meta = stage / "git"
        extract_safe(location / "git-metadata.tar.gz", meta)
        trees = {}
        for role in manifest["capture"]["roles"]:
            tree = stage / ("checkout-" + role["id"])
            source_git = meta
            if not offline:
                rc, _ = git(pool, "cat-file", "-e", role["head"] + "^{commit}")
                if rc == 0:
                    source_git = pool / ".git"
            materialize(source_git, role["head"], tree, stage)
            trim_generated_tree(tree, role["entries"])
            extract_safe(location / "local-files.tar.gz", tree, prefix=role["id"])
            verify_tree(tree, role["entries"])
            apply_metadata(tree, role["entries"])
            trees[role["path"]] = tree
        # Restore exact Git metadata, including stashes, index, refs, reflogs and worktree registrations.
        main_tree = trees[manifest["path"]]
        shutil.copytree(meta, main_tree / ".git")
        for role in manifest["capture"]["roles"]:
            if role["path"] != manifest["path"]:
                if not role.get("pointer", "").startswith("gitdir: "):
                    refuse("A linked worktree pointer is unavailable in this archive.")
                (trees[role["path"]] / ".git").write_text(role["pointer"])
        # Assemble nested worktrees inside their parent before moving top-level roots.
        for role in sorted(manifest["capture"]["roles"], key=lambda r: -len(r["path"])):
            target_path = Path(role["path"])
            parents = [r for r in manifest["capture"]["roles"] if r["path"] != role["path"] and O.under(target_path, Path(r["path"]))]
            if parents:
                parent = max(parents, key=lambda r: len(r["path"]))
                dest = trees[parent["path"]] / target_path.relative_to(parent["path"])
                dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                if dest.exists() or dest.is_symlink():
                    refuse("Nested worktree assembly found a conflicting path.")
                rename_exclusive(trees[role["path"]], dest)
        # Check destinations again after all downloads and validation.
        for root in manifest["roots"]:
            p = Path(root)
            if p.exists() and root not in resumed and not (str(p) == manifest["path"] and is_stub(p, identifier)):
                refuse("A restore destination changed while preparing. Nothing was overwritten.")
        manifest.update(state="rehydrating", message="Thawing is in progress. Repeat recovery if it is interrupted.")
        O.save(location / "manifest.json", manifest)
        moved = []
        try:
            main = Path(manifest["path"])
            if is_stub(main, identifier):
                (main / MARKER).unlink()
                main.rmdir()
            for root in manifest["roots"]:
                if root in resumed:
                    continue
                rename_exclusive(trees[root], root)
                moved.append(root)
                manifest["restoredRoots"] = sorted(resumed | set(moved))
                O.save(location / "manifest.json", manifest)
        except Exception:
            # Published roots stay in place. Recovery can finish the remaining roots
            # without moving or deleting source the user may have started editing.
            manifest["message"] = "Thawing was interrupted. Existing restored files remain; run recovery again."
            O.save(location / "manifest.json", manifest)
            raise
    # Interrupted removal leftovers are retained: never erase possible post-crash edits automatically.
    manifest.update(state="rehydrated", rehydratedAt=O.now(),
                    message="Frozen state restored. Dependencies need reinstalling; update source from the remote when ready.")
    O.save(location / "manifest.json", manifest)
    leftovers = [v["temporary"] for v in manifest.get("moves", []) if Path(v["temporary"]).exists()]
    message = "Project thawed to its preserved state. Local files, Git state and worktrees are back. Reinstall dependencies, then pull updates when ready."
    if leftovers:
        message += " Interrupted-removal copies were retained for review: " + ", ".join(leftovers)
    return {"message": message}


def backup_reason(error):
    return str(error) if isinstance(error, O.Refusal) else "The backup could not complete. Check the folder's permissions and retry."


def backup_destination():
    raw = O.CONFIG.get("freezerBackupRoot", "")
    if not raw:
        return {"cloud": False, "acceptanceRoot": "", "cloudAccepted": False}
    root = Path(raw).expanduser()
    if not root.is_absolute() or ".." in root.parts:
        refuse("Choose an absolute Freezer backup folder without traversal.")
    # Compare components, not string prefixes. Check both lexical and resolved
    # paths on both sides: cloud folders themselves can redirect to a provider.
    def folded(path):
        parts = tuple(part.casefold() for part in path.parts)
        # macOS exposes /Users through the Data volume firmlink as well.
        # Normalize only that spelling for classification; consent below still
        # uses the resolved destination identity, not this comparison value.
        if parts[:5] == ("/", "system", "volumes", "data", "users"):
            return ("/",) + parts[4:]
        return parts
    roots = [folded(root), folded(root.resolve())]
    home = Path.home()
    clouds = [home / "Library/Mobile Documents", home / "Library/CloudStorage"]
    parents = [folded(path) for cloud in clouds for path in (cloud, cloud.resolve())]
    cloud = any(parts[:len(parent)] == parent for parts in roots for parent in parents)
    identity = preferences.backup_root_identity(root)
    return {"cloud": cloud, "acceptanceRoot": identity,
            "cloudAccepted": O.CONFIG.get("freezerBackupCloudAcceptedRoot", "") == identity}


def backup_base(create=False):
    raw = O.CONFIG.get("freezerBackupRoot", "")
    if not raw:
        refuse("Choose a Freezer backup folder first.")
    root = Path(raw).expanduser()
    if not root.is_absolute() or ".." in root.parts:
        refuse("Choose an absolute Freezer backup folder without traversal.")
    resolved = root.resolve()
    destination = backup_destination()
    if destination["cloud"] and not destination["cloudAccepted"]:
        refuse("Confirm that local-only files will upload unencrypted before using this cloud backup folder.")
    base = resolved / BACKUP_FOLDER
    for protected in (O.VAULT.resolve(), O.STATE.resolve()):
        if O.under(resolved, protected) or O.under(protected, base):
            refuse("The backup folder must be separate from the Freezer and Igloo's state folder.")
    if root.is_symlink() or base.is_symlink():
        refuse("The backup folder cannot be a symbolic link.")
    if not root.is_dir():
        refuse("The backup folder is missing or unavailable. Reconnect it and retry.")
    if base.exists() and not base.is_dir():
        refuse("The backup location is not a folder.")
    if create:
        base.mkdir(mode=0o700, exist_ok=True)
        base.chmod(0o700)
    return base


def backup_sources():
    # Do not create the Freezer during status/verification or hide damaged
    # entries: a damaged live archive still prevents removal of its copy.
    if O.VAULT.is_symlink():
        refuse("The Freezer cannot be a symbolic link.")
    if not O.VAULT.exists():
        return {}
    sources = {}
    for location in O.VAULT.iterdir():
        if not re.fullmatch(r"[0-9a-f]{32}", location.name):
            continue
        manifest = None
        try:
            if not location.is_symlink() and not (location / "manifest.json").is_symlink():
                manifest = backup_json(location / "manifest.json")
        except (OSError, ValueError):
            pass
        if isinstance(manifest, dict) and manifest.get("id") == location.name and manifest.get("state") == "rehydrated":
            continue
        sources[location.name] = location
    return sources


def backup_failures():
    try:
        value = O.load(O.STATE / "freezer-backup.json", {})
        if isinstance(value, dict) and value.get("root") == O.CONFIG.get("freezerBackupRoot", ""):
            failures = value.get("failures", {})
            if isinstance(failures, dict):
                return dict(failures)
    except (OSError, ValueError):
        pass
    return {}


def save_backup_failures(failures):
    O.save(O.STATE / "freezer-backup.json", {"version": 1,
           "root": O.CONFIG.get("freezerBackupRoot", ""), "failures": failures})


def backup_manifest(location, identifier):
    if location.is_symlink() or not location.is_dir() or (location / "manifest.json").is_symlink():
        refuse("A Freezer archive is missing or is a symbolic link.")
    manifest = backup_json(location / "manifest.json")
    checksums = manifest.get("checksums") if isinstance(manifest, dict) else None
    if not isinstance(manifest, dict) or manifest.get("id") != identifier or not isinstance(checksums, dict) or set(checksums) != BACKUP_FILES:
        refuse("A Freezer archive has no complete recovery manifest.")
    if any(not isinstance(h, str) or not re.fullmatch(r"[0-9a-f]{64}", h) for h in checksums.values()):
        refuse("A Freezer archive has invalid checksums.")
    return manifest


def backup_receipt(copy, identifier):
    if copy.is_symlink() or not copy.is_dir() or (copy / "backup.json").is_symlink():
        refuse("The backup copy is missing or is a symbolic link.")
    receipt = backup_json(copy / "backup.json")
    checksums = receipt.get("checksums") if isinstance(receipt, dict) else None
    if (not isinstance(receipt, dict) or receipt.get("version") != 1 or receipt.get("id") != identifier
            or not isinstance(checksums, dict) or set(checksums) != BACKUP_FILES | {"manifest.json"}
            or not isinstance(receipt.get("lastVerified"), str)):
        refuse("The backup copy has no valid verification receipt.")
    if any(not isinstance(h, str) or not re.fullmatch(r"[0-9a-f]{64}", h) for h in checksums.values()):
        refuse("The backup copy has invalid checksums.")
    return receipt


def backup_regular(path):
    # O_NOFOLLOW prevents a replaced file from redirecting archive reads.
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            refuse("Backup recovery files must be normal files.")
        stream = os.fdopen(descriptor, "rb")
    except Exception:
        os.close(descriptor)
        raise
    return stream


def backup_hash(path):
    checksum = hashlib.sha256()
    with backup_regular(path) as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def backup_json(path):
    try:
        with backup_regular(path) as stream:
            return json.load(stream)
    except FileNotFoundError:
        return None


def verify_backup_copy(copy, identifier, source=None):
    receipt = backup_receipt(copy, identifier)
    for name, expected in receipt["checksums"].items():
        path = copy / name
        if path.is_symlink() or not path.is_file() or backup_hash(path) != expected:
            refuse("Backup verification failed: a recovery file is missing or has changed.")
    if source is not None:
        manifest = backup_manifest(source, identifier)
        for name, expected in manifest["checksums"].items():
            path = source / name
            if path.is_symlink() or not path.is_file() or backup_hash(path) != expected or receipt["checksums"][name] != expected:
                refuse("Backup verification failed: the source archive and its copy do not match.")
    # The copied manifest is a snapshot. Freeze/Thaw update the live manifest,
    # so compare its saved bytes to the receipt, not to live lifecycle state.
    receipt["lastVerified"] = O.now()
    O.save(copy / "backup.json", receipt)
    return receipt


def write_backup_copy(base, identifier, source):
    destination = base / identifier
    if destination.exists() or destination.is_symlink():
        verify_backup_copy(destination, identifier, source)
        return
    manifest = backup_manifest(source, identifier)
    checksums = dict(manifest["checksums"], **{"manifest.json": backup_hash(source / "manifest.json")})
    with tempfile.TemporaryDirectory(prefix=".backup-", dir=base) as temporary:
        stage = Path(temporary)
        stage.chmod(0o700)
        for name, expected in checksums.items():
            target = stage / name
            with backup_regular(source / name) as incoming, os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as outgoing:
                shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
                outgoing.flush()
                os.fsync(outgoing.fileno())
            if backup_hash(target) != expected:
                refuse("Backup verification failed while copying. No copy was published.")
        O.save(stage / "backup.json", {"version": 1, "id": identifier,
               "checksums": checksums, "lastVerified": O.now()})
        verify_backup_copy(stage, identifier, source)
        rename_exclusive(stage, destination)


def backup_row(identifier, copy=None, source=None, reason=""):
    row = {"id": identifier, "copyID": identifier, "backedUp": False,
           "lastVerified": None, "missing": True, "status": "Not backed up", "reason": reason}
    if copy is not None:
        try:
            receipt = backup_receipt(copy, identifier)
            row["lastVerified"] = receipt["lastVerified"]
            row["missing"] = any((copy / n).is_symlink() or not (copy / n).is_file() for n in receipt["checksums"])
            if source is not None:
                manifest = backup_manifest(source, identifier)
                if any(receipt["checksums"][n] != h for n, h in manifest["checksums"].items()):
                    row["reason"] = "The backup copy does not match the Freezer archive."
            if not row["missing"] and not row["reason"]:
                row.update(backedUp=True, status="Backed up")
        except (O.Refusal, OSError, ValueError, TypeError):
            if not row["reason"]:
                row["reason"] = "No valid backup copy is available."
    if not row["backedUp"] and not row["reason"]:
        row["reason"] = "A recovery file is missing from the backup." if row["missing"] else "The copy has not been verified."
    return row


def backup_status():
    result = {"configured": bool(O.CONFIG.get("freezerBackupRoot")), "available": False,
              "reason": "", "archives": [], "orphans": [],
              "cloud": False, "acceptanceRoot": "", "cloudAccepted": False}
    try:
        result.update(backup_destination())
        sources = backup_sources()
    except (O.Refusal, OSError, ValueError, TypeError, KeyError) as error:
        result["reason"] = result["error"] = backup_reason(error)
        return result
    reason, base, copies = "", None, []
    try:
        base = backup_base()
        if base.exists():
            copies = sorted(base.iterdir())
    except (O.Refusal, OSError, ValueError) as error:
        reason = backup_reason(error)
        base = None
        if result["configured"]:
            result["error"] = reason
    failures = backup_failures()
    archives = [backup_row(identifier, base / identifier if base else None, source,
                           reason or failures.get(identifier, "")) for identifier, source in sorted(sources.items())]
    orphans = []
    for copy in copies:
        if re.fullmatch(r"[0-9a-f]{32}", copy.name) and copy.name not in sources:
            orphans.append(backup_row(copy.name, copy, reason=failures.get(copy.name, "")))
    result.update(available=base is not None, reason=reason, archives=archives, orphans=orphans)
    return result


def backup_run(identifiers=None):
    return backup_process(False, identifiers)


def backup_verify():
    return backup_process(True)


def backup_process(verify_only, identifiers=None):
    base = backup_base(create=not verify_only)
    sources = backup_sources()
    selected = set(identifiers) if identifiers is not None else set(sources)
    if verify_only and base.exists():
        selected.update(p.name for p in base.iterdir() if re.fullmatch(r"[0-9a-f]{32}", p.name))
    failures = backup_failures()
    failed = False
    for identifier in sorted(selected):
        try:
            if not re.fullmatch(r"[0-9a-f]{32}", identifier):
                refuse("Invalid backup copy identifier.")
            if verify_only:
                verify_backup_copy(base / identifier, identifier, sources.get(identifier))
            else:
                write_backup_copy(base, identifier, sources[identifier])
            failures.pop(identifier, None)
        except (O.Refusal, OSError, ValueError, TypeError, KeyError) as error:
            failures[identifier] = backup_reason(error)
            failed = True
    save_backup_failures(failures)
    if failed:
        refuse("Backup verification failed for one or more archives. See backup-status for each reason.")
    result = backup_status()
    result["message"] = "Freezer backup verified." if verify_only else "Freezer backup completed."
    return result


def backup_remove(identifier):
    if not re.fullmatch(r"[0-9a-f]{32}", identifier):
        refuse("Invalid backup copy identifier.")
    base = backup_base()
    if identifier in backup_sources():
        refuse("This archive is still in the Freezer. Its backup copy cannot be removed.")
    copy = base / identifier
    backup_receipt(copy, identifier)
    # Delete only recognized flat files, never traverse directories or links.
    entries = list(copy.iterdir())
    if any(p.name not in BACKUP_FILES | {"manifest.json", "backup.json"} or p.is_symlink() or not p.is_file() for p in entries):
        refuse("The backup copy contains unexpected files. Review it manually.")
    for path in entries:
        path.unlink()
    copy.rmdir()
    failures = backup_failures()
    failures.pop(identifier, None)
    save_backup_failures(failures)
    return {"copyID": identifier, "removed": True, "message": "Copy removed from backup."}


def vault_entries():
    base = O.VAULT
    rows = []
    try:
        if not base.exists() or base.is_symlink():
            return []
        entries = list(base.iterdir())
    except OSError:
        return []  # backup-status carries the lookup error to the native caller.
    for p in entries:
        try:
            if p.is_symlink() or not p.is_dir() or not re.fullmatch(r"[0-9a-f]{32}", p.name):
                continue
            m = O.load(p / "manifest.json")
            if m and m.get("id") == p.name:
                rows.append({"id": m["id"], "name": m["name"], "path": m["path"], "state": m["state"],
                             "created": m["created"], "destination": m["destination"], "message": m.get("message", ""),
                             "archiveLocation": str(p), "checkouts": len(m["capture"]["roles"]),
                             "paths": [r["path"] for r in m["capture"]["roles"]],
                             "leftovers": [v["temporary"] for v in m.get("moves", []) if Path(v["temporary"]).exists()]})
        except (OSError, ValueError, KeyError):
            continue
    status_error = ""
    try:
        status = backup_status()
        statuses = {row["id"]: row for row in status["archives"]}
        status_error = status.get("error", "")
    except (O.Refusal, OSError, ValueError, TypeError, KeyError) as error:
        statuses = {}
        status_error = backup_reason(error)
    for row in rows:
        if row["id"] in statuses:
            row["backup"] = statuses[row["id"]]
        elif status_error:
            row["backup"] = dict(backup_row(row["id"], reason=status_error), error=status_error)
    return sorted(rows, key=lambda r: r["created"], reverse=True)


def dispatch(command, args):
    if command in {"backup-run", "backup-verify", "backup-status"}:
        if args:
            refuse("This backup command takes no arguments.")
        return {"backup-run": backup_run, "backup-verify": backup_verify, "backup-status": backup_status}[command]()
    if command == "backup-remove":
        if len(args) != 1:
            refuse("Choose one backup copy identifier to remove.")
        return backup_remove(args[0])
    if command == "github-status": return github_status()
    if command == "remote-add": return connect_remote(*args)
    if command == "github-create": return create_private_repo(*args)
    if command == "commit-preview": return preview_commit(args[0], args[1:])
    if command == "commit-execute": return execute_commit(*args)
    if command == "freeze-preview": return preview_freeze(*args)
    if command == "freeze-blockers": return freeze_blockers(*args)
    if command == "freeze-execute": return execute_freeze(*args)
    if command == "freeze-retire": return freeze_retire(*args)
    if command == "rehydrate": return rehydrate(args[0], offline=len(args)>1 and args[1]=="offline")
    if command == "vault-list": return {"archives": vault_entries(), "backup": backup_status()}
    if command == "github-repos": return github_repositories(*args)
    if command == "clone-preview": return preview_clones(args[0], args[1:])
    if command == "clone-execute": return execute_clones(*args)
    refuse("Unknown lifecycle action.")


def github_identity(url):
    from urllib.parse import urlsplit
    match = re.fullmatch(r"git@github\.com:([^/]+/[^/]+)/?", url, re.I)
    if match:
        return match.group(1).removesuffix(".git").casefold()
    try:
        parsed = urlsplit(url)
        if parsed.hostname and parsed.hostname.casefold() == "github.com":
            return parsed.path.strip("/").removesuffix(".git").casefold()
    except ValueError:
        pass
    return None


def known_github_clones():
    known = {}
    for project in O.snapshot()["projects"]:
        if not project.get("git"):
            continue
        for remote in project.get("remotes", []):
            identity = github_identity(remote["url"])
            if identity and Path(project["path"]).is_dir():
                known.setdefault(identity, []).append(project["path"])
    for row in O.load(O.STATE / "clones.json", []):
        p = Path(row["path"])
        if O.under(p, O.ROOT) and p.is_dir() and (p / ".git").exists():
            rc, url = git(p, "remote", "get-url", "origin")
            identity = github_identity(url.strip()) if not rc else None
            if identity:
                known.setdefault(identity, []).append(str(p))
    return {k: sorted(set(v)) for k, v in known.items()}


def github_repositories(owner):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", owner):
        refuse("Enter a GitHub account or organization name.")
    rc, data = gh("repo", "list", owner, "--limit", "1000", "--json",
                  "name,nameWithOwner,url,isPrivate,isArchived,description,pushedAt")
    if rc:
        refuse("Repositories could not be listed. Check the owner name, GitHub access and gh authentication.")
    values = json.loads(data)
    known = known_github_clones()
    repos = []
    for r in values:
        slug = r["nameWithOwner"]
        paths = known.get(slug.casefold(), [])
        repos.append({"id": slug, "name": r["name"], "owner": slug.split("/")[0], "slug": slug,
                      "url": r["url"], "isPrivate": r["isPrivate"], "archived": r["isArchived"],
                      "description": r.get("description") or "", "pushedAt": r.get("pushedAt") or "",
                      "alreadyLocal": bool(paths), "localPaths": paths})
    return {"repositories": sorted(repos, key=lambda r: r["slug"].casefold()),
            "owner": owner, "truncated": len(values) >= 1000,
            "message": "Local matches use configured GitHub URLs from your latest analysis and recent app clones."}


def clone_parent(value):
    p = Path(value).expanduser()
    if not p.is_absolute() or ".." in p.parts or not O.under(p, O.ROOT) or not p.is_dir():
        refuse("Choose an existing clone folder inside the configured Projects root.")
    if O.under(p, O.APP) or O.under(p, O.VAULT) or O.under(p, O.STATE):
        refuse("Choose a project folder outside application, state and archive storage.")
    current = O.ROOT
    for part in p.relative_to(O.ROOT).parts:
        current = current / part
        if current.is_symlink():
            refuse("Clone destinations cannot pass through symbolic links.")
    return p


def preview_clones(parent, slugs):
    parent = clone_parent(parent)
    if not slugs:
        refuse("Select at least one GitHub repository.")
    items, names = [], set()
    for slug in dict.fromkeys(slugs):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]{1,100}", slug) or slug.split("/")[1] in {".", ".."}:
            refuse("A selected GitHub repository name is invalid.")
        name = slug.split("/")[1]
        if name.casefold() in names:
            refuse("Two repositories have the same folder name. Clone them into separate folders.")
        names.add(name.casefold())
        target = parent / name
        if target.exists() or target.is_symlink():
            refuse("The destination already exists: " + str(target))
        items.append({"slug": slug, "path": str(target), "url": "https://github.com/" + slug + ".git"})
    plan = {"id": uuid.uuid4().hex, "created": O.now(), "parent": str(parent), "items": items}
    O.save(plan_path("clone", plan["id"]), plan)
    return plan


def execute_clones(identifier):
    location, plan = reviewed_plan("clone", identifier)
    parent = clone_parent(plan["parent"])
    plan["executed"] = O.now()
    O.save(location, plan)
    results = []
    for item in plan["items"]:
        try:
            if Path(item["path"]).parent != parent:
                refuse("A clone destination changed.")
            target = parent / item["slug"].split("/")[1]
            if str(target) != item["path"] or target.exists() or target.is_symlink():
                refuse("The destination now exists. Nothing was overwritten.")
            O.progress("Cloning " + item["slug"] + "…")
            with tempfile.TemporaryDirectory(prefix=".project-clone-", dir=parent) as directory:
                stage = Path(directory) / "repository"
                rc, _ = git(parent, "clone", "--no-hardlinks", "--", item["url"], str(stage), timeout=600)
                if rc:
                    refuse("Clone failed. Check GitHub access; existing folders were not changed.")
                rc, url = git(stage, "remote", "get-url", "origin")
                if rc or github_identity(url.strip()) != item["slug"].casefold():
                    refuse("The cloned repository's identity could not be verified.")
                rename_exclusive(stage, target)
            note = "Cloned " + item["slug"]
            try:
                journal = O.load(O.STATE / "clones.json", [])
                journal.append({"slug": item["slug"], "path": str(target), "at": O.now()})
                O.save(O.STATE / "clones.json", journal)
            except (OSError, ValueError):
                note += ". Local index update failed; run Analyse Projects."
            results.append({"path": str(target), "ok": True, "message": note})
        except (O.Refusal, OSError):
            results.append({"path": item["path"], "ok": False,
                            "message": "Not cloned. Check access or a destination conflict; existing folders were preserved."})
    good = sum(r["ok"] for r in results)
    return {"clones": results, "message": f"Cloned {good} of {len(results)} repositories. Run Analyse Projects to index the new checkouts."}
