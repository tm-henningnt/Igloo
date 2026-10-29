"""Shared user configuration. Paths and defaults only; never credentials."""
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile


RELEASE_ID = "local.igloo"
DEV_ID = "local.igloo.dev"


def app_id():
    """The app passes its bundle ID in IGLOO_APP_ID. The CLI and other callers use the release."""
    value = os.environ.get("IGLOO_APP_ID") or RELEASE_ID
    if value not in {RELEASE_ID, DEV_ID}:
        raise ValueError("Unknown Igloo app identity.")
    return value


def is_dev():
    return app_id() == DEV_ID


def config_path(home=None):
    folder = ".config/igloo.dev" if is_dev() else ".config/igloo"
    return Path(home or Path.home()) / folder / "config.json"


def app_bundle(path):
    """The .app folder that contains path, or None."""
    parts = Path(path).parts
    for index, part in enumerate(parts[:-1]):
        if part.endswith(".app") and parts[index + 1] == "Contents":
            return Path(*parts[:index + 1])
    return None


def foreign_bundle_path(path):
    """True for a path inside an app bundle other than the bundle that runs this backend."""
    if not isinstance(path, str) or not path:
        return False
    own = app_bundle(Path(__file__).resolve())
    other = app_bundle(Path(path).expanduser().resolve())
    return own is not None and other is not None and other != own


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
            "quietDays": 60, "quietReminderEnabled": False,
            "quietReminderWeekday": 1, "quietReminderTime": "09:00", "githubOwner": "",
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
    if type(result.get("quietReminderEnabled")) is not bool:
        raise ValueError("quietReminderEnabled must be true or false.")
    weekday = result.get("quietReminderWeekday")
    if type(weekday) is not int or not 0 <= weekday <= 6:
        raise ValueError("quietReminderWeekday must be a weekday from Sunday (0) through Saturday (6).")
    if not isinstance(result.get("quietReminderTime"), str) or not re.fullmatch(
            r"(?:[01]\d|2[0-3]):[0-5]\d", result["quietReminderTime"]):
        raise ValueError("quietReminderTime must use 24-hour HH:MM time.")
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
    # A bundled Python of another app copy is a runtime choice of that copy. Use the default; keep the file.
    if foreign_bundle_path(merged.get("pythonExecutable")):
        merged["pythonExecutable"] = base["pythonExecutable"]
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
