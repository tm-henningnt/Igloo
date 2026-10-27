#!/usr/bin/env python3
"""Launch independent demo captures from an already built Igloo app on macOS."""
import argparse
import colorsys
import json
import os
import math
import time
import stat
import struct
import zlib
from collections import Counter
from pathlib import Path
import subprocess
import sys


SHEET_VIEWS = {"freeze", "shelf", "freeze-review", "preserve-review", "freeze-result", "freeze-failure",
               "freeze-hold", "freeze-no-remote", "freeze-no-files", "freeze-no-commit", "freeze-own-source", "freeze-process",
               "cleanup-review", "cleanup-hold", "wizard-review", "task-review", "settings", "settings-cloud"}
UPDATE_FAILURES = ("quota", "quota-no-reset", "forbidden", "offline", "timeout", "server", "malformed",
                   "cancelled-download", "digest-mismatch")
SHEET_VIEWS.update("update-" + name for name in UPDATE_FAILURES)


def has_sheet_image(view):
    return view in SHEET_VIEWS or view.startswith("update-")


def scenarios():
    rows = []
    for appearance in ("light", "dark"):
        cases = [(view, "1440x900", "shown", "full", "normal") for view in
                 ("overview", "git", "worktrees", "local-files", "cleanup", "plan", "freeze", "shelf")]
        cases += [("update-" + name, "1440x900", "shown", "full", "normal") for name in
                  ("quota", "quota-no-reset", "forbidden", "offline", "timeout", "server", "malformed", "cancelled-download", "digest-mismatch")]
        cases += [("overview", "850x700", "hidden", "compact", "normal"),
                  ("plan", "850x700", "hidden", "compact", "normal"),
                  ("overview", "1100x700", "shown", "compact", "normal"),
                  ("plan", "1100x700", "shown", "compact", "normal"),
                  ("overview", "1440x900", "hidden", "compact", "long"),
                  ("overview", "1440x900", "shown", "full", "empty")]
        # Project-row badges need every rail/scope combination at both sizes,
        # including a long name to expose truncation problems.
        for size in ("850x700", "1440x900"):
            for scope in ("shown", "hidden"):
                for rail in ("compact", "full"):
                    for data in ("normal", "long"):
                        case = ("overview", size, scope, rail, data)
                        if case not in cases:
                            cases.append(case)
        added_views = sorted(SHEET_VIEWS | {"hold"})
        cases += [(view, "1440x900", "shown", "full", "normal") for view in added_views
                  if view not in {"freeze", "shelf"} and not view.startswith("update-")]
        cases += [(view, "850x700", "hidden", "compact", "normal") for view in added_views]
        for view, size, scope, rail, data in cases:
            name = "-".join((appearance, view, size, scope, rail, data))
            rows.append(dict(name=name, appearance=appearance, view=view, size=size, scope=scope, rail=rail, data=data))
    return rows


def build_launch_command(app, folder, scenario, window_server=False):
    if not folder.is_absolute():
        raise ValueError("--demo-capture requires an absolute path")
    args = ["/usr/bin/open", "-n", "-W", str(app), "--args", "--demo", "--demo-" + scenario["appearance"],
            "--demo-size", scenario["size"], "--demo-view", scenario["view"], "--demo-scope", scenario["scope"],
            "--demo-rail", scenario["rail"], "--demo-data", scenario["data"], "--demo-capture", str(folder)]
    if window_server:
        args.append("--demo-window-server")
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


def write_private_json(path, data):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        os.fchmod(output.fileno(), 0o600)
        json.dump(data, output, indent=2)
        output.write("\n")


def launch(app, folder, scenario, window_server=False):
    args = build_launch_command(app, folder, scenario, window_server)
    if not window_server:
        subprocess.run(args, check=True, timeout=60)
        return []
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


def expected_size(scenario):
    width, height = (int(part) for part in scenario["size"].split("x"))
    if scenario["scope"] == "shown":
        minimum_width = 1300
    else:
        minimum_width = 850 if scenario["rail"] == "compact" else 1100
    # The native minimum includes the window chrome in capture metadata.
    return max(width, minimum_width), max(height, 793)


def png_nonbackground_share(png, allow_sparse_sheet=False):
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
        if placeholder > 0.02:
            raise ValueError("AppKit-backed views need a window-server capture")
        share = 1 - max(counts.values()) / total
        if share <= 0.01:
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
                "freeze-result": ["Outcome", "Done"], "freeze-failure": ["Outcome", "Done"],
                "cleanup-review": ["Review cleanup", "Expected outcome"],
                "cleanup-hold": ["Review cleanup", "This project is on Hold"],
                "wizard-review": ["Expected outcome", "Review"],
                "task-review": ["Review task actions", "Expected outcome"],
                "settings": ["Igloo Settings", "Freezer backup folder"],
                "settings-cloud": ["Confirm cloud backup"],
                "shelf": ["Done", "Show archive", "Thaw from remote"]}.get(scenario["view"], [])
    def labels(node):
        if not isinstance(node, dict):
            return []
        values = [node.get(key) for key in ("title", "label", "value") if isinstance(node.get(key), str)]
        return values + [value for child in node.get("children", []) for value in labels(child)]
    values = [value.casefold() for value in labels(tree)]
    return [name for name in required if not any(name.casefold() in value for value in values)]


