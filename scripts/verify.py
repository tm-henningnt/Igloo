#!/usr/bin/env python3
"""Run Igloo's local verification gate using only the Python standard library."""
import argparse
from fnmatch import fnmatchcase
import json
import os
import posixpath
from pathlib import Path
import plistlib
import platform
import re
import subprocess
import sys
import tempfile
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
# Keep the acceptance suite explicit: discovery also reruns imported test classes.
TEST_NAMES = (
    "tests.test_organizer.GuardTests",
    "tests.test_lifecycle.LifecycleTests",
    "tests.test_preferences.PreferencesTests",
    "tests.test_space.SpaceTests",
    "tests.test_planning.PlanningTests",
    "tests.test_classification.ClassificationTests",
    "tests.test_tasks.TaskTests",
    "tests.test_releases",
    "tests.test_updates",
    "tests.test_update_installer",
    "tests.test_site",
    "tests.test_verify",
    "tests.test_seams.SeamTests",
    "tests.test_capture_states",
)
MANUAL_CHECKS = (
    "native capture launcher",
    "native window-state review",
    "Intel execution",
    "live updater acceptance",
    "installed-app update",
)


def command_environment(root=None):
    # Hooks can inherit locations belonging to the repository being pushed.
    env = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # Site staging rejects generated output below the source tree. Fall back
    # to OS temporary storage when the caller puts temporary files in source.
    if root is not None:
        for name in ("TMPDIR", "TMP", "TEMP"):
            if env.get(name) and Path(env[name]).resolve().is_relative_to(root):
                del env[name]
    return env


def execute(root, command, *, home=None):
    env = command_environment(root)
    if home is not None:
        env["HOME"] = home
    return subprocess.run(command, cwd=root, env=env, capture_output=True, text=True)


def report(status, name, reason):
    # Keep each result on one line, including failed subprocess diagnostics.
    print(f"{status} {name}: {' '.join(str(reason).split())}", flush=True)


def command_check(root, name, command, *, isolated_home=False):
    try:
        if isolated_home:
            with tempfile.TemporaryDirectory(prefix="igloo-verify-home-") as home:
                result = execute(root, command, home=home)
        else:
            result = execute(root, command)
    except OSError as error:
        report("FAIL", name, error)
        return False
    output = " ".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
    report("PASS" if result.returncode == 0 else "FAIL", name,
           f"exit {result.returncode}" + (f"; {output}" if output else ""))
    return result.returncode == 0


