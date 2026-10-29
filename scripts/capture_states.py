#!/usr/bin/env python3
"""Launch independent demo captures from an already built Igloo app on macOS."""
import argparse
import colorsys
import ctypes
import fnmatch
import json
import os
import math
import plistlib
import re
import shlex
import signal
import time
import stat
import struct
import zlib
from collections import Counter
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


SHEET_VIEWS = {"freeze", "shelf", "freeze-review", "preserve-review", "freeze-result", "freeze-failure",
               "freeze-hold", "freeze-no-remote", "freeze-no-files", "freeze-no-commit", "freeze-own-source", "freeze-process",
               "cleanup-review", "cleanup-result", "cleanup-hold", "trash-history", "push-review", "wizard-review", "wizard-result",
               "wizard-start", "wizard-thaw-selection", "wizard-thaw-review", "wizard-thaw-ready", "wizard-thaw-result",
               "wizard-thaw-occupied", "wizard-thaw-remote", "wizard-thaw-corrupt", "wizard-thaw-not-frozen",
               "wizard-thaw-rehydrating",
               "task-review", "task-history", "settings", "settings-cloud", "repositories-loading",
               "help-getting-started", "help-freeze-thaw", "help-safety-storage", "tasks-rules", "tasks-schedule",
               "space-sunburst", "space-icicle", "space-marimekko", "space-drilldown", "repositories",
               "remote-setup", "commit-setup", "wizard-rules", "wizard-actions"}
# Main-window views that open one Git section or the scope inspector on the selected project.
DETAIL_VIEWS = ("git-branches", "git-saved-history", "git-remotes", "scope-selected")
UPDATE_FAILURES = ("quota", "quota-no-reset", "forbidden", "offline", "timeout", "server", "malformed",
                   "cancelled-download", "digest-mismatch")
SHEET_VIEWS.update("update-" + name for name in UPDATE_FAILURES)
SHEET_VIEWS.update({"update-markdown", "update-ready", "update-install-failed"})

# Each locator names a role, the visible name that cua-driver matches, a
# 1-based ordinal among the matching elements, and anchor text that the app
# must show before the step acts. A wait anchor is text that only the next
# screen shows.
INTERACTIVE_FLOWS = {
    "wizard-thaw": [
        {"click": {"role": "button", "name": "Thaw projects", "ordinal": 1, "anchor": "Choose a path"},
         "capture": "01-selection", "wait_for": {"anchor": "Select projects"}},
        {"click": {"role": "checkbox", "name": "Ice Garden", "ordinal": 1, "anchor": "Select projects"},
         "capture": "02-selected", "expect": {"value": True}},
        {"click": {"role": "button", "name": "Create Thaw plan", "ordinal": 1, "anchor": "Select projects"},
         "capture": "03-plan", "wait_for": {"anchor": "Expected outcome"}},
        {"click": {"role": "checkbox", "name": "I reviewed the remote refs", "ordinal": 1, "anchor": "Thaw plan"},
         "capture": "05-confirmed", "expect": {"value": True},
         "expect_control": {"locator": {"role": "button", "name": "Thaw 1 ready project", "ordinal": 1,
                                        "anchor": "Thaw plan"},
                            "enabled": True}},
        {"click": {"role": "button", "name": "Thaw 1 ready project", "ordinal": 1, "anchor": "Thaw plan"},
         "capture": "06-result", "wait_for": {"anchor": "Local-only files restored"},
         "expect_labels": ["Local-only files restored"]},
    ]
}


def validate_locator(locator, name="Interactive click"):
    if not isinstance(locator, dict) or set(locator) - {"role", "name", "ordinal", "anchor", "label"}:
        raise ValueError(f"{name} requires a role, ordinal, and anchor")
    if not isinstance(locator.get("role"), str) or not locator["role"].strip():
        raise ValueError(f"{name} requires a role")
    if type(locator.get("ordinal")) is not int or locator["ordinal"] < 1:
        raise ValueError(f"{name} ordinal must be a positive integer")
    if not isinstance(locator.get("anchor"), str) or not locator["anchor"].strip():
        raise ValueError(f"{name} requires an anchor")
    for field in ("name", "label"):
        if field in locator and (not isinstance(locator[field], str) or not locator[field].strip()):
            raise ValueError(f"{name} {field} must be non-empty text")
    if not locator.get("name") and not locator.get("label"):
        raise ValueError(f"{name} requires a name or label")
    return dict(locator)


def parse_interactive_steps(steps):
    if not isinstance(steps, list) or not steps:
        raise ValueError("Interactive steps must be a non-empty list")
    parsed = []
    for step in steps:
        if not isinstance(step, dict):
            raise ValueError("Interactive step must be an object")
        unknown = set(step) - {"click", "key", "capture", "wait_for", "wait_for_selection",
                               "expect", "expect_control", "expect_labels"}
        if unknown:
            raise ValueError("Interactive step contains an unknown field")
        has_click, has_key = "click" in step, "key" in step
        if has_click == has_key:
            raise ValueError("Interactive step requires exactly one click or key action")
        click, capture_name = step.get("click"), step.get("capture")
        if has_click:
            click = validate_locator(click)
        else:
            key = step.get("key")
            if key not in ("Up", "Down"):
                raise ValueError("Interactive key must be Up or Down")
            if "wait_for_selection" not in step or "wait_for" in step or "expect" in step or "expect_control" in step:
                raise ValueError("Interactive key requires a selection wait and cannot declare click expectations")
            selection = validate_locator(step["wait_for_selection"], "Interactive selection")
            if selection["role"] != "outline row" or not selection.get("label"):
                raise ValueError("Interactive selection requires a labeled outline row")
        if has_click and "wait_for_selection" in step:
            raise ValueError("Interactive selection wait requires a key action")
        if (not isinstance(capture_name, str) or not capture_name or
                any(char not in "abcdefghijklmnopqrstuvwxyz0123456789-" for char in capture_name)):
            raise ValueError("Interactive capture name is invalid")
        wait_for = step.get("wait_for")
        if wait_for is not None and (not isinstance(wait_for, dict) or set(wait_for) != {"anchor"} or
                                     not isinstance(wait_for["anchor"], str) or not wait_for["anchor"].strip()):
            raise ValueError("Interactive wait requires an anchor")
        expect = step.get("expect")
        if expect is not None and (not isinstance(expect, dict) or set(expect) != {"value"} or
                                   type(expect["value"]) is not bool):
            raise ValueError("Interactive expectation must declare a boolean value")
        control = step.get("expect_control")
        if control is not None and (not isinstance(control, dict) or set(control) != {"locator", "enabled"} or
                                    type(control["enabled"]) is not bool):
            raise ValueError("Interactive control expectation is invalid")
        if control is not None:
            control["locator"] = validate_locator(control["locator"], "Interactive control")
        labels = step.get("expect_labels", [])
        if not isinstance(labels, list) or any(not isinstance(label, str) or not label for label in labels):
            raise ValueError("Interactive expected labels must be non-empty strings")
        parsed_step = {**step}
        if has_click:
            parsed_step["click"] = click
        else:
            parsed_step["wait_for_selection"] = selection
        parsed.append(parsed_step)
    names = [step["capture"] for step in parsed]
    if len(names) != len(set(names)):
        raise ValueError("Interactive capture names must be unique")
    return parsed


def has_sheet_image(view):
    return view in SHEET_VIEWS or view.startswith("update-")


# The app skips the offscreen sheet render for these views (see DemoCatalog); the real-pixel capture covers them.
NO_SHEET_RENDER_VIEWS = {"tasks-rules", "tasks-schedule"}


def renderer_path(folder, kind):
    if kind not in ("window", "sheet"):
        raise ValueError("Unknown renderer artifact kind")
    return folder / "render" / f"{kind}-render.png"


