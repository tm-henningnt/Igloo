"""Capture launcher checks use disposable output and a fake process boundary."""
import contextlib
import importlib.util
import io
import json
import stat
import struct
import zlib
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("capture_states", ROOT / "scripts/capture_states.py")
capture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(capture)


def sample_png(uniform=False, near_uniform=False, placeholder=False, yellow=(255, 204, 0), red=(255, 59, 48)):
    width, height = 64, 64
    def chunk(kind, payload):
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xffffffff)
    rows = []
    for y in range(height):
        pixels = bytearray()
        for x in range(width):
            black = uniform or (near_uniform and (x, y) != (0, 0)) or (not near_uniform and (x + y) % 2)
            color = (0, 0, 0, 255) if black else (240, 240, 240, 255)
            if placeholder and not black:
                color = (*yellow, 255) if y < 32 else (*red, 255)
            pixels.extend(color)
        rows.append(b"\0" + pixels)
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b"")


def sparse_sheet_png(noise=False):
    """White sheet with either two small text-like rows or scattered dark noise."""
    width, height = 128, 128
    def chunk(kind, payload):
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xffffffff)
    rows = []
    for y in range(height):
        pixels = bytearray()
        for x in range(width):
            if noise:
                dark = y < 80 and x == (y * 17) % width
            else:
                dark = (20 <= y < 25 or 50 <= y < 55) and any(start <= x < start + 2 for start in (20, 30, 40, 50))
            pixels.extend((0, 0, 0, 255) if dark else (255, 255, 255, 255))
        rows.append(b"\0" + pixels)
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b"")


def write_window_metadata(folder, pid, sheet=False):
    frame = {"x": 0, "y": 0, "width": 900, "height": 700}
    windows = [{"kind": "window", "windowNumber": 123, "frame": frame, "isOnScreen": True}]
    if sheet:
        windows.append({"kind": "sheet", "windowNumber": 124, "frame": frame, "isOnScreen": True})
    (folder / "windows.json").write_text(json.dumps({"pid": pid, "windows": windows}))


