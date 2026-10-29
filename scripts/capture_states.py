#!/usr/bin/env python3
"""Launch independent demo captures from an already built Igloo app on macOS."""
import argparse
import base64
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
               "cleanup-review", "cleanup-result", "cleanup-hold", "trash-history", "wizard-review", "wizard-result",
               "wizard-start", "wizard-thaw-selection", "wizard-thaw-review", "wizard-thaw-ready", "wizard-thaw-result",
               "wizard-thaw-occupied", "wizard-thaw-remote", "wizard-thaw-corrupt", "wizard-thaw-not-frozen",
               "wizard-thaw-rehydrating",
               "task-review", "task-history", "settings", "settings-cloud", "repositories-loading"}
UPDATE_FAILURES = ("quota", "quota-no-reset", "forbidden", "offline", "timeout", "server", "malformed",
                   "cancelled-download", "digest-mismatch")
SHEET_VIEWS.update("update-" + name for name in UPDATE_FAILURES)
SHEET_VIEWS.update({"update-markdown", "update-ready", "update-install-failed"})

INTERACTIVE_FLOWS = {
    "wizard-thaw": [
        {"click": {"role": "button", "ordinal": 2, "anchor": "Choose a path"}, "capture": "01-selection",
         "wait_for": {"anchor": "Select projects"}},
        {"click": {"role": "checkbox", "ordinal": 1,
                    "parent": {"role": "outline row", "ordinal": 1}, "anchor": "Select projects"},
         "capture": "02-selected",
         "expect": {"value": True}},
        {"click": {"role": "button", "ordinal": 3, "anchor": "Select projects"}, "capture": "03-plan",
         "wait_for": {"anchor": "Thaw plan"}},
        {"click": {"role": "checkbox", "ordinal": 1, "anchor": "Thaw plan"}, "capture": "05-confirmed",
         "expect": {"value": True},
         "expect_control": {"locator": {"role": "button", "ordinal": 3, "anchor": "Thaw plan"},
                            "enabled": True}},
        {"click": {"role": "button", "ordinal": 3, "anchor": "Thaw plan"}, "capture": "06-result",
         "wait_for": {"anchor": "Results"}, "expect_labels": ["Local-only files restored"]},
    ]
}


def validate_orca_locator(locator, name="Interactive click"):
    if not isinstance(locator, dict) or set(locator) - {"role", "ordinal", "parent", "anchor", "label"}:
        raise ValueError(f"{name} requires a role, ordinal, and anchor")
    if not isinstance(locator.get("role"), str) or not locator["role"].strip():
        raise ValueError(f"{name} requires a role")
    if type(locator.get("ordinal")) is not int or locator["ordinal"] < 1:
        raise ValueError(f"{name} ordinal must be a positive integer")
    if not isinstance(locator.get("anchor"), str) or not locator["anchor"].strip():
        raise ValueError(f"{name} requires an anchor")
    if "label" in locator and (not isinstance(locator["label"], str) or not locator["label"].strip()):
        raise ValueError(f"{name} label must be non-empty text")
    parent = locator.get("parent")
    if parent is not None and (not isinstance(parent, dict) or set(parent) != {"role", "ordinal"} or
                               not isinstance(parent["role"], str) or not parent["role"].strip() or
                               type(parent["ordinal"]) is not int or parent["ordinal"] < 1):
        raise ValueError(f"{name} parent requires a role and positive ordinal")
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
            click = validate_orca_locator(click)
        else:
            key = step.get("key")
            if key not in ("Up", "Down"):
                raise ValueError("Interactive key must be Up or Down")
            if "wait_for_selection" not in step or "wait_for" in step or "expect" in step or "expect_control" in step:
                raise ValueError("Interactive key requires a selection wait and cannot declare click expectations")
            selection = validate_orca_locator(step["wait_for_selection"], "Interactive selection")
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
            control["locator"] = validate_orca_locator(control["locator"], "Interactive control")
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
    hold_open = backend in ("orca", "window-server")
    args = ["/usr/bin/open", "-n"]
    if hold_open or window_server:
        args.append("-W")
    args += [str(app), "--args", "--demo", "--demo-" + scenario["appearance"],
            "--demo-size", scenario["size"], "--demo-view", scenario["view"], "--demo-scope", scenario["scope"],
            "--demo-rail", scenario["rail"], "--demo-data", scenario["data"], "--demo-capture", str(folder)]
    if hold_open or window_server:
        args.append("--demo-window-server")
    if backend == "orca":
        args.append("--demo-orca")
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


