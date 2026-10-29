"""Cross-layer contracts for the app, bundle, CLI, defaults, and public export."""
import ast
import json
from pathlib import Path
import re
import shutil
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def read(root, name):
    return (root / name).read_text(encoding="utf-8")


def assigned_value(path, name):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        if any(isinstance(target, ast.Name) and target.id == name for target in targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"missing {name} assignment in {path.name}")


def backend_module_contract(root):
    actual = {path.name for path in (root / "backend").glob("*.py")}
    automation = set(assigned_value(root / "backend/automation.py", "MODULES")) - {"LICENSE"}
    copy_line = next(line for line in read(root, "build.command").splitlines() if line.startswith("cp backend/"))
    build = {Path(part).name for part in copy_line.split() if part.startswith("backend/")}
    public = set(json.loads(read(root, "public-files.json"))["files"])
    exported = {Path(name).name for name in public if name.startswith("backend/") and name.endswith(".py")}
    if not (actual == automation == build == exported):
        raise AssertionError(f"backend module sets differ: tree={actual}, automation={automation}, build={build}, export={exported}")
    if "LICENSE" not in assigned_value(root / "backend/automation.py", "MODULES") or "LICENSE" not in copy_line.split():
        raise AssertionError("the CLI and app bundle must both include the source license")
    if "LICENSE" not in public:
        raise AssertionError("the source license must remain in the public export")


def swift_sources(root):
    build = read(root, "build.command")
    match = re.search(r"(?m)^SOURCES=\(([^)]*)\)", build)
    if not match:
        raise AssertionError("build.command has no Swift SOURCES array")
    listed = set(match.group(1).split())
    actual = {path.relative_to(root).as_posix() for path in (root / "app").glob("*.swift")}
    exported = set(json.loads(read(root, "public-files.json"))["files"])
    if listed != actual:
        raise AssertionError(f"Swift build source set differs: tree={actual}, build={listed}")
    missing = actual - exported
    if missing:
        raise AssertionError(f"Swift sources missing from public export: {missing}")


def update_installer_contract(root):
    if not (root / "scripts/update_installer.py").is_file():
        raise AssertionError("the update installer helper is missing")
    build = read(root, "build.command")
    if 'cp scripts/update_installer.py "$APP_BUNDLE/Contents/Resources/"' not in build:
        raise AssertionError("the update installer helper is not copied into the app bundle")
    exported = set(json.loads(read(root, "public-files.json"))["files"])
    if "scripts/update_installer.py" not in exported:
        raise AssertionError("the update installer helper is missing from the public export")


