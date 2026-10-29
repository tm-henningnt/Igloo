#!/usr/bin/env python3
"""Preflight, prepare and stage reviewed releases for publication."""
import argparse
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import plistlib
import re
import shutil
import subprocess
import tempfile

PUBLIC_REPO = "tm-henningnt/Igloo"
PRIVATE_PATHS_FILE = Path(__file__).resolve().parents[1] / "private-paths.json"
MAX_ZIP = 512 * 1024 * 1024
VERSION = re.compile(r"(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})(\.(0|[1-9][0-9]{0,5}))?")
SECRET_PATTERNS = [
    re.compile(rb"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(rb"github_pat_[A-Za-z0-9_]{35,}"),
    re.compile(rb"AKIA[A-Z0-9]{16}"),
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"/Users/(?!demo/)[A-Za-z0-9._-]+/"),
]

def run(args, cwd=None, env=None, check=True):
    result = subprocess.run([str(a) for a in args], cwd=cwd, env=env, capture_output=True)
    if check and result.returncode:
        raise ValueError("Command failed: " + str(args[0]) + ". Review its result in Terminal before continuing.")
    return result

def output(args, cwd=None):
    return run(args, cwd).stdout.decode().strip()

def read_json(path):
    return json.loads(Path(path).read_text())

def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2) + "\n")

def safe_name(name):
    path = PurePosixPath(name)
    if not name or path.is_absolute() or ".." in path.parts or any(
        part.startswith(".env") or part in {".git", ".igloo", "state", "__pycache__"} or part.endswith(".app")
        for part in path.parts
    ) or "\\" in name:
        raise ValueError("Unsafe publication path.")
    return name

def scan(name, data):
    safe_name(name)
    if any(pattern.search(data) for pattern in SECRET_PATTERNS):
        raise ValueError("A private path or credential pattern was found in " + name + ".")
    if name.endswith((".pem", ".key", ".p12", ".pfx")):
        raise ValueError("Private-key file types cannot be published.")

def metadata(value):
    if not isinstance(value, dict) or not VERSION.fullmatch(value.get("version", "")) or \
       type(value.get("build")) is not int or value["build"] < 1 or \
       value.get("repository") != PUBLIC_REPO or \
       not VERSION.fullmatch(value.get("minimumSystemVersion", "")):
        raise ValueError("Invalid release.json.")
    return value