class CaptureStatesTests(unittest.TestCase):
    def test_shown_scope_minimum_fits_inspector_and_detail_tabs(self):
        scenario = {"size": "1100x700", "scope": "shown"}
        self.assertEqual(capture.expected_size(scenario), (1300, 793))

    def test_full_rail_scope_hidden_uses_its_minimum_width(self):
        scenario = {"size": "850x700", "scope": "hidden", "rail": "full"}
        self.assertEqual(capture.expected_size(scenario), (1100, 793))

    def test_demo_remote_urls_are_valid(self):
        catalog = json.loads((ROOT / "assets/demo-catalog.json").read_text())
        urls = [url for project in catalog["projects"] for remote in project.get("remotes", [])
                for url in (remote.get("url", ""), remote.get("pushURL", "")) if url]
        self.assertTrue(urls)
        for url in urls:
            with self.subTest(url=url):
                parsed = urlsplit(url)
                self.assertEqual(parsed.scheme, "https")
                self.assertTrue(parsed.netloc)
                self.assertNotRegex(url, r"\s")

    def test_demo_urls_in_catalog_and_task_fixtures_have_no_spaces(self):
        def values(value):
            if isinstance(value, dict):
                for child in value.values():
                    yield from values(child)
            elif isinstance(value, list):
                for child in value:
                    yield from values(child)
            elif isinstance(value, str) and value.startswith("https://"):
                yield value

        urls = []
        for fixture in ("demo-catalog.json", "demo-tasks.json"):
            data = json.loads((ROOT / "assets" / fixture).read_text())
            urls.extend(values(data))
        self.assertTrue(urls)
        for url in urls:
            with self.subTest(url=url):
                self.assertNotRegex(url, r"\s")

    def test_demo_wizard_freeze_outcome_matches_its_project(self):
        tasks = json.loads((ROOT / "assets/demo-tasks.json").read_text())
        catalog = json.loads((ROOT / "assets/demo-catalog.json").read_text())
        review = tasks["demoWizardReview"]
        freeze_rows = [row for row in review["rows"] if row["action"] == "freeze"]
        self.assertEqual(review["outcome"]["checkoutsRemoved"]["paths"], [row["path"] for row in freeze_rows])
        harbor_cli = next(project for project in catalog["projects"] if project["name"] == "Harbor CLI")
        self.assertLessEqual(catalog["reviewOutcomes"]["freeze"]["bytesFreedEstimate"], harbor_cli["size"])
        self.assertLessEqual(catalog["reviewOutcomes"]["wizard"]["bytesFreedEstimate"], harbor_cli["size"])
        for key in ("demoWizardReview", "demoTaskReview"):
            item = tasks[key]
            row_paths = {row["path"] for row in item["rows"]}
            self.assertTrue(all(held["path"] in row_paths for held in item["outcome"]["held"]))
        for pushed in review["outcome"]["refsPushed"]:
            self.assertRegex(pushed["sha"], r"^[0-9a-f]{7,12}$")
            project = next(project for project in catalog["projects"] if project["path"] == freeze_rows[0]["path"])
            self.assertIn(pushed["destination"], [remote["pushURL"] for remote in project["remotes"]])

    def test_updater_scenarios_are_captured_as_sheets(self):
        for scenario in capture.scenarios():
            if scenario["view"].startswith("update-"):
                with self.subTest(view=scenario["view"]):
                    self.assertIn(scenario["view"], capture.SHEET_VIEWS)

    def test_matrix_covers_required_states(self):
        scenarios = capture.scenarios()
        self.assertEqual({s["appearance"] for s in scenarios}, {"light", "dark"})
        self.assertTrue({"850x700", "1100x700", "1300x700", "1440x900"}.issubset({s["size"] for s in scenarios}))
        self.assertTrue({"overview", "git", "worktrees", "local-files", "cleanup", "plan", "freeze", "shelf"}.issubset({s["view"] for s in scenarios}))
        self.assertEqual({s["scope"] for s in scenarios}, {"shown", "hidden"})
        self.assertEqual({s["rail"] for s in scenarios}, {"compact", "full"})
        self.assertTrue({"normal", "long", "empty"}.issubset({s["data"] for s in scenarios}))
        self.assertEqual(len({s["name"] for s in scenarios}), len(scenarios))

    def test_review_and_settings_matrix_includes_minimum_and_default_in_both_appearances(self):
        views = {"freeze-review", "preserve-review", "freeze-result", "cleanup-review", "wizard-review",
                 "task-review", "settings", "hold", "freeze", "shelf"}
        scenarios = capture.scenarios()
        for view in views:
            for appearance in ("light", "dark"):
                for size in ("850x700", "1440x900"):
                    with self.subTest(view=view, appearance=appearance, size=size):
                        self.assertTrue(any(s["view"] == view and s["appearance"] == appearance and s["size"] == size
                                            for s in scenarios))

    def test_review_sheet_requires_sheet_artifact_and_metadata(self):
        scenario = next(s for s in capture.scenarios() if s["view"] == "cleanup-review" and s["size"] == "1440x900")
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / "window.png").write_bytes(sample_png())
            (folder / "started.json").write_text(json.dumps({"pid": 123}))
            write_window_metadata(folder, 123)
            (folder / "accessibility.json").write_text(json.dumps({"pid": 123, "tree": {"role": "AXWindow"},
                                                                    "actualWidth": 1440, "actualHeight": 900}))
            with self.assertRaisesRegex(ValueError, "sheet"):
                capture.validate_result(folder, scenario)

    def test_sheet_evidence_is_reported_independently_of_black_window(self):
        scenario = next(s for s in capture.scenarios() if s["view"] == "freeze")
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / "window.png").write_bytes(sample_png(uniform=True))
            (folder / "sheet.png").write_bytes(sample_png())
            (folder / "started.json").write_text(json.dumps({"pid": 123}))
            write_window_metadata(folder, 123, sheet=True)
            (folder / "accessibility.json").write_text(json.dumps({"pid": 123, "tree": {"role": "AXSheet"},
                                                                    "actualWidth": 1440, "actualHeight": 900}))
            result = capture.validate_sheet_result(folder, scenario)
            self.assertGreater(result["sheetNonBackgroundShare"], 0.01)
            with self.assertRaisesRegex(ValueError, "uniform"):
                capture.validate_result(folder, scenario)

    def test_demo_projects_have_checklist_and_hold_data(self):
        catalog = json.loads((ROOT / "assets/demo-catalog.json").read_text())
        for project in catalog["projects"]:
            with self.subTest(project=project["name"]):
                self.assertIsInstance(project.get("blockers"), list)
                self.assertIsInstance(project.get("hold"), bool)

    def test_update_failure_scenarios_cover_both_appearances(self):
        expected = {"quota", "quota-no-reset", "forbidden", "offline", "timeout", "server", "malformed", "cancelled-download", "digest-mismatch"}
        captures = [row for row in capture.scenarios()
                    if row["view"].startswith("update-") and row["view"] != "update-markdown"]
        self.assertEqual({row["view"].removeprefix("update-") for row in captures}, expected)
        for failure in expected:
            states = [row for row in captures if row["view"] == "update-" + failure]
            self.assertEqual({row["appearance"] for row in states}, {"light", "dark"})
            self.assertEqual({row["size"] for row in states}, {"850x700", "1440x900"})

    def test_markdown_update_sheet_covers_minimum_and_default_sizes(self):
        captures = [row for row in capture.scenarios() if row["view"] == "update-markdown"]
        self.assertEqual({(row["appearance"], row["size"]) for row in captures},
                         {(appearance, size) for appearance in ("light", "dark")
                          for size in ("850x700", "1440x900")})

    def test_preservation_detail_covers_scope_minimum_and_default_sizes(self):
        captures = [row for row in capture.scenarios() if row["view"] == "plan" and row["scope"] == "shown"]
        self.assertTrue({(appearance, size) for appearance in ("light", "dark")
                         for size in ("1300x700", "1440x900")}.issubset(
                             {(row["appearance"], row["size"]) for row in captures}))

    def test_overview_covers_scope_visible_minimum_and_default_sizes(self):
        captures = [row for row in capture.scenarios() if row["view"] == "overview" and row["scope"] == "shown"]
        self.assertTrue({(appearance, size) for appearance in ("light", "dark")
                         for size in ("1300x700", "1440x900")}.issubset(
                             {(row["appearance"], row["size"]) for row in captures}))


    def test_new_review_accessibility_reports_missing_controls(self):
        expected = {"cleanup-review": "Review cleanup", "wizard-review": "Expected outcome",
                    "task-review": "Review task actions", "settings": "Freezer backup folder",
                    "freeze-review": "Expected outcome", "freeze-hold": "This project is on Hold"}
        for view, label in expected.items():
            with self.subTest(view=view):
                self.assertIn(label, capture.accessibility_missing({"role": "AXSheet"}, {"view": view}))

    def test_named_controls_require_identifiers_enabled_state_and_frame(self):
        requirements = {
            "plan": ["detail.connectRemote", "detail.commitSelectedFiles", "detail.preserve",
                      "detail.freeze", "detail.hold", "detail.preservationNotes",
                      "detail.exportProjectReport", "detail.savePreservation"],
            "freeze": ["freeze.cancel", "freeze.buildReview"],
            "shelf": ["shelf.backUpNow", "shelf.verifyBackup", "shelf.done", "shelf.showArchive",
                      "shelf.freeze", "shelf.offlineRecovery", "shelf.thawRemote"],
        }
        for view, identifiers in requirements.items():
            with self.subTest(view=view):
                tree = {"role": "AXCapture", "children": [{
                    "role": "AXWindow", "windowKind": "sheet" if view != "plan" else "window",
                    "children": [{"role": {"detail.hold": "AXCheckBox",
                                             "detail.preservationNotes": "AXTextArea"}.get(identifier, "AXButton"),
                                  "identifier": identifier, "label": identifier,
                                  "enabled": True,
                                  "frame": {"x": 1, "y": 2, "width": 40, "height": 20}}
                                 for identifier in identifiers]
                }]}
                self.assertEqual(capture.accessibility_control_issues(tree, {"view": view}), [])
                node = tree["children"][0]["children"][-1]
                node["identifier"] = "wrong.identifier"
                self.assertIn(identifiers[-1], capture.accessibility_control_issues(tree, {"view": view}))
                node["identifier"] = identifiers[-1]
                node["role"] = "AXTextField"
                self.assertIn(identifiers[-1], capture.accessibility_control_issues(tree, {"view": view}))
                node["role"] = "AXButton"
                node["enabled"] = None
                self.assertIn(identifiers[-1], capture.accessibility_control_issues(tree, {"view": view}))
                node["enabled"] = True
                del node["frame"]
                self.assertIn(identifiers[-1], capture.accessibility_control_issues(tree, {"view": view}))
        self.assertIsNone(capture.accessibility_control_issues({"children": []}, {"view": "overview"}))

    def test_accessibility_dump_counts_each_captured_window_tree(self):
        tree = {"role": "AXCapture", "children": [
            {"role": "AXWindow", "windowKind": "window", "children": [{"role": "AXButton"}]},
            {"role": "AXWindow", "windowKind": "sheet", "children": [{"role": "AXButton"}, {"role": "AXButton"}]},
        ]}
        self.assertEqual(capture.accessibility_node_counts(tree),
                         {"total": 5, "windows": {"window": 2, "sheet": 3}})

    def test_empty_repository_checklist_capture_exists_in_both_appearances_and_sizes(self):
        cases = [row for row in capture.scenarios() if row["view"] == "freeze-no-files"]
        self.assertEqual({(row["appearance"], row["size"]) for row in cases},
                         {(appearance, size) for appearance in ("light", "dark")
                          for size in ("850x700", "1440x900")})

    def test_capture_requires_real_png_and_accessibility_output(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            with self.assertRaises(ValueError):
                capture.validate_result(folder, capture.scenarios()[0])
            (folder / "window.png").write_bytes(b"not a PNG")
            (folder / "accessibility.json").write_text(json.dumps({"pid": 1, "tree": {"role": "AXWindow"}}))
            with self.assertRaises(ValueError):
                capture.validate_result(folder, capture.scenarios()[0])

    def test_rejects_black_and_near_uniform_png(self):
        with self.assertRaisesRegex(ValueError, "uniform"):
            capture.png_nonbackground_share(sample_png(uniform=True))
        with self.assertRaisesRegex(ValueError, "uniform"):
            capture.png_nonbackground_share(sample_png(near_uniform=True))
        self.assertGreater(capture.png_nonbackground_share(sample_png()), 0.01)

    def test_sparse_sheet_text_is_partial_not_uniform(self):
        png = sparse_sheet_png()
        with self.assertRaisesRegex(ValueError, "uniform"):
            capture.png_nonbackground_share(png)
        share = capture.png_nonbackground_share(png, allow_sparse_sheet=True)
        self.assertGreater(share, 0)
        self.assertLessEqual(share, 0.01)

    def test_sparse_sheet_does_not_accept_scattered_noise(self):
        with self.assertRaisesRegex(ValueError, "uniform"):
            capture.png_nonbackground_share(sparse_sheet_png(noise=True), allow_sparse_sheet=True)
        with self.assertRaisesRegex(ValueError, "uniform"):
            capture.png_nonbackground_share(sample_png(uniform=True), allow_sparse_sheet=True)

    def test_sparse_sheet_result_validates_with_required_metadata(self):
        scenario = next(s for s in capture.scenarios() if s["view"] == "freeze-result")
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / "sheet.png").write_bytes(sparse_sheet_png())
            (folder / "started.json").write_text(json.dumps({"pid": 123}))
            write_window_metadata(folder, 123, sheet=True)
            (folder / "accessibility.json").write_text(json.dumps({"pid": 123, "tree": {"role": "AXSheet"},
                                                                    "actualWidth": 1440, "actualHeight": 900}))
            result = capture.validate_sheet_result(folder, scenario)
            self.assertGreater(result["sheetNonBackgroundShare"], 0)
            self.assertLessEqual(result["sheetNonBackgroundShare"], 0.01)

    def test_rejects_appkit_placeholder_colors(self):
        with self.assertRaisesRegex(ValueError, "AppKit-backed views need a window-server capture"):
            capture.png_nonbackground_share(sample_png(placeholder=True))

    def test_rejects_dark_and_pure_yellow_appkit_placeholders(self):
        neutral = (240, 240, 240)
        for yellow, red in (((255, 214, 10), neutral), ((255, 255, 0), neutral), (neutral, (255, 69, 58))):
            with self.subTest(yellow=yellow, red=red):
                with self.assertRaisesRegex(ValueError, "AppKit-backed views need a window-server capture"):
                    capture.png_nonbackground_share(sample_png(placeholder=True, yellow=yellow, red=red))

    def test_rejects_insecure_app_created_scenario_directory(self):
        scenario = capture.scenarios()[0]
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / "scenario"
            folder.mkdir(mode=0o755)
            folder.chmod(0o755)
            with self.assertRaisesRegex(ValueError, "permissions"):
                capture.validate_result(folder, scenario)

    def test_capture_rejects_wrong_content_size(self):
        scenario = next(s for s in capture.scenarios() if s["size"] == "850x700" and not capture.has_sheet_image(s["view"]))
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / "window.png").write_bytes(sample_png())
            (folder / "started.json").write_text(json.dumps({"pid": 123}))
            write_window_metadata(folder, 123)
            (folder / "accessibility.json").write_text(json.dumps({"pid": 123, "tree": {"role": "AXWindow"},
                                                                      "actualWidth": 1100, "actualHeight": 793}))
            with self.assertRaisesRegex(ValueError, "size"):
                capture.validate_result(folder, scenario)

    def test_capture_accepts_the_reported_clamped_size(self):
        scenario = next(s for s in capture.scenarios() if s["size"] == "850x700" and not capture.has_sheet_image(s["view"]))
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / "window.png").write_bytes(sample_png())
            (folder / "started.json").write_text(json.dumps({"pid": 123}))
            write_window_metadata(folder, 123)
            (folder / "accessibility.json").write_text(json.dumps({"pid": 123, "tree": {"role": "AXWindow", "children": [
                                                                          {"role": "AXRadioButton", "label": "Preservation"},
                                                                          {"role": "AXButton", "title": "Connect remote…"},
                                                                          {"role": "AXButton", "title": "Freeze…"},
                                                                          {"role": "AXButton", "title": "Hold"}]},
                                                                      "actualWidth": 850, "actualHeight": 793}))
            self.assertEqual(capture.validate_result(folder, scenario)["actualWidth"], 850)

    def test_capture_marks_sheet_tree_missing_required_controls_as_partial(self):
        scenario = next(s for s in capture.scenarios() if s["view"] == "freeze")
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / "window.png").write_bytes(sample_png())
            (folder / "sheet.png").write_bytes(sample_png())
            (folder / "started.json").write_text(json.dumps({"pid": 123}))
            write_window_metadata(folder, 123, sheet=True)
            (folder / "accessibility.json").write_text(json.dumps({"pid": 123, "tree": {"role": "AXSheet"},
                                                                      "actualWidth": 1440, "actualHeight": 900}))
            result = capture.validate_result(folder, scenario)
            self.assertEqual(result["accessibilityMissing"], ["Requirements", "Cancel", "Build review"])

    def test_launch_rejects_relative_capture_folder(self):
        with patch.object(capture.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "absolute"):
                capture.launch(Path("/example/Igloo.app"), Path("captures/relative"), capture.scenarios()[0])
            run.assert_not_called()

    def test_main_refuses_existing_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = root / "Igloo.app"
            executable = app / "Contents/MacOS/Igloo"
            executable.parent.mkdir(parents=True)
            executable.touch()
            output = root / "existing"
            output.mkdir()
            args = ["capture_states", "--app", str(app), "--output", str(output)]
            with patch.object(sys, "argv", args), patch.object(capture.sys, "platform", "darwin"), \
                 patch.object(capture, "launch") as launch:
                with self.assertRaises((SystemExit, FileExistsError)):
                    capture.main()
                launch.assert_not_called()

    def test_main_refuses_dangling_output_symlink(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = root / "Igloo.app"
            executable = app / "Contents/MacOS/Igloo"
            executable.parent.mkdir(parents=True)
            executable.touch()
            target = root / "must-not-be-created"
            output = root / "link"
            output.symlink_to(target)
            args = ["capture_states", "--app", str(app), "--output", str(output)]
            with patch.object(sys, "argv", args), patch.object(capture.sys, "platform", "darwin"), \
                 patch.object(capture, "launch") as launch:
                with self.assertRaises(SystemExit):
                    capture.main()
                launch.assert_not_called()
            self.assertTrue(output.is_symlink())
            self.assertFalse(target.exists())

    def test_main_creates_private_output_and_passes_new_path_to_app(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = root / "Igloo.app"
            executable = app / "Contents/MacOS/Igloo"
            executable.parent.mkdir(parents=True)
            executable.touch()
            output = root / "output"
            scenario = capture.scenarios()[0]
            args = ["capture_states", "--app", str(app), "--output", str(output), "--scenario", scenario["name"]]
            def fake_launch(app_path, folder, selected):
                self.assertFalse(folder.exists(), "the app must create its own new scenario directory")
                folder.mkdir(mode=0o700)
            result = {"pid": 123, "actualWidth": 1440, "actualHeight": 900}
            text = io.StringIO()
            with patch.object(sys, "argv", args), patch.object(capture.sys, "platform", "darwin"), \
                 patch.object(capture, "launch", side_effect=fake_launch), \
                 patch.object(capture, "validate_result", return_value=result), contextlib.redirect_stdout(text):
                self.assertEqual(capture.main(), 0)
            self.assertIn("PARTIAL " + scenario["name"], text.getvalue())
            self.assertNotIn("PASS " + scenario["name"], text.getvalue())
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)

    def test_timeout_reports_started_pid_without_stopping_it(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = root / "Igloo.app"
            executable = app / "Contents/MacOS/Igloo"
            executable.parent.mkdir(parents=True)
            executable.touch()
            output = root / "output"
            scenario = next(s for s in capture.scenarios() if s["view"] == "freeze")
            args = ["capture_states", "--app", str(app), "--output", str(output), "--scenario", scenario["name"]]
            def timeout(app_path, folder, selected):
                folder.mkdir(mode=0o700)
                (folder / "started.json").write_text(json.dumps({"pid": 4321}))
                raise subprocess.TimeoutExpired(["open"], 60)
            text = io.StringIO()
            with patch.object(sys, "argv", args), patch.object(capture.sys, "platform", "darwin"), \
                 patch.object(capture, "launch", side_effect=timeout), contextlib.redirect_stdout(text):
                self.assertEqual(capture.main(), 1)
            self.assertIn("OPEN " + scenario["name"], text.getvalue())
            self.assertIn("PID 4321", text.getvalue())

    def test_window_server_argument_is_forwarded_only_when_requested(self):
        scenario = capture.scenarios()[0]
        normal = capture.build_launch_command(Path("/example/Igloo.app"), Path("/tmp/capture"), scenario, False)
        server = capture.build_launch_command(Path("/example/Igloo.app"), Path("/tmp/capture"), scenario, True)
        self.assertNotIn("--demo-window-server", normal)
        self.assertIn("--demo-window-server", server)

    def test_window_metadata_requires_number_frame_and_screen_state(self):
        window = {"kind": "window", "windowNumber": 123, "frame": {"x": 1, "y": 2, "width": 900, "height": 700}, "isOnScreen": True}
        self.assertEqual(capture.validate_windows({"pid": 42, "windows": [window]}), [window])
        for missing in ("windowNumber", "frame", "isOnScreen"):
            broken = dict(window)
            broken.pop(missing)
            with self.assertRaises(ValueError):
                capture.validate_windows({"pid": 42, "windows": [broken]})

    def test_window_server_captures_only_onscreen_windows(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            frame = {"x": 1, "y": 2, "width": 900, "height": 700}
            metadata = {"pid": 42, "windows": [{"kind": "window", "windowNumber": 123, "frame": frame, "isOnScreen": True},
                                               {"kind": "sheet", "windowNumber": 124, "frame": frame, "isOnScreen": False}]}
            (folder / "windows.json").write_text(json.dumps(metadata))
            (folder / "started.json").write_text(json.dumps({"pid": 42}))
            def fake_run(args, **kwargs):
                Path(args[-1]).write_bytes(sample_png())
            with patch.object(capture.subprocess, "run", side_effect=fake_run) as run:
                result = capture.capture_window_server(folder)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0][:5], ["/usr/sbin/screencapture", "-o", "-x", "-l", "123"])
            self.assertEqual(result[0]["status"], "PARTIAL")
            self.assertEqual(result[1]["status"], "OPEN")
            self.assertIn("display asleep or locked", result[1]["reason"])

    def test_launch_passes_demo_only_capture_arguments(self):
        scenario = capture.scenarios()[0]
        with tempfile.TemporaryDirectory() as temp, patch.object(capture.subprocess, "run") as run:
            capture.launch(Path("/example/Igloo.app"), Path(temp), scenario)
            args = run.call_args.args[0]
            self.assertEqual(args[:4], ["/usr/bin/open", "-n", "-W", "/example/Igloo.app"])
            for argument in ("--demo", "--demo-size", "--demo-view", "--demo-capture", "--demo-scope", "--demo-rail", "--demo-data"):
                self.assertIn(argument, args)