def brand_resource_contract(root):
    build = read(root, "build.command")
    bundle_copy = 'cp assets/brand/*.png "$APP_BUNDLE/Contents/Resources/brand/"'
    if bundle_copy not in build:
        raise AssertionError("brand PNG resources are not copied into the app bundle")
    if '"$APP_BUNDLE/Contents/Resources/brand"' not in build:
        raise AssertionError("the app bundle brand resource folder is not created")
    kinds = ("freeze-result", "thaw-result", "freezer-empty")
    variants = ("", "-2x", "-night", "-night-2x")
    expected = {f"assets/brand/{kind}{variant}.png" for kind in kinds for variant in variants}
    actual = {path.relative_to(root).as_posix() for path in (root / "assets/brand").glob("*.png")}
    if not expected <= actual:
        raise AssertionError(f"required brand PNG resources are missing: {expected - actual}")
    for name in expected:
        path = root / name
        data = path.read_bytes()
        if data[:8] != b"\x89PNG\r\n\x1a\n" or len(data) < 24:
            raise AssertionError(f"brand resource is not a PNG: {name}")
        scale = 2 if "-2x.png" in name else 1
        width, height = (160, 120) if "freezer-empty" in name else (96, 72)
        actual_size = (int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big"))
        if actual_size != (width * scale, height * scale):
            raise AssertionError(f"brand resource dimensions differ for {name}: {actual_size}")
    exported = set(json.loads(read(root, "public-files.json"))["files"])
    if not expected <= exported:
        raise AssertionError(f"brand PNG resources are missing from the public export: {expected - exported}")


def app_commands(root):
    found = set()
    for path in (root / "app").glob("*.swift"):
        source = path.read_text(encoding="utf-8")
        found.update(re.findall(r"\b(?:store\.)?(?:invoke|action)\(\s*\[\s*\"([a-z][a-z0-9-]*)\"", source))
        # Lifecycle setup builds a command array first, then passes that local to action().
        for match in re.finditer(r"\blet\s+(\w+)\s*=([\s\S]*?)\bstore\.action\(\s*\1\s*\)", source):
            found.update(re.findall(r"\[\s*\"([a-z][a-z0-9-]*)\"", match.group(2)))
    return found


def accepted_commands(root):
    tree = ast.parse(read(root, "backend/organizer.py"))
    expression = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "commands" for target in node.targets):
            expression = node.value
            break
    if expression is None:
        raise AssertionError("organizer.main has no command choices")
    result = {node.value for node in ast.walk(expression) if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    result.update(assigned_value(root / "backend/lifecycle.py", "COMMANDS"))
    return result


def runtime_path_contract(root):
    preferences = read(root, "app/PreferencesViews.swift")
    automation = read(root, "backend/automation.py")
    app_pattern = r'appendingPathComponent\(\s*"python/"\s*\+\s*architecture\s*\+\s*"/bin/python3"\s*\)'
    backend_pattern = r'source\.parent\s*/\s*"python"\s*/\s*platform\.machine\(\)\s*/\s*"bin/python3"'
    architecture_pattern = r'#if arch\(arm64\)\s*let architecture = "arm64"\s*#else\s*let architecture = "x86_64"\s*#endif'
    if not re.search(app_pattern, preferences):
        raise AssertionError("app bundled-runtime path is not python/<architecture>/bin/python3")
    if not re.search(architecture_pattern, preferences):
        raise AssertionError("app architecture names do not match the bundled runtime directories")
    if not re.search(backend_pattern, automation):
        raise AssertionError("CLI bundled-runtime path is not python/<platform.machine()>/bin/python3")


def config_keys(root):
    swift = read(root, "app/PreferencesViews.swift")
    swift = swift.split("static var bundledPython", 1)[0]
    swift_fields = set(re.findall(r"(?m)^\s*var\s+([A-Za-z_]\w*)\s*(?::|=)", swift))
    tree = ast.parse(read(root, "backend/preferences.py"))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "defaults")
    returned = next(node.value for node in ast.walk(function) if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict))
    python_keys = {ast.literal_eval(key) for key in returned.keys}
    if swift_fields != python_keys:
        raise AssertionError(f"default configuration keys differ: Swift={swift_fields}, Python={python_keys}")