def preflight(repo, unnotarized=False):
    """Read local and remote prerequisites without creating public resources."""
    repo = Path(repo).resolve()
    checks = []

    def check(name, action):
        try:
            message = action()
            passed = True
        except (ValueError, OSError, KeyError, TypeError, AttributeError) as error:
            message = str(error)
            passed = False
        checks.append({"check": name, "passed": passed, "message": message})

    def require(condition, message):
        if not condition:
            raise ValueError(message)

    def remote(args):
        result = run(args, check=False)
        require(result.returncode == 0, "Remote check unreadable; verify GitHub access before continuing.")
        return result.stdout.decode()

    def clean_tree():
        require(not output(["git", "status", "--porcelain"], repo), "Working tree is dirty; commit or set aside local changes.")
        return "Clean."

    def release_metadata():
        value = metadata(read_json(repo / "release.json"))
        return "Version " + value["version"] + ", build " + str(value["build"]) + "."

    def progression():
        candidate = metadata(read_json(repo / "release.json"))
        latest = run(["gh", "release", "view", "--repo", PUBLIC_REPO,
                      "--json", "tagName,isDraft,isPrerelease"], check=False)
        if latest.returncode:
            # An empty successful list distinguishes no stable release from an unreadable latest release.
            published_tags = remote(["gh", "api", "--method", "GET",
                                     "repos/" + PUBLIC_REPO + "/releases?per_page=100", "--paginate",
                                     "--jq", ".[] | select(.draft == false and .prerelease == false) | .tag_name"])
            require(not published_tags.strip(), "Latest published release unreadable.")
            return "No published release exists (drafts and prereleases excluded)."
        latest = json.loads(latest.stdout)
        require(latest.get("isDraft") is False and latest.get("isPrerelease") is False,
                "Expected the latest stable published release.")
        tag = latest["tagName"]
        require(isinstance(tag, str) and tag.startswith("v") and VERSION.fullmatch(tag[1:]),
                "Invalid published release tag.")
        previous = metadata(json.loads(remote(["gh", "release", "download", tag, "--repo", PUBLIC_REPO,
                                               "--pattern", "igloo-update.json", "--output", "-"])))
        require(tag == "v" + previous["version"], "The published release and update manifest versions disagree.")
        def version_parts(value):
            parts = [int(part) for part in value.split(".")]
            return tuple(parts + [0] * (3 - len(parts)))
        require(version_parts(candidate["version"]) > version_parts(previous["version"]) and
                candidate["build"] > previous["build"],
                "Version and build must both be greater than the latest published release.")
        return "Version and build exceed " + tag + " (build " + str(previous["build"]) + ")."

    def notes():
        value = metadata(read_json(repo / "release.json"))
        path = repo / "release-notes" / (value["version"] + ".md")
        require(path.is_file(), "Versioned release notes are missing.")
        if unnotarized:
            require("not notarized" in path.read_text().lower(), "Release notes must say the app is not notarized.")
        return "Versioned notes exist" + (" and state the app is not notarized." if unnotarized else ".")

    def allowlist():
        names = read_json(repo / "public-files.json").get("files")
        require(isinstance(names, list) and names and len(names) == len(set(names)),
                "Choose an explicit, unique public file list.")
        for name in names:
            safe_name(name)
            path = repo / name
            require(path.is_file() and not path.is_symlink(), "An allowlisted path is missing or is not an ordinary file.")
        return "All " + str(len(names)) + " allowlisted files exist."

    def license_file():
        path = repo / "LICENSE"
        require(path.is_file() and not path.is_symlink(), "LICENSE is missing or is not an ordinary file.")
        return "LICENSE exists."

    def public_repository():
        value = json.loads(remote(["gh", "repo", "view", PUBLIC_REPO, "--json", "nameWithOwner,isPrivate,defaultBranchRef"]))
        require(value.get("nameWithOwner") == PUBLIC_REPO and value.get("isPrivate") is False,
                "The public Igloo repository must already exist and be public.")
        require((value.get("defaultBranchRef") or {}).get("name") == "main", "The public repository must use main.")
        return "Expected public repository uses main."

    def authentication():
        require(run(["gh", "auth", "status", "--hostname", "github.com"], check=False).returncode == 0,
                "gh is not authenticated to GitHub, or authentication is unreadable.")
        return "gh is authenticated to GitHub."

    def pages():
        endpoint = "repos/" + PUBLIC_REPO + "/environments/github-pages"
        environment = json.loads(remote(["gh", "api", "--method", "GET", endpoint]))
        policy = environment["deployment_branch_policy"]
        if policy is None:
            return "All branches and tags are allowed, including main and v*."
        require(policy.get("custom_branch_policies") is True and policy.get("protected_branches") is False,
                "Pages must allow main and v* tags; protected-branch-only policy cannot assure tag deployments.")
        pages = json.loads(remote(["gh", "api", "--method", "GET", endpoint + "/deployment-branch-policies?per_page=100",
                                  "--paginate", "--slurp"]))
        rules = [rule for page in pages for rule in page["branch_policies"]]
        branches = [rule["name"] for rule in rules if rule.get("type") == "branch"]
        tags = [rule["name"] for rule in rules if rule.get("type") == "tag"]
        require(any(fnmatch.fnmatchcase("main", name) for name in branches) and any(name in {"v*", "*"} for name in tags),
                "Pages deployment rules must allow branch main and tags matching v*.")
        return "Deployment rules allow branch main and v* tags."

    def tool_present(tool):
        require(shutil.which(tool) is not None, "Required tool is missing: " + tool + ".")
        return "Available."

    check("working tree", clean_tree)
    check("release metadata", release_metadata)
    check("release progression", progression)
    check("release notes", notes)
    check("public allowlist", allowlist)
    check("source license", license_file)
    check("public repository", public_repository)
    check("GitHub authentication", authentication)
    check("Pages environment", pages)
    for tool in ("git", "gh", "codesign", "xcrun", "python3"):
        check("tool " + tool, lambda tool=tool: tool_present(tool))
    return {"passed": all(item["passed"] for item in checks), "checks": checks}