class OrcaError(ValueError):
    """Orca could not read or act on the launched demo window."""

    def __init__(self, message, code=None, next_steps=()):
        super().__init__(message)
        self.message = message
        self.code = code
        self.next_steps = tuple(next_steps)

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


def _orca_error(payload):
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return None
    code = error.get("code") if isinstance(error.get("code"), str) else None
    message = error.get("message") if isinstance(error.get("message"), str) else None
    data = error.get("data") if isinstance(error.get("data"), dict) else {}
    next_steps = data.get("nextSteps", error.get("nextSteps", []))
    if not isinstance(next_steps, list):
        next_steps = []
    next_steps = [step for step in next_steps if isinstance(step, str)]
    if not message:
        message = "Orca could not read the window"
    return OrcaError(message, code=code, next_steps=next_steps)


def _orca_restore_suggested(error):
    return any("--restore-window" in step for step in error.next_steps)


def orca_json(arguments, _restore_attempted=False):
    command = shlex.split(os.environ.get("ORCA_CLI_COMMAND", "orca"))
    if not command:
        raise OrcaError("Orca command is unavailable")
    args = command + ["computer"] + list(arguments) + ["--json"]
    try:
        result = subprocess.run(args, check=False, timeout=45, capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError) as error:
        raise OrcaError(f"Orca could not read the window ({type(error).__name__})") from error
    payload = None
    parse_error = None
    for output in (result.stdout, result.stderr):
        if not output:
            continue
        try:
            candidate = json.loads(output)
        except (TypeError, ValueError) as error:
            parse_error = error
            continue
        if isinstance(candidate, dict):
            payload = candidate
            break
    if payload is None:
        if result.returncode != 0:
            raise OrcaError(f"Orca command failed (exit {result.returncode})") from parse_error
        raise OrcaError("Orca returned invalid window data") from parse_error
    error = _orca_error(payload)
    if error is not None or payload.get("ok") is False or result.returncode != 0:
        error = error or OrcaError(f"Orca command failed (exit {result.returncode})")
        if not _restore_attempted and _orca_restore_suggested(error) and "--restore-window" not in arguments:
            return orca_json(list(arguments) + ["--restore-window"], _restore_attempted=True)
        raise error
    return payload


def _window_target_args(window):
    if window.get("id") is not None:
        return ["--window-id", str(window["id"])]
    if type(window.get("index")) is int and window["index"] >= 0:
        return ["--window-index", str(window["index"])]
    return []


def _orca_windows(payload):
    result = payload.get("result", payload) if isinstance(payload, dict) else {}
    if isinstance(result, list):
        windows = result
    else:
        windows = result.get("windows", result.get("items", [])) if isinstance(result, dict) else []
    if isinstance(windows, dict):
        windows = list(windows.values())
    if not isinstance(windows, list):
        return []
    normalized = []
    for position, window in enumerate(windows):
        if not isinstance(window, dict):
            continue
        identifier = next((window[key] for key in ("windowId", "window_id", "id", "windowNumber")
                           if window.get(key) is not None), None)
        index = next((window[key] for key in ("windowIndex", "window_index", "index")
                      if type(window.get(key)) is int and window[key] >= 0), position)
        record = dict(window)
        record["id"] = identifier
        record["index"] = index
        if _window_target_args(record):
            normalized.append(record)
    return normalized


def _window_sort_key(window, anchor, position):
    text = " ".join(str(window.get(key, "")) for key in ("title", "label", "name")).casefold()
    kind = " ".join(str(window.get(key, "")) for key in ("role", "type", "kind", "subrole")).casefold()
    is_main = any(window.get(key) is True for key in ("isMain", "isMainWindow", "is_main", "main"))
    is_sheet = "sheet" in kind or "dialog" in kind or any(
        window.get(key) is True for key in ("isSheet", "is_sheet", "isDialog"))
    is_frontmost = any(window.get(key) is True for key in ("isKey", "isKeyWindow", "isFocused", "focused", "key"))
    return (0 if is_sheet or not is_main else 1, 0 if is_frontmost else 1,
            0 if anchor.casefold() in text else 1, -position)