def scenarios():
    rows = []
    for appearance in ("light", "dark"):
        cases = [(view, "1440x900", "shown", "full", "normal") for view in
                 ("overview", "git", "worktrees", "local-files", "cleanup", "plan", "freeze", "shelf")]
        update_scope = {"850x700": ("hidden", "compact"), "1440x900": ("shown", "full")}
        cases += [("update-" + name, size, *update_scope[size], "normal")
                  for name in UPDATE_FAILURES for size in ("850x700", "1440x900")]
        cases += [("update-markdown", size, "hidden", "compact", "normal")
                  for size in ("850x700", "1440x900")]
        cases.append(("update-ready", "1440x900", "shown", "full", "normal"))
        cases += [("update-install-failed", size, "hidden", "compact", "normal") for size in ("850x700", "1440x900")]
        cases += [("overview", "850x700", "hidden", "compact", "normal"),
                  ("plan", "850x700", "hidden", "compact", "normal"),
                  ("overview", "1100x700", "hidden", "full", "normal"),
                  ("plan", "1100x700", "hidden", "full", "normal"),
                  ("overview", "1300x700", "shown", "full", "normal"),
                  ("plan", "1300x700", "shown", "full", "normal"),
                  ("overview", "1440x900", "hidden", "compact", "long"),
                  ("overview", "1440x900", "shown", "full", "empty")]
        cases += [(view, size, *update_scope[size], "normal") for view in DETAIL_VIEWS
                  for size in ("850x700", "1440x900") if view != "scope-selected"]
        cases += [("scope-selected", size, "shown", "full", "normal") for size in ("1300x700", "1440x900")]
        cases += [(view, "1300x700", "shown", "full", "normal")
                  for view in ("cleanup-result", "freeze-result", "wizard-result", "task-history", "trash-history")]
        # Minimum-size row checks must reach 850 points. Cover every rail and
        # scope combination at the default size, including long names.
        for size in ("850x700", "1440x900"):
            combinations = (("hidden", "compact"),) if size == "850x700" else tuple(
                (scope, rail) for scope in ("shown", "hidden") for rail in ("compact", "full"))
            for scope, rail in combinations:
                for data in ("normal", "long"):
                    case = ("overview", size, scope, rail, data)
                    if case not in cases:
                        cases.append(case)
        added_views = sorted(SHEET_VIEWS | {"hold"})
        cases += [(view, "1440x900", "shown", "full", "normal") for view in added_views
                  if view not in {"freeze", "shelf"} and not view.startswith("update-")]
        cases += [(view, "850x700", "hidden", "compact", "normal") for view in added_views
                  if not view.startswith("update-")]
        for view, size, scope, rail, data in cases:
            name = "-".join((appearance, view, size, scope, rail, data))
            rows.append(dict(name=name, appearance=appearance, view=view, size=size, scope=scope, rail=rail, data=data))
        rows.append(dict(name="-".join((appearance, "wizard-thaw-flow", "1440x900", "shown", "full", "normal")),
                         appearance=appearance, view="wizard-start", size="1440x900", scope="shown",
                         rail="full", data="normal", flow="wizard-thaw"))
    return rows


SCENARIO_SOURCE_PATTERNS = {
    "app/AboutView.swift": {"update-*"},
    "app/DemoCatalog.swift": {"*"},
    "app/DetailViews.swift": {"*"},
    "app/Igloo.swift": {"*"},
    "app/LifecycleViews.swift": {"*"},
    "app/NavigationViews.swift": {"*"},
    "app/PreferencesViews.swift": {"*"},
    "app/ReviewViews.swift": {"*"},
    "app/ScopeInspector.swift": {"*"},
    "app/SpaceViews.swift": {"*"},
    "app/TasksView.swift": {"*"},
    "app/UpdateInstaller.swift": {"update-*"},
    "app/Updates.swift": {"update-*"},
    "app/WizardViews.swift": {"*"},
}


def scenarios_for_changed_paths(paths, matrix=None):
    matrix = scenarios() if matrix is None else matrix
    affected = set()
    for raw_path in paths:
        path = Path(raw_path).as_posix()
        if not (path.startswith("app/") and path.endswith(".swift")):
            continue
        patterns = SCENARIO_SOURCE_PATTERNS.get(path, {"*"})
        affected.update(row["name"] for row in matrix
                        if any(fnmatch.fnmatchcase(row["view"], pattern) for pattern in patterns))
    return [row for row in matrix if row["name"] in affected]