def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            h.update(block)
    return h.hexdigest()

def inventory(folder):
    folder = Path(folder)
    result = {}
    for path in sorted(folder.rglob("*")):
        if path.is_symlink():
            raise ValueError("Publication source must not contain symbolic links.")
        if path.is_file():
            name = path.relative_to(folder).as_posix()
            scan(name, path.read_bytes())
            result[name] = digest(path)
    return result

def reject_private_paths(names, policy_path):
    try:
        policy = read_json(policy_path)
    except FileNotFoundError:
        return
    patterns = policy.get("paths") if isinstance(policy, dict) else None
    if not isinstance(patterns, list) or any(not isinstance(pattern, str) or not pattern for pattern in patterns):
        raise ValueError("Invalid private-paths.json.")
    for name in names:
        if any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns):
            raise ValueError("A private development path was found in the export: " + name + ".")

def export_snapshot(repo, destination, ref="HEAD"):
    repo, destination = Path(repo), Path(destination)
    if output(["git", "status", "--porcelain"], repo):
        raise ValueError("Commit or set aside local changes before preparing a release.")
    commit = output(["git", "rev-parse", "--verify", ref + "^{commit}"], repo)
    policy = json.loads(run(["git", "show", commit + ":public-files.json"], repo).stdout)
    names = policy.get("files")
    if not isinstance(names, list) or not names or len(names) != len(set(names)):
        raise ValueError("Choose an explicit, unique public file list.")
    reject_private_paths(names, repo / "private-paths.json")
    destination.mkdir(parents=True, exist_ok=False)
    for name in names:
        safe_name(name)
        mode = output(["git", "ls-tree", commit, "--", name], repo)
        if not mode or mode.split()[0] not in {"100644", "100755"} or mode.split()[1] != "blob":
            raise ValueError("Every publication file must be a tracked ordinary file: " + name)
        data = run(["git", "show", commit + ":" + name], repo).stdout
        scan(name, data)
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(0o755 if mode.startswith("100755") else 0o644)
    return commit, inventory(destination)

def manifest_for(release, architecture, archive, runtime="bundled"):
    metadata(release)
    expected = "Igloo-" + release["version"] + "-" + architecture + ("-system-python" if runtime == "system" else "") + ".zip"
    if runtime not in {"bundled", "system"} or architecture not in {"arm64", "x86_64", "universal"} or Path(archive).name != expected:
        raise ValueError("Invalid architecture or archive name.")
    size = Path(archive).stat().st_size
    if not 0 < size <= MAX_ZIP:
        raise ValueError("The release archive is empty or too large.")
    tag = "v" + release["version"]
    return {
        "schemaVersion": 1, "repository": PUBLIC_REPO, "tag": tag,
        "version": release["version"], "build": release["build"],
        "minimumSystemVersion": release["minimumSystemVersion"],
        "assets": [{"architecture": architecture, "runtime": runtime, "filename": expected, "size": size,
                    "sha256": digest(archive),
                    "url": "https://github.com/" + PUBLIC_REPO + "/releases/download/" + tag + "/" + expected}],
    }

def zip_app(app, archive):
    run(["/usr/bin/ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", app, archive])
    # Igloo 0.3.0 and 0.3.1 reject the bare __MACOSX/ directory entry that ditto writes.
    # Exit code 12 means the archive has no such entry.
    if run(["/usr/bin/zip", "-q", "-d", archive, "__MACOSX/"], check=False).returncode not in (0, 12):
        raise ValueError("Command failed: zip. Review its result in Terminal before continuing.")

