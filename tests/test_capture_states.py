"""Capture launcher checks use disposable output and a fake process boundary."""
import contextlib
import fnmatch
import importlib.util
import io
import json
import plistlib
import re
import stat
import signal
import struct
import zlib
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, call, patch
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


def write_renderer_png(folder, kind, png):
    path = capture.renderer_path(folder, kind)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    path.write_bytes(png)


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

    def test_demo_thaw_catalog_covers_blockers_and_uses_demo_paths(self):
        tasks = json.loads((ROOT / "assets/demo-tasks.json").read_text())
        thaw = tasks["demoWizardThaw"]
        rows = thaw["review"]["rows"]
        messages = "\n".join(row["message"] for row in rows)
        for blocker in ("occupied", "unreachable", "checksum", "no longer Frozen", "Open Freezer"):
            self.assertIn(blocker, messages)
        result = thaw["result"]
        self.assertEqual({row["id"] for row in result["rows"]}, {row["id"] for row in rows})
        blocked_paths = {row["path"] for row in result["rows"] if row["status"] == "blocked"}
        self.assertEqual(blocked_paths, {item["path"] for item in result["outcome"]["held"]})
        restored = result["outcome"]["refsRestored"]
        self.assertEqual([item["path"] for item in restored], ["/Users/demo/Projects/Ice Garden"])
        self.assertTrue(all(len(item["sha"]) == 7 for item in restored))
        catalog = json.loads((ROOT / "assets/demo-catalog.json").read_text())
        paths = [archive["path"] for archive in catalog["archives"]]
        paths += [row["archiveLocation"] for row in rows]
        paths += [path for row in rows for path in row["checkouts"] + row["localFiles"]]
        self.assertTrue(all(path.startswith("/Users/demo/") for path in paths))
        self.assertTrue(all(row["destination"].startswith("https://example.com/") for row in rows))
        self.assertEqual(thaw["result"]["outcome"]["bytesFreed"], None)

    def test_demo_uses_one_frozen_set_for_the_wizard_freezer_and_project_list(self):
        catalog = json.loads((ROOT / "assets/demo-catalog.json").read_text())
        tasks = json.loads((ROOT / "assets/demo-tasks.json").read_text())
        source = (ROOT / "app/DemoCatalog.swift").read_text()
        thaw = tasks["demoWizardThaw"]
        self.assertNotIn("archives", thaw, "the Thaw wizard must read the Freezer archives of the catalog")
        self.assertIn('(snapshot().archives ?? []).filter { $0.state == "frozen" }', source)
        frozen = {archive["id"]: archive for archive in catalog["archives"] if archive["state"] == "frozen"}
        project_paths = {project["path"] for project in catalog["projects"]}
        frozen_paths = {path for archive in frozen.values() for path in [archive["path"]] + archive["paths"]}
        self.assertEqual(frozen_paths & project_paths, set(), "a Frozen project has no checkout in the project list")
        for plan in (thaw["review"], thaw["result"]):
            for row in plan["rows"]:
                if row.get("state") == "frozen":
                    self.assertIn(row["id"], frozen)
                    self.assertEqual(row["path"], frozen[row["id"]]["path"])
        backups = {row["id"] for row in catalog["backupStatus"]["archives"]}
        self.assertTrue(set(frozen).issubset(backups))

    def test_open_sheets_show_refusals_and_progress_themselves(self):
        main = (ROOT / "app/Igloo.swift").read_text()
        self.assertIn("store.error != nil && store.sheetErrorHosts.isEmpty", main)
        self.assertIn("store.error != nil && store.sheetErrorHosts.last == host", main)
        sheets = {"LifecycleViews.swift": "struct RemoteSetupView", "PreferencesViews.swift": "struct RemoteRepositoriesView",
                  "WizardViews.swift": "struct CleanupWizardView"}
        for name, start in sheets.items():
            with self.subTest(sheet=start):
                body = (ROOT / "app" / name).read_text().split(start, 1)[1].split("\nstruct ", 1)[0]
                self.assertIn(".sheetErrorAlert(store)", body)
                self.assertIn("SheetProgressRow(", body)
        remote = (ROOT / "app/LifecycleViews.swift").read_text().split("struct RemoteSetupView", 1)[1]
        self.assertIn("if !store.isDemo { if let onClose { onClose() } else { store.remoteSetup = nil } }", remote)
        self.assertIn("repositories-loading", capture.SHEET_VIEWS)

    def test_demo_result_fixtures_match_the_selected_action_and_space_labels(self):
        catalog = json.loads((ROOT / "assets/demo-catalog.json").read_text())
        tasks = json.loads((ROOT / "assets/demo-tasks.json").read_text())
        cleanup = catalog["demoResults"]["cleanup"]
        self.assertEqual(cleanup["observedSpace"]["restoredBytes"], 0)
        selected_freeze_paths = [row["path"] for row in tasks["demoWizardReview"]["rows"]
                                 if row["action"] == "freeze"]
        self.assertEqual(catalog["demoResults"]["wizard"]["checkoutsRemoved"]["paths"], selected_freeze_paths)
        hashes = []
        def collect_hashes(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key == "sha" and isinstance(item, str):
                        hashes.append(item)
                    else:
                        collect_hashes(item)
            elif isinstance(value, list):
                for item in value:
                    collect_hashes(item)
        collect_hashes(catalog)
        self.assertTrue(all(len(value) <= 12 for value in hashes))

    def test_demo_wizard_trash_and_restore_totals_match_the_reviewed_actions(self):
        catalog = json.loads((ROOT / "assets/demo-catalog.json").read_text())
        tasks = json.loads((ROOT / "assets/demo-tasks.json").read_text())
        source = (ROOT / "app/DemoCatalog.swift").read_text()
        cleanup_items = [item for row in tasks["demoWizardReview"]["rows"]
                         if row["status"] == "ready" and row["action"] == "cleanup"
                         for item in row["cleanup"]["items"]]
        result = catalog["demoResults"]["wizard"]
        self.assertEqual(result["movedToTrash"], len(cleanup_items))
        self.assertEqual(result["observedSpace"]["inTrashBytes"], sum(item["size"] for item in cleanup_items))
        self.assertEqual(result["observedSpace"]["restoredBytes"], 0)
        wizard_run = next(run for run in catalog["spaceReport"]["runs"] if run["kind"] == "wizard")
        self.assertEqual(wizard_run["observed"]["inTrashBytes"], result["observedSpace"]["inTrashBytes"])
        self.assertEqual(wizard_run["observed"]["restoredBytes"], 0)
        cleanup_case = source.split('case "cleanup-result":', 1)[1].split('case "wizard-result":', 1)[0]
        self.assertEqual(cleanup_case.count("ActionResult(path:"), catalog["demoResults"]["cleanup"]["movedToTrash"])

    def test_demo_space_report_totals_equal_the_rows_and_the_trash_history(self):
        catalog = json.loads((ROOT / "assets/demo-catalog.json").read_text())
        report = catalog["spaceReport"]
        fields = ("freedBytes", "inTrashBytes", "restoredBytes")
        for name in ("last30Days", "allTime"):
            with self.subTest(totals=name):
                totals = report["totals"][name]
                self.assertEqual(totals["estimateBytes"], sum(run["estimateBytes"] for run in report["runs"]))
                for field in fields:
                    self.assertEqual(totals["observed"][field], sum(run["observed"][field] for run in report["runs"]))
        for run in report["runs"]:
            with self.subTest(run=run["id"]):
                items = [item for item in catalog["history"] if item["runID"] == run["id"]]
                self.assertEqual(run["observed"]["inTrashBytes"],
                                 sum(item["size"] for item in items if item["status"] == "trashed"))
                self.assertEqual(run["observed"]["restoredBytes"],
                                 sum(item["size"] for item in items if item["status"] == "restored"))
        frozen = {archive["name"] for archive in catalog["archives"] if archive["state"] == "frozen"}
        for run in report["runs"]:
            if run["kind"] == "freeze":
                self.assertIn(run["label"], frozen)

    def test_demo_remote_status_and_branch_commits_agree_with_the_project(self):
        catalog = json.loads((ROOT / "assets/demo-catalog.json").read_text())
        for project in catalog["projects"]:
            with self.subTest(project=project["name"]):
                if not project["remotes"]:
                    self.assertEqual(project["remoteStatus"], "No remote")
                    self.assertTrue(any(blocker["code"] == "no-remote" for blocker in project["blockers"]))
                    self.assertTrue(all(branch["upstream"] == "" for branch in project["branches"]))
                for branch in project["branches"]:
                    self.assertRegex(branch["sha"], r"^[0-9a-f]{7,12}$")

    def test_updater_scenarios_are_captured_as_sheets(self):
        for scenario in capture.scenarios():
            if scenario["view"].startswith("update-"):
                with self.subTest(view=scenario["view"]):
                    self.assertIn(scenario["view"], capture.SHEET_VIEWS)

    def test_ready_update_view_is_in_the_swift_capture_allow_list(self):
        source = (ROOT / "app/DemoCatalog.swift").read_text()
        self.assertIn('static let updateViews = AppUpdater.demoFailureNames.map { "update-" + $0 } + ["update-markdown", "update-ready", "update-install-failed"]', source)
        self.assertIn('updateViews).union(extraSheetViews)).contains(view)', source)
        shadows_shared_views = 'let updateViews = AppUpdater.demoFailureNames.map { "update-" + $0 } + ["update-markdown"]' in source
        self.assertFalse(shadows_shared_views, "capture options must use the shared update view set")

    def test_thaw_review_copy_uses_project_noun_consistently(self):
        backend = (ROOT / "backend/planning.py").read_text()
        fixture = json.loads((ROOT / "assets/demo-tasks.json").read_text())
        self.assertTrue('"message": "Only ready projects will run. Blocked projects stay untouched."' in backend,
                        "backend thaw review copy must use project wording")
        self.assertEqual(fixture["demoWizardThaw"]["review"]["message"],
                         "Only ready projects will run. Blocked projects stay untouched.")

    def test_matrix_covers_required_states(self):
        scenarios = capture.scenarios()
        self.assertEqual({s["appearance"] for s in scenarios}, {"light", "dark"})
        self.assertTrue({"850x700", "1100x700", "1300x700", "1440x900"}.issubset({s["size"] for s in scenarios}))
        self.assertTrue({"overview", "git", "worktrees", "local-files", "cleanup", "plan", "freeze", "shelf"}.issubset({s["view"] for s in scenarios}))
        self.assertEqual({s["scope"] for s in scenarios}, {"shown", "hidden"})
        self.assertEqual({s["rail"] for s in scenarios}, {"compact", "full"})
        self.assertTrue({"normal", "long", "empty"}.issubset({s["data"] for s in scenarios}))
        self.assertEqual(len({s["name"] for s in scenarios}), len(scenarios))
        minimum = [row for row in scenarios if row["size"] == "850x700"]
        self.assertTrue(minimum)
        self.assertTrue(all((row["scope"], row["rail"]) == ("hidden", "compact") for row in minimum))
        self.assertEqual({capture.expected_size(row) for row in scenarios},
                         {(850, 793), (1100, 793), (1300, 793), (1440, 900)})

    def test_changed_preferences_view_selects_all_shared_user_preference_scenarios(self):
        selected = capture.scenarios_for_changed_paths(["app/PreferencesViews.swift"])
        self.assertTrue(selected)
        self.assertEqual({row["name"] for row in selected}, {row["name"] for row in capture.scenarios()})

    def test_shared_type_definition_maps_cover_every_user_view(self):
        shared_types = ("OverviewView", "CleanupView", "PlanView", "OutcomeSummaryView", "ReviewOutcome",
                        "HistoryView", "WizardPrimaryButtonStyle", "TasksView", "SpaceData", "IglooPalette",
                        "UserPreferences")
        sources = {path.as_posix(): path.read_text()
                   for path in (ROOT / "app").glob("*.swift")}
        matrix = capture.scenarios()
        declaration = re.compile(r"\b(?:struct|class|enum|protocol|typealias)\s+({})\b")
        for name in shared_types:
            with self.subTest(type=name):
                definitions = [path for path, source in sources.items()
                               if re.search(declaration.pattern.format(re.escape(name)), source)]
                self.assertEqual(len(definitions), 1, f"expected one declaration for {name}")
                definition = definitions[0]
                definition_scenarios = {row["name"] for row in
                                        capture.scenarios_for_changed_paths([definition], matrix)}
                users = [path for path, source in sources.items()
                         if path != definition and re.search(rf"\b{re.escape(name)}\b", source)]
                self.assertTrue(users, f"expected at least one user of {name}")
                for user in users:
                    user_scenarios = {row["name"] for row in capture.scenarios_for_changed_paths([user], matrix)}
                    self.assertTrue(user_scenarios <= definition_scenarios,
                                    f"{name} in {definition} affects views not covered for {user}")

    def test_unmapped_swift_file_selects_the_full_matrix(self):
        selected = capture.scenarios_for_changed_paths(["app/NewUnmappedView.swift"])
        self.assertEqual({row["name"] for row in selected}, {row["name"] for row in capture.scenarios()})

    def test_since_selection_unions_changed_views_with_explicit_scenarios(self):
        matrix = capture.scenarios()
        explicit = next(row["name"] for row in matrix if row["view"] == "overview")
        resolved = subprocess.CompletedProcess(["git", "rev-parse"], 0, stdout="deadbeef\n", stderr="")
        empty = subprocess.CompletedProcess(["git"], 0, stdout="", stderr="")
        diff = subprocess.CompletedProcess(["git", "diff"], 0, stdout="app/Updates.swift\n", stderr="")
        with patch.object(capture.subprocess, "run", side_effect=[resolved, diff, empty]) as run:
            selected = capture.select_scenarios(matrix, explicit=[explicit], since="main")
        self.assertEqual({row["view"] for row in selected}, {"overview"} | {row["view"] for row in matrix
                                                                            if row["view"].startswith("update-")})
        self.assertEqual(run.call_count, 3)
        self.assertEqual(run.call_args.kwargs["check"], True)
        self.assertEqual(run.call_args_list[0].args[0],
                         ["git", "rev-parse", "--verify", "--end-of-options", "main^{commit}"])
        self.assertEqual(run.call_args_list[1].args[0],
                         ["git", "diff", "--name-only", "deadbeef", "--", "app"])

    def test_since_selection_returns_no_scenarios_for_non_view_changes(self):
        resolved = subprocess.CompletedProcess(["git", "rev-parse"], 0, stdout="deadbeef\n", stderr="")
        empty = subprocess.CompletedProcess(["git"], 0, stdout="", stderr="")
        with patch.object(capture.subprocess, "run", side_effect=[resolved, empty, empty]):
            selected = capture.select_scenarios(capture.scenarios(), since="main")
        self.assertEqual(selected, [])

    def test_since_rejects_an_unverified_revision_before_diff(self):
        error = subprocess.CalledProcessError(128, ["git", "rev-parse"], stderr="bad revision")
        with patch.object(capture.subprocess, "run", side_effect=error) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                capture.changed_app_paths("--output")
        run.assert_called_once_with(["git", "rev-parse", "--verify", "--end-of-options",
                                     "--output^{commit}"], cwd=capture.ROOT, check=True,
                                    capture_output=True, text=True)

    def test_thaw_interactive_scenario_is_in_both_appearances(self):
        flow_rows = [row for row in capture.scenarios() if row.get("flow") == "wizard-thaw"]
        self.assertEqual({row["appearance"] for row in flow_rows}, {"light", "dark"})
        self.assertTrue(all(row["view"] == "wizard-start" for row in flow_rows))

    def test_interactive_thaw_steps_are_declarative_and_parseable(self):
        steps = capture.parse_interactive_steps(capture.INTERACTIVE_FLOWS["wizard-thaw"])
        self.assertEqual([step.get("key", step.get("click", {}).get("role")) for step in steps], [
            "button", "checkbox", "button", "checkbox", "button",
        ])
        self.assertTrue(all(step["click"].get("anchor") and step["click"].get("name") for step in steps))
        self.assertEqual(steps[3]["capture"], "05-confirmed")
        self.assertEqual(steps[1]["click"]["name"], "Ice Garden")
        self.assertEqual(steps[-1]["wait_for"], {"anchor": "Local-only files restored"})
        with self.assertRaisesRegex(ValueError, "anchor"):
            capture.parse_interactive_steps([{"click": {"role": "button", "name": "Back", "ordinal": 1},
                                             "capture": "bad"}])
        with self.assertRaisesRegex(ValueError, "ordinal"):
            capture.parse_interactive_steps([{"click": {"role": "button", "name": "Back", "ordinal": 0,
                                                        "anchor": "Choose a path"}, "capture": "bad"}])
        with self.assertRaisesRegex(ValueError, "name or label"):
            capture.parse_interactive_steps([{"click": {"role": "button", "ordinal": 1, "anchor": "Choose a path"},
                                             "capture": "bad"}])
        with self.assertRaisesRegex(ValueError, "label"):
            capture.parse_interactive_steps([{"click": {"role": "outline row", "ordinal": 1,
                                                          "anchor": "Thaw plan", "label": " "},
                                             "capture": "bad"}])
        with self.assertRaisesRegex(ValueError, "role, ordinal, and anchor"):
            capture.parse_interactive_steps([{"click": {"role": "checkbox", "name": "Ice Garden", "ordinal": 1,
                                                        "anchor": "Select projects",
                                                        "parent": {"role": "outline row", "ordinal": 1}},
                                             "capture": "bad"}])
        with self.assertRaisesRegex(ValueError, "selection"):
            capture.parse_interactive_steps([{"key": "Down", "capture": "bad"}])
        with self.assertRaisesRegex(ValueError, "key"):
            capture.parse_interactive_steps([{"key": "Space", "capture": "bad",
                                               "wait_for_selection": {"role": "outline row", "ordinal": 1,
                                                                      "anchor": "Thaw plan", "label": "Signal Lab"}}])

    def test_process_stop_requires_the_target_bundle_executable(self):
        with tempfile.TemporaryDirectory() as temp:
            app = Path(temp) / "Igloo.app"
            executable = app / "Contents/MacOS/Igloo"
            executable.parent.mkdir(parents=True)
            executable.touch()
            other_executable = Path(temp) / "Other.app/Contents/MacOS/Igloo"
            other_executable.parent.mkdir(parents=True)
            other_executable.touch()
            with patch.object(capture, "process_executable_path", return_value=other_executable), \
                 patch.object(capture.os, "kill") as kill:
                with self.assertRaisesRegex(ValueError, "executable path"):
                    capture.stop_verified_process(123, app)
                kill.assert_called_once_with(123, 0)

    def test_probe_option_is_documented_by_the_capture_cli(self):
        output = io.StringIO()
        with patch.object(capture.sys, "argv", ["capture_states.py", "--help"]), \
             contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            capture.main()
        self.assertEqual(raised.exception.code, 0)
        self.assertIn("--probe", output.getvalue())

    def test_review_and_settings_matrix_includes_minimum_and_default_in_both_appearances(self):
        views = {"freeze-review", "preserve-review", "freeze-result", "cleanup-review", "cleanup-result",
                 "trash-history", "wizard-review", "wizard-result", "task-review", "task-history",
                 "settings", "hold", "freeze", "shelf"}
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
            write_renderer_png(folder, "window", sample_png())
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
            write_renderer_png(folder, "window", sample_png(uniform=True))
            write_renderer_png(folder, "sheet", sample_png())
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
                    if row["view"].startswith("update-") and row["view"] not in {"update-markdown", "update-ready", "update-install-failed"}]
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

    def test_install_failed_update_sheet_covers_minimum_and_default_sizes(self):
        captures = [row for row in capture.scenarios() if row["view"] == "update-install-failed"]
        self.assertEqual({(row["appearance"], row["size"]) for row in captures},
                         {(appearance, size) for appearance in ("light", "dark")
                          for size in ("850x700", "1440x900")})

    def test_ready_update_sheet_covers_light_and_dark(self):
        captures = [row for row in capture.scenarios() if row["view"] == "update-ready"]
        self.assertEqual({(row["appearance"], row["size"]) for row in captures},
                         {(appearance, "1440x900") for appearance in ("light", "dark")})

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
                    "wizard-start": "Project wizard", "wizard-thaw-selection": "Create Thaw plan",
                    "wizard-thaw-review": "Thaw plan", "wizard-thaw-result": "Checkouts restored",
                    "wizard-thaw-rehydrating": "Thawing Ice Garden",
                    "task-review": "Review task actions", "settings": "Reminder threshold",
                    "freeze-review": "Expected outcome", "freeze-hold": "This project is on Hold",
                    "cleanup-result": "In Trash (can be restored)", "trash-history": "Space report",
                    "push-review": "Push one branch", "wizard-actions": "Choose actions",
                    "wizard-result": "In Trash (can be restored)", "task-history": "Task run history"}
        for view, label in expected.items():
            with self.subTest(view=view):
                self.assertIn(label, capture.accessibility_missing({"role": "AXSheet"}, {"view": view}))

    def test_thaw_blocker_capture_contracts_include_short_reasons_and_action_buttons(self):
        expected = {
            "wizard-thaw-occupied": ["Thaw plan", "Path occupied", "Show path in Finder"],
            "wizard-thaw-remote": ["Thaw plan", "Remote unavailable", "Check remote again"],
            "wizard-thaw-corrupt": ["Thaw plan", "Archive needs attention", "Open Freezer"],
            "wizard-thaw-not-frozen": ["Thaw plan", "No longer Frozen", "Open Freezer"],
        }
        empty_tree = {"role": "AXSheet"}
        for view, labels in expected.items():
            with self.subTest(view=view):
                missing = capture.accessibility_missing(empty_tree, {"view": view})
                self.assertEqual(missing, labels)

    def test_thaw_wizard_steps_and_blockers_have_minimum_and_default_captures(self):
        views = {"wizard-start", "wizard-thaw-selection", "wizard-thaw-review", "wizard-thaw-ready", "wizard-thaw-result",
                 "wizard-thaw-occupied", "wizard-thaw-remote", "wizard-thaw-corrupt", "wizard-thaw-not-frozen",
                 "wizard-thaw-rehydrating"}
        captures = [row for row in capture.scenarios() if row["view"] in views]
        self.assertEqual({row["view"] for row in captures}, views)
        self.assertTrue({(view, appearance, size) for view in views for appearance in ("light", "dark")
                         for size in ("850x700", "1440x900")}.issubset(
                             {(row["view"], row["appearance"], row["size"]) for row in captures}))

    def test_thaw_ready_capture_covers_unchecked_confirmation_in_both_appearances(self):
        captures = [row for row in capture.scenarios() if row["view"] == "wizard-thaw-ready"]
        self.assertEqual({(row["appearance"], row["size"]) for row in captures},
                         {(appearance, size) for appearance in ("light", "dark")
                          for size in ("850x700", "1440x900")})
        source = (ROOT / "app/DemoCatalog.swift").read_text()
        wizard = (ROOT / "app/WizardViews.swift").read_text()
        self.assertIn('"wizard-thaw-ready"', source)
        self.assertIn('"wizard-thaw-ready": "demo-icegarden"', wizard)

    def test_thaw_ready_capture_contract_checks_checkbox_and_disabled_button(self):
        def control(identifier, role, *, enabled, value=None):
            node = {"identifier": identifier, "role": role, "label": identifier, "enabled": enabled,
                    "frame": {"x": 1, "y": 2, "width": 40, "height": 20}}
            if value is not None:
                node["value"] = value
            return node

        tree = {"role": "AXCapture", "children": [
            {"label": "Thaw projects"}, {"label": "Only ready projects will run"},
            {"label": "Ice Garden"}, {"label": "Thaw 1 ready project"},
            control("wizard.thaw.acceptance", "AXCheckBox", enabled=True, value=False),
            control("wizard.thaw.execute", "AXButton", enabled=False),
        ]}
        tree["children"][-2]["label"] = "I reviewed the remote refs, original paths and local-only files shown for Ice Garden."
        self.assertEqual(capture.accessibility_missing(tree, {"view": "wizard-thaw-ready"}), [])
        self.assertEqual(capture.accessibility_control_issues(tree, {"view": "wizard-thaw-ready"}), {
            "wizard.thaw.acceptance": "found", "wizard.thaw.execute": "found"})
        tree["children"][-1]["enabled"] = True
        self.assertEqual(capture.accessibility_control_issues(tree, {"view": "wizard-thaw-ready"})[
            "wizard.thaw.execute"], "missing")
        tree["children"][-1]["enabled"] = False
        tree["children"][-2]["value"] = True
        self.assertEqual(capture.accessibility_control_issues(tree, {"view": "wizard-thaw-ready"})[
            "wizard.thaw.acceptance"], "missing")

    def test_demo_capture_activates_and_keys_its_window_before_capture(self):
        source = (ROOT / "app/DemoCatalog.swift").read_text()
        capture_start = source.split("static func start(store:", 1)[1].split("private static func windowMetadata", 1)[0]
        self.assertIn("NSApp.activate(ignoringOtherApps: true)", capture_start)
        self.assertIn("window.makeKeyAndOrderFront(nil)", capture_start)
        self.assertLess(capture_start.index("window.makeKeyAndOrderFront(nil)"), capture_start.index("view.layoutSubtreeIfNeeded()"))
        self.assertIn("NSApp.isActive && (target.isKeyWindow || window.isKeyWindow || target.attachedSheet?.isKeyWindow == true)", capture_start)
        self.assertLess(capture_start.index("NSApp.isActive && (target.isKeyWindow || window.isKeyWindow || target.attachedSheet?.isKeyWindow == true)"),
                        capture_start.index("view.layoutSubtreeIfNeeded()"))

    def test_update_sheet_focuses_the_default_action_and_skips_the_release_link(self):
        source = (ROOT / "app/Updates.swift").read_text()
        view = source.split("struct UpdatesView: View {", 1)[1].split("private struct ReleaseNotesView", 1)[0]
        self.assertIn("@FocusState private var defaultActionFocused: Bool", view)
        self.assertIn(".focusable(false)", view)
        self.assertEqual(view.count(".defaultFocus($defaultActionFocused, true)"), 2)

    def test_capture_review_documents_matrix_geometry_and_measured_contrast(self):
        guide = (ROOT / "docs/development.md").read_text()
        checklist = (ROOT / "docs/capture-review.md").read_text()
        self.assertIn("`850x700` request captures at 850 × 793", guide)
        self.assertIn("`1100x700` request captures at 1100 × 793", guide)
        self.assertIn("`1300x700` request captures at 1300 × 793", guide)
        self.assertIn("`1440x900` request captures at 1440 × 900", guide)
        self.assertIn("2.29:1", checklist)
        self.assertIn("3.02:1", checklist)
        self.assertIn("5.22:1", checklist)
        self.assertIn("7.67:1", checklist)
        self.assertIn("1.77:1", checklist)
        self.assertIn("1.00:1", checklist)
        self.assertIn("10.22:1", checklist)
        self.assertIn("4.13:1", checklist)
        self.assertIn("3.65:1", checklist)
        self.assertIn("Inactive parent window", checklist)
        self.assertIn("inactive user-interface components", checklist)
        self.assertIn("WCAG relative-luminance formula", checklist)

    def test_issue_50_review_panes_freezer_tables_and_disabled_actions_have_explanations(self):
        lifecycle = (ROOT / "app/LifecycleViews.swift").read_text()
        review = lifecycle.split("} else if let review = plan ?? captureReview {", 1)[1].split(
            '} else {\n                Picker("Action"', 1)[0]
        self.assertIn("alignment: .topLeading", lifecycle)
        self.assertIn("reviewRows(review.localFiles", review)
        self.assertIn("reviewRows(review.excluded", review)
        self.assertIn("reviewRows(review.checkouts.map", review)
        freezer = lifecycle.split("struct FrozenProjectsView", 1)[1]
        table = freezer.split("@ViewBuilder private func archiveTable", 1)[1].split(
            "@ViewBuilder private func backupCell", 1)[0]
        self.assertIn("if archives.isEmpty", table)
        self.assertNotIn(".overlay", table)
        self.assertIn('TableColumn("Backup") { archive in', table)
        self.assertIn(".frame(height: archiveTableHeight(archives))", table)
        self.assertIn("private func archiveTableHeight(_ archives: [FrozenArchive])", freezer)
        self.assertIn("archiveRowHeight(for: archive)", freezer)
        self.assertIn("}.width(min: 150)", table)
        backup_cell = freezer.split("@ViewBuilder private func backupCell", 1)[1].split(
            "private func emptyArchiveState", 1)[0]
        self.assertIn(".lineLimit(2)", backup_cell)
        self.assertIn(".help(([description, reason]", backup_cell)
        self.assertIn(".frame(height: orphanRowsHeight(backup.orphans))", freezer)
        self.assertIn("private func orphanRowsHeight(_ orphans: [FreezerBackupRow])", freezer)
        self.assertIn(".frame(height: reviewHistoryPaneHeight(review), alignment: .top)", review)
        self.assertIn(".frame(height: reviewOutcomeHeight(review.outcome), alignment: .top)", review)
        self.assertIn("private func reviewHistoryPaneHeight(_ review: PreservationReview)", lifecycle)
        self.assertIn("private func reviewPaneHeight(_ review: PreservationReview)", lifecycle)
        self.assertIn("private func reviewOutcomeHeight(_ outcome: ReviewOutcome)", lifecycle)
        for control in ("shelf.removeBackupCopy", "shelf.backUpNow", "shelf.verifyBackup", "shelf.showArchive",
                        "shelf.freeze", "shelf.offlineRecovery", "shelf.thawRemote"):
            with self.subTest(control=control):
                position = freezer.index(f'"{control}"')
                self.assertIn(".help(", freezer[max(0, position - 500):position])

    def test_issue56_compact_review_and_history_layouts_budget_real_rows(self):
        cleanup = (ROOT / "app/ReviewViews.swift").read_text()
        review = cleanup.split("struct CleanupReview: View {", 1)[1].split("struct PushReview: View {", 1)[0]
        self.assertIn("}.frame(maxHeight: 96)", review)

        detail = (ROOT / "app/DetailViews.swift").read_text()
        saved = detail.split("struct SavedHistoryView: View {", 1)[1].split("struct RemotesView: View {", 1)[0]
        self.assertIn("compactTableHeight(rows.count, rowHeight: 38)", saved)

        trash = cleanup.split("struct HistoryView: View {", 1)[1].split("func loadSpaceReport()", 1)[0]
        self.assertIn("compactTableHeight(history.count, rowHeight: 32, maxRows: 4, maxHeight: 170)", trash)

        tasks = (ROOT / "app/TasksView.swift").read_text()
        table = tasks.split("struct TaskRunHistoryTable: View {", 1)[1].split("struct TaskHistoryCaptureView: View {", 1)[0]
        self.assertIn("private var compact: Bool { sheetWidth(980) < 900 }", table)
        self.assertIn("ideal: compact ? 300 : 340", table)
        self.assertIn("alternatingRowBackgrounds(.disabled)", table)
        self.assertIn("lineLimit(4).fixedSize(horizontal: false, vertical: true)", table)
        self.assertIn("rowHeight: 84", table)

        lifecycle = (ROOT / "app/LifecycleViews.swift").read_text()
        freezer = lifecycle.split("struct FrozenProjectsView", 1)[1]
        self.assertIn("min(compact ? 112 : 210, 36 + rowsHeight)", freezer)
        self.assertIn("return 36", freezer)
        self.assertIn("return 44", freezer)
        self.assertIn("Thaw restores the Frozen project. Its Freezer copy stays available", freezer)
        self.assertIn("fixedSize(horizontal: false, vertical: true)", freezer)

        wizard = (ROOT / "app/WizardViews.swift").read_text()
        start = wizard.split("var startStep: some View {", 1)[1].split("func enterThaw()", 1)[0]
        self.assertIn('Text("Review cleanup and Freeze suggestions.")', start)
        self.assertIn('Text("Restore multiple Frozen projects.")', start)
        self.assertEqual(start.count(".fixedSize(horizontal: false, vertical: true).padding(16)"), 2)

    def test_issue17_glossary_avoided_phrases_are_absent_from_copy(self):
        paths = [ROOT / "app", ROOT / "docs/guide.md", ROOT / "README.md",
                 ROOT / "assets/demo-catalog.json", ROOT / "assets/demo-tasks.json"]
        contents = []
        for path in paths:
            if path.is_file():
                contents.append(path.read_text())
            else:
                contents.extend(file.read_text() for file in path.rglob("*.swift"))
        text = "\n".join(contents)
        self.assertNotRegex(text, r"(?i)\barchive\s*&\s*push\b")
        self.assertNotRegex(text, r"(?i)\barchive\s+folder\b")
        self.assertNotRegex(text, r'(?i)Button\("archive"\)')
        self.assertNotIn("archive important ignored files", text)
        self.assertNotIn("Freeze archives local files", text)
        self.assertIn("preserve important ignored files", text)
        self.assertIn("Freeze preserves local files and Git metadata", text)

    def test_overview_summary_and_list_badge_read_one_blocker_source(self):
        model = (ROOT / "app/Igloo.swift").read_text()
        detail = (ROOT / "app/DetailViews.swift").read_text()
        summary = model[model.index("var overviewSummary"):model.index("struct Project: Decodable")]
        self.assertIn("let open = blockers ?? []", summary)
        self.assertIn('"Blocked: " + first.shortLabel', summary)
        self.assertLess(summary.index("open.first"), summary.index('"Ready for a closer review"'))
        self.assertIn("ProjectBlockerBadges(blockers: project.blockers ?? []", model)
        self.assertIn("project.overviewSummary.title", detail)
        self.assertNotIn("no immediate preservation flags", detail)

    def test_issue_50_warning_and_selected_row_colors_use_contrasting_source_styles(self):
        navigation = (ROOT / "app/NavigationViews.swift").read_text()
        wizard = (ROOT / "app/WizardViews.swift").read_text()
        self.assertIn("Color(red: 0.55, green: 0.23, blue: 0.00)", navigation)
        self.assertIn(".foregroundStyle(blockerColor)", navigation)
        self.assertIn("Color(red: 0.55, green: 0.23, blue: 0.00)", wizard)
        self.assertIn("@Environment(\\.backgroundProminence) private var backgroundProminence", navigation)
        self.assertIn("selected && backgroundProminence == .increased", navigation)
        self.assertIn("@Environment(\\.controlActiveState) private var controlActiveState", wizard)
        self.assertIn("controlActiveState == .key ? Color(nsColor: .selectedMenuItemTextColor) : Color.primary", wizard)
        self.assertIn("row.id == selectedThawID ? selectedRowTextColor", wizard)
        self.assertIn("Color(nsColor: .selectedMenuItemTextColor)", navigation)

    def test_thaw_wizard_capture_contract_uses_shared_height_and_scrollable_list(self):
        source = (ROOT / "app/WizardViews.swift").read_text()
        height = source.split("private var wizardSheetHeight: CGFloat {", 1)[1].split("private var stepTitle", 1)[0]
        self.assertIn('if mode == "thaw"', height)
        self.assertNotIn("thawPhase ==", height)
        self.assertIn(".scrollIndicators(.visible)", source)
        review = source.split("var thawReviewStep: some View {", 1)[1].split("var thawRowsAndDetails", 1)[0]
        self.assertNotIn('Text("Thaw plan")', review)
        self.assertEqual(review.count("Blocked projects and their Freezer archives remain untouched."), 0)

    def test_thaw_review_keeps_refs_and_freezer_location_in_one_detail_pane(self):
        source = (ROOT / "app/WizardViews.swift").read_text()
        review = source.split("var thawReviewStep: some View {", 1)[1].split("var thawRowsAndDetails", 1)[0]
        details = source.split("@ViewBuilder func thawDetails(", 1)[1].split("func shortThawReason", 1)[0]
        self.assertNotIn("ForEach(row.refs)", review)
        self.assertNotIn("row.archiveLocation", review)
        self.assertEqual(details.count("ForEach(row.refs)"), 1)
        self.assertIn('Text("Freezer archive")', details)
        self.assertIn('Text(thawPlan?.message ?? "")', review)
        self.assertNotIn("Blocked projects and their Freezer archives remain untouched.", review)

    def test_disabled_wizard_buttons_and_thaw_progress_keep_visible_contrast(self):
        source = (ROOT / "app/WizardViews.swift").read_text()
        button = source.split("struct WizardPrimaryButtonStyle", 1)[1].split("struct CleanupWizardView", 1)[0]
        self.assertIn("temperature?.actionLabel(for: colorScheme) ?? Color.white", button)
        self.assertIn("temperature?.foreground(for: colorScheme) ?? Color.accentColor", button)
        self.assertIn(".foregroundStyle(isEnabled ? label : Color.primary.opacity(0.28))", button)
        self.assertIn(".background(isEnabled ? fill : Color.primary.opacity(0.07)", button)
        progress = source.split("if isThawExecuting {", 1)[1].split("} else if thawPlan?.status", 1)[0]
        self.assertIn("Text(thawProgressMessage)", progress)
        self.assertIn("ProgressView().progressViewStyle(.linear)", progress)
        self.assertIn(".tint(TemperatureAccent.warm.foreground(for: colorScheme))", progress)
        self.assertIn(".symbolEffect(.variableColor.iterative", progress)

    def test_result_sheets_use_sized_rows_and_reduced_result_frames(self):
        cleanup = (ROOT / "app/ReviewViews.swift").read_text()
        wizard = (ROOT / "app/WizardViews.swift").read_text()
        lifecycle = (ROOT / "app/LifecycleViews.swift").read_text()
        self.assertIn('item.size.map { " · " + bytes($0) }', cleanup)
        self.assertIn('item.size.map { " · " + bytes($0) }', wizard)
        self.assertIn("height: resultHeight", cleanup)
        self.assertIn("if isShowingResult { return 390 }", lifecycle)
        self.assertIn("reviewSheetChromeHeight + reviewPaneHeight(review) + reviewOutcomeHeight(review.outcome)", lifecycle)
        self.assertIn("Demo only: these results are simulated.", lifecycle)

    def test_issue_56_brand_art_and_motion_are_lifecycle_scoped(self):
        palette = (ROOT / "app/SpaceViews.swift").read_text()
        artwork = (ROOT / "app/ReviewViews.swift").read_text()
        lifecycle = (ROOT / "app/LifecycleViews.swift").read_text()
        wizard = (ROOT / "app/WizardViews.swift").read_text()
        self.assertIn("0x2B6E9E", palette)
        self.assertIn("0x8FC9EA", palette)
        self.assertIn("0x756118", palette)
        self.assertIn("0xE6D28B", palette)
        self.assertIn('colorScheme == .dark ? "-night" : ""', artwork)
        self.assertIn(".mask(IglooDomeMask())", artwork)
        self.assertIn(".mask(IglooDoorGlowMask())", artwork)
        self.assertIn(".onChange(of: reduceMotion) { if reduceMotion { showFinalFrame() } }", artwork)
        self.assertIn("complete = result.outcome.observed && result.outcome.checkoutsRemoved.count > 0", lifecycle)
        self.assertIn("TemperatureStateLabel(title: \"Frozen\", accent: .cold)", lifecycle)
        self.assertIn("plan.rows.allSatisfy { $0.status == \"done\" }", wizard)
        self.assertIn(".symbolEffectsRemoved(reduceMotion)", wizard)

    def test_outcome_summary_avoids_duplicate_measurements_and_labels_each_ref(self):
        source = (ROOT / "app/ReviewViews.swift").read_text()
        self.assertIn("return bytes(freed) == bytes(outcome.bytesFreedEstimate)", source)
        self.assertIn('"Ref pushed: "', source)
        self.assertIn("spaceExplanation", source)
        self.assertIn('"Total outcome for both actions"', (ROOT / "app/WizardViews.swift").read_text())

    def test_named_controls_require_identifiers_enabled_state_and_frame(self):
        requirements = {
            "plan": ["detail.connectRemote", "detail.commitSelectedFiles", "detail.preserve",
                      "detail.freeze", "detail.hold", "detail.preservationNotes",
                      "detail.exportProjectReport", "detail.savePreservation"],
            "freeze": ["freeze.cancel", "freeze.buildReview"],
            "shelf": ["shelf.backUpNow", "shelf.verifyBackup", "shelf.done", "shelf.showArchive",
                      "shelf.freeze", "shelf.offlineRecovery", "shelf.thawRemote"],
            "wizard-start": ["wizard.start.makeSpace", "wizard.start.thaw"],
            "wizard-thaw-selection": ["wizard.thaw.back", "wizard.thaw.prepare", "wizard.thaw.archive.demo-icegarden"],
            "wizard-thaw-review": ["wizard.thaw.acceptance", "wizard.thaw.execute"],
            "wizard-thaw-occupied": ["wizard.thaw.showPath"],
            "wizard-thaw-remote": ["wizard.thaw.checkRemote"],
            "wizard-thaw-corrupt": ["wizard.thaw.openFreezer"],
            "wizard-thaw-not-frozen": ["wizard.thaw.openFreezer"],
            "wizard-thaw-result": ["wizard.thaw.newPlan"],
        }
        for view, identifiers in requirements.items():
            with self.subTest(view=view):
                tree = {"role": "AXCapture", "children": [{
                    "role": "AXWindow", "windowKind": "sheet" if view != "plan" else "window",
                    "children": [{"role": {"detail.hold": "AXCheckBox",
                                             "wizard.thaw.acceptance": "AXCheckBox",
                                             "wizard.thaw.archive.demo-icegarden": "AXCheckBox",
                                             "detail.preservationNotes": "AXTextArea"}.get(identifier, "AXButton"),
                                  "identifier": identifier, "label": identifier,
                                  "enabled": True,
                                  "frame": {"x": 1, "y": 2, "width": 40, "height": 20}}
                                 for identifier in identifiers]
                }]}
                statuses = capture.accessibility_control_issues(tree, {"view": view})
                self.assertEqual(statuses, {identifier: "found" for identifier in identifiers})
                node = tree["children"][0]["children"][-1]
                node["identifier"] = "wrong.identifier"
                self.assertEqual(capture.accessibility_control_issues(tree, {"view": view})[identifiers[-1]], "missing")
                node["identifier"] = identifiers[-1]
                node["role"] = "AXTextField"
                self.assertEqual(capture.accessibility_control_issues(tree, {"view": view})[identifiers[-1]], "missing")
                node["role"] = "AXButton"
                node["enabled"] = None
                self.assertEqual(capture.accessibility_control_issues(tree, {"view": view})[identifiers[-1]], "missing")
                node["enabled"] = True
                del node["frame"]
                self.assertEqual(capture.accessibility_control_issues(tree, {"view": view})[identifiers[-1]], "missing")
        self.assertIsNone(capture.accessibility_control_issues({"children": []}, {"view": "overview"}))

    def test_cua_wizard_start_controls_are_found_in_the_main_window_tree(self):
        tree_markdown = "\n".join((
            "- [110] AXButton (Make space, Review cleanup and Freeze suggestions.) [id=wizard.start.makeSpace actions=[press]]",
            "- [111] AXButton (Thaw projects, Restore multiple Frozen projects.) [id=wizard.start.thaw actions=[press]]",
            '- [112] AXButton (Close) [help="Close the Project wizard." actions=[press]]',
        ))
        payload = {"tree_markdown": tree_markdown, "truncated": False, "total_element_count": 3}
        statuses = capture.accessibility_control_issues(
            tree_markdown, {"view": "wizard-start"}, source="cua",
            complete=capture.cua_accessibility_tree_complete(payload))
        self.assertEqual(statuses, {"wizard.start.makeSpace": "found", "wizard.start.thaw": "found"})
        self.assertTrue(capture.cua_accessibility_tree_complete(payload))
        self.assertIn("found: wizard.start.makeSpace, wizard.start.thaw",
                      capture.format_accessibility_control_statuses(statuses))

    def test_cua_control_absent_from_complete_tree_is_missing(self):
        tree_markdown = "- [110] AXButton (Make space) [id=wizard.start.makeSpace actions=[press]]"
        payload = {"tree_markdown": tree_markdown, "truncated": False, "total_element_count": 1}
        statuses = capture.accessibility_control_issues(
            tree_markdown, {"view": "wizard-start"}, source="cua",
            complete=capture.cua_accessibility_tree_complete(payload))
        self.assertEqual(statuses["wizard.start.makeSpace"], "found")
        self.assertEqual(statuses["wizard.start.thaw"], "missing")

    def test_cua_control_absent_from_truncated_tree_is_unverified(self):
        tree_markdown = "- [110] AXButton (Make space) [id=wizard.start.makeSpace actions=[press]]"
        payload = {"tree_markdown": tree_markdown, "truncated": True, "total_element_count": 2}
        statuses = capture.accessibility_control_issues(
            tree_markdown, {"view": "wizard-start"}, source="cua",
            complete=capture.cua_accessibility_tree_complete(payload))
        self.assertEqual(statuses["wizard.start.makeSpace"], "found")
        self.assertEqual(statuses["wizard.start.thaw"], "unverified (tree incomplete)")
        self.assertIn("unverified: wizard.start.thaw (tree incomplete)",
                      capture.format_accessibility_control_statuses(statuses))
        self.assertFalse(capture.cua_accessibility_tree_complete({
            "tree_markdown": tree_markdown, "truncated": False, "total_element_count": 2}))

    def test_cua_control_with_wrong_role_is_missing(self):
        tree_markdown = "- [110] AXCheckBox (Make space) [id=wizard.start.makeSpace actions=[press]]"
        statuses = capture.accessibility_control_issues(
            tree_markdown, {"view": "wizard-start"}, source="cua", complete=True)
        self.assertEqual(statuses["wizard.start.makeSpace"], "missing")
        self.assertEqual(statuses["wizard.start.thaw"], "missing")

    def test_cua_disabled_controls_without_ids_are_unverified_with_a_reason(self):
        freeze_tree = "\n".join((
            "- [113] AXButton (Cancel) [id=freeze.cancel actions=[press]]",
            "- AXButton (Build review…)"))
        freeze_statuses = capture.accessibility_control_issues(
            freeze_tree, {"view": "freeze"}, source="cua", complete=True)
        self.assertEqual(freeze_statuses["freeze.cancel"], "found")
        self.assertEqual(freeze_statuses["freeze.buildReview"],
                         "unverified (disabled, id not exposed by cua-driver)")
        self.assertIn("unverified: freeze.buildReview (disabled, id not exposed by cua-driver)",
                      capture.format_accessibility_control_statuses(freeze_statuses))

        shelf_tree = "\n".join((
            "- [136] AXButton (Done) [id=shelf.done actions=[press]]",
            "- AXButton (Back up now)"))
        shelf_statuses = capture.accessibility_control_issues(
            shelf_tree, {"view": "shelf"}, source="cua", complete=True)
        self.assertEqual(shelf_statuses["shelf.done"], "found")
        self.assertEqual(shelf_statuses["shelf.backUpNow"],
                         "unverified (disabled, id not exposed by cua-driver)")
        formatted = capture.format_accessibility_control_statuses(shelf_statuses)
        self.assertTrue(formatted.startswith("found: shelf.done; missing:"))
        self.assertIn("unverified: shelf.backUpNow (disabled, id not exposed by cua-driver)", formatted)

    def test_cua_disabled_inference_requires_the_expected_label(self):
        tree_markdown = "- AXButton (Build review unavailable)"
        statuses = capture.accessibility_control_issues(
            tree_markdown, {"view": "freeze"}, source="cua", complete=True)
        self.assertEqual(statuses["freeze.buildReview"], "missing")

    def test_cua_expected_identifier_with_wrong_role_stays_missing(self):
        tree_markdown = "- [114] AXTextField (Build review…) [id=freeze.buildReview]"
        statuses = capture.accessibility_control_issues(
            tree_markdown, {"view": "freeze"}, source="cua", complete=True)
        self.assertEqual(statuses["freeze.buildReview"], "missing")

    def test_cua_plan_contract_reads_detail_controls_from_main_window_tree(self):
        tree_markdown = "\n".join((
            "- [40] AXButton (Connect remote) [id=detail.connectRemote actions=[press]]",
            "- [41] AXButton (Commit selected files) [id=detail.commitSelectedFiles actions=[press]]",
            "- [42] AXButton (Preserve) [id=detail.preserve actions=[press]]",
            "- [43] AXButton (Freeze) [id=detail.freeze actions=[press]]",
            "- [44] AXCheckBox (Hold) [id=detail.hold actions=[press]]",
            "- [45] AXTextArea (Preservation notes) [id=detail.preservationNotes]",
            "- [46] AXButton (Export project report) [id=detail.exportProjectReport actions=[press]]",
            "- [47] AXButton (Save preservation) [id=detail.savePreservation actions=[press]]",
        ))
        statuses = capture.accessibility_control_issues(
            tree_markdown, {"view": "plan"}, source="cua", complete=True)
        self.assertEqual(set(statuses.values()), {"found"})
        self.assertEqual(set(statuses), {
            identifier for identifier, _, _ in capture.accessibility_control_contract({"view": "plan"})})

    def test_cua_control_capture_requests_the_main_window_tree_for_sheet_views(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            tree_markdown = "\n".join((
                "- [110] AXButton (Make space, Review cleanup and Freeze suggestions.) [id=wizard.start.makeSpace actions=[press]]",
                "- [111] AXButton (Thaw projects, Restore multiple Frozen projects.) [id=wizard.start.thaw actions=[press]]",
                '- [112] AXButton (Close) [help="Close the Project wizard." actions=[press]]',
            ))
            with patch.object(capture, "cua_window_targets", return_value={"window": 123, "sheet": 124}), \
                 patch.object(capture, "cua_call", return_value={
                     "tree_markdown": tree_markdown, "truncated": False, "total_element_count": 3}) as call_driver:
                stored = capture.capture_cua_accessibility_tree(42, folder, {"view": "wizard-start"}, "session")
            tool, arguments = call_driver.call_args.args
            self.assertEqual(tool, "get_window_state")
            self.assertEqual(arguments["window_id"], 123)
            self.assertEqual(arguments["max_elements"], capture.CUA_ACCESSIBILITY_TREE_LIMIT)
            self.assertTrue(stored["complete"])
            self.assertEqual(json.loads((folder / "cua-accessibility.json").read_text()), stored)

    def test_cua_capture_flow_requests_named_controls_for_each_contract_view(self):
        for view in ("wizard-start", "plan"):
            with self.subTest(view=view):
                with patch.object(capture, "verify_target_process"), \
                     patch.object(capture, "cua_capture_windows", return_value=[{"status": "PARTIAL"}]), \
                     patch.object(capture, "capture_cua_accessibility_tree") as read_controls:
                    capture._capture_cua_states(
                        42, Path("/example/Igloo.app"), Path("/tmp/scenario"), {"view": view}, "session")
                read_controls.assert_called_once_with(42, Path("/tmp/scenario"), {"view": view}, "session")

    def test_cua_validation_uses_saved_main_window_tree_for_sheet_controls(self):
        scenario = next(row for row in capture.scenarios()
                        if row["view"] == "wizard-start" and row["size"] == "1440x900")
        tree_markdown = "\n".join((
            "- [110] AXButton (Make space, Review cleanup and Freeze suggestions.) [id=wizard.start.makeSpace actions=[press]]",
            "- [111] AXButton (Thaw projects, Restore multiple Frozen projects.) [id=wizard.start.thaw actions=[press]]",
        ))
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            folder.chmod(0o700)
            write_renderer_png(folder, "window", sample_png())
            write_renderer_png(folder, "sheet", sample_png())
            (folder / "started.json").write_text(json.dumps({"pid": 123}))
            write_window_metadata(folder, 123, sheet=True)
            (folder / "accessibility.json").write_text(json.dumps({
                "pid": 123, "tree": {"role": "AXCapture", "children": []},
                "actualWidth": 1440, "actualHeight": 900,
            }))
            (folder / "cua-accessibility.json").write_text(json.dumps({
                "tree_markdown": tree_markdown, "truncated": False,
                "total_element_count": 2, "complete": True,
            }))
            result = capture.validate_result(folder, scenario, reject_renderer_placeholders=False, backend="cua")
        self.assertEqual(result["accessibilityControlIssues"], {
            "wizard.start.makeSpace": "found", "wizard.start.thaw": "found"})

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
            write_renderer_png(folder, "window", b"not a PNG")
            (folder / "accessibility.json").write_text(json.dumps({"pid": 1, "tree": {"role": "AXWindow"}}))
            with self.assertRaises(ValueError):
                capture.validate_result(folder, capture.scenarios()[0])

    def test_capture_reads_renderer_pngs_from_render_subfolder(self):
        scenario = next(s for s in capture.scenarios() if s["view"] == "overview" and s["size"] == "1440x900"
                        and s["data"] == "normal")
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            folder.chmod(0o700)
            render = folder / "render"
            render.mkdir(mode=0o700)
            write_renderer_png(folder, "window", sample_png())
            (folder / "started.json").write_text(json.dumps({"pid": 123}))
            write_window_metadata(folder, 123)
            (folder / "accessibility.json").write_text(json.dumps({"pid": 123, "tree": {"role": "AXWindow"},
                                                                    "actualWidth": 1440, "actualHeight": 900}))
            self.assertEqual(capture.validate_result(folder, scenario)["pid"], 123)

    def test_real_pixel_backend_keeps_placeholder_render_as_diagnostic(self):
        scenario = next(s for s in capture.scenarios() if s["view"] == "overview" and s["size"] == "1440x900"
                        and s["data"] == "normal")
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            write_renderer_png(folder, "window", sample_png(placeholder=True))
            (folder / "started.json").write_text(json.dumps({"pid": 123}))
            write_window_metadata(folder, 123)
            (folder / "accessibility.json").write_text(json.dumps({"pid": 123, "tree": {"role": "AXWindow"},
                                                                    "actualWidth": 1440, "actualHeight": 900}))
            result = capture.validate_result(folder, scenario, reject_renderer_placeholders=False)
            self.assertGreater(result["nonBackgroundShare"], 0.01)

    def test_real_pixel_backend_keeps_uniform_render_as_diagnostic(self):
        scenario = next(s for s in capture.scenarios() if s["view"] == "update-ready" and s["size"] == "1440x900")
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            write_renderer_png(folder, "window", sample_png(uniform=True))
            write_renderer_png(folder, "sheet", sample_png(uniform=True))
            (folder / "started.json").write_text(json.dumps({"pid": 123}))
            write_window_metadata(folder, 123, sheet=True)
            (folder / "accessibility.json").write_text(json.dumps({"pid": 123, "tree": {"role": "AXWindow"},
                                                                    "actualWidth": 1440, "actualHeight": 900}))
            result = capture.validate_result(folder, scenario, reject_renderer_placeholders=False)
            self.assertIsNone(result["nonBackgroundShare"])
            self.assertIn("uniform", result["rendererDiagnostic"])
            self.assertIsNone(result["sheetNonBackgroundShare"])
            with self.assertRaisesRegex(ValueError, "uniform"):
                capture.validate_result(folder, scenario)

    def test_app_renderer_files_use_the_render_subfolder(self):
        source = (ROOT / "app/DemoCatalog.swift").read_text()
        self.assertIn('folder.appendingPathComponent("render", isDirectory: true)', source)
        self.assertIn('renderFolder.appendingPathComponent("window-render.png")', source)
        self.assertIn('renderFolder.appendingPathComponent("sheet-render.png")', source)
        self.assertNotIn('folder.appendingPathComponent("window.png")', source)
        self.assertNotIn('folder.appendingPathComponent("sheet.png")', source)

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
        scenario = next(s for s in capture.scenarios() if s["view"] == "freeze-result" and s["size"] == "1440x900")
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            write_renderer_png(folder, "sheet", sparse_sheet_png())
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
            write_renderer_png(folder, "window", sample_png())
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
            write_renderer_png(folder, "window", sample_png())
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
            write_renderer_png(folder, "window", sample_png())
            write_renderer_png(folder, "sheet", sample_png())
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

    def test_main_refuses_a_bundle_with_the_installed_local_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = root / "DifferentName.app"
            contents = app / "Contents"
            executable = contents / "MacOS/Igloo"
            executable.parent.mkdir(parents=True)
            executable.touch()
            with (contents / "Info.plist").open("wb") as info:
                plistlib.dump({"CFBundleIdentifier": "local.igloo"}, info)
            output = root / "output"
            args = ["capture_states", "--app", str(app), "--output", str(output)]
            with patch.object(sys, "argv", args), patch.object(capture.sys, "platform", "darwin"), \
                 patch.object(capture, "launch") as launch:
                with self.assertRaises(SystemExit):
                    capture.main()
                launch.assert_not_called()
            self.assertFalse(output.exists())

    def test_installed_bundle_path_is_refused_even_without_reading_its_contents(self):
        self.assertTrue(capture.is_protected_app_bundle(Path("/Applications/Igloo.app")))

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
            def fake_launch(app_path, folder, selected, backend=None):
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
            def timeout(app_path, folder, selected, backend=None):
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
        args = capture.build_launch_command(Path("/example/Igloo.app"), Path("/tmp/capture"), scenario, backend="cua")
        self.assertEqual(args[:4], ["/usr/bin/open", "-n", "-W", "/example/Igloo.app"])
        for argument in ("--demo", "--demo-size", "--demo-view", "--demo-capture", "--demo-scope", "--demo-rail", "--demo-data", "--demo-window-server", "--demo-driver"):
            self.assertIn(argument, args)

    def test_driver_demo_flag_selects_its_longer_app_deadline(self):
        source = (ROOT / "app" / "DemoCatalog.swift").read_text()
        self.assertIn('let driverCapture = args.contains("--demo-driver")', source)
        self.assertIn("if driverCapture && (!windowServer || folder == nil)", source)
        self.assertIn("options.driverCapture ? 900 : (options.windowServer ? 60 : 15)", source)

    def test_cua_is_the_default_backend(self):
        output = io.StringIO()
        with patch.object(capture.sys, "argv", ["capture_states.py", "--help"]), \
             contextlib.redirect_stdout(output), self.assertRaises(SystemExit):
            capture.main()
        self.assertIn("default: cua", output.getvalue())
        self.assertNotIn("orca", output.getvalue().casefold())
        with patch.object(capture, "launch_cua", return_value=[]) as launch:
            capture.launch(Path("/example/Igloo.app"), Path("/tmp/capture"), capture.scenarios()[0])
        launch.assert_called_once()

    def test_cua_call_runs_one_tool_and_parses_its_json(self):
        completed = subprocess.CompletedProcess(["cua-driver"], 0, stdout=json.dumps({"windows": []}), stderr="")
        with patch.dict(capture.os.environ, {"CUA_DRIVER_COMMAND": "/example/cua-driver"}), \
             patch.object(capture.subprocess, "run", return_value=completed) as run:
            self.assertEqual(capture.cua_call("list_windows", {"pid": 42}), {"windows": []})
        args = run.call_args.args[0]
        self.assertEqual(args[:3], ["/example/cua-driver", "call", "list_windows"])
        self.assertEqual(json.loads(args[3]), {"pid": 42})
        self.assertTrue(run.call_args.kwargs["capture_output"])

    def test_cua_call_reports_refusal_code_and_capped_hint(self):
        refusal = {"code": "window_id_not_found", "hint": "No window matches. " * 100}
        with patch.object(capture.subprocess, "run", return_value=subprocess.CompletedProcess(
                ["cua-driver"], 1, stdout=json.dumps(refusal), stderr="")):
            with self.assertRaisesRegex(capture.CaptureError, "^window_id_not_found: No window matches") as raised:
                capture.cua_call("get_window_state", {"pid": 42, "window_id": 7})
        self.assertLessEqual(len(raised.exception.message), capture.CUA_OUTPUT_LIMIT)

    def test_cua_call_reports_script_errors_and_bare_failures(self):
        script_error = {"ok": False, "error": {"message": "Error: timed out waiting for text: Results", "line": 12}}
        cases = ((subprocess.CompletedProcess(["cua-driver"], 1, stdout=json.dumps(script_error), stderr=""),
                  r"timed out waiting for text: Results \(script line 12\)"),
                 (subprocess.CompletedProcess(["cua-driver"], 1, stdout="", stderr="daemon is not running"),
                  r"cua-driver run_script failed \(exit 1\)"),
                 (subprocess.CompletedProcess(["cua-driver"], 0, stdout="not json", stderr=""),
                  "returned invalid data"))
        for completed, message in cases:
            with self.subTest(message=message), \
                 patch.object(capture.subprocess, "run", return_value=completed), \
                 self.assertRaisesRegex(capture.CaptureError, message):
                capture.cua_call("run_script", {"script": "return 1;"})
        with patch.object(capture.subprocess, "run", side_effect=FileNotFoundError("cua-driver")), \
             self.assertRaisesRegex(capture.CaptureError, "could not run"):
            capture.cua_call("list_windows", {"pid": 42})

    def test_cua_window_targets_use_the_app_metadata_and_ignore_other_pids(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            write_window_metadata(folder, 42, sheet=True)
            listed = {"windows": [{"window_id": 123, "pid": 42, "layer": 0, "z_index": 1},
                                  {"window_id": 124, "pid": 99, "layer": 0, "z_index": 2}]}
            with patch.object(capture, "cua_call", return_value=listed) as call_driver:
                targets = capture.cua_window_targets(42, folder, "igloo-capture-42")
            call_driver.assert_called_once_with("list_windows", {"pid": 42, "session": "igloo-capture-42"})
            self.assertEqual(targets, {"window": 123, "sheet": None})
            with self.assertRaisesRegex(capture.CaptureError, "PID mismatch"):
                capture.cua_window_targets(7, folder, "igloo-capture-7")

    def test_cua_window_targets_fall_back_to_the_frontmost_window_of_the_pid(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            write_window_metadata(folder, 42)
            listed = {"windows": [{"window_id": 500, "pid": 42, "layer": 0, "z_index": 1},
                                  {"window_id": 501, "pid": 42, "layer": 0, "z_index": 4},
                                  {"window_id": 502, "pid": 42, "layer": 3, "z_index": 9}]}
            with patch.object(capture, "cua_call", return_value=listed):
                self.assertEqual(capture.cua_window_targets(42, folder, "s"), {"window": 501})
            with patch.object(capture, "cua_call", return_value={"windows": []}), \
                 self.assertRaisesRegex(capture.CaptureError, "lists no window"):
                capture.cua_window_targets(42, folder, "s")

    def test_cua_screenshot_is_saved_as_private_real_pixel_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            png = sample_png() + b"\0" * 1024
            def fake_call(tool, arguments, timeout=60):
                Path(arguments["screenshot_out_file"]).write_bytes(png)
                return {"screenshot_file_path": arguments["screenshot_out_file"]}
            with patch.object(capture, "cua_call", side_effect=fake_call) as call_driver, \
                 patch.object(capture, "png_nonbackground_share", return_value=0.25):
                result = capture.cua_screenshot(42, 123, folder, "window-cua.png", "s")
            tool, arguments = call_driver.call_args.args
            self.assertEqual(tool, "get_window_state")
            self.assertEqual(arguments["pid"], 42)
            self.assertEqual(arguments["window_id"], 123)
            self.assertIs(arguments["include_accessibility_tree"], False)
            self.assertEqual(arguments["screenshot_out_file"], str(folder / "window-cua.png"))
            self.assertEqual(stat.S_IMODE((folder / "window-cua.png").stat().st_mode), 0o600)
            self.assertEqual(result["status"], "PARTIAL")
            self.assertEqual(result["path"], "window-cua.png")
            with self.assertRaisesRegex(capture.CaptureError, "already exists"):
                capture.cua_screenshot(42, 123, folder, "window-cua.png", "s")

    def test_cua_missing_or_tiny_screenshot_is_an_error(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            with patch.object(capture, "cua_call", return_value={}), \
                 self.assertRaisesRegex(capture.CaptureError, "wrote no screenshot"):
                capture.cua_screenshot(42, 123, folder, "window-cua.png", "s")
            def tiny(tool, arguments, timeout=60):
                Path(arguments["screenshot_out_file"]).write_bytes(b"\x89PNG")
            with patch.object(capture, "cua_call", side_effect=tiny), \
                 self.assertRaisesRegex(capture.CaptureError, "too small"):
                capture.cua_screenshot(42, 123, folder, "sheet-cua.png", "s")

    def test_cua_capture_verifies_the_pid_and_reports_an_unlisted_sheet_open(self):
        with patch.object(capture, "verify_target_process") as verify, \
             patch.object(capture, "cua_window_targets", return_value={"window": 123, "sheet": None}), \
             patch.object(capture, "cua_screenshot", return_value={"kind": "window", "status": "PARTIAL"}) as shot:
            results = capture.cua_capture_windows(42, Path("/example/Igloo.app"), Path("/tmp/s"), "window", "s")
        verify.assert_called_once_with(42, Path("/example/Igloo.app"))
        shot.assert_called_once_with(42, 123, Path("/tmp/s"), "window-cua.png", "s")
        self.assertEqual(results[1]["status"], "OPEN")
        with patch.object(capture, "verify_target_process"), \
             patch.object(capture, "cua_window_targets", return_value={"window": 123, "sheet": 124}), \
             patch.object(capture, "cua_screenshot", return_value={"status": "PARTIAL"}) as shot:
            capture.cua_capture_windows(42, Path("/example/Igloo.app"), Path("/tmp/s"), "03-plan", "s")
        self.assertEqual(shot.call_args_list[1], call(42, 124, Path("/tmp/s"), "03-plan-sheet-cua.png", "s",
                                                      kind="sheet"))

    def test_capture_cua_states_runs_every_interactive_step_and_captures_after_each(self):
        app, folder = Path("/example/Igloo.app"), Path("/tmp/scenario")
        with patch.object(capture, "verify_target_process"), \
             patch.object(capture, "cua_window_targets", return_value={"window": 123, "sheet": 124}), \
             patch.object(capture, "cua_capture_windows", return_value=[{"status": "PARTIAL"}]) as shots, \
             patch.object(capture, "cua_run_step") as run_step:
            results = capture._capture_cua_states(42, app, folder, {"flow": "wizard-thaw"}, "s")
        self.assertEqual(len(results), 6)
        self.assertEqual([item.args[3] for item in shots.call_args_list],
                         ["00-start", "01-selection", "02-selected", "03-plan", "05-confirmed", "06-result"])
        self.assertEqual(run_step.call_count, 5)
        self.assertTrue(all(item.args[:3] == (42, app, 123) for item in run_step.call_args_list))
        with patch.object(capture, "verify_target_process"), \
             patch.object(capture, "cua_capture_windows", return_value=[{"status": "PARTIAL"}]) as shots, \
             patch.object(capture, "cua_run_step") as run_step:
            capture._capture_cua_states(42, app, folder, {"view": "overview"}, "s")
        shots.assert_called_once_with(42, app, folder, "window", "s")
        run_step.assert_not_called()

    def test_failed_cua_step_names_the_step(self):
        with patch.object(capture, "verify_target_process"), \
             patch.object(capture, "cua_window_targets", return_value={"window": 123}), \
             patch.object(capture, "cua_capture_windows", return_value=[]), \
             patch.object(capture, "cua_run_step", side_effect=capture.CaptureError("no enabled button")):
            with self.assertRaisesRegex(capture.CaptureError, "step 01-selection: no enabled button"):
                capture._capture_cua_states(42, Path("/example/Igloo.app"), Path("/tmp/s"),
                                            {"flow": "wizard-thaw"}, "s")

    def test_cua_step_verifies_the_pid_before_any_input(self):
        step = capture.parse_interactive_steps(capture.INTERACTIVE_FLOWS["wizard-thaw"])[0]
        with patch.object(capture, "verify_target_process", side_effect=ValueError("path mismatch")), \
             patch.object(capture, "cua_call") as call_driver:
            with self.assertRaisesRegex(ValueError, "path mismatch"):
                capture.cua_run_step(42, Path("/example/Igloo.app"), 123, step, "s")
        call_driver.assert_not_called()
        with patch.object(capture, "verify_target_process"), \
             patch.object(capture, "cua_call", return_value={"ok": True, "value": {"role": "AXButton"}}) as call_driver:
            self.assertEqual(capture.cua_run_step(42, Path("/example/Igloo.app"), 123, step, "s"),
                             {"role": "AXButton"})
        tool, arguments = call_driver.call_args.args
        self.assertEqual(tool, "run_script")
        self.assertEqual(arguments["session"], "s")
        self.assertIn('"windowId": 123', arguments["script"])

    def test_cua_step_script_maps_locators_to_pid_scoped_targets(self):
        steps = capture.parse_interactive_steps(capture.INTERACTIVE_FLOWS["wizard-thaw"])
        script = capture.cua_step_script(42, 123, steps[3])
        params = json.loads(script.split("const p = ", 1)[1].split(";\n", 1)[0])
        self.assertEqual(params["pid"], 42)
        self.assertEqual(params["windowId"], 123)
        self.assertEqual(params["anchor"], "Thaw plan")
        self.assertEqual(params["click"], {"roles": ["checkbox"], "name": "I reviewed the remote refs", "nth": 0})
        self.assertIs(params["expectValue"], True)
        self.assertEqual(params["control"], {"roles": ["button"], "name": "Thaw 1 ready project", "nth": 0,
                                             "enabled": True})
        self.assertIn("cua.getApp({pid: p.pid, windowId: p.windowId})", script)
        self.assertNotIn("local.igloo", script)
        key_step = {"key": "Down", "capture": "04-row",
                    "wait_for_selection": {"role": "outline row", "ordinal": 1, "anchor": "Thaw plan",
                                           "label": "Signal Lab"}}
        params = json.loads(capture.cua_step_script(42, 123, capture.parse_interactive_steps([key_step])[0])
                            .split("const p = ", 1)[1].split(";\n", 1)[0])
        self.assertEqual(params["key"], "down")
        self.assertIsNone(params["click"])
        self.assertEqual(params["selection"], {"roles": ["row", "outlinerow"], "name": "Signal Lab", "nth": 0})

    def _fake_app(self, temp):
        app = Path(temp) / "Igloo.app"
        executable = app / "Contents/MacOS/Igloo"
        executable.parent.mkdir(parents=True)
        executable.touch()
        return app, executable

    def test_launch_cua_stops_the_verified_pid_after_success_and_failure(self):
        for failure in (None, capture.CaptureError("capture failed")):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temp:
                app, executable = self._fake_app(temp)
                process = Mock()
                results = [{"kind": "window", "status": "PARTIAL"}]
                with patch.object(capture.subprocess, "Popen", return_value=process) as popen, \
                     patch.object(capture, "_wait_for_launched_capture", return_value=123), \
                     patch.object(capture, "process_executable_path", return_value=executable), \
                     patch.object(capture, "cua_call") as call_driver, \
                     patch.object(capture.os, "kill") as kill, \
                     patch.object(capture, "_capture_cua_states", side_effect=failure, return_value=results):
                    if failure is None:
                        self.assertIs(capture.launch_cua(app, Path(temp) / "scenario", capture.scenarios()[0]),
                                      results)
                    else:
                        with self.assertRaisesRegex(capture.CaptureError, "capture failed"):
                            capture.launch_cua(app, Path(temp) / "scenario", capture.scenarios()[0])
                self.assertIn("--demo-driver", popen.call_args.args[0])
                call_driver.assert_called_once_with("end_session", {"session": "igloo-capture-123"}, timeout=20)
                self.assertEqual(kill.call_args_list, [call(123, 0), call(123, signal.SIGTERM)])
                process.wait.assert_called_once_with(timeout=20)

    def test_launch_cua_stops_the_started_pid_after_metadata_timeout(self):
        with tempfile.TemporaryDirectory() as temp:
            app, executable = self._fake_app(temp)
            folder = Path(temp) / "scenario"
            folder.mkdir()
            (folder / "started.json").write_text(json.dumps({"pid": 123}))
            process = Mock()
            with patch.object(capture.subprocess, "Popen", return_value=process), \
                 patch.object(capture, "_wait_for_launched_capture",
                              side_effect=capture.CaptureError("Timed out waiting for capture metadata")), \
                 patch.object(capture, "process_executable_path", return_value=executable), \
                 patch.object(capture, "cua_call"), \
                 patch.object(capture.os, "kill") as kill:
                with self.assertRaisesRegex(capture.CaptureError, "Timed out"):
                    capture.launch_cua(app, folder, capture.scenarios()[0])
            self.assertEqual(kill.call_args_list, [call(123, 0), call(123, signal.SIGTERM)])

    def test_launch_cua_does_not_signal_a_pid_outside_the_app(self):
        with tempfile.TemporaryDirectory() as temp:
            app, _ = self._fake_app(temp)
            other = Path(temp) / "Other.app/Contents/MacOS/Igloo"
            other.parent.mkdir(parents=True)
            other.touch()
            with patch.object(capture.subprocess, "Popen", return_value=Mock()), \
                 patch.object(capture, "_wait_for_launched_capture", return_value=123), \
                 patch.object(capture, "process_executable_path", return_value=other), \
                 patch.object(capture, "cua_call"), \
                 patch.object(capture.os, "kill") as kill, \
                 patch.object(capture, "_capture_cua_states") as states:
                with self.assertRaisesRegex(capture.CaptureError, "PID 123 may still be running"):
                    capture.launch_cua(app, Path(temp) / "scenario", capture.scenarios()[0])
            states.assert_not_called()
            self.assertEqual(kill.call_args_list, [call(123, 0)])

    def test_cua_probe_prints_a_capped_tree_without_screenshots(self):
        with patch.object(capture, "verify_target_process"), \
             patch.object(capture, "cua_window_targets", return_value={"window": 123, "sheet": 124}), \
             patch.object(capture, "cua_call", return_value={"tree_markdown": "x" * 50000}) as call_driver:
            tree = capture.cua_probe_tree(42, Path("/example/Igloo.app"), Path("/tmp/s"), "s")
        tool, arguments = call_driver.call_args.args
        self.assertEqual((tool, arguments["window_id"], arguments["include_screenshot"]),
                         ("get_window_state", 124, False))
        self.assertTrue(tree.endswith("[tree output capped]"))
        self.assertLess(len(tree), capture.CUA_PROBE_LIMIT + 100)

    def test_cua_failure_is_reported_open(self):
        with tempfile.TemporaryDirectory() as temp:
            app, _ = self._fake_app(temp)
            scenario = capture.scenarios()[0]
            args = ["capture_states", "--app", str(app), "--output", str(Path(temp) / "output"),
                    "--scenario", scenario["name"]]
            text = io.StringIO()
            with patch.object(sys, "argv", args), patch.object(capture.sys, "platform", "darwin"), \
                 patch.object(capture, "launch_cua", side_effect=capture.CaptureError("window unavailable")), \
                 contextlib.redirect_stdout(text):
                self.assertEqual(capture.main(), 1)
            self.assertIn("OPEN " + scenario["name"] + ": window unavailable", text.getvalue())
            self.assertNotIn("may still be running", text.getvalue())

    def test_every_app_view_has_a_capture_inventory_entry(self):
        declaration = re.compile(r"^\s*(?:(?:private|fileprivate|internal|public)\s+)?struct\s+(\w+)\s*(?:<[^>{]*>)?"
                                 r"\s*:\s*([^{]*)\{", re.MULTILINE)
        views = set()
        for source in sorted((ROOT / "app").glob("*.swift")):
            for name, conformances in declaration.findall(source.read_text()):
                if "View" in re.findall(r"\w+", conformances):
                    views.add(name)
        self.assertIn("OrganizerView", views)
        rows = {}
        for line in (ROOT / "docs/capture-inventory.md").read_text().splitlines():
            if line.startswith("| `"):
                cells = [cell.strip() for cell in line.strip("|").split("|")]
                rows[cells[0].strip("`")] = cells
        missing = sorted(views - set(rows))
        self.assertEqual(missing, [], "add each view to docs/capture-inventory.md")
        matrix_views = {row["view"] for row in capture.scenarios()}
        for name, cells in rows.items():
            with self.subTest(view=name):
                self.assertEqual(len(cells), 4)
                scenario_cell = cells[3]
                if scenario_cell.startswith("None:"):
                    continue
                patterns = re.findall(r"`([^`]+)`", scenario_cell)
                self.assertTrue(patterns, "name a scenario view or state the reason after None:")
                for pattern in patterns:
                    self.assertTrue(any(fnmatch.fnmatchcase(view, pattern) for view in matrix_views), pattern)

    def test_new_inventory_scenarios_run_at_minimum_and_default_sizes_in_both_appearances(self):
        new_views = {"help-getting-started", "help-freeze-thaw", "help-safety-storage", "tasks-rules", "tasks-schedule",
                     "space-sunburst", "space-icicle", "space-marimekko", "space-drilldown", "repositories",
                     "remote-setup", "commit-setup", "wizard-rules", "git-branches", "git-saved-history",
                     "git-remotes", "scope-selected", "push-review", "wizard-actions"}
        matrix = capture.scenarios()
        for view in new_views:
            minimum = "1300x700" if view == "scope-selected" else "850x700"
            for appearance in ("light", "dark"):
                for size in (minimum, "1440x900"):
                    with self.subTest(view=view, appearance=appearance, size=size):
                        self.assertTrue(any(row["view"] == view and row["appearance"] == appearance and
                                            row["size"] == size for row in matrix))
        source = (ROOT / "app/DemoCatalog.swift").read_text()
        swift_sheets = source.split("static let extraSheetViews: Set<String> = [", 1)[1].split("]", 1)[0]
        swift_details = source.split("static let detailViews = [", 1)[1].split("]", 1)[0]
        for view in new_views:
            with self.subTest(view=view):
                if view in capture.SHEET_VIEWS:
                    self.assertIn(f'"{view}"', swift_sheets)
                else:
                    self.assertIn(view, capture.DETAIL_VIEWS)
                    self.assertIn(f'"{view}"', swift_details)

    def test_demo_initial_states_name_existing_view_choices(self):
        catalog = (ROOT / "app/DemoCatalog.swift").read_text()
        self.assertIn('"Saved history"', (ROOT / "app/DetailViews.swift").read_text())
        self.assertIn('ForEach(["Sunburst", "Icicle", "Marimekko"]', (ROOT / "app/SpaceViews.swift").read_text())
        self.assertIn('ForEach(["Rules", "Schedule", "History"]', (ROOT / "app/TasksView.swift").read_text())
        help_view = (ROOT / "app/Igloo.swift").read_text().split("struct AppHelpView: View {", 1)[1]
        for topic in ("Getting started", "Freeze & thaw", "Safety & storage"):
            self.assertIn(f'.tag("{topic}")', help_view)
            self.assertIn(f'"{topic}"', catalog)
        self.assertIn('basis == "Selected project"', (ROOT / "app/ScopeInspector.swift").read_text())
        self.assertIn('"remote-setup": "no-remote", "commit-setup": "no-commit"', catalog)

    def test_demo_capture_reads_native_accessibility_identifiers(self):
        source = (ROOT / "app/DemoCatalog.swift").read_text()
        self.assertIn('[(attribute("AXIdentifier") as? String), (object as? NSView)?.identifier?.rawValue]', source)
        self.assertIn('.compactMap { $0 }.first { !$0.isEmpty }', source)

    def test_settings_explains_the_freeze_threshold_used_by_reminders(self):
        settings = (ROOT / "app/PreferencesViews.swift").read_text()
        guide = (ROOT / "docs/guide.md").read_text()
        catalog = (ROOT / "app/DemoCatalog.swift").read_text()
        self.assertIn("Reminder threshold: more than \\(reminderFreezeDays) days", settings)
        self.assertIn("saved Freeze threshold in Project wizard", guide)
        self.assertIn('case "settings": expectedLabels = ["Igloo Settings", "Freezer backup folder", "Reminder threshold"]', catalog)

    def test_issue56_view_copy_matches_help_docs_and_demo_expectations(self):
        lifecycle = (ROOT / "app/LifecycleViews.swift").read_text()
        wizard = (ROOT / "app/WizardViews.swift").read_text()
        catalog = (ROOT / "app/DemoCatalog.swift").read_text()
        navigation = (ROOT / "app/NavigationViews.swift").read_text()
        docs = (ROOT / "docs/guide.md").read_text() + (ROOT / "docs/tasks.md").read_text()
        self.assertIn('Button("Show Freezer copy")', lifecycle)
        self.assertIn('"Show Freezer copy", "Freezer backup verified"', catalog)
        self.assertIn('return "Freezer backup verified "', lifecycle)
        self.assertIn('return "No verified Freezer backup copy: "', lifecycle)
        self.assertIn('Choose **Show Freezer copy**', docs)
        self.assertIn("Each Freezer row names the verified backup copy", docs)
        self.assertIn('Button("Open Terminal") { store.terminal(checkout) }\n                        .disabled(store.isDemo)', lifecycle)
        self.assertIn('Text("Project wizard")', wizard)
        self.assertIn('**Project wizard**', docs)
        self.assertIn('"Make space", "Thaw projects"', catalog)
        self.assertIn('"Cleanup candidates"', navigation)
        self.assertIn('"Cleanup candidates"]', catalog)
        self.assertIn("cleanup candidates", docs)
        self.assertIn('remote = "archive"', lifecycle)
        self.assertIn('.onChange(of: mode) { accepted = false }', lifecycle)
        self.assertIn('Resolve all \\(count) open', lifecycle)
        self.assertIn('Text("in \\(grouped.count) of \\(projects.count) projects")', (ROOT / "app/SpaceViews.swift").read_text())

    def test_hold_save_state_waits_for_hold_and_note_saves(self):
        lifecycle = (ROOT / "app/LifecycleViews.swift").read_text().split("struct PlanView", 1)[1].split("struct LifecycleControls", 1)[0]
        self.assertIn('store.invoke(["hold", project.id, hold ? "true" : "false"]', lifecycle)
        self.assertIn('store.invoke(["note", project.id, note]', lifecycle)
        self.assertLess(lifecycle.index('store.invoke(["note", project.id, note]'), lifecycle.index("saved = true"))