def changed_app_paths(since, repo=ROOT):
    resolved = subprocess.run(["git", "rev-parse", "--verify", "--end-of-options", f"{since}^{{commit}}"],
                              cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
    if not resolved:
        raise ValueError("--since did not resolve to a commit")
    result = subprocess.run(["git", "diff", "--name-only", resolved, "--", "app"], cwd=repo,
                            check=True, capture_output=True, text=True)
    untracked = subprocess.run(["git", "ls-files", "--others", "--exclude-standard", "--", "app"], cwd=repo,
                               check=True, capture_output=True, text=True)
    return sorted({line.strip() for output in (result.stdout, untracked.stdout)
                   for line in output.splitlines() if line.strip()})


def select_scenarios(matrix, explicit=None, since=None, repo=ROOT):
    explicit = set(explicit or ())
    known = {row["name"] for row in matrix}
    unknown = explicit - known
    if unknown:
        raise ValueError("unknown scenario: " + ", ".join(sorted(unknown)))
    if not since and not explicit:
        return list(matrix)
    selected = set(explicit)
    if since:
        selected.update(row["name"] for row in scenarios_for_changed_paths(changed_app_paths(since, repo), matrix))
    return [row for row in matrix if row["name"] in selected]


def build_launch_command(app, folder, scenario, window_server=False, backend=None):
    if not folder.is_absolute():
        raise ValueError("--demo-capture requires an absolute path")
    hold_open = backend in ("cua", "window-server")
    args = ["/usr/bin/open", "-n"]
    if hold_open or window_server:
        args.append("-W")
    args += [str(app), "--args", "--demo", "--demo-" + scenario["appearance"],
            "--demo-size", scenario["size"], "--demo-view", scenario["view"], "--demo-scope", scenario["scope"],
            "--demo-rail", scenario["rail"], "--demo-data", scenario["data"], "--demo-capture", str(folder),
            # After a crash, macOS can ask to reopen windows and block every later launch.
            "-ApplePersistenceIgnoreState", "YES"]
    if hold_open or window_server:
        args.append("--demo-window-server")
    if backend == "cua":
        args.append("--demo-driver")
    return args


def validate_windows(data):
    if not isinstance(data, dict) or type(data.get("pid")) is not int or data["pid"] <= 0:
        raise ValueError("Invalid window metadata PID")
    windows = data.get("windows")
    if not isinstance(windows, list) or not windows or len(windows) > 2:
        raise ValueError("Invalid window metadata list")
    kinds = set()
    for window in windows:
        if not isinstance(window, dict) or window.get("kind") not in ("window", "sheet") or window["kind"] in kinds:
            raise ValueError("Invalid window kind")
        kinds.add(window["kind"])
        if type(window.get("windowNumber")) is not int or window["windowNumber"] <= 0:
            raise ValueError("Invalid window number")
        if window.get("isOnScreen") is not None and type(window.get("isOnScreen")) is not bool:
            raise ValueError("Invalid on-screen state")
        if "isOnScreen" not in window:
            raise ValueError("Missing on-screen state")
        frame = window.get("frame")
        if not isinstance(frame, dict) or any(type(frame.get(key)) not in (int, float) or not math.isfinite(frame[key])
                                              for key in ("x", "y", "width", "height")):
            raise ValueError("Invalid window frame")
        if frame["width"] <= 0 or frame["height"] <= 0:
            raise ValueError("Invalid window frame size")
    if "window" not in kinds:
        raise ValueError("Missing main window")
    return windows


def capture_window_server(folder):
    try:
        data = json.loads((folder / "windows.json").read_text())
    except (OSError, ValueError) as error:
        raise ValueError(f"Missing or invalid window metadata: {error}") from error
    windows = validate_windows(data)
    pid = started_pid(folder)
    if pid is None or data["pid"] != pid:
        raise ValueError("Window metadata PID mismatch")
    results = []
    for window in windows:
        kind = window["kind"]
        if window["isOnScreen"] is not True:
            results.append({"kind": kind, "status": "OPEN", "reason": "display asleep or locked, or window on-screen state unavailable"})
            continue
        path = folder / ("window-ws.png" if kind == "window" else "sheet-ws.png")
        try:
            if path.exists() or path.is_symlink():
                raise ValueError("window-server output already exists")
            subprocess.run(["/usr/sbin/screencapture", "-o", "-x", "-l", str(window["windowNumber"]), str(path)],
                           check=True, timeout=20, umask=0o077, capture_output=True)
            path.chmod(0o600)
            share = png_nonbackground_share(path.read_bytes(), allow_sparse_sheet=kind == "sheet")
            results.append({"kind": kind, "status": "PARTIAL", "reason": f"captured; non-background share {share:.3%}; visual review required"})
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            results.append({"kind": kind, "status": "OPEN", "reason": str(error)})
    return results


class CaptureError(ValueError):
    """The capture driver could not read or act on the launched demo window."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.message = message
        self.code = code

    def __str__(self):
        return f"{self.code}: {self.message}" if self.code else self.message

def process_executable_path(pid):
    if sys.platform != "darwin":
        raise ValueError("Process executable path lookup requires macOS")
    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    proc_pidpath = library.proc_pidpath
    proc_pidpath.argtypes = (ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32)
    proc_pidpath.restype = ctypes.c_int
    buffer = ctypes.create_string_buffer(4096)
    length = proc_pidpath(pid, buffer, len(buffer))
    if length <= 0:
        raise ValueError("Could not verify launched PID executable path")
    return Path(os.fsdecode(buffer.value))


def verify_target_process(pid, app):
    if type(pid) is not int or pid <= 0:
        raise ValueError("Invalid launched PID")
    expected = (app.resolve(strict=True) / "Contents/MacOS/Igloo").resolve(strict=True)
    actual = Path(process_executable_path(pid)).resolve(strict=True)
    if actual != expected:
        raise ValueError("Launched PID executable path does not match --app")
    return actual


def is_protected_app_bundle(app):
    app = Path(app)
    if app.resolve() == Path("/Applications/Igloo.app").resolve():
        return True
    info = app / "Contents" / "Info.plist"
    if not info.is_file():
        return False
    try:
        with info.open("rb") as source:
            metadata = plistlib.load(source)
    except (OSError, plistlib.InvalidFileException, ValueError) as error:
        raise ValueError("Could not read --app bundle identity") from error
    return isinstance(metadata, dict) and metadata.get("CFBundleIdentifier") == "local.igloo"


def stop_verified_process(pid, app):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    verify_target_process(pid, app)
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    return True


CUA_OUTPUT_LIMIT = 300
CUA_PROBE_LIMIT = 20000
CUA_ROLE_ALIASES = {"outline row": ("row", "outlinerow")}
CUA_ACCESSIBILITY_TREE_LIMIT = 1000


def _cap(text, limit=CUA_OUTPUT_LIMIT):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _cua_error(payload, tool):
    """Return a CaptureError for a cua-driver refusal or script failure, or None."""
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if isinstance(error, dict):
        code = error.get("code") if isinstance(error.get("code"), str) else None
        message = error.get("message") if isinstance(error.get("message"), str) else f"cua-driver {tool} failed"
        line = error.get("line")
        if type(line) is int:
            message = f"{message} (script line {line})"
        return CaptureError(_cap(message), code=code)
    if isinstance(payload.get("code"), str):
        detail = next((payload[key] for key in ("hint", "suggestion", "message")
                       if isinstance(payload.get(key), str)), f"cua-driver {tool} refused the call")
        return CaptureError(_cap(detail), code=payload["code"])
    return None


def cua_call(tool, arguments, timeout=60):
    """Run one `cua-driver call` and return its JSON object. Output is parsed, never printed."""
    command = shlex.split(os.environ.get("CUA_DRIVER_COMMAND", "cua-driver"))
    if not command:
        raise CaptureError("cua-driver command is unavailable")
    args = command + ["call", tool, json.dumps(arguments)]
    try:
        result = subprocess.run(args, check=False, timeout=timeout, capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError) as error:
        raise CaptureError(f"cua-driver {tool} could not run ({type(error).__name__})") from error
    payload = None
    for output in (result.stdout, result.stderr):
        try:
            candidate = json.loads(output) if output and output.strip() else None
        except (TypeError, ValueError):
            continue
        if isinstance(candidate, dict):
            payload = candidate
            break
    error = _cua_error(payload, tool)
    if error is not None:
        raise error
    if payload is None:
        if result.returncode != 0:
            raise CaptureError(f"cua-driver {tool} failed (exit {result.returncode})")
        raise CaptureError(f"cua-driver {tool} returned invalid data")
    if result.returncode != 0 or payload.get("ok") is False:
        raise CaptureError(f"cua-driver {tool} failed (exit {result.returncode})")
    return payload


def cua_session(pid):
    return f"igloo-capture-{pid}"


def cua_window_targets(pid, folder, session):
    """Map the app's own window metadata to window IDs that cua-driver lists for the same PID."""
    try:
        metadata = json.loads((folder / "windows.json").read_text())
    except (OSError, ValueError) as error:
        raise CaptureError(f"Missing or invalid window metadata: {error}") from error
    windows = validate_windows(metadata)
    if metadata["pid"] != pid:
        raise CaptureError("Window metadata PID mismatch")
    payload = cua_call("list_windows", {"pid": pid, "session": session})
    records = payload.get("windows")
    if not isinstance(records, list):
        raise CaptureError("cua-driver returned no window list")
    listed = {record["window_id"]: record for record in records
              if isinstance(record, dict) and record.get("pid") == pid and type(record.get("window_id")) is int}
    targets = {window["kind"]: window["windowNumber"] for window in windows if window["windowNumber"] in listed}
    if "window" not in targets:
        candidates = [record for record in listed.values() if record.get("layer") == 0]
        if not candidates:
            raise CaptureError("cua-driver lists no window for the launched PID")
        frontmost = max(candidates, key=lambda record: record["z_index"] if type(record.get("z_index")) is int else -1)
        targets["window"] = frontmost["window_id"]
    if "sheet" not in targets and any(window["kind"] == "sheet" for window in windows):
        targets["sheet"] = None
    return targets


def cua_screenshot(pid, window_id, folder, filename, session, kind="window"):
    """Save the real window pixels that cua-driver captures for one window ID."""
    path = folder / filename
    if path.exists() or path.is_symlink():
        raise CaptureError(f"{filename} already exists")
    cua_call("get_window_state", {"pid": pid, "window_id": window_id, "include_accessibility_tree": False,
                                  "max_image_dimension": 0, "screenshot_out_file": str(path), "session": session})
    try:
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            raise CaptureError("cua-driver screenshot is not a regular file")
        path.chmod(0o600)
        png = path.read_bytes()
    except CaptureError:
        raise
    except OSError as error:
        raise CaptureError("cua-driver wrote no screenshot") from error
    if len(png) < 1024:
        raise CaptureError("cua-driver screenshot is too small to be reliable")
    try:
        # Dark appearance screenshots fall mostly into one sampled colour bucket.
        share = png_nonbackground_share(png, allow_sparse_sheet=kind == "sheet", reject_placeholders=False,
                                        min_share=0.002)
    except ValueError as error:
        raise CaptureError(f"cua-driver screenshot is invalid: {error}") from error
    return {"kind": kind, "status": "PARTIAL", "path": filename,
            "reason": f"real-pixel screenshot captured; non-background share {share:.3%}; visual review required"}


def cua_capture_windows(pid, app, folder, stem, session):
    """Capture the main window and, when present, its sheet as `<stem>-cua.png` files."""
    verify_target_process(pid, app)
    targets = cua_window_targets(pid, folder, session)
    results = [cua_screenshot(pid, targets["window"], folder, f"{stem}-cua.png", session)]
    if "sheet" in targets:
        if targets["sheet"] is None:
            results.append({"kind": "sheet", "status": "OPEN",
                            "reason": "cua-driver does not list the sheet window; review the sheet in "
                                      f"{stem}-cua.png"})
        else:
            results.append(cua_screenshot(pid, targets["sheet"], folder, f"{stem}-sheet-cua.png", session,
                                          kind="sheet"))
    return results


def cua_target(locator):
    """Map a step locator to a cua target: role aliases, a name, and a 0-based nth."""
    name = locator.get("name") or locator.get("label")
    if not name:
        raise ValueError("A cua target requires a name")
    role = locator["role"].casefold()
    roles = CUA_ROLE_ALIASES.get(role, ("".join(char for char in role if char.isalpha()),))
    return {"roles": list(roles), "name": name, "nth": locator["ordinal"] - 1}


# The step script runs inside the cua-driver sandbox. It has no filesystem
# access, so the launcher saves each screenshot after the script returns.
CUA_STEP_SCRIPT = r"""
const p = __PARAMS__;
const app = await cua.getApp({pid: p.pid, windowId: p.windowId});
const norm = (value) => String(value ?? "").toLowerCase().replace(/^ax/, "").replace(/[^a-z]/g, "");
const strings = (row) => [row.label, row.value, row.text, row.name, row.title].filter((v) => typeof v === "string");
const truth = (value) => {
  if (value === true || value === 1) return true;
  if (value === false || value === 0) return false;
  const text = String(value ?? "").toLowerCase();
  if (["true", "1", "on", "checked"].includes(text)) return true;
  if (["false", "0", "off", "unchecked"].includes(text)) return false;
  return null;
};
async function rows(query) {
  try { return await app.query(query); } catch (error) { return []; }
}
async function shown(text) {
  const wanted = text.toLowerCase();
  for (const query of [{text}, {name: text}]) {
    const found = await rows(query);
    if (found.some((row) => strings(row).some((value) => value.toLowerCase().includes(wanted)))) return true;
  }
  return false;
}
async function waitShown(text) {
  for (let attempt = 0; attempt < p.attempts; attempt++) {
    if (await shown(text)) return;
    await cua.sleep(500);
  }
  throw new Error("timed out waiting for text: " + text);
}
async function find(target) {
  const wanted = target.name.toLowerCase();
  const matches = (await rows({name: target.name})).filter((row) =>
    row.element_token && target.roles.includes(norm(row.role)) &&
    strings(row).some((value) => value.toLowerCase().includes(wanted)));
  return matches[target.nth] ?? null;
}
async function waitFor(target, check, message) {
  for (let attempt = 0; attempt < p.attempts; attempt++) {
    const row = await find(target);
    if (row && check(row)) return row;
    await cua.sleep(500);
  }
  throw new Error(message);
}
await waitShown(p.anchor);
let acted = null;
if (p.click) {
  const row = await waitFor(p.click, (item) => item.enabled !== false,
                            `no enabled ${p.click.roles[0]} named ${p.click.name}`);
  await app.click(row.element_token);
  acted = {role: row.role, label: row.label};
} else {
  await app.pressKey(p.key);
  acted = {key: p.key};
}
if (p.waitFor) await waitShown(p.waitFor);
if (p.selection) {
  await waitFor(p.selection, (item) => item.selected === true,
                `key ${p.key} did not change selection to ${p.selection.name}`);
}
if (p.expectValue !== null) {
  await waitFor(p.click, (item) => truth(item.value) === p.expectValue, "the expected control value did not appear");
}
if (p.control) {
  await waitFor(p.control, (item) => (item.enabled !== false) === p.control.enabled,
                "the expected control state did not appear");
}
for (const label of p.labels) await waitShown(label);
return acted;
"""


def cua_step_script(pid, window_id, step, attempts=40):
    """Build the run_script body for one interactive step from its declarative definition."""
    click = step.get("click")
    selection = step.get("wait_for_selection")
    control = step.get("expect_control")
    params = {
        "pid": pid, "windowId": window_id, "attempts": attempts,
        "anchor": (click or selection)["anchor"],
        "click": cua_target(click) if click else None,
        "key": step["key"].lower() if "key" in step else None,
        "waitFor": step["wait_for"]["anchor"] if step.get("wait_for") else None,
        "selection": cua_target(selection) if selection else None,
        "expectValue": step["expect"]["value"] if step.get("expect") else None,
        "control": ({**cua_target(control["locator"]), "enabled": control["enabled"]} if control else None),
        "labels": list(step.get("expect_labels", [])),
    }
    return CUA_STEP_SCRIPT.replace("__PARAMS__", json.dumps(params))


def cua_run_step(pid, app, window_id, step, session):
    """Verify the PID, then run one step inside cua-driver and check its verified result."""
    verify_target_process(pid, app)
    payload = cua_call("run_script", {"session": session, "script": cua_step_script(pid, window_id, step),
                                      "timeout_ms": 90000, "max_calls": 400}, timeout=120)
    if payload.get("ok") is not True:
        raise CaptureError(f"cua-driver step {step['capture']} did not finish")
    return payload.get("value")


def _capture_cua_states(pid, app, folder, scenario, session):
    verify_target_process(pid, app)
    if not scenario.get("flow"):
        results = cua_capture_windows(pid, app, folder, "window", session)
        capture_cua_accessibility_tree(pid, folder, scenario, session)
        return results
    steps = parse_interactive_steps(INTERACTIVE_FLOWS[scenario["flow"]])
    results = list(cua_capture_windows(pid, app, folder, "00-start", session))
    capture_cua_accessibility_tree(pid, folder, scenario, session)
    window_id = cua_window_targets(pid, folder, session)["window"]
    for step in steps:
        try:
            cua_run_step(pid, app, window_id, step, session)
        except CaptureError as error:
            raise CaptureError(f"step {step['capture']}: {error}") from error
        results += cua_capture_windows(pid, app, folder, step["capture"], session)
    return results


def write_private_json(path, data):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        os.fchmod(output.fileno(), 0o600)
        json.dump(data, output, indent=2)
        output.write("\n")


def _wait_for_launched_capture(folder, process, timeout=45):
    deadline = time.monotonic() + timeout
    while True:
        pid = started_pid(folder)
        if pid is not None and (folder / "windows.json").is_file() and (folder / "accessibility.json").is_file():
            return pid
        if process.poll() is not None:
            raise CaptureError("App exited before capture metadata was written")
        if time.monotonic() >= deadline:
            # The app writes metadata only after it becomes active; macOS can refuse that while the user works elsewhere.
            raise CaptureError("Timed out waiting for capture metadata; the app may not have become active")
        time.sleep(0.1)


def _run_cua_launch(app, folder, scenario, action, label):
    """Launch one demo instance, run `action(pid, session)`, then stop the verified PID."""
    process = subprocess.Popen(build_launch_command(app, folder, scenario, backend="cua"))
    pid = None
    value = None
    failure = None
    try:
        pid = _wait_for_launched_capture(folder, process)
        verify_target_process(pid, app)
        value = action(pid, cua_session(pid))
    except Exception as error:
        failure = error
    if pid is None:
        # A timed-out app keeps running; stop_verified_process checks its path first.
        pid = started_pid(folder)
    if pid is not None:
        try:
            cua_call("end_session", {"session": cua_session(pid)}, timeout=20)
        except CaptureError:
            pass
        try:
            stop_verified_process(pid, app)
        except (OSError, ValueError):
            detail = f"PID {pid} may still be running because its executable path could not be verified"
            failure = CaptureError(f"{failure}; {detail}" if failure else detail)
    try:
        process.wait(timeout=20)
    except subprocess.SubprocessError:
        detail = ("after a TERM request to its verified PID" if pid is not None else
                  "because the app PID could not be verified")
        message = f"{label} app did not exit {detail}; its process may still be running"
        failure = CaptureError(f"{failure}; {message}" if failure else message)
    if failure is not None:
        if isinstance(failure, CaptureError):
            raise failure
        if isinstance(failure, (ValueError, OSError, subprocess.SubprocessError)):
            raise CaptureError(f"{failure}") from failure
        raise failure
    return value


def launch_cua(app, folder, scenario):
    return _run_cua_launch(app, folder, scenario,
                           lambda pid, session: _capture_cua_states(pid, app, folder, scenario, session),
                           "cua-driver capture")


def cua_probe_tree(pid, app, folder, session):
    verify_target_process(pid, app)
    targets = cua_window_targets(pid, folder, session)
    window_id = targets.get("sheet") or targets["window"]
    payload = cua_call("get_window_state", {"pid": pid, "window_id": window_id, "include_screenshot": False,
                                            "max_elements": CUA_ACCESSIBILITY_TREE_LIMIT, "session": session})
    tree = payload.get("tree_markdown")
    if not isinstance(tree, str) or not tree.strip():
        raise CaptureError("cua-driver returned no accessibility tree")
    return tree if len(tree) <= CUA_PROBE_LIMIT else tree[:CUA_PROBE_LIMIT] + "\n[tree output capped]"


def cua_accessibility_element_count(tree_markdown):
    """Count indexed AX rows returned in cua-driver's markdown tree."""
    if not isinstance(tree_markdown, str):
        return 0
    return len(re.findall(r"(?m)^\s*-\s+\[\d+\]\s+AX[A-Za-z0-9]+\b", tree_markdown))


def cua_accessibility_tree_complete(payload):
    """Treat explicit truncation or a short markdown tree as incomplete evidence."""
    if not isinstance(payload, dict):
        return False
    tree_markdown = payload.get("tree_markdown")
    if not isinstance(tree_markdown, str) or not tree_markdown.strip() or payload.get("truncated") is True:
        return False
    total = payload.get("total_element_count")
    returned = cua_accessibility_element_count(tree_markdown)
    return not (type(total) is int and total > returned)


def capture_cua_accessibility_tree(pid, folder, scenario, session):
    """Save the main-window CUA tree used by named-control checks, including for sheets."""
    if not accessibility_control_contract(scenario):
        return None
    try:
        targets = cua_window_targets(pid, folder, session)
        payload = cua_call("get_window_state", {
            "pid": pid, "window_id": targets["window"], "include_screenshot": False,
            "max_elements": CUA_ACCESSIBILITY_TREE_LIMIT, "session": session,
        })
    except (CaptureError, ValueError):
        payload = {"tree_markdown": "", "truncated": True}
    tree_markdown = payload.get("tree_markdown")
    stored = {
        "tree_markdown": tree_markdown if isinstance(tree_markdown, str) else "",
        "truncated": payload.get("truncated") is True,
        "total_element_count": payload.get("total_element_count"),
        "returned_element_count": cua_accessibility_element_count(tree_markdown),
        "complete": cua_accessibility_tree_complete(payload),
    }
    write_private_json(Path(folder) / "cua-accessibility.json", stored)
    return stored


def launch_cua_probe(app, folder, scenario):
    """Print the bounded accessibility tree of the launched window without input or screenshots."""
    return _run_cua_launch(app, folder, scenario,
                           lambda pid, session: cua_probe_tree(pid, app, folder, session), "cua-driver probe")


def launch_window_server(app, folder, scenario):
    args = build_launch_command(app, folder, scenario, backend="window-server")
    process = subprocess.Popen(args)
    deadline = time.monotonic() + 45
    results = []
    try:
        while not (folder / "windows.json").is_file():
            if process.poll() is not None:
                raise ValueError("App exited before window metadata was written")
            if time.monotonic() >= deadline:
                raise TimeoutError("Timed out waiting for window metadata")
            time.sleep(0.1)
        results = capture_window_server(folder)
        write_private_json(folder / "window-server.json", {"windows": results})
    finally:
        if folder.is_dir():
            write_private_json(folder / "window-server.done", {"complete": True})
    remaining = max(1, deadline - time.monotonic())
    if process.wait(timeout=remaining) != 0:
        raise ValueError("Window-server launch failed")
    return results


def launch(app, folder, scenario, window_server=False, backend=None):
    backend = backend or ("window-server" if window_server else "cua")
    if backend == "cua":
        return launch_cua(app, folder, scenario)
    if backend == "window-server":
        return launch_window_server(app, folder, scenario)
    raise ValueError("Unknown capture backend")


def expected_size(scenario):
    width, height = (int(part) for part in scenario["size"].split("x"))
    if scenario["scope"] == "shown":
        minimum_width = 1300
    else:
        minimum_width = 850 if scenario["rail"] == "compact" else 1100
    # The native minimum includes the window chrome in capture metadata.
    return max(width, minimum_width), max(height, 793)


def png_nonbackground_share(png, allow_sparse_sheet=False, reject_placeholders=True, min_share=0.01):
    """Reject uniform captures; sparse light sheets may be marked for visual review."""
    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("Capture is not a PNG")
    position, header, chunks = 8, None, []
    try:
        while position + 12 <= len(png):
            length = struct.unpack_from(">I", png, position)[0]
            kind = png[position + 4:position + 8]
            end = position + 12 + length
            if end > len(png):
                raise ValueError("Truncated PNG")
            payload = png[position + 8:position + 8 + length]
            checksum = struct.unpack_from(">I", png, position + 8 + length)[0]
            if zlib.crc32(kind + payload) & 0xffffffff != checksum:
                raise ValueError("PNG checksum mismatch")
            if kind == b"IHDR":
                header = struct.unpack(">IIBBBBB", payload)
            elif kind == b"IDAT":
                chunks.append(payload)
            elif kind == b"IEND":
                break
            position = end
        if not header or not chunks:
            raise ValueError("Incomplete PNG")
        width, height, depth, color, compression, filtering, interlace = header
        if not (0 < width <= 8192 and 0 < height <= 8192 and depth == 8 and color in (2, 6)
                and compression == filtering == interlace == 0):
            raise ValueError("Unsupported PNG encoding")
        channels = 4 if color == 6 else 3
        stride = width * channels
        raw = zlib.decompress(b"".join(chunks))
        if len(raw) != (stride + 1) * height:
            raise ValueError("Invalid PNG data size")
        counts = Counter()
        placeholder_pixels = 0
        white_samples = 0
        text_rows = []
        previous = bytearray(stride)
        xstep, ystep = max(1, width // 64), max(1, height // 64)
        for y in range(height):
            offset = y * (stride + 1)
            method = raw[offset]
            row = bytearray(raw[offset + 1:offset + 1 + stride])
            if method > 4:
                raise ValueError("Invalid PNG filter")
            if method:
                for i in range(stride):
                    left = row[i - channels] if i >= channels else 0
                    above = previous[i]
                    upper_left = previous[i - channels] if i >= channels else 0
                    if method == 1:
                        predictor = left
                    elif method == 2:
                        predictor = above
                    elif method == 3:
                        predictor = (left + above) // 2
                    else:
                        estimate = left + above - upper_left
                        distances = (abs(estimate - left), abs(estimate - above), abs(estimate - upper_left))
                        predictor = (left, above, upper_left)[distances.index(min(distances))]
                    row[i] = (row[i] + predictor) & 255
            if y % ystep == 0:
                for x in range(0, width, xstep):
                    i = x * channels
                    if channels == 4 and row[i + 3] < 16:
                        counts[("transparent",)] += 1
                    else:
                        red, green, blue = row[i], row[i + 1], row[i + 2]
                        counts[(red // 16, green // 16, blue // 16)] += 1
                        if min(red, green, blue) >= 235 and (channels == 3 or row[i + 3] >= 240):
                            white_samples += 1
                        hue, saturation, value = colorsys.rgb_to_hsv(red / 255, green / 255, blue / 255)
                        degrees = hue * 360
                        if saturation > 0.6 and value > 0.8 and (40 <= degrees <= 60 or degrees <= 10 or degrees >= 350):
                            placeholder_pixels += 1
            if allow_sparse_sheet:
                # Inspect full-resolution dark runs: sparse text can be missed by
                # the 64x64 sample. Broad lines and scattered single pixels do
                # not count as text-like rows.
                max_run = max(20, min(64, width // 40))
                runs, run = [], 0
                for x in range(width):
                    i = x * channels
                    dark = max(row[i], row[i + 1], row[i + 2]) < 120 and (channels == 3 or row[i + 3] >= 240)
                    if dark:
                        run += 1
                    elif run:
                        if run <= max_run:
                            runs.append(run)
                        run = 0
                if run and run <= max_run:
                    runs.append(run)
                if len(runs) >= 3:
                    text_rows.append((y, sum(runs)))
            previous = row
        total = sum(counts.values())
        placeholder = placeholder_pixels / total
        if reject_placeholders and placeholder > 0.02:
            raise ValueError("AppKit-backed views need a window-server capture")
        share = 1 - max(counts.values()) / total
        if share <= min_share:
            # A sparse result sheet can contain only a few text lines on white.
            # Require multiple text-like bands, not isolated noise or one line.
            bands = []
            for y, pixels in text_rows:
                if not bands or y - bands[-1]["last"] > 2:
                    bands.append({"last": y, "rows": 0, "pixels": 0})
                bands[-1]["last"] = y
                bands[-1]["rows"] += 1
                bands[-1]["pixels"] += pixels
            text_bands = [band for band in bands if band["rows"] >= 2 and band["pixels"] >= 12]
            sparse_text = (allow_sparse_sheet and white_samples / total >= 0.95 and len(text_bands) >= 2
                           and sum(band["pixels"] for band in text_bands) >= 60)
            if not sparse_text:
                raise ValueError(f"Capture is black or near-uniform (non-background share {share:.3%})")
        return share
    except (struct.error, zlib.error) as error:
        raise ValueError(f"Invalid PNG: {error}") from error


def accessibility_missing(tree, scenario):
    required = {"plan": ["Preservation", "Connect remote", "Freeze", "Hold", "Cleanup candidates"],
                "hold": ["Preservation", "Hold"],
                "freeze": ["Requirements", "Cancel", "Build review"],
                "freeze-no-files": ["Add files to this project", "Open in Finder", "Open Terminal"],
                "freeze-hold": ["This project is on Hold", "Requirements"],
                "freeze-review": ["Expected outcome", "Git history"],
                "preserve-review": ["Expected outcome", "Git history"],
                "freeze-result": ["Outcome", "Checkout removed", "Net space change after the archive copy", "Done"],
                "freeze-failure": ["Outcome", "Done"],
                "cleanup-result": ["Cleanup result", "Freed from disk", "In Trash (can be restored)", "Done"],
                "trash-history": ["Trash & restore history", "Space report", "Last 30 days", "All time"],
                "wizard-result": ["Make space", "Results", "Estimate", "Checkout removed", "Freed from disk", "In Trash (can be restored)"],
                "task-history": ["Task run history", "Space", "Estimate"],
                "cleanup-review": ["Review cleanup", "Expected outcome"],
                "cleanup-hold": ["Review cleanup", "This project is on Hold"],
                "push-review": ["Push one branch", "feature/search", "Destination:", "Push this branch"],
                "wizard-review": ["Expected outcome", "Review"],
                "wizard-start": ["Project wizard", "Make space", "Thaw projects"],
                "wizard-actions": ["Make space", "Choose actions", "Harbor CLI", "Signal Lab", "Build review"],
                "wizard-thaw-selection": ["Frozen projects", "Create Thaw plan"],
                "wizard-thaw-review": ["Thaw plan", "Expected outcome"],
                "wizard-thaw-ready": ["Thaw projects", "Only ready projects will run", "Ice Garden",
                                      "I reviewed the remote refs", "Thaw 1 ready project"],
                "wizard-thaw-occupied": ["Thaw plan", "Path occupied", "Show path in Finder"],
                "wizard-thaw-remote": ["Thaw plan", "Remote unavailable", "Check remote again"],
                "wizard-thaw-corrupt": ["Thaw plan", "Archive needs attention", "Open Freezer"],
                "wizard-thaw-not-frozen": ["Thaw plan", "No longer Frozen", "Open Freezer"],
                "wizard-thaw-rehydrating": ["Thawing Ice Garden"],
                "wizard-thaw-result": ["Checkouts restored", "Remote refs restored", "Start a new Thaw plan", "Done"],
                "task-review": ["Review task actions", "Expected outcome"],
                "settings": ["Igloo Settings", "Freezer backup folder", "Reminder threshold"],
                "settings-cloud": ["Confirm cloud backup"],
                "repositories-loading": ["GitHub repositories", "Loading repositories for example"],
                "repositories": ["GitHub repositories", "orbit-maps"],
                "help-getting-started": ["Igloo Help", "Getting started"],
                "help-freeze-thaw": ["Igloo Help", "Freeze & thaw"],
                "help-safety-storage": ["Igloo Help", "Safety & storage"],
                "tasks-rules": ["Tasks", "Rules"],
                "tasks-schedule": ["Tasks", "Run while your Mac user is logged in"],
                "remote-setup": ["Connect a remote"],
                "commit-setup": ["Select files to commit"],
                "shelf": ["Done", "Show Freezer copy", "Freezer backup verified", "Thaw from remote"]}.get(scenario["view"], [])
    def labels(node):
        if not isinstance(node, dict):
            return []
        values = [node.get(key) for key in ("title", "label", "value") if isinstance(node.get(key), str)]
        return values + [value for child in node.get("children", []) for value in labels(child)]
    values = [value.casefold() for value in labels(tree)]
    return [name for name in required if not any(name.casefold() in value for value in values)]


def accessibility_control_contract(scenario):
    """Return expected named controls for a view, preserving identifier, role and visible label."""
    return {
        "plan": [
            ("detail.connectRemote", "AXButton", "Connect remote…"),
            ("detail.commitSelectedFiles", "AXButton", "Commit selected files…"),
            ("detail.preserve", "AXButton", "Preserve…"),
            ("detail.freeze", "AXButton", "Freeze…"),
            ("detail.hold", "AXCheckBox", "Hold"),
            ("detail.preservationNotes", "AXTextArea", "Preservation notes"),
            ("detail.exportProjectReport", "AXButton", "Export project report…"),
            ("detail.savePreservation", "AXButton", "Save"),
        ],
        "freeze": [("freeze.cancel", "AXButton", "Cancel"),
                   ("freeze.buildReview", "AXButton", "Build review…")],
        "shelf": [
            ("shelf.backUpNow", "AXButton", "Back up now"),
            ("shelf.verifyBackup", "AXButton", "Verify backup"),
            ("shelf.done", "AXButton", "Done"),
            ("shelf.showArchive", "AXButton", "Show Freezer copy"),
            ("shelf.freeze", "AXButton", "Freeze…"),
            ("shelf.offlineRecovery", "AXButton", "Offline recovery…"),
            ("shelf.thawRemote", "AXButton", "Thaw from remote…"),
        ],
        "wizard-start": [("wizard.start.makeSpace", "AXButton", "Make space"),
                         ("wizard.start.thaw", "AXButton", "Thaw projects")],
        "wizard-thaw-selection": [("wizard.thaw.back", "AXButton", "Back"),
                                  ("wizard.thaw.prepare", "AXButton", "Create Thaw plan"),
                                  ("wizard.thaw.archive.demo-icegarden", "AXCheckBox", "Ice Garden")],
        "wizard-thaw-review": [("wizard.thaw.acceptance", "AXCheckBox", "I reviewed the remote refs"),
                               ("wizard.thaw.execute", "AXButton", "Thaw ")],
        "wizard-thaw-ready": [("wizard.thaw.acceptance", "AXCheckBox", "I reviewed the remote refs"),
                              ("wizard.thaw.execute", "AXButton", "Thaw ")],
        "wizard-thaw-occupied": [("wizard.thaw.showPath", "AXButton", "Show path in Finder")],
        "wizard-thaw-remote": [("wizard.thaw.checkRemote", "AXButton", "Check remote again")],
        "wizard-thaw-corrupt": [("wizard.thaw.openFreezer", "AXButton", "Open Freezer")],
        "wizard-thaw-not-frozen": [("wizard.thaw.openFreezer", "AXButton", "Open Freezer")],
        "wizard-thaw-result": [("wizard.thaw.newPlan", "AXButton", "Start a new Thaw plan")],
    }.get(scenario.get("view"), [])


def _cua_markdown_controls(tree_markdown):
    """Parse CUA markdown rows into identifier, role, visible label and action fields."""
    controls = []
    if not isinstance(tree_markdown, str):
        return controls
    for line in tree_markdown.splitlines():
        identifier_match = re.search(r"\[id=([^\s\]]+)", line)
        role_match = re.search(r"\b(AX[A-Za-z0-9]+)\b", line)
        if not role_match:
            continue
        label_match = re.search(r"\((.*?)\)", line)
        help_match = re.search(r"\bhelp=(?:\"([^\"]*)\"|'([^']*)'|([^\s\]]+))", line)
        label = label_match.group(1).strip() if label_match else ""
        help_text = next((value for value in help_match.groups() if value is not None), "").strip() if help_match else ""
        controls.append({
            "identifier": identifier_match.group(1) if identifier_match else None,
            "role": role_match.group(1) if role_match else None,
            "label": label,
            "has_label_or_help": bool(label or help_text),
            "has_actions": bool(re.search(r"\bactions=\[", line)),
        })
    return controls


def accessibility_control_issues(tree, scenario, *, source="window-server", complete=True):
    """Classify expected controls as found, missing, or unverified for the selected tree source."""
    required = accessibility_control_contract(scenario)
    if not required:
        return None

    if source == "cua":
        captured = _cua_markdown_controls(tree)
        statuses = {}
        for identifier, role, expected_label in required:
            candidates = [item for item in captured if item["identifier"] == identifier]
            if any(item["role"] == role and item["has_label_or_help"] for item in candidates):
                statuses[identifier] = "found"
            elif candidates:
                statuses[identifier] = "missing"
            else:
                disabled_without_id = any(
                    item["identifier"] is None and item["role"] == role
                    and item["label"].startswith(expected_label) and not item["has_actions"]
                    for item in captured
                )
                if disabled_without_id:
                    statuses[identifier] = "unverified (disabled, id not exposed by cua-driver)"
                else:
                    statuses[identifier] = "missing" if complete else "unverified (tree incomplete)"
        return statuses
    if source != "window-server":
        raise ValueError("Unknown accessibility control source")

    def nodes(node):
        if isinstance(node, dict):
            yield node
            for child in node.get("children", []):
                yield from nodes(child)

    captured = list(nodes(tree))

    def valid(node, identifier, role):
        if node.get("identifier") != identifier or node.get("role") != role:
            return False
        if type(node.get("enabled")) is not bool:
            return False
        if not any(isinstance(node.get(key), str) and node[key].strip()
                   for key in ("label", "title")):
            return False
        frame = node.get("frame")
        return (isinstance(frame, dict)
                and all(type(frame.get(key)) in (int, float) and math.isfinite(frame[key])
                        for key in ("x", "y", "width", "height"))
                and frame["width"] > 0 and frame["height"] > 0)

    statuses = {identifier: ("found" if any(valid(node, identifier, role) for node in captured) else "missing")
                for identifier, role, _ in required}
    if scenario["view"] == "wizard-thaw-ready":
        expected_enabled = {"wizard.thaw.acceptance": True, "wizard.thaw.execute": False}
        for identifier, enabled in expected_enabled.items():
            node = next((item for item in captured if item.get("identifier") == identifier), None)
            if node is None or node.get("enabled") is not enabled:
                statuses[identifier] = "missing"
        node = next((item for item in captured if item.get("identifier") == "wizard.thaw.acceptance"), None)
        value = node.get("value") if node else None
        if not (value is False or type(value) is int and value == 0):
            statuses["wizard.thaw.acceptance"] = "missing"
    return statuses


def format_accessibility_control_statuses(statuses):
    if statuses is None:
        return "not configured for this view"
    if not isinstance(statuses, dict):
        return "not configured for this view" if not statuses else (
            f"found: none; missing: {', '.join(str(value) for value in statuses)}; unverified: none")
    groups = []
    for status in ("found", "missing", "unverified"):
        entries = []
        for identifier, outcome in statuses.items():
            if outcome == status:
                entries.append(identifier)
            elif (status == "unverified" and isinstance(outcome, str)
                  and outcome.startswith("unverified (") and outcome.endswith(")")):
                reason = outcome[len("unverified ("):-1]
                entries.append(f"{identifier} ({reason})")
        groups.append(f"{status}: {', '.join(entries) if entries else 'none'}")
    return "; ".join(groups)


def accessibility_node_counts(tree):
    """Count nodes under each captured native window, excluding the wrapper root."""
    def count(node):
        if not isinstance(node, dict):
            return 0
        return 1 + sum(count(child) for child in node.get("children", []))

    windows = {}
    for root in tree.get("children", []):
        kind = root.get("windowKind", "other") if isinstance(root, dict) else "other"
        windows[kind] = windows.get(kind, 0) + count(root)
    return {"total": sum(windows.values()), "windows": windows}


def validate_result(folder, scenario, reject_renderer_placeholders=True, backend="window-server"):
    try:
        mode = folder.lstat().st_mode
    except OSError as error:
        raise ValueError(f"Missing scenario directory: {error}") from error
    if not stat.S_ISDIR(mode) or stat.S_IMODE(mode) != 0o700:
        raise ValueError("Scenario directory permissions must be 0700")
    try:
        png = renderer_path(folder, "window").read_bytes()
        result = json.loads((folder / "accessibility.json").read_text())
        started = json.loads((folder / "started.json").read_text())
        window_metadata = json.loads((folder / "windows.json").read_text())
    except (OSError, ValueError) as error:
        raise ValueError(f"Missing or invalid capture output: {error}") from error
    if not isinstance(result, dict) or not isinstance(started, dict):
        raise ValueError("Invalid capture metadata")
    try:
        result["nonBackgroundShare"] = png_nonbackground_share(
            png, reject_placeholders=reject_renderer_placeholders)
    except ValueError as error:
        # With a real-pixel backend the renderer image is a diagnostic only.
        if reject_renderer_placeholders:
            raise
        result["nonBackgroundShare"] = None
        result["rendererDiagnostic"] = str(error)
    if has_sheet_image(scenario["view"]) and scenario["view"] not in NO_SHEET_RENDER_VIEWS:
        try:
            result["sheetNonBackgroundShare"] = png_nonbackground_share(
                renderer_path(folder, "sheet").read_bytes(), allow_sparse_sheet=True,
                reject_placeholders=reject_renderer_placeholders)
        except OSError as error:
            raise ValueError(f"Missing sheet capture: {error}") from error
        except ValueError as error:
            if reject_renderer_placeholders:
                raise
            result["sheetNonBackgroundShare"] = None
            result["rendererSheetDiagnostic"] = str(error)
    if not isinstance(result.get("tree"), dict) or not result["tree"]:
        raise ValueError("Accessibility tree is empty")
    if type(result.get("pid")) is not int or result["pid"] != started.get("pid"):
        raise ValueError("Capture PID does not match the launched instance")
    windows = validate_windows(window_metadata)
    if window_metadata["pid"] != result["pid"]:
        raise ValueError("Window metadata PID mismatch")
    if scenario["view"] in SHEET_VIEWS and not any(window["kind"] == "sheet" for window in windows):
        raise ValueError("Missing sheet window metadata")
    width, height = (int(part) for part in scenario["size"].split("x"))
    expected_width, expected_height = expected_size(scenario)
    actual_width, actual_height = result.get("actualWidth"), result.get("actualHeight")
    if not isinstance(actual_width, (int, float)) or not isinstance(actual_height, (int, float)):
        raise ValueError("Capture size is missing")
    if abs(actual_width - expected_width) > 0.5 or abs(actual_height - expected_height) > 0.5:
        raise ValueError(f"Capture size mismatch: requested {width}x{height}, actual {actual_width}x{actual_height}, expected {expected_width}x{expected_height}")
    result["accessibilityMissing"] = accessibility_missing(result["tree"], scenario)
    if backend == "cua":
        try:
            cua_tree = json.loads((folder / "cua-accessibility.json").read_text())
        except (OSError, ValueError):
            cua_tree = {"tree_markdown": "", "complete": False}
        result["accessibilityControlIssues"] = accessibility_control_issues(
            cua_tree.get("tree_markdown"), scenario, source="cua",
            complete=cua_tree.get("complete") is True and cua_accessibility_tree_complete(cua_tree))
    else:
        result["accessibilityControlIssues"] = accessibility_control_issues(result["tree"], scenario)
    result["accessibilityNodeCounts"] = accessibility_node_counts(result["tree"])
    return result


def validate_sheet_result(folder, scenario, reject_renderer_placeholders=True):
    """Validate a sheet independently, including its metadata and dimensions."""
    if not has_sheet_image(scenario["view"]):
        raise ValueError("Scenario has no sheet image")
    try:
        mode = folder.lstat().st_mode
        if not stat.S_ISDIR(mode) or stat.S_IMODE(mode) != 0o700:
            raise ValueError("Scenario directory permissions must be 0700")
        png = renderer_path(folder, "sheet").read_bytes()
        result = json.loads((folder / "accessibility.json").read_text())
        started = json.loads((folder / "started.json").read_text())
        metadata = json.loads((folder / "windows.json").read_text())
    except (OSError, ValueError) as error:
        raise ValueError(f"Missing or invalid sheet capture: {error}") from error
    if not isinstance(result, dict) or not isinstance(started, dict):
        raise ValueError("Invalid capture metadata")
    result["sheetNonBackgroundShare"] = png_nonbackground_share(
        png, allow_sparse_sheet=True, reject_placeholders=reject_renderer_placeholders)
    if not isinstance(result.get("tree"), dict) or not result["tree"]:
        raise ValueError("Accessibility tree is empty")
    if type(result.get("pid")) is not int or result["pid"] != started.get("pid"):
        raise ValueError("Capture PID does not match the launched instance")
    windows = validate_windows(metadata)
    if metadata["pid"] != result["pid"]:
        raise ValueError("Window metadata PID mismatch")
    if scenario["view"] in SHEET_VIEWS and not any(window["kind"] == "sheet" for window in windows):
        raise ValueError("Missing sheet window metadata")
    width, height = result.get("actualWidth"), result.get("actualHeight")
    expected_width, expected_height = expected_size(scenario)
    if not isinstance(width, (int, float)) or not isinstance(height, (int, float)):
        raise ValueError("Capture size is missing")
    if abs(width - expected_width) > 0.5 or abs(height - expected_height) > 0.5:
        raise ValueError("Capture size mismatch")
    result["accessibilityMissing"] = accessibility_missing(result["tree"], scenario)
    result["accessibilityControlIssues"] = accessibility_control_issues(result["tree"], scenario)
    result["accessibilityNodeCounts"] = accessibility_node_counts(result["tree"])
    return result


def started_pid(folder):
    try:
        data = json.loads((folder / "started.json").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    pid = data.get("pid")
    return pid if type(pid) is int and pid > 0 else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, default=Path("Igloo.app"), help="already built app bundle")
    parser.add_argument("--output", type=Path, help="new output directory")
    parser.add_argument("--scenario", action="append", help="scenario name from --list (repeatable); defaults to all")
    parser.add_argument("--since", help="select scenarios for changed app views since this commit")
    parser.add_argument("--list", action="store_true", help="list scenarios without launching")
    backend = parser.add_mutually_exclusive_group()
    backend.add_argument("--backend", choices=("cua", "window-server"), default="cua",
                         help="real-pixel capture backend (default: cua)")
    backend.add_argument("--window-server", action="store_const", dest="backend", const="window-server",
                         help="compatibility alias for --backend window-server")
    parser.add_argument("--probe", action="store_true",
                        help="launch one scenario and print its bounded cua-driver accessibility tree without input")
    args = parser.parse_args()
    matrix = scenarios()
    try:
        selected = select_scenarios(matrix, explicit=args.scenario, since=args.since)
    except (ValueError, subprocess.CalledProcessError) as error:
        parser.error(str(error))
    if args.list:
        for scenario in selected:
            print(scenario["name"])
        return 0
    if not selected:
        print("OPEN: --since selected no changed app-view scenarios; no capture was run", flush=True)
        return 1
    if args.probe and (args.backend != "cua" or len(selected) != 1 or len(args.scenario or []) != 1 or args.since):
        parser.error("--probe requires the cua backend and exactly one explicit --scenario")
    if not args.output:
        parser.error("--output is required")
    if sys.platform != "darwin":
        parser.error("capture requires macOS")
    app = args.app.resolve()
    try:
        if is_protected_app_bundle(app):
            parser.error("--app must not be /Applications/Igloo.app or a bundle with ID local.igloo")
    except (OSError, ValueError) as error:
        parser.error(str(error))
    if not (app / "Contents/MacOS/Igloo").is_file():
        parser.error("--app must name a built Igloo.app")
    output = args.output.absolute()
    if output.is_symlink():
        parser.error("--output must not be a symlink")
    try:
        parent = output.parent.resolve(strict=True)
        if not parent.is_dir():
            parser.error("--output parent must be an existing directory")
        output = parent / output.name
        output.mkdir(mode=0o700, exist_ok=False)
        output.chmod(0o700)
    except FileExistsError:
        parser.error("--output must name a new directory")
    except OSError as error:
        parser.error(f"--output parent must be an existing directory: {error}")
    if args.probe:
        scenario = selected[0]
        try:
            print(launch_cua_probe(app, output / scenario["name"], scenario), flush=True)
            return 0
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            print(f"OPEN {scenario['name']}: {error}", flush=True)
            return 1
    failed = False
    for scenario in selected:
        folder = output / scenario["name"]
        try:
            capture_results = launch(app, folder, scenario, backend=args.backend)
            for item in capture_results or []:
                detail = f"{item['path']}: " if item.get("path") else ""
                print(f"{item['status']} {scenario['name']} {item['kind']} {args.backend}: {detail}{item['reason']}", flush=True)
                if item.get("status") == "OPEN":
                    failed = True
            if scenario["view"].startswith("update-"):
                try:
                    sheet_share = png_nonbackground_share(renderer_path(folder, "sheet").read_bytes(),
                                                          allow_sparse_sheet=True, reject_placeholders=False)
                    diagnostic = f"non-background share {sheet_share:.3%}"
                except (OSError, ValueError) as error:
                    diagnostic = str(error)
                print(f"PARTIAL {scenario['name']} renderer sheet diagnostic: {diagnostic}", flush=True)
            result = validate_result(folder, scenario, reject_renderer_placeholders=False, backend=args.backend)
            width, height = (int(part) for part in scenario["size"].split("x"))
            expected_width, expected_height = expected_size(scenario)
            missing = result.get("accessibilityMissing", [])
            detail = ", ".join(missing) if missing else "completeness not independently established"
            control_issues = result.get("accessibilityControlIssues", [])
            controls = format_accessibility_control_statuses(control_issues)
            counts = result.get("accessibilityNodeCounts", {})
            windows = ", ".join(f"{kind}={count}" for kind, count in sorted(counts.get("windows", {}).items()))
            print(f"PARTIAL {scenario['name']}: PID {result['pid']}; requested {width}x{height}, actual {result['actualWidth']}x{result['actualHeight']}, expected {expected_width}x{expected_height}; AX labels {detail}; named controls {controls}; AX nodes {counts.get('total', 0)} [{windows}]; PNG needs visual review", flush=True)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            pid = started_pid(folder)
            detail = (f"; PID {pid} may still be running (not stopped)"
                      if pid is not None and (args.backend == "window-server" or not isinstance(error, CaptureError)) else "")
            print(f"OPEN {scenario['name']}: {error}{detail}", flush=True)
            if args.backend == "window-server" and has_sheet_image(scenario["view"]):
                try:
                    sheet = validate_sheet_result(folder, scenario, reject_renderer_placeholders=False)
                    missing = sheet.get("accessibilityMissing", [])
                    ax = ", ".join(missing) if missing else "completeness not independently established"
                    control_issues = sheet.get("accessibilityControlIssues", [])
                    controls = format_accessibility_control_statuses(control_issues)
                    counts = sheet.get("accessibilityNodeCounts", {})
                    windows = ", ".join(f"{kind}={count}" for kind, count in sorted(counts.get("windows", {}).items()))
                    print(f"PARTIAL {scenario['name']} renderer sheet diagnostic: PID {sheet['pid']}; AX labels {ax}; named controls {controls}; AX nodes {counts.get('total', 0)} [{windows}]; real-pixel evidence needs visual review", flush=True)
                except (OSError, ValueError) as sheet_error:
                    print(f"OPEN {scenario['name']} sheet: {sheet_error}", flush=True)
            failed = True
    print("Review checklist: docs/capture-review.md", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