def build_dmg(app, destination, source):
    try:
        import dmgbuild
    except ImportError:
        raise ValueError("Install scripts/release-requirements.txt in the release build environment.")
    source = Path(source)
    with tempfile.TemporaryDirectory(prefix="igloo-dmg-art-") as temporary:
        temporary = Path(temporary)
        for scale in (1, 2):
            name = "dmg-background.png" if scale == 1 else "dmg-background@2x.png"
            run(["/usr/bin/sips", "-z", str(480 * scale), str(720 * scale),
                 source / "assets/InstallerBackground.png", "--out", temporary / name])
        dmgbuild.build_dmg(str(destination), "Install Igloo", settings={
            "format": "UDZO", "files": [str(app)], "symlinks": {"Applications": "/Applications"},
            "icon": str(Path(app) / "Contents/Resources/AppIcon.icns"),
            "background": str(temporary / "dmg-background.png"),
            "window_rect": ((200, 200), (720, 480)), "default_view": "icon-view",
            "icon_size": 112, "text_size": 13,
            "icon_locations": {"Igloo.app": (200, 245), "Applications": (520, 245)},
            "show_toolbar": False, "show_status_bar": False, "show_sidebar": False,
        })

def notarize(path, profile):
    result = run(["/usr/bin/xcrun", "notarytool", "submit", path,
                  "--keychain-profile", profile, "--wait", "--output-format", "json"])
    if json.loads(result.stdout).get("status") != "Accepted":
        raise ValueError("Apple did not accept the distribution for notarization.")


def verify_app(app, release, architecture, distribution=False, runtime="bundled"):
    app = Path(app)
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    if (info.get("CFBundleIdentifier"), info.get("CFBundleShortVersionString"), info.get("CFBundleVersion")) != \
       ("local.igloo", release["version"], str(release["build"])):
        raise ValueError("The built app does not match the reviewed version.")
    if info.get("IglooRuntime") != runtime:
        raise ValueError("The app runtime variant does not match its download.")
    if runtime == "bundled" and not (app / "Contents/Resources/python").is_dir():
        raise ValueError("The bundled Python runtime is missing.")
    if runtime == "system" and (app / "Contents/Resources/python").exists():
        raise ValueError("The system-Python build still contains a runtime.")
    expected = {"arm64", "x86_64"} if architecture == "universal" else {architecture}
    actual = set(output(["/usr/bin/lipo", "-archs", app / "Contents/MacOS/Igloo"]).split())
    if actual != expected:
        raise ValueError("The archive does not contain its advertised architecture.")
    run(["/usr/bin/codesign", "--verify", "--deep", "--strict", app])
    if distribution:
        details = run(["/usr/bin/codesign", "-dv", "--verbose=4", app]).stderr.decode()
        if "Authority=Developer ID Application:" not in details:
            raise ValueError("Public downloads require Developer ID signing and notarization.")
        run(["/usr/bin/xcrun", "stapler", "validate", app])
        run(["/usr/sbin/spctl", "--assess", "--type", "execute", app])

