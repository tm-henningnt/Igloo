"""Shared user configuration. Paths and defaults only; never credentials."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile


def config_path(home=None):
    return Path(home or Path.home()) / ".config/igloo/config.json"


def defaults(home=None):
    home = Path(home or Path.home())
    root = home / "Projects"
    def executable(name, choices):
        found = shutil.which(name)
        return found or next((str(p) for p in map(Path, choices) if p.is_file()), choices[-1])
    return {"version": 1, "projectsRoot": str(root),
            "archiveRoot": str(home / ".igloo/vault"), "cloneRoot": str(root),
            "stateRoot": str(home / ".igloo"),
            "freezerBackupRoot": "", "freezerBackupCloudAcceptedRoot": "",
            "quietDays": 60, "githubOwner": "",
            "pythonExecutable": executable("python3", ["/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"]),
            "gitExecutable": executable("git", ["/usr/bin/git"]),
            "githubExecutable": executable("gh", ["/opt/homebrew/bin/gh", "/usr/local/bin/gh"])}


def backup_root_identity(value):
    """Stable destination identity for consent on case-insensitive macOS disks."""
    return str(Path(value).expanduser().resolve()).casefold()


def validate(value):
    if value.get("version") != 1:
        raise ValueError("Unsupported Igloo configuration version.")
    result = dict(value)
    # A legacy global flag cannot authorize an unidentified destination.
    result.pop("freezerBackupCloudAccepted", None)
    for key in ("freezerBackupRoot", "freezerBackupCloudAcceptedRoot"):
        raw = result.setdefault(key, "")
        if not isinstance(raw, str) or "\x00" in raw or (raw and not raw.strip()):
            raise ValueError("Set a valid path for " + key + ", or leave it empty.")
        if raw:
            p = Path(raw).expanduser()
            if not p.is_absolute() or ".." in p.parts:
                raise ValueError(key + " must be an absolute path without traversal.")
            result[key] = backup_root_identity(p) if key == "freezerBackupCloudAcceptedRoot" else str(p)
    for key in ("projectsRoot", "archiveRoot", "stateRoot", "cloneRoot", "pythonExecutable", "gitExecutable", "githubExecutable"):
        raw = result.get(key)
        if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
            raise ValueError("Set a valid path for " + key + ".")
        p = Path(raw).expanduser()
        if not p.is_absolute():
            raise ValueError(key + " must be an absolute path.")
        result[key] = str(p)
    root = Path(result["projectsRoot"])
    home = Path.home()
    if root in {Path("/"), home, home.parent}:
        raise ValueError("Choose a project workspace, not the whole home or filesystem.")
    for key in ("archiveRoot", "stateRoot"):
        p = Path(result[key])
        if p == root or p in root.parents or p in {Path("/"), home, home.parent}:
            raise ValueError(key + " must be a dedicated subfolder.")
    age = result.get("quietDays")
    if type(age) is not int or not 1 <= age <= 3650:
        raise ValueError("quietDays must be between 1 and 3650.")
    owner = result.get("githubOwner", "")
    if not isinstance(owner, str) or any(c.isspace() for c in owner):
        raise ValueError("The GitHub owner default must be an account or organization name.")
    return result


def load(path=None, home=None):
    path = Path(path or config_path(home))
    base = defaults(home)
    try:
        with path.open() as f:
            value = json.load(f)
    except FileNotFoundError:
        return validate(base)
    if not isinstance(value, dict):
        raise ValueError("The configuration must be a JSON object.")
    merged = dict(base, **value)
    if "projectsRoot" in value:
        if "cloneRoot" not in value: merged["cloneRoot"] = value["projectsRoot"]
    return validate(merged)


def write(value, path=None):
    value = validate(value)
    path = Path(path or config_path())
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    fd, temporary = tempfile.mkstemp(prefix=".config-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(value, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