def rosetta_check(root):
    """Build both universal runtime variants and exercise their x86_64 paths."""
    if sys.platform != "darwin":
        report("SKIPPED", "Rosetta", "requires macOS")
        return True
    if platform.machine() != "arm64":
        report("SKIPPED", "Rosetta", "host is not Apple Silicon; translation is not applicable")
        return True

    env = command_environment(root)
    try:
        package = subprocess.run(["/usr/sbin/pkgutil", "--pkg-info", "com.apple.pkg.RosettaUpdateAuto"],
                                 cwd=root, env=env, capture_output=True, text=True)
    except OSError:
        package = None
    if package is None or package.returncode:
        report("SKIPPED", "Rosetta", "x86_64 translation is not installed")
        return True
    try:
        probe = subprocess.run(["/usr/bin/arch", "-x86_64", "/usr/bin/true"], cwd=root,
                               env=env, capture_output=True, text=True)
    except OSError:
        report("SKIPPED", "Rosetta", "x86_64 translation is unavailable")
        return True
    if probe.returncode:
        report("SKIPPED", "Rosetta", "x86_64 translation is not installed")
        return True

    def run(command, *, home=None, system_path=False):
        current = command_environment(root)
        if system_path:
            current["PATH"] = "/usr/bin:/bin"
        if home is not None:
            current["HOME"] = str(home)
        try:
            result = subprocess.run(command, cwd=root, env=current, capture_output=True, text=True,
                                    timeout=300)
        except (OSError, subprocess.TimeoutExpired):
            report("FAIL", "Rosetta", f"{command[0]} command could not complete")
            return False
        if result.returncode:
            report("FAIL", "Rosetta", f"{command[0]} command exited {result.returncode}")
            return False
        return True

    app_binary = root / "Igloo.app/Contents/MacOS/Igloo"
    python_binary = root / "Igloo.app/Contents/Resources/python/x86_64/bin/python3"
    backend = root / "Igloo.app/Contents/Resources/backend"
    with tempfile.TemporaryDirectory(prefix="igloo-rosetta-home-") as temporary:
        for runtime, build_args in (("bundled", ["universal"]), ("system", ["universal", "system"])):
            if not run(["./build.command", *build_args]):
                return False
            home = Path(temporary) / runtime
            (home / "Projects").mkdir(parents=True)
            if not run(["/usr/bin/arch", "-x86_64", str(app_binary), "--smoke-test"], home=home,
                       system_path=True):
                return False
            if runtime == "bundled":
                for script, args in ((backend / "organizer.py", ["snapshot"]),
                                     (backend / "task_cli.py", ["--help"])):
                    if not run(["/usr/bin/arch", "-x86_64", str(python_binary), "-E", "-s", "-B",
                                str(script), *args], home=home, system_path=True):
                        return False
    report("PASS", "Rosetta", "universal bundled and system builds; x86_64 app smoke test; bundled organizer snapshot and task CLI help")
    return True


COMMIT_RULE = "Commit message must include #N or its subject must start with Record or Merge."


def valid_commit_message(message):
    subject = message.splitlines()[0] if message.splitlines() else ""
    return bool(re.search(r"#[0-9]+", message)) or subject.startswith(("Record", "Merge"))


def commit_message_file(path):
    try:
        message = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        print(f"Cannot read commit message: {error}", file=sys.stderr)
        return 1
    if not valid_commit_message(message):
        print(COMMIT_RULE, file=sys.stderr)
        return 1
    return 0


def resolve_commit(root, ref, *, required=True):
    result = execute(root, ["git", "rev-parse", "--verify", "--quiet", "--end-of-options", ref + "^{commit}"])
    value = result.stdout.strip()
    if result.returncode or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value):
        if required:
            raise ValueError("Cannot resolve a commit in the requested range")
        return None
    return value