def prepare(repo, destination, notes, architecture="universal", identity=None, profile=None, runtime="both", unnotarized=False):
    repo, destination, notes = Path(repo).resolve(), Path(destination).resolve(), Path(notes).resolve()
    if destination == repo or repo in destination.parents or destination in repo.parents:
        raise ValueError("Prepare releases outside the development checkout.")
    if destination.exists():
        raise ValueError("The prepared-release folder already exists.")
    note_data = notes.read_bytes()
    scan("release-notes.md", note_data)
    if not note_data.strip() or len(note_data) > 30000:
        raise ValueError("Write release notes of at most 30 KB.")
    if bool(identity) != bool(profile):
        raise ValueError("Provide both a signing identity and a notarization keychain profile.")
    if unnotarized and (identity or profile):
        raise ValueError("Choose unnotarized distribution or Developer ID signing, not both.")
    if unnotarized and "not notarized" not in note_data.decode().lower():
        raise ValueError("Unnotarized release notes must clearly say the app is not notarized.")
    destination.mkdir(mode=0o700)
    commit, files = export_snapshot(repo, destination / "source")
    release = metadata(read_json(destination / "source/release.json"))
    build_env = dict(os.environ, IGLOO_SIGNING_IDENTITY=identity or "-", IGLOO_RELEASE_BUILD="1")
    run(["/bin/zsh", "build.command", architecture], destination / "source", env=build_env)
    build = destination / "build"
    build.mkdir()
    built_app = build / "Igloo.app"
    shutil.move(str(destination / "source/Igloo.app"), built_app)
    assets = destination / "assets"
    assets.mkdir()
    variants = ["bundled", "system"] if runtime == "both" else [runtime]
    manifest = None
    images = []
    for variant in variants:
        app = build / variant / "Igloo.app"
        app.parent.mkdir()
        shutil.copytree(built_app, app, symlinks=True)
        if variant == "system":
            shutil.rmtree(app / "Contents/Resources/python")
            info_path = app / "Contents/Info.plist"
            info = plistlib.loads(info_path.read_bytes())
            info["IglooRuntime"] = "system"
            info_path.write_bytes(plistlib.dumps(info))
            signing = ["/usr/bin/codesign", "--force", "--sign", identity or "-"]
            if identity: signing += ["--options", "runtime", "--timestamp"]
            run(signing + [app])
        verify_app(app, release, architecture, runtime=variant)
        stem = "Igloo-" + release["version"] + "-" + architecture + ("-system-python" if variant == "system" else "")
        archive = assets / (stem + ".zip")
        zip_app(app, archive)
        if profile:
            notarize(archive, profile)
            run(["/usr/bin/xcrun", "stapler", "staple", app])
            verify_app(app, release, architecture, distribution=True, runtime=variant)
            archive.unlink()
            zip_app(app, archive)
        disk_image = assets / (stem + ".dmg")
        build_dmg(app, disk_image, destination / "source")
        if profile:
            run(["/usr/bin/codesign", "--sign", identity, "--timestamp", disk_image])
            notarize(disk_image, profile)
            run(["/usr/bin/xcrun", "stapler", "staple", disk_image])
        part = manifest_for(release, architecture, archive, variant)
        if manifest is None: manifest = part
        else: manifest["assets"].extend(part["assets"])
        images.append({"filename": disk_image.name, "sha256": digest(disk_image),
                       "size": disk_image.stat().st_size, "runtime": variant})
    shutil.rmtree(built_app)
    if inventory(destination / "source") != files:
        raise ValueError("The source snapshot changed during the build.")
    (destination / "notes.md").write_bytes(note_data)
    write_json(assets / "igloo-update.json", manifest)
    write_json(destination / "review.json", {
        "sourceCommit": commit, "files": files, "manifestHash": digest(assets / "igloo-update.json"),
        "notesHash": digest(destination / "notes.md"), "distributionReady": bool(profile),
        "unnotarizedDistribution": bool(unnotarized),
        "diskImages": images,
    })
    return {"prepared": str(destination), "version": release["version"], "sourceFiles": len(files),
            "distributionReady": bool(profile), "unnotarizedDistribution": bool(unnotarized),
            "message": "Prepared locally. Nothing was published."}

def validate_prepared(folder):
    folder = Path(folder).resolve()
    review = read_json(folder / "review.json")
    source = folder / "source"
    reject_private_paths(review["files"], PRIVATE_PATHS_FILE)
    reject_private_paths((path.relative_to(source).as_posix() for path in sorted(source.rglob("*"))), PRIVATE_PATHS_FILE)
    files = inventory(source)
    if files != review["files"] or digest(folder / "notes.md") != review["notesHash"] or \
       digest(folder / "assets/igloo-update.json") != review["manifestHash"]:
        raise ValueError("The prepared release changed. Prepare a fresh review.")
    release = metadata(read_json(folder / "source/release.json"))
    manifest = read_json(folder / "assets/igloo-update.json")
    assets = manifest.get("assets", [])
    images = review.get("diskImages", [])
    if not 1 <= len(assets) <= 2 or len(images) != len(assets):
        raise ValueError("Expected one download for each reviewed runtime variant.")
    keys = [(a["architecture"], a["runtime"]) for a in assets]
    if len(set(keys)) != len(keys):
        raise ValueError("Duplicate update variant.")
    expected_manifest = None
    for asset in assets:
        safe_name(asset["filename"])
        archive = folder / "assets" / asset["filename"]
        part = manifest_for(release, asset["architecture"], archive, asset["runtime"])
        if archive.is_symlink():
            raise ValueError("The release download is a symbolic link.")
        if expected_manifest is None: expected_manifest = part
        else: expected_manifest["assets"].extend(part["assets"])
        matching = [i for i in images if i["runtime"] == asset["runtime"]]
        if len(matching) != 1:
            raise ValueError("Missing reviewed installer.")
        image = matching[0]
        expected_image = asset["filename"].removesuffix(".zip") + ".dmg"
        if image["filename"] != expected_image:
            raise ValueError("Invalid installer filename.")
        disk_image = folder / "assets" / expected_image
        if disk_image.is_symlink() or disk_image.stat().st_size != image["size"] or digest(disk_image) != image["sha256"]:
            raise ValueError("The reviewed installer changed.")
    if expected_manifest != manifest:
        raise ValueError("The release archive or manifest changed.")
    scan("notes.md", (folder / "notes.md").read_bytes())
    return release, manifest, review