def _snapshot_window_id(state):
    snapshot = _snapshot(state)
    window = snapshot.get("window") if isinstance(snapshot, dict) else None
    if not isinstance(window, dict):
        return None
    return next((window[key] for key in ("windowId", "window_id", "id", "windowNumber")
                 if window.get(key) is not None), None)


def _state_has_sheet_anchor(state, anchor):
    snapshot = _snapshot(state)
    nodes = parse_orca_tree_text(snapshot.get("treeText"))
    return bool(_orca_sheet_for_anchor(nodes, anchor))


def _orca_state_for_sheet(pid, anchor, locator=None, include_screenshot=True):
    windows_payload = orca_json(["list-windows", "--app", f"pid:{pid}"])
    windows = _orca_windows(windows_payload)
    windows.sort(key=lambda window: _window_sort_key(window, anchor, window["index"]))
    failures = []
    for window in windows:
        target = _window_target_args(window)
        try:
            state = orca_app_state(pid, window=target, include_screenshot=False)
        except OrcaError as error:
            failures.append(error)
            continue
        snapshot_id = _snapshot_window_id(state)
        if snapshot_id is not None and window["id"] is not None and str(snapshot_id) != str(window["id"]):
            continue
        if not _state_has_sheet_anchor(state, anchor):
            continue
        if locator is not None and find_orca_locator(state, locator) is None:
            continue
        if include_screenshot:
            state = orca_app_state(pid, window=target)
        return state, target
    if failures:
        raise failures[-1]
    suffix = " with the requested control" if locator is not None else ""
    raise OrcaError(f"Orca could not find the {anchor} sheet{suffix} in a listed window")


def orca_app_state(pid, window=None, include_screenshot=True):
    arguments = ["get-app-state", "--app", f"pid:{pid}"]
    if window:
        arguments.extend(window)
    if not include_screenshot:
        arguments.append("--no-screenshot")
    return orca_json(arguments)


def _snapshot(state):
    result = state.get("result", state) if isinstance(state, dict) else {}
    return result.get("snapshot", {}) if isinstance(result, dict) else {}