def validate_result(folder, scenario):
    try:
        mode = folder.lstat().st_mode
    except OSError as error:
        raise ValueError(f"Missing scenario directory: {error}") from error
    if not stat.S_ISDIR(mode) or stat.S_IMODE(mode) != 0o700:
        raise ValueError("Scenario directory permissions must be 0700")
    try:
        png = (folder / "window.png").read_bytes()
        result = json.loads((folder / "accessibility.json").read_text())
        started = json.loads((folder / "started.json").read_text())
        window_metadata = json.loads((folder / "windows.json").read_text())
    except (OSError, ValueError) as error:
        raise ValueError(f"Missing or invalid capture output: {error}") from error
    if not isinstance(result, dict) or not isinstance(started, dict):
        raise ValueError("Invalid capture metadata")
    result["nonBackgroundShare"] = png_nonbackground_share(png)
    if has_sheet_image(scenario["view"]):
        try:
            result["sheetNonBackgroundShare"] = png_nonbackground_share((folder / "sheet.png").read_bytes(), allow_sparse_sheet=True)
        except OSError as error:
            raise ValueError(f"Missing sheet capture: {error}") from error
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
    return result


def validate_sheet_result(folder, scenario):
    """Validate a sheet independently, including its metadata and dimensions."""
    if not has_sheet_image(scenario["view"]):
        raise ValueError("Scenario has no sheet image")
    try:
        mode = folder.lstat().st_mode
        if not stat.S_ISDIR(mode) or stat.S_IMODE(mode) != 0o700:
            raise ValueError("Scenario directory permissions must be 0700")
        png = (folder / "sheet.png").read_bytes()
        result = json.loads((folder / "accessibility.json").read_text())
        started = json.loads((folder / "started.json").read_text())
        metadata = json.loads((folder / "windows.json").read_text())
    except (OSError, ValueError) as error:
        raise ValueError(f"Missing or invalid sheet capture: {error}") from error
    if not isinstance(result, dict) or not isinstance(started, dict):
        raise ValueError("Invalid capture metadata")
    result["sheetNonBackgroundShare"] = png_nonbackground_share(png, allow_sparse_sheet=True)
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
    parser.add_argument("--list", action="store_true", help="list scenarios without launching")
    parser.add_argument("--window-server", action="store_true", help="also capture on-screen app windows with screencapture")
    args = parser.parse_args()
    matrix = scenarios()
    if args.list:
        for scenario in matrix:
            print(scenario["name"])
        return 0
    if not args.output:
        parser.error("--output is required")
    if sys.platform != "darwin":
        parser.error("capture requires macOS")
    app = args.app.resolve()
    if not (app / "Contents/MacOS/Igloo").is_file():
        parser.error("--app must name a built Igloo.app")
    chosen = set(args.scenario or [row["name"] for row in matrix])
    unknown = chosen - {row["name"] for row in matrix}
    if unknown:
        parser.error("unknown scenario: " + ", ".join(sorted(unknown)))
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
    failed = False
    for scenario in matrix:
        if scenario["name"] not in chosen:
            continue
        folder = output / scenario["name"]
        try:
            server_results = launch(app, folder, scenario, True) if args.window_server else launch(app, folder, scenario)
            for server in server_results or []:
                print(f"{server['status']} {scenario['name']} {server['kind']} window-server: {server['reason']}", flush=True)
            if scenario["view"].startswith("update-"):
                sheet_share = png_nonbackground_share((folder / "sheet.png").read_bytes(), allow_sparse_sheet=True)
                print(f"PARTIAL {scenario['name']} update sheet: non-background share {sheet_share:.3%}; visual review required", flush=True)
            result = validate_result(folder, scenario)
            width, height = (int(part) for part in scenario["size"].split("x"))
            expected_width, expected_height = expected_size(scenario)
            missing = result.get("accessibilityMissing", [])
            detail = ", ".join(missing) if missing else "completeness not independently established"
            print(f"PARTIAL {scenario['name']}: PID {result['pid']}; requested {width}x{height}, actual {result['actualWidth']}x{result['actualHeight']}, expected {expected_width}x{expected_height}; AX {detail}; PNG needs visual review", flush=True)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            pid = started_pid(folder)
            detail = f"; PID {pid} may still be running (not stopped)" if pid is not None else ""
            print(f"OPEN {scenario['name']}: {error}{detail}", flush=True)
            if has_sheet_image(scenario["view"]):
                try:
                    sheet = validate_sheet_result(folder, scenario)
                    missing = sheet.get("accessibilityMissing", [])
                    ax = ", ".join(missing) if missing else "completeness not independently established"
                    print(f"PARTIAL {scenario['name']} sheet: PID {sheet['pid']}; AX {ax}; PNG needs visual review", flush=True)
                except (OSError, ValueError) as sheet_error:
                    print(f"OPEN {scenario['name']} sheet: {sheet_error}", flush=True)
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