def public_commit_environment():
    identity = json.loads(output(["gh", "api", "user", "--jq", "{login,id}"]))
    login, identifier = identity.get("login"), identity.get("id")
    if not isinstance(login, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", login) or \
       type(identifier) is not int or identifier < 1:
        raise ValueError("The public commit identity could not be verified.")
    email = str(identifier) + "+" + login + "@users.noreply.github.com"
    environment = dict(os.environ, GIT_AUTHOR_NAME=login, GIT_COMMITTER_NAME=login,
                       GIT_AUTHOR_EMAIL=email, GIT_COMMITTER_EMAIL=email)
    environment.pop("GIT_AUTHOR_DATE", None)
    environment.pop("GIT_COMMITTER_DATE", None)
    return environment

def sync_public(source, checkout, tag, commit_env=None):
    """Only the public checkout's own history is used, never the private repository."""
    source, checkout = Path(source), Path(checkout)
    if (source / ".git").exists():
        raise ValueError("A source snapshot must not contain Git history.")
    if output(["git", "status", "--porcelain"], checkout):
        raise ValueError("The public staging checkout is not clean.")
    if output(["git", "symbolic-ref", "--short", "HEAD"], checkout) != "main":
        raise ValueError("The public repository must use main.")
    if run(["git", "rev-parse", "--verify", "refs/tags/" + tag], checkout, check=False).returncode == 0:
        raise ValueError("That public release tag already exists.")
    run(["git", "rm", "-r", "--ignore-unmatch", "--", "."], checkout)
    for path in source.rglob("*"):
        if path.is_symlink():
            raise ValueError("Symbolic link in source snapshot.")
        relative = path.relative_to(source)
        if path.is_file():
            target = checkout / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    run(["git", "add", "--all"], checkout)
    run(["git", "commit", "-m", "Igloo " + tag.removeprefix("v")], checkout, env=commit_env)
    run(["git", "tag", tag], checkout)

def stage(folder, accept=False, allow_unnotarized=False):
    if not accept:
        raise ValueError("Review the prepared source, notes and assets, then pass --accept-publication.")
    folder = Path(folder).resolve()
    release, manifest, review = validate_prepared(folder)
    unnotarized = review.get("unnotarizedDistribution", False)
    if not review["distributionReady"]:
        if not unnotarized:
            raise ValueError("This is a local preview. Prepare a signed build or explicitly prepare --unnotarized.")
        if not allow_unnotarized:
            raise ValueError("Unnotarized publication requires --allow-unnotarized after reviewing the first-launch instructions.")
        if "not notarized" not in (folder / "notes.md").read_text().lower():
            raise ValueError("The release notes must clearly say the app is not notarized.")
    elif unnotarized:
        raise ValueError("The reviewed signing modes conflict. Prepare a fresh release.")
    for asset in manifest["assets"]:
        verify_app(folder / "build" / asset["runtime"] / "Igloo.app", release,
                   asset["architecture"], distribution=review["distributionReady"], runtime=asset["runtime"])
    for image in review["diskImages"]:
        disk_image = folder / "assets" / image["filename"]
        if review["distributionReady"]:
            run(["/usr/bin/codesign", "--verify", disk_image])
            run(["/usr/bin/xcrun", "stapler", "validate", disk_image])
    remote = json.loads(output(["gh", "repo", "view", PUBLIC_REPO, "--json", "nameWithOwner,isPrivate,defaultBranchRef"]))
    if remote.get("nameWithOwner") != PUBLIC_REPO or remote.get("isPrivate") is not False:
        raise ValueError("The public Igloo repository must already exist and be public. No visibility was changed.")
    branch = remote.get("defaultBranchRef")
    if branch and branch.get("name") not in {"", "main"}:
        raise ValueError("The public repository must use main.")
    tag = manifest["tag"]
    if run(["gh", "release", "view", tag, "--repo", PUBLIC_REPO], check=False).returncode == 0:
        raise ValueError("That release already exists; it was not overwritten.")
    commit_env = public_commit_environment()
    with tempfile.TemporaryDirectory(prefix="igloo-public-") as temporary:
        checkout = Path(temporary) / "public"
        run(["git", "clone", "--", "https://github.com/" + PUBLIC_REPO + ".git", checkout])
        if not output(["git", "branch", "--show-current"], checkout) == "main":
            if run(["git", "rev-parse", "--verify", "HEAD"], checkout, check=False).returncode == 0:
                raise ValueError("The public repository must use main.")
            run(["git", "symbolic-ref", "HEAD", "refs/heads/main"], checkout)
        sync_public(folder / "source", checkout, tag, commit_env=commit_env)
        run(["git", "push", "--atomic", "--no-follow-tags", "--recurse-submodules=no", "origin", "HEAD:refs/heads/main", "refs/tags/" + tag], checkout)
    # The uploaded release remains a draft pending publication approval.
    draft = run(["gh", "release", "create", tag, "--repo", PUBLIC_REPO, "--verify-tag", "--draft",
         "--title", "Igloo " + release["version"], "--notes-file", folder / "notes.md",
         folder / "assets/igloo-update.json",
         *[folder / "assets" / a["filename"] for a in manifest["assets"]],
         *[folder / "assets" / i["filename"] for i in review["diskImages"]]])
    return {"tag": tag, "draftUrl": draft.stdout.decode().strip(),
            "nextStep": "Review the draft and request publication approval."}

def publish(folder=None, accept=False, allow_unnotarized=False):
    raise ValueError("Publication is disabled here. Stage a draft and use the approved publication process.")

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("preflight", help="Check release prerequisites without publishing or changing remote settings.")
    p.add_argument("--unnotarized", action="store_true", help="Require versioned notes to state the app is not notarized.")
    p = commands.add_parser("prepare", help="Prepare local artifacts for review.")
    p.add_argument("--output", required=True)
    p.add_argument("--notes", required=True)
    p.add_argument("--architecture", choices=["arm64", "x86_64", "universal"], default="universal")
    p.add_argument("--runtime", choices=["bundled", "system", "both"], default="both")
    p.add_argument("--signing-identity")
    p.add_argument("--notary-profile")
    p.add_argument("--unnotarized", action="store_true", help="Prepare an explicitly labeled public build without Developer ID signing or notarization.")
    p = commands.add_parser("stage", help="Push the reviewed snapshot and upload a draft for Owner approval.")
    p.add_argument("folder")
    p.add_argument("--accept-publication", action="store_true")
    p.add_argument("--allow-unnotarized", action="store_true", help="Accept the reviewed unnotarized distribution and its documented macOS approval step.")
    p = commands.add_parser("publish", help="Disabled: publication is handled separately.")
    p.add_argument("folder", nargs="?")
    p.add_argument("--accept-publication", action="store_true")
    p.add_argument("--allow-unnotarized", action="store_true")
    args = parser.parse_args()
    try:
        repo = Path(__file__).resolve().parent.parent
        if args.command == "preflight":
            result = preflight(repo, unnotarized=args.unnotarized)
            for item in result["checks"]:
                print(("PASS " if item["passed"] else "FAIL ") + item["check"] + ": " + item["message"])
            if not result["passed"]:
                parser.exit(1)
            return
        if args.command == "prepare":
            result = prepare(repo, args.output, args.notes, args.architecture, args.signing_identity,
                             args.notary_profile, args.runtime, args.unnotarized)
        elif args.command == "stage":
            result = stage(args.folder, args.accept_publication, args.allow_unnotarized)
        else:
            result = publish()
        print(json.dumps(result))
    except (ValueError, OSError, KeyError, TypeError) as error:
        parser.exit(1, "Stopped: " + str(error) + "\n")

if __name__ == "__main__":
    main()
