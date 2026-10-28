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
        self.assertEqual([item["path"] for item in restored], ["/Users/demo/Projects/Harbor CLI"])
        self.assertTrue(all(len(item["sha"]) == 7 for item in restored))
        paths = [archive["path"] for archive in thaw["archives"]]
        paths += [row["archiveLocation"] for row in rows]
        paths += [path for row in rows for path in row["checkouts"] + row["localFiles"]]
        self.assertTrue(all(path.startswith("/Users/demo/") for path in paths))
        self.assertTrue(all(row["destination"].startswith("https://example.com/") for row in rows))
        self.assertEqual(thaw["result"]["outcome"]["bytesFreed"], None)

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

    def test_updater_scenarios_are_captured_as_sheets(self):
        for scenario in capture.scenarios():
            if scenario["view"].startswith("update-"):
                with self.subTest(view=scenario["view"]):
                    self.assertIn(scenario["view"], capture.SHEET_VIEWS)

    def test_ready_update_view_is_in_the_swift_capture_allow_list(self):
        source = (ROOT / "app/DemoCatalog.swift").read_text()
        self.assertIn('static let updateViews = AppUpdater.demoFailureNames.map { "update-" + $0 } + ["update-markdown", "update-ready"]', source)
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
                    if row["view"].startswith("update-") and row["view"] not in {"update-markdown", "update-ready"}]
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
                    "wizard-start": "Make space", "wizard-thaw-selection": "Create Thaw plan",
                    "wizard-thaw-review": "Thaw plan", "wizard-thaw-result": "Checkouts restored",
                    "wizard-thaw-rehydrating": "Thawing Harbor CLI",
                    "task-review": "Review task actions", "settings": "Freezer backup folder",
                    "freeze-review": "Expected outcome", "freeze-hold": "This project is on Hold",
                    "cleanup-result": "In Trash (can be restored)", "trash-history": "Space report",
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
        self.assertIn('"wizard-thaw-ready": "demo-harbor"', wizard)

    def test_thaw_ready_capture_contract_checks_checkbox_and_disabled_button(self):
        def control(identifier, role, *, enabled, value=None):
            node = {"identifier": identifier, "role": role, "label": identifier, "enabled": enabled,
                    "frame": {"x": 1, "y": 2, "width": 40, "height": 20}}
            if value is not None:
                node["value"] = value
            return node

        tree = {"role": "AXCapture", "children": [
            {"label": "Thaw projects"}, {"label": "Only ready projects will run"},
            {"label": "Harbor CLI"}, {"label": "Thaw 1 ready project"},
            control("wizard.thaw.acceptance", "AXCheckBox", enabled=True, value=False),
            control("wizard.thaw.execute", "AXButton", enabled=False),
        ]}
        tree["children"][-2]["label"] = "I reviewed the remote refs, original paths and local-only files shown for Harbor CLI."
        self.assertEqual(capture.accessibility_missing(tree, {"view": "wizard-thaw-ready"}), [])
        self.assertEqual(capture.accessibility_control_issues(tree, {"view": "wizard-thaw-ready"}), [])
        tree["children"][-1]["enabled"] = True
        self.assertIn("wizard.thaw.execute", capture.accessibility_control_issues(tree, {"view": "wizard-thaw-ready"}))
        tree["children"][-1]["enabled"] = False
        tree["children"][-2]["value"] = True
        self.assertIn("wizard.thaw.acceptance", capture.accessibility_control_issues(tree, {"view": "wizard-thaw-ready"}))

    def test_demo_capture_activates_and_keys_its_window_before_capture(self):
        source = (ROOT / "app/DemoCatalog.swift").read_text()
        capture_start = source.split("static func start(store:", 1)[1].split("private static func windowMetadata", 1)[0]
        self.assertIn("NSApp.activate(ignoringOtherApps: true)", capture_start)
        self.assertIn("window.makeKeyAndOrderFront(nil)", capture_start)
        self.assertLess(capture_start.index("window.makeKeyAndOrderFront(nil)"), capture_start.index("view.layoutSubtreeIfNeeded()"))

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
        self.assertIn("isEnabled ? Color.white : Color.primary.opacity(0.38)", button)
        self.assertIn("isEnabled ? Color.accentColor : Color.primary.opacity(0.07)", button)
        progress = source.split("if isThawExecuting {", 1)[1].split("} else if thawPlan?.status", 1)[0]
        self.assertIn("Text(thawProgressMessage)", progress)
        self.assertIn("ProgressView().progressViewStyle(.linear)", progress)
        self.assertIn("Color(red: 0.08, green: 0.34, blue: 0.78)", progress)

    def test_result_sheets_use_sized_rows_and_reduced_result_frames(self):
        cleanup = (ROOT / "app/ReviewViews.swift").read_text()
        wizard = (ROOT / "app/WizardViews.swift").read_text()
        lifecycle = (ROOT / "app/LifecycleViews.swift").read_text()
        self.assertIn('item.size.map { " · " + bytes($0) }', cleanup)
        self.assertIn('item.size.map { " · " + bytes($0) }', wizard)
        self.assertIn("height: resultHeight", cleanup)
        self.assertIn("isShowingResult ? 390 : 690", lifecycle)
        self.assertIn("Demo only: these results are simulated.", lifecycle)

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
            "wizard-thaw-selection": ["wizard.thaw.back", "wizard.thaw.prepare"],
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
        scenario = next(s for s in capture.scenarios() if s["view"] == "freeze-result" and s["size"] == "1440x900")
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