class SeamTests(unittest.TestCase):
    def fixture(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        shutil.copyfile(ROOT / "build.command", root / "build.command")
        shutil.copyfile(ROOT / "public-files.json", root / "public-files.json")
        (root / "scripts").mkdir()
        shutil.copyfile(ROOT / "scripts/update_installer.py", root / "scripts/update_installer.py")
        for folder in ("app", "backend"):
            shutil.copytree(ROOT / folder, root / folder, ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(ROOT / "assets/brand", root / "assets/brand")
        return root

    def test_backend_module_set_is_bundled_installable_and_exported(self):
        backend_module_contract(ROOT)
        fixture = self.fixture()
        (fixture / "backend/organizer.py").unlink()
        with self.assertRaisesRegex(AssertionError, "backend module sets differ"):
            backend_module_contract(fixture)

    def test_every_swift_file_is_compiled_and_exported(self):
        swift_sources(ROOT)
        fixture = self.fixture()
        source = read(fixture, "build.command").replace("app/Igloo.swift ", "", 1)
        (fixture / "build.command").write_text(source, encoding="utf-8")
        with self.assertRaisesRegex(AssertionError, "Swift build source set differs"):
            swift_sources(fixture)
        fixture = self.fixture()
        listing = json.loads(read(fixture, "public-files.json"))
        listing["files"].remove("app/Igloo.swift")
        (fixture / "public-files.json").write_text(json.dumps(listing), encoding="utf-8")
        with self.assertRaisesRegex(AssertionError, "Swift sources missing from public export"):
            swift_sources(fixture)

    def test_update_installer_helper_is_bundled_and_exported(self):
        update_installer_contract(ROOT)
        fixture = self.fixture()
        build = read(fixture, "build.command").replace(
            'cp scripts/update_installer.py "$APP_BUNDLE/Contents/Resources/"\n', "", 1)
        (fixture / "build.command").write_text(build, encoding="utf-8")
        with self.assertRaisesRegex(AssertionError, "not copied into the app bundle"):
            update_installer_contract(fixture)

        fixture = self.fixture()
        listing = json.loads(read(fixture, "public-files.json"))
        listing["files"].remove("scripts/update_installer.py")
        (fixture / "public-files.json").write_text(json.dumps(listing), encoding="utf-8")
        with self.assertRaisesRegex(AssertionError, "missing from the public export"):
            update_installer_contract(fixture)

    def test_brand_images_are_bundled_and_exported(self):
        brand_resource_contract(ROOT)
        fixture = self.fixture()
        (fixture / "assets/brand/freeze-result.png").unlink()
        with self.assertRaisesRegex(AssertionError, "required brand PNG resources are missing"):
            brand_resource_contract(fixture)

        fixture = self.fixture()
        listing = json.loads(read(fixture, "public-files.json"))
        listing["files"].remove("assets/brand/freezer-empty-night-2x.png")
        (fixture / "public-files.json").write_text(json.dumps(listing), encoding="utf-8")
        with self.assertRaisesRegex(AssertionError, "missing from the public export"):
            brand_resource_contract(fixture)

        fixture = self.fixture()
        build = read(fixture, "build.command").replace(
            'cp assets/brand/*.png "$APP_BUNDLE/Contents/Resources/brand/"\n', "", 1)
        (fixture / "build.command").write_text(build, encoding="utf-8")
        with self.assertRaisesRegex(AssertionError, "not copied into the app bundle"):
            brand_resource_contract(fixture)

    def test_app_backend_commands_are_accepted_by_organizer(self):
        commands = app_commands(ROOT)
        self.assertTrue(commands, "the app command inventory must not silently become empty")
        self.assertFalse(commands - accepted_commands(ROOT), commands - accepted_commands(ROOT))
        fixture = self.fixture()
        source = read(fixture, "app/Igloo.swift").replace('["snapshot"]', '["new-command"]', 1)
        (fixture / "app/Igloo.swift").write_text(source, encoding="utf-8")
        self.assertTrue(app_commands(fixture) - accepted_commands(fixture))

    def test_app_and_cli_resolve_the_same_bundled_runtime_layout(self):
        runtime_path_contract(ROOT)
        fixture = self.fixture()
        source = read(fixture, "app/PreferencesViews.swift").replace('"python/"', '"runtimes/"', 1)
        (fixture / "app/PreferencesViews.swift").write_text(source, encoding="utf-8")
        with self.assertRaisesRegex(AssertionError, "app bundled-runtime path"):
            runtime_path_contract(fixture)

    def test_app_and_backend_defaults_have_the_same_configuration_keys(self):
        config_keys(ROOT)
        fixture = self.fixture()
        source = read(fixture, "backend/preferences.py").replace('"quietDays": 60,', '', 1)
        (fixture / "backend/preferences.py").write_text(source, encoding="utf-8")
        with self.assertRaisesRegex(AssertionError, "default configuration keys differ"):
            config_keys(fixture)


if __name__ == "__main__":
    unittest.main()