def _walk_nodes(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_nodes(child)


ORCA_TREE_ROLES = ("scroll area", "outline row", "progress indicator", "radio button", "split group",
                   "table row", "application", "window", "sheet", "button", "checkbox", "heading",
                   "list", "cell", "text", "row", "table", "group", "image", "tab", "unknown")


def parse_orca_tree_text(tree_text):
    if not isinstance(tree_text, str):
        return []
    roles = sorted(ORCA_TREE_ROLES, key=len, reverse=True)
    nodes = []
    for line_number, line in enumerate(tree_text.splitlines()):
        match = re.match(r"^\s*(?:\[(\d+)\]|(\d+))\s+(.*)$", line)
        if not match:
            continue
        index = int(match.group(1) or match.group(2))
        remainder = match.group(3)
        role = next((candidate for candidate in roles
                     if remainder.casefold().startswith(candidate) and
                     (len(remainder) == len(candidate) or remainder[len(candidate)] in " ,:(\t")), None)
        if role is None:
            role = remainder.split(maxsplit=1)[0].rstrip(",:") if remainder else "unknown"
        details = remainder[len(role):].strip(" \t,: ")
        prefix = line[:len(line) - len(line.lstrip(" \t"))]
        depth = prefix.count("\t") if "\t" in prefix else len(prefix) // 2
        value_match = re.search(r"\bvalue\s*[:=]\s*(true|false|1|0)\b", details, re.IGNORECASE)
        node = {"elementIndex": index, "role": role, "text": details, "depth": depth,
                "line": line_number}
        if role == "outline row":
            node["selected"] = bool(re.match(r"\(selected\)(?:\s|$)", details, re.IGNORECASE))
        if value_match:
            node["value"] = value_match.group(1).casefold() in ("true", "1")
        if role in ("button", "checkbox", "radio button"):
            node["enabled"] = not re.search(r"\bdisabled\b|\(disabled\)", details, re.IGNORECASE)
        nodes.append(node)
    return nodes


def _orca_subtree(nodes, root):
    end = next((position for position in range(nodes.index(root) + 1, len(nodes))
                if nodes[position]["depth"] <= root["depth"]), len(nodes))
    return nodes[nodes.index(root):end]


def _orca_sheet_for_anchor(nodes, anchor):
    folded_anchor = anchor.casefold()
    for node in nodes:
        if node["role"] != "sheet":
            continue
        subtree = _orca_subtree(nodes, node)
        if any(folded_anchor in item["text"].casefold() for item in subtree):
            return subtree
    return None


def _orca_role_ordinal(nodes, role, ordinal, label=None):
    matches = [node for node in nodes if node["role"] == role and
               (label is None or label.casefold() in node["text"].casefold())]
    return matches[ordinal - 1] if len(matches) >= ordinal else None


def find_orca_locator(state, locator):
    snapshot = _snapshot(state)
    nodes = parse_orca_tree_text(snapshot.get("treeText"))
    sheet = _orca_sheet_for_anchor(nodes, locator["anchor"])
    if not sheet:
        return None
    candidates = sheet[1:]
    parent = locator.get("parent")
    if parent:
        parent_node = _orca_role_ordinal(candidates, parent["role"], parent["ordinal"])
        if parent_node is None:
            return None
        parent_subtree = _orca_subtree(nodes, parent_node)
        candidates = [node for node in parent_subtree[1:] if node in candidates]
    return _orca_role_ordinal(candidates, locator["role"], locator["ordinal"], locator.get("label"))


def orca_sheet_subtree(tree_text):
    nodes = parse_orca_tree_text(tree_text)
    sheet = next((node for node in nodes if node["role"] == "sheet"), None)
    if sheet is None:
        raise OrcaError("Orca snapshot has no sheet subtree")
    subtree = _orca_subtree(nodes, sheet)
    lines = tree_text.splitlines()
    return "\n".join(lines[subtree[0]["line"]:subtree[-1]["line"] + 1])


def _element_index(node):
    if not isinstance(node, dict):
        return None
    for key in ("elementIndex", "element_index"):
        value = node.get(key)
        if type(value) is int and value >= 0:
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def _state_strings(state):
    snapshot = _snapshot(state)
    values = []
    tree_text = snapshot.get("treeText")
    if isinstance(tree_text, str):
        values.append(tree_text)
    for node in _walk_nodes(snapshot):
        for key in ("label", "title", "value", "text"):
            value = node.get(key)
            if isinstance(value, str):
                values.append(value)
    return values


def _boolean_value(value):
    if type(value) is bool:
        return value
    if type(value) is int and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.casefold() in ("true", "false", "on", "off", "checked", "unchecked", "1", "0"):
        return value.casefold() in ("true", "on", "checked", "1")
    return None


def _wait_for_anchor(pid, app, anchor, timeout=20):
    deadline = time.monotonic() + timeout
    while True:
        verify_target_process(pid, app)
        try:
            state, _ = _orca_state_for_sheet(pid, anchor, include_screenshot=False)
        except OrcaError as error:
            if error.code or not str(error).startswith("Orca could not find the "):
                raise
            state = None
        if state is not None:
            verify_target_process(pid, app)
            state, _ = _orca_state_for_sheet(pid, anchor)
            return state
        if time.monotonic() >= deadline:
            raise OrcaError(f"Timed out waiting for sheet text {anchor}")
        time.sleep(0.5)


def _selected_outline_row_text(state, anchor):
    nodes = parse_orca_tree_text(_snapshot(state).get("treeText"))
    sheet = _orca_sheet_for_anchor(nodes, anchor)
    if not sheet:
        return None
    for node in sheet:
        if node["role"] == "outline row" and node.get("selected"):
            return re.sub(r"^\(selected\)\s*", "", node["text"], flags=re.IGNORECASE).strip() or "unknown"
    return None


def _wait_for_selection(pid, app, key, locator, timeout=20):
    deadline = time.monotonic() + timeout
    current = None
    while True:
        verify_target_process(pid, app)
        try:
            state, window = _orca_state_for_sheet(pid, locator["anchor"], include_screenshot=False)
        except OrcaError as error:
            if error.code or not str(error).startswith("Orca could not find the "):
                raise
            state, window = None, []
        if state is not None:
            selected = find_orca_locator(state, locator)
            if selected is not None and selected.get("selected") is True:
                verify_target_process(pid, app)
                screenshot_state = orca_app_state(pid, window=window)
                selected = find_orca_locator(screenshot_state, locator)
                if selected is not None and selected.get("selected") is True:
                    return screenshot_state
                state = screenshot_state
            current = _selected_outline_row_text(state, locator["anchor"])
        if time.monotonic() >= deadline:
            current_text = current or "unknown"
            raise OrcaError(f"Orca key {key} did not change selection to {locator['label']}; "
                            f"current selected row is {current_text}")
        time.sleep(0.5)


def _verify_step_state(state, step):
    expectation = step.get("expect")
    if expectation:
        node = find_orca_locator(state, step["click"])
        if node is None or _boolean_value(node.get("value")) is not expectation["value"]:
            raise OrcaError("Orca did not verify the expected control value")
    control = step.get("expect_control")
    if control:
        target = find_orca_locator(state, control["locator"])
        enabled = _boolean_value(target.get("enabled")) if target else None
        if enabled is not control["enabled"]:
            raise OrcaError("Orca did not verify the expected control state")
    missing = [label for label in step.get("expect_labels", [])
               if not any(label.casefold() in text.casefold() for text in _state_strings(state))]
    if missing:
        raise OrcaError("Orca did not verify the expected screen text")


def write_private_bytes(path, data):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        os.fchmod(output.fileno(), 0o600)
        output.write(data)


def capture_orca_screenshot(state, folder, filename):
    result = state.get("result", state) if isinstance(state, dict) else {}
    screenshot = result.get("screenshot", {}) if isinstance(result, dict) else {}
    if not isinstance(screenshot, dict):
        raise OrcaError("Orca returned no screenshot")
    source_path = None
    try:
        source = screenshot.get("path")
        if isinstance(source, str) and source:
            source_path = Path(source)
            source_mode = source_path.lstat().st_mode
            if stat.S_ISLNK(source_mode) or not stat.S_ISREG(source_mode):
                raise OrcaError("Orca screenshot path is not a regular file")
            png = source_path.read_bytes()
        elif isinstance(screenshot.get("data"), str):
            png = base64.b64decode(screenshot["data"], validate=True)
        else:
            raise OrcaError("Orca returned no screenshot")
    except OrcaError:
        raise
    except (OSError, ValueError) as error:
        raise OrcaError("Orca screenshot could not be read") from error
    if len(png) < 1024:
        raise OrcaError("Orca screenshot is too small to be reliable")
    try:
        # Dark appearance screenshots fall mostly into one sampled colour bucket.
        share = png_nonbackground_share(png, reject_placeholders=False, min_share=0.002)
    except ValueError as error:
        raise OrcaError(f"Orca screenshot is invalid: {error}") from error
    path = folder / filename
    try:
        write_private_bytes(path, png)
    except OSError as error:
        raise OrcaError("Could not save the Orca screenshot") from error
    if source_path is not None:
        try:
            source_path.unlink()
        except OSError as error:
            raise OrcaError("Could not remove the Orca temporary screenshot") from error
    return {"kind": "window", "status": "PARTIAL", "path": filename,
            "reason": f"real-pixel screenshot captured; non-background share {share:.3%}; visual review required"}


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
            raise OrcaError("App exited before Orca capture metadata was written")
        if time.monotonic() >= deadline:
            # The app writes metadata only after it becomes active; macOS can refuse that while the user works elsewhere.
            raise OrcaError("Timed out waiting for Orca capture metadata; the app may not have become active")
        time.sleep(0.1)


def _click_orca_locator(pid, app, locator):
    verify_target_process(pid, app)
    state, window = _orca_state_for_sheet(pid, locator["anchor"], locator=locator,
                                           include_screenshot=False)
    node = find_orca_locator(state, locator)
    index = _element_index(node)
    if index is None:
        raise OrcaError(f"Orca could not find an actionable {locator['role']} at ordinal {locator['ordinal']} in the anchored sheet")
    verify_target_process(pid, app)
    orca_json(["click", "--app", f"pid:{pid}", *window, "--element-index", str(index)])


def _press_orca_key(pid, app, key):
    verify_target_process(pid, app)
    orca_json(["press-key", "--app", f"pid:{pid}", "--key", key, "--no-screenshot"])


def _capture_orca_states(pid, app, folder, scenario):
    verify_target_process(pid, app)
    results = []
    if scenario.get("flow"):
        steps = parse_interactive_steps(INTERACTIVE_FLOWS[scenario["flow"]])
        state, _ = _orca_state_for_sheet(pid, steps[0]["click"]["anchor"])
        results.append(capture_orca_screenshot(state, folder, "00-start-orca.png"))
        for step in steps:
            if "click" in step:
                _click_orca_locator(pid, app, step["click"])
            else:
                _press_orca_key(pid, app, step["key"])
            wait_for = step.get("wait_for")
            if wait_for:
                state = _wait_for_anchor(pid, app, wait_for["anchor"])
            elif "wait_for_selection" in step:
                state = _wait_for_selection(pid, app, step["key"], step["wait_for_selection"])
            else:
                verify_target_process(pid, app)
                state, _ = _orca_state_for_sheet(pid, step["click"]["anchor"])
            _verify_step_state(state, step)
            results.append(capture_orca_screenshot(state, folder, step["capture"] + "-orca.png"))
    else:
        state = orca_app_state(pid)
        results.append(capture_orca_screenshot(state, folder, "window-orca.png"))
    return results


def launch_orca(app, folder, scenario):
    args = build_launch_command(app, folder, scenario, backend="orca")
    process = subprocess.Popen(args)
    pid = None
    results = None
    failure = None
    try:
        pid = _wait_for_launched_capture(folder, process)
        verify_target_process(pid, app)
        results = _capture_orca_states(pid, app, folder, scenario)
    except Exception as error:
        failure = error
    if pid is None:
        # A timed-out app keeps running; stop_verified_process checks its path first.
        pid = started_pid(folder)
    if pid is not None:
        try:
            stop_verified_process(pid, app)
        except (OSError, ValueError) as error:
            detail = f"PID {pid} may still be running because its executable path could not be verified"
            failure = OrcaError(f"{failure}; {detail}" if failure else detail)
    try:
        process.wait(timeout=20)
    except subprocess.SubprocessError as error:
        detail = ("after a TERM request to its verified PID" if pid is not None else
                  "because the app PID could not be verified")
        message = f"Orca capture app did not exit {detail}; its process may still be running"
        failure = OrcaError(f"{failure}; {message}" if failure else message)
    if failure is not None:
        if isinstance(failure, (ValueError, OSError, subprocess.SubprocessError)):
            raise OrcaError(f"{failure}") from failure
        raise failure
    return results


def launch_orca_probe(app, folder, scenario):
    """Print the current Orca sheet subtree without clicking or taking screenshots."""
    process = subprocess.Popen(build_launch_command(app, folder, scenario, backend="orca"))
    pid = None
    subtree = None
    failure = None
    try:
        pid = _wait_for_launched_capture(folder, process)
        verify_target_process(pid, app)
        if scenario.get("flow"):
            first = parse_interactive_steps(INTERACTIVE_FLOWS[scenario["flow"]])[0]
            state, _ = _orca_state_for_sheet(pid, first["click"]["anchor"], include_screenshot=False)
        else:
            state = orca_app_state(pid, include_screenshot=False)
        subtree = orca_sheet_subtree(_snapshot(state).get("treeText"))
    except Exception as error:
        failure = error
    if pid is None:
        pid = started_pid(folder)
    if pid is not None:
        try:
            stop_verified_process(pid, app)
        except (OSError, ValueError) as error:
            detail = f"PID {pid} may still be running because its executable path could not be verified"
            failure = OrcaError(f"{failure}; {detail}" if failure else detail)
    try:
        process.wait(timeout=20)
    except subprocess.SubprocessError as error:
        detail = ("after a TERM request to its verified PID" if pid is not None else
                  "because the app PID could not be verified")
        message = f"Orca probe app did not exit {detail}; its process may still be running"
        failure = OrcaError(f"{failure}; {message}" if failure else message)
    if failure is not None:
        if isinstance(failure, (ValueError, OSError, subprocess.SubprocessError)):
            raise OrcaError(f"{failure}") from failure
        raise failure
    return subtree


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
    backend = backend or ("window-server" if window_server else "orca")
    if backend == "orca":
        return launch_orca(app, folder, scenario)
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
    required = {"plan": ["Preservation", "Connect remote", "Freeze", "Hold"],
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
                "wizard-result": ["Cleanup wizard", "Results", "Estimate", "Checkout removed", "Freed from disk", "In Trash (can be restored)"],
                "task-history": ["Task run history", "Space", "Estimate"],
                "cleanup-review": ["Review cleanup", "Expected outcome"],
                "cleanup-hold": ["Review cleanup", "This project is on Hold"],
                "wizard-review": ["Expected outcome", "Review"],
                "wizard-start": ["Make space", "Thaw projects"],
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
                "settings": ["Igloo Settings", "Freezer backup folder"],
                "settings-cloud": ["Confirm cloud backup"],
                "repositories-loading": ["GitHub repositories", "Loading repositories for example"],
                "shelf": ["Done", "Show archive", "Thaw from remote"]}.get(scenario["view"], [])
    def labels(node):
        if not isinstance(node, dict):
            return []
        values = [node.get(key) for key in ("title", "label", "value") if isinstance(node.get(key), str)]
        return values + [value for child in node.get("children", []) for value in labels(child)]
    values = [value.casefold() for value in labels(tree)]
    return [name for name in required if not any(name.casefold() in value for value in values)]


def accessibility_control_issues(tree, scenario):
    """Return missing control issues, or None when the view has no control contract."""
    required = {
        "plan": [
            ("detail.connectRemote", "AXButton"), ("detail.commitSelectedFiles", "AXButton"),
            ("detail.preserve", "AXButton"), ("detail.freeze", "AXButton"),
            ("detail.hold", "AXCheckBox"), ("detail.preservationNotes", "AXTextArea"),
            ("detail.exportProjectReport", "AXButton"), ("detail.savePreservation", "AXButton"),
        ],
        "freeze": [("freeze.cancel", "AXButton"), ("freeze.buildReview", "AXButton")],
        "shelf": [
            ("shelf.backUpNow", "AXButton"), ("shelf.verifyBackup", "AXButton"),
            ("shelf.done", "AXButton"), ("shelf.showArchive", "AXButton"),
            ("shelf.freeze", "AXButton"), ("shelf.offlineRecovery", "AXButton"),
            ("shelf.thawRemote", "AXButton"),
        ],
        "wizard-start": [("wizard.start.makeSpace", "AXButton"), ("wizard.start.thaw", "AXButton")],
        "wizard-thaw-selection": [("wizard.thaw.back", "AXButton"), ("wizard.thaw.prepare", "AXButton"),
                                  ("wizard.thaw.archive.demo-icegarden", "AXCheckBox")],
        "wizard-thaw-review": [("wizard.thaw.acceptance", "AXButton"), ("wizard.thaw.execute", "AXButton")],
        "wizard-thaw-ready": [("wizard.thaw.acceptance", "AXCheckBox"), ("wizard.thaw.execute", "AXButton")],
        "wizard-thaw-occupied": [("wizard.thaw.showPath", "AXButton")],
        "wizard-thaw-remote": [("wizard.thaw.checkRemote", "AXButton")],
        "wizard-thaw-corrupt": [("wizard.thaw.openFreezer", "AXButton")],
        "wizard-thaw-not-frozen": [("wizard.thaw.openFreezer", "AXButton")],
        "wizard-thaw-result": [("wizard.thaw.newPlan", "AXButton")],
    }.get(scenario["view"], [])
    if not required:
        return None

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

    issues = [identifier for identifier, role in required
              if not any(valid(node, identifier, role) for node in captured)]
    if scenario["view"] == "wizard-thaw-ready":
        expected_enabled = {"wizard.thaw.acceptance": True, "wizard.thaw.execute": False}
        for identifier, enabled in expected_enabled.items():
            node = next((item for item in captured if item.get("identifier") == identifier), None)
            if node is None or node.get("enabled") is not enabled:
                issues.append(identifier)
        node = next((item for item in captured if item.get("identifier") == "wizard.thaw.acceptance"), None)
        value = node.get("value") if node else None
        if not (value is False or type(value) is int and value == 0):
            issues.append("wizard.thaw.acceptance")
    return list(dict.fromkeys(issues))


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


def validate_result(folder, scenario, reject_renderer_placeholders=True):
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
    if has_sheet_image(scenario["view"]):
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
    backend.add_argument("--backend", choices=("orca", "window-server"), default="orca",
                         help="real-pixel capture backend (default: orca)")
    backend.add_argument("--window-server", action="store_const", dest="backend", const="window-server",
                         help="compatibility alias for --backend window-server")
    parser.add_argument("--probe", action="store_true",
                        help="launch one scenario and print its Orca sheet subtree without clicking")
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
    if args.probe and (args.backend != "orca" or len(selected) != 1 or len(args.scenario or []) != 1 or args.since or
                       not has_sheet_image(selected[0]["view"])):
        parser.error("--probe requires Orca and exactly one explicit sheet --scenario")
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
            print(launch_orca_probe(app, output / scenario["name"], scenario), flush=True)
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
            result = validate_result(folder, scenario, reject_renderer_placeholders=False)
            width, height = (int(part) for part in scenario["size"].split("x"))
            expected_width, expected_height = expected_size(scenario)
            missing = result.get("accessibilityMissing", [])
            detail = ", ".join(missing) if missing else "completeness not independently established"
            control_issues = result.get("accessibilityControlIssues", [])
            controls = ("not configured for this view" if control_issues is None else
                        (", ".join(control_issues) if control_issues else "none"))
            counts = result.get("accessibilityNodeCounts", {})
            windows = ", ".join(f"{kind}={count}" for kind, count in sorted(counts.get("windows", {}).items()))
            print(f"PARTIAL {scenario['name']}: PID {result['pid']}; requested {width}x{height}, actual {result['actualWidth']}x{result['actualHeight']}, expected {expected_width}x{expected_height}; AX labels {detail}; named controls missing {controls}; AX nodes {counts.get('total', 0)} [{windows}]; PNG needs visual review", flush=True)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            pid = started_pid(folder)
            detail = (f"; PID {pid} may still be running (not stopped)"
                      if pid is not None and (args.backend == "window-server" or not isinstance(error, OrcaError)) else "")
            print(f"OPEN {scenario['name']}: {error}{detail}", flush=True)
            if args.backend == "window-server" and has_sheet_image(scenario["view"]):
                try:
                    sheet = validate_sheet_result(folder, scenario, reject_renderer_placeholders=False)
                    missing = sheet.get("accessibilityMissing", [])
                    ax = ", ".join(missing) if missing else "completeness not independently established"
                    control_issues = sheet.get("accessibilityControlIssues", [])
                    controls = ("not configured for this view" if control_issues is None else
                                (", ".join(control_issues) if control_issues else "none"))
                    counts = sheet.get("accessibilityNodeCounts", {})
                    windows = ", ".join(f"{kind}={count}" for kind, count in sorted(counts.get("windows", {}).items()))
                    print(f"PARTIAL {scenario['name']} renderer sheet diagnostic: PID {sheet['pid']}; AX labels {ax}; named controls missing {controls}; AX nodes {counts.get('total', 0)} [{windows}]; real-pixel evidence needs visual review", flush=True)
                except (OSError, ValueError) as sheet_error:
                    print(f"OPEN {scenario['name']} sheet: {sheet_error}", flush=True)
            failed = True
    print("Review checklist: docs/capture-review.md", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