def list_range(root, head, excluded):
    result = execute(root, ["git", "rev-list", "--reverse", head, *("^" + item for item in excluded)])
    if result.returncode:
        raise ValueError("Cannot enumerate the requested commit range")
    commits = result.stdout.splitlines()
    if not all(re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", item) for item in commits):
        raise ValueError("Invalid commit identifier in the requested range")
    return commits


def check_commit_list(root, commits):
    invalid = []
    for commit in dict.fromkeys(commits):
        result = execute(root, ["git", "show", "-s", "--format=%B", commit])
        if result.returncode:
            raise ValueError("Cannot read a commit message")
        if not valid_commit_message(result.stdout):
            invalid.append(commit[:7])
    if invalid:
        raise ValueError("invalid commits " + ", ".join(invalid) + "; " + COMMIT_RULE)
    return len(dict.fromkeys(commits))


def check_fast_commit_messages(root, base=None):
    try:
        head = resolve_commit(root, "HEAD", required=base is not None)
        if head is None:
            report("SKIPPED", "commit messages", "no commit history is available")
            return True
        if base is not None:
            baseline = resolve_commit(root, base)
        else:
            baseline = resolve_commit(root, "@{upstream}", required=False) or resolve_commit(root, "refs/remotes/origin/main", required=False)
        commits = list_range(root, head, [baseline] if baseline else [])
        count = check_commit_list(root, commits)
        report("PASS", "commit messages", f"checked {count} commits" + (" since base" if baseline else " in available history"))
        return True
    except (OSError, ValueError) as error:
        report("FAIL", "commit messages", error)
        return False


def check_push_messages(root, remote, lines):
    try:
        commits = []
        remote_excludes = None
        for line in lines:
            parts = line.split()
            if len(parts) != 4:
                raise ValueError("Invalid pre-push reference input")
            _, local_sha, _, remote_sha = parts
            if not all(re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", item) for item in (local_sha, remote_sha)):
                raise ValueError("Invalid pre-push commit identifier")
            if set(local_sha) == {"0"}:
                continue
            head = resolve_commit(root, local_sha)
            if set(remote_sha) == {"0"}:
                if remote_excludes is None:
                    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", remote) or ".." in remote:
                        raise ValueError("Cannot identify the remote for a new branch")
                    refs = execute(root, ["git", "for-each-ref", "--format=%(objectname)", "refs/remotes/" + remote + "/"])
                    if refs.returncode:
                        raise ValueError("Cannot read remote-tracking references")
                    remote_excludes = [resolve_commit(root, value) for value in refs.stdout.splitlines()]
                excluded = remote_excludes
            else:
                excluded = [resolve_commit(root, remote_sha)]
            commits.extend(list_range(root, head, excluded))
        count = check_commit_list(root, commits)
        report("PASS", "push commit messages", f"checked {count} commits")
        return True
    except (OSError, ValueError) as error:
        report("FAIL", "push commit messages", error)
        return False


def static_check(name, check):
    try:
        reason = check()
    except (OSError, ValueError, TypeError, KeyError) as error:
        report("FAIL", name, error)
        return False
    report("PASS", name, reason)
    return True


def test_coverage(root, test_names):
    modules = {".".join(name.split(".")[:2]) for name in test_names}
    found = {f"tests.{path.stem}" for path in (root / "tests").glob("test_*.py")}
    missing = sorted(found - modules)
    if missing:
        raise ValueError("uncovered test modules: " + ", ".join(missing))
    return f"all {len(found)} test modules are in the explicit lists"


def versions(root):
    sources = {}
    for name in ("release.json", "package.json"):
        path = root / name
        if name == "package.json" and not path.exists():
            report("SKIPPED", "package.json version comparison", "private import metadata is absent from this source tree")
            continue
        value = json.loads(path.read_text(encoding="utf-8"))["version"]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must contain a nonempty version")
        sources[name] = value
    # The current build generates its bundle version from release.json. Check
    # an authored plist as well if one is introduced; ignore old build outputs.
    for name in ("app/Info.plist", "Info.plist"):
        path = root / name
        if path.exists():
            with path.open("rb") as handle:
                sources[name] = plistlib.load(handle)["CFBundleShortVersionString"]
    if any(value != sources["release.json"] for value in sources.values()):
        raise ValueError("version mismatch: " + ", ".join(f"{name}={value}" for name, value in sources.items()))
    return sources["release.json"] + " from " + ", ".join(sources)


def public_files(root):
    files = json.loads((root / "public-files.json").read_text(encoding="utf-8"))["files"]
    if not isinstance(files, list) or not all(isinstance(name, str) for name in files):
        raise ValueError("public-files.json must list file paths")
    missing = [name for name in files if not (root / name).resolve().is_relative_to(root)
               or not (root / name).is_file()]
    if missing:
        raise ValueError("missing or out-of-tree public files: " + ", ".join(missing))
    return f"all {len(files)} listed paths exist in the tree"


def public_content(root):
    files = json.loads((root / "public-files.json").read_text(encoding="utf-8"))["files"]
    if not isinstance(files, list) or not all(isinstance(name, str) for name in files):
        raise ValueError("public-files.json must list file paths")
    policy = json.loads((root / "private-paths.json").read_text(encoding="utf-8"))
    terms = policy["terms"]
    allowed_lines = policy["allowedLines"]
    private_patterns = policy["paths"]
    if not isinstance(terms, list) or not all(isinstance(term, str) and term for term in terms):
        raise ValueError("private content terms must be nonempty strings")
    if not isinstance(private_patterns, list) or not all(isinstance(item, str) for item in private_patterns):
        raise ValueError("private paths must be strings")
    if not isinstance(allowed_lines, dict) or not all(
        isinstance(name, str) and isinstance(lines, list) and all(isinstance(line, str) for line in lines)
        for name, lines in allowed_lines.items()
    ):
        raise ValueError("allowedLines must map file paths to lists of exact lines")
    violations = set()
    for name in files:
        path = (root / name).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError("public content path missing or outside source: " + name)
        for number, line in enumerate(path.read_bytes().splitlines(), 1):
            lower = line.lower()
            decoded = line.decode("utf-8", errors="replace")
            for term in terms:
                if term.lower().encode("utf-8") not in lower:
                    continue
                if decoded in allowed_lines.get(name, []):
                    continue
                violations.add(f"{name}:{number}")
            if Path(name).suffix.lower() not in (".md", ".html"):
                continue
            for match in re.finditer(r'''\]\(([^)\s]+)|href\s*=\s*["']([^"']+)["']''', decoded, flags=re.IGNORECASE):
                link = match.group(1) or match.group(2)
                parsed = urlsplit(link)
                if not parsed.path:
                    continue
                link_path = unquote(parsed.path)
                if parsed.scheme or parsed.netloc:
                    target = link_path.lstrip("/")
                elif link_path.startswith("/"):
                    target = link_path.lstrip("/")
                else:
                    target = posixpath.normpath(posixpath.join(posixpath.dirname(name), link_path))
                if any(fnmatchcase(target.lower(), pattern.lower()) or fnmatchcase(target.lower(), "*/" + pattern.lower())
                       for pattern in private_patterns):
                    violations.add(f"{name}:{number}")
    if violations:
        raise ValueError("private references in public files: " + ", ".join(sorted(violations)))
    return f"checked {len(files)} public files and their documentation links"


def private_paths(root):
    patterns = json.loads((root / "private-paths.json").read_text(encoding="utf-8"))["paths"]
    if not isinstance(patterns, list) or not all(isinstance(pattern, str) for pattern in patterns):
        raise ValueError("private-paths.json must list path patterns")
    files = json.loads((root / "public-files.json").read_text(encoding="utf-8"))["files"]
    if not isinstance(files, list) or not all(isinstance(name, str) for name in files):
        raise ValueError("public-files.json must list file paths")
    matches = [name for name in files if any(fnmatchcase(name, pattern) for pattern in patterns)]
    if matches:
        raise ValueError("public entries match private path patterns: " + ", ".join(matches))
    return f"no private path patterns match the {len(files)} public entries"


def documentation(root):
    violations = []
    checked = []
    names = ["docs/development.md"]
    policy = root / "private-paths.json"
    if policy.exists():
        extra = json.loads(policy.read_text(encoding="utf-8")).get("documentationFiles", [])
        if not isinstance(extra, list) or not all(isinstance(name, str) for name in extra):
            raise ValueError("documentationFiles must list paths")
        names.extend(extra)
    else:
        report("SKIPPED", "private documentation", "private content policy is absent from this source export")
    for name in names:
        path = (root / name).resolve()
        if not path.is_relative_to(root):
            raise ValueError("documentation path outside source: " + name)
        if not path.exists() and name != "docs/development.md":
            report("SKIPPED", "documentation " + name, "optional document is absent")
            continue
        checked.append(name)
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"\bpython[\w.]*\b.*\bunittest\b|\bunittest\s+(?:discover\b|-)", line):
                violations.append(f"{name}:{number}")
    if violations:
        raise ValueError("use python3 scripts/verify.py instead of unittest commands: " + ", ".join(violations))
    return "no direct test commands in " + ", ".join(checked)


def release_context(root):
    passed = True
    for name, command in (
        ("tested commit", ["git", "rev-parse", "HEAD"]),
        ("working tree", ["git", "status", "--porcelain", "--untracked-files=all"]),
    ):
        try:
            result = execute(root, command)
        except OSError as error:
            report("FAIL", name, error)
            passed = False
            continue
        if result.returncode:
            report("FAIL", name, f"exit {result.returncode}; {result.stderr}")
            passed = False
        else:
            if name == "tested commit":
                reason = result.stdout.strip()
            elif result.stdout:
                reason = "dirty (includes untracked files)"
            else:
                reason = "clean"
            report("PASS", name, reason)
    return passed


def main(argv=None, *, root=ROOT, test_names=TEST_NAMES):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    fast = modes.add_parser("fast", help="run whitespace, tests, and static checks")
    fast.add_argument("--base", help="exclude commits reachable from this base")
    release = modes.add_parser("release", help="run fast checks, build, and verify the signature; list separate acceptance checks")
    release.add_argument("--base", help="exclude commits reachable from this base")
    modes.add_parser("install-hook", help="set this clone's local core.hooksPath to .githooks")
    message = modes.add_parser("commit-msg", help="check a commit message file")
    message.add_argument("message_file", type=Path)
    push = modes.add_parser("check-push", help="check commits named by pre-push input")
    push.add_argument("remote")
    args = parser.parse_args(argv)
    root = Path(root).resolve()
    if args.mode == "commit-msg":
        return commit_message_file(args.message_file)
    if args.mode == "check-push":
        return 0 if check_push_messages(root, args.remote, sys.stdin) else 1
    if args.mode == "install-hook":
        hooks = [root / ".githooks" / name for name in ("pre-push", "commit-msg")]
        if not all(path.is_file() and os.access(path, os.X_OK) for path in hooks):
            report("FAIL", "install hook", "both executable hook files must be present")
            return 1
        return 0 if command_check(root, "install hook", ["git", "config", "--local", "core.hooksPath", ".githooks"]) else 1

    passed = release_context(root) if args.mode == "release" else True
    passed = check_fast_commit_messages(root, args.base) and passed
    passed = command_check(root, "whitespace", ["git", "diff", "--check"]) and passed
    passed = command_check(root, "test suite", [sys.executable, "-B", "-m", "unittest", "-q", *test_names],
                           isolated_home=True) and passed
    for name, check in (
        ("test coverage", lambda: test_coverage(root, test_names)),
        ("versions", lambda: versions(root)),
        ("public files", lambda: public_files(root)),
        ("documentation", lambda: documentation(root)),
    ):
        passed = static_check(name, check) and passed
    if (root / "private-paths.json").exists():
        passed = static_check("public content", lambda: public_content(root)) and passed
        passed = static_check("private paths", lambda: private_paths(root)) and passed
    else:
        report("SKIPPED", "public content", "private content policy is absent from this source export")
        report("SKIPPED", "private paths", "private-paths.json is absent from this source tree; public exports omit the private policy")

    if args.mode == "release":
        if not passed:
            report("SKIPPED", "native build", "fast checks or release context failed")
            report("SKIPPED", "signature", "no verified build from this run")
        elif sys.platform != "darwin":
            report("SKIPPED", "native build", "requires macOS and Xcode Command Line Tools")
            report("SKIPPED", "signature", "requires a macOS build")
        else:
            passed = command_check(root, "native build", ["./build.command"])
            if passed:
                passed = command_check(root, "signature", ["codesign", "--verify", "--deep", "--strict", "Igloo.app"])
            else:
                report("SKIPPED", "signature", "native build failed; an older bundle is not evidence")
        if passed:
            passed = rosetta_check(root) and passed
        else:
            report("SKIPPED", "Rosetta", "fast checks, release context, native build, or signature failed")
        for name in MANUAL_CHECKS:
            report("SKIPPED", name, "requires separate acceptance; this command does not exercise it")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
