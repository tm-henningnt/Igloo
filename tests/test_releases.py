"""Release tests use disposable local repositories; no public resources are created."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("release_tools", Path(__file__).resolve().parents[1] / "scripts/release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)

class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "private"
        self.repo.mkdir()
        self.git(self.repo, "init", "-q", "-b", "main")
        self.configure(self.repo)
        self.info = {"version": "0.1.0", "build": 13, "minimumSystemVersion": "14.0", "repository": release.PUBLIC_REPO}
        (self.repo / "release.json").write_text(json.dumps(self.info))
        (self.repo / "README.md").write_text("Public source")
        (self.repo / ".env").write_text("private history")
        (self.repo / "public-files.json").write_text(json.dumps({"files": ["README.md", "release.json", "public-files.json"]}))
        self.commit(self.repo)
        (self.repo / ".env").write_text("another private revision")
        self.commit(self.repo)
    def tearDown(self):
        self.temp.cleanup()
    def git(self, cwd, *args, check=True):
        result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
        if check:
            self.assertEqual(result.returncode, 0, result.stderr)
        return result
    def configure(self, repo):
        self.git(repo, "config", "user.name", "Release Fixture")
        self.git(repo, "config", "user.email", "release@example.invalid")
    def commit(self, repo):
        self.git(repo, "add", "--all")
        self.git(repo, "commit", "-qm", "fixture")
    def exported(self, name="snapshot"):
        destination = self.root / name
        release.export_snapshot(self.repo, destination)
        return destination
    def prepared(self):
        folder = self.root / "prepared"
        folder.mkdir()
        release.export_snapshot(self.repo, folder / "source")
        assets = folder / "assets"
        assets.mkdir()
        archive = assets / "Igloo-0.1.0-universal.zip"
        archive.write_bytes(b"fixture archive")
        image = assets / "Igloo-0.1.0-universal.dmg"
        image.write_bytes(b"fixture disk image")
        manifest = release.manifest_for(self.info, "universal", archive)
        release.write_json(assets / "igloo-update.json", manifest)
        (folder / "notes.md").write_text("A reviewed release.")
        review = {"sourceCommit": self.git(self.repo, "rev-parse", "HEAD").stdout.strip(),
                  "files": release.inventory(folder / "source"),
                  "manifestHash": release.digest(assets / "igloo-update.json"),
                  "notesHash": release.digest(folder / "notes.md"), "distributionReady": False,
                  "diskImages": [{"filename": image.name, "sha256": release.digest(image), "size": image.stat().st_size, "runtime": "bundled"}]}
        release.write_json(folder / "review.json", review)
        return folder
    def test_snapshot_has_current_allowlisted_files_without_private_history(self):
        snapshot = self.exported()
        self.assertEqual(set(release.inventory(snapshot)), {"README.md", "release.json", "public-files.json"})
        self.assertFalse((snapshot / ".git").exists())
        self.assertFalse((snapshot / ".env").exists())
        self.assertEqual((snapshot / "README.md").read_text(), "Public source")
    def test_dirty_checkout_is_refused(self):
        (self.repo / "README.md").write_text("unreviewed")
        with self.assertRaisesRegex(ValueError, "Commit"):
            self.exported()
    def test_symlink_in_public_policy_is_refused(self):
        (self.repo / "leak").symlink_to(".env")
        (self.repo / "public-files.json").write_text(json.dumps({"files": ["leak"]}))
        self.commit(self.repo)
        with self.assertRaisesRegex(ValueError, "ordinary"):
            self.exported()
    def test_private_path_and_key_patterns_stop_export(self):
        for content in [b"/Users/" + b"someone/Secrets/", b"ghp_" + b"x" * 40,
                        b"-----BEGIN " + b"PRIVATE KEY-----"]:
            with self.subTest(content=content[:8]):
                with self.assertRaises(ValueError):
                    release.scan("README.md", content)
    def test_path_traversal_and_sensitive_filenames_are_refused(self):
        for name in ["../escape", "/tmp/escape", ".git/config", "nested/.env", "Igloo.app/file"]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                release.safe_name(name)
    def test_manifest_uses_exact_repository_version_size_and_digest(self):
        folder = self.prepared()
        value = release.read_json(folder / "assets/igloo-update.json")
        asset = value["assets"][0]
        self.assertEqual(asset["sha256"], hashlib.sha256(b"fixture archive").hexdigest())
        self.assertEqual(asset["size"], len(b"fixture archive"))
        self.assertEqual(asset["url"], "https://github.com/tm-henningnt/Igloo/releases/download/v0.1.0/Igloo-0.1.0-universal.zip")
        self.assertEqual(release.validate_prepared(folder)[0], self.info)
    def test_changed_source_notes_zip_or_dmg_invalidate_review(self):
        for name in ["source/README.md", "notes.md", "assets/Igloo-0.1.0-universal.zip", "assets/Igloo-0.1.0-universal.dmg"]:
            with self.subTest(name=name):
                folder = self.prepared()
                (folder / name).write_bytes(b"changed")
                with self.assertRaises(ValueError):
                    release.validate_prepared(folder)
                import shutil
                shutil.rmtree(folder)
    def test_unlisted_source_file_invalidates_review(self):
        folder = self.prepared()
        (folder / "source/local.txt").write_text("unreviewed")
        with self.assertRaises(ValueError):
            release.validate_prepared(folder)
    def test_preview_cannot_publish_and_publication_requires_acceptance(self):
        folder = self.prepared()
        with patch.object(release, "run") as execute:
            with self.assertRaisesRegex(ValueError, "accept-publication"):
                release.publish(folder)
            with self.assertRaisesRegex(ValueError, "local preview"):
                release.publish(folder, accept=True)
            execute.assert_not_called()
    def test_unnotarized_publication_requires_preparation_and_explicit_opt_in(self):
        folder = self.prepared()
        with patch.object(release, "run") as execute:
            with self.assertRaisesRegex(ValueError, "local preview"):
                release.publish(folder, accept=True, allow_unnotarized=True)
            execute.assert_not_called()
        review = release.read_json(folder / "review.json")
        review["unnotarizedDistribution"] = True
        release.write_json(folder / "review.json", review)
        with patch.object(release, "run") as execute:
            with self.assertRaisesRegex(ValueError, "allow-unnotarized"):
                release.publish(folder, accept=True)
            execute.assert_not_called()
        with patch.object(release, "run") as execute:
            with self.assertRaisesRegex(ValueError, "release notes"):
                release.publish(folder, accept=True, allow_unnotarized=True)
            execute.assert_not_called()
        (folder / "notes.md").write_text("This release is not notarized. See Privacy & Security to approve its first launch.")
        review["notesHash"] = release.digest(folder / "notes.md")
        release.write_json(folder / "review.json", review)
        remote = {"nameWithOwner": release.PUBLIC_REPO, "isPrivate": False, "defaultBranchRef": {"name": "main"}}
        with patch.object(release, "verify_app") as verify, patch.object(release, "run") as execute, \
             patch.object(release, "output", return_value=json.dumps(remote)):
            execute.return_value.returncode = 0
            with self.assertRaisesRegex(ValueError, "release already exists"):
                release.publish(folder, accept=True, allow_unnotarized=True)
            self.assertFalse(verify.call_args.kwargs["distribution"])
            self.assertFalse(any(call.args[0][0] in {"git", "/usr/bin/xcrun", "/usr/sbin/spctl"}
                                 for call in execute.call_args_list))

    def test_unnotarized_preparation_requires_notice_and_excludes_signing(self):
        notes = self.root / "notes.md"
        notes.write_text("A release without an installation notice.")
        destination = self.root / "distribution"
        with self.assertRaisesRegex(ValueError, "release notes"):
            release.prepare(self.repo, destination, notes, unnotarized=True)
        self.assertFalse(destination.exists())
        notes.write_text("This release is not notarized.")
        with self.assertRaisesRegex(ValueError, "not both"):
            release.prepare(self.repo, destination, notes, identity="fixture", profile="fixture", unnotarized=True)
        self.assertFalse(destination.exists())

    def test_public_repository_begins_with_only_snapshot_history(self):
        public = self.root / "public"
        public.mkdir()
        self.git(public, "init", "-q", "-b", "main")
        self.configure(public)
        release.sync_public(self.exported(), public, "v0.1.0")
        self.assertEqual(self.git(public, "rev-list", "--count", "HEAD").stdout.strip(), "1")
        self.assertEqual(self.git(public, "log", "--format=%s").stdout.strip(), "Igloo 0.1.0")
        self.assertFalse((public / ".env").exists())
        self.assertNotEqual(self.git(public, "rev-parse", "HEAD").stdout, self.git(self.repo, "rev-parse", "HEAD").stdout)
        # Subsequent releases preserve the public snapshot history, without force pushes.
        (self.repo / "README.md").write_text("Next public release")
        self.commit(self.repo)
        release.sync_public(self.exported("next"), public, "v0.1.1")
        self.assertEqual(self.git(public, "rev-list", "--count", "HEAD").stdout.strip(), "2")
    def test_existing_public_tag_or_dirty_tree_is_not_overwritten(self):
        public = self.root / "public"
        public.mkdir()
        self.git(public, "init", "-q", "-b", "main")
        self.configure(public)
        snapshot = self.exported()
        release.sync_public(snapshot, public, "v0.1.0")
        with self.assertRaisesRegex(ValueError, "tag"):
            release.sync_public(snapshot, public, "v0.1.0")
        (public / "README.md").write_text("unsaved public work")
        with self.assertRaisesRegex(ValueError, "clean"):
            release.sync_public(snapshot, public, "v0.1.1")
    def test_both_runtime_variants_share_one_reviewed_manifest(self):
        folder = self.prepared()
        assets = folder / "assets"
        archive = assets / "Igloo-0.1.0-universal-system-python.zip"
        archive.write_bytes(b"system variant")
        image = assets / "Igloo-0.1.0-universal-system-python.dmg"
        image.write_bytes(b"system disk image")
        manifest = release.read_json(assets / "igloo-update.json")
        manifest["assets"].extend(release.manifest_for(self.info, "universal", archive, "system")["assets"])
        release.write_json(assets / "igloo-update.json", manifest)
        review = release.read_json(folder / "review.json")
        review["manifestHash"] = release.digest(assets / "igloo-update.json")
        review["diskImages"].append({"filename": image.name, "sha256": release.digest(image),
                                    "size": image.stat().st_size, "runtime": "system"})
        release.write_json(folder / "review.json", review)
        self.assertEqual(len(release.validate_prepared(folder)[1]["assets"]), 2)

    def test_publication_refuses_private_or_wrong_destination(self):
        for remote in [{"nameWithOwner": release.PUBLIC_REPO, "isPrivate": True},
                       {"nameWithOwner": "someone/else", "isPrivate": False}]:
            folder = self.prepared()
            review = release.read_json(folder / "review.json")
            review["distributionReady"] = True
            release.write_json(folder / "review.json", review)
            with patch.object(release, "verify_app"), patch.object(release, "run") as execute, \
                 patch.object(release, "output", return_value=json.dumps(remote)):
                with self.assertRaisesRegex(ValueError, "already exist and be public"):
                    release.publish(folder, accept=True)
                self.assertFalse(any(call.args[0][0] == "git" for call in execute.call_args_list))
            import shutil
            shutil.rmtree(folder)

    def test_empty_public_repository_can_reach_first_release_check(self):
        folder = self.prepared()
        review = release.read_json(folder / "review.json")
        review["distributionReady"] = True
        release.write_json(folder / "review.json", review)
        for branch in [None, {"name": ""}, {"name": "main"}]:
            remote = {"nameWithOwner": release.PUBLIC_REPO, "isPrivate": False, "defaultBranchRef": branch}
            with self.subTest(branch=branch), patch.object(release, "verify_app"), \
                 patch.object(release, "run") as execute, \
                 patch.object(release, "output", return_value=json.dumps(remote)):
                execute.return_value.returncode = 0
                with self.assertRaisesRegex(ValueError, "release already exists"):
                    release.publish(folder, accept=True)
                self.assertFalse(any(call.args[0][0] == "git" for call in execute.call_args_list))

    def test_public_snapshot_uses_github_noreply_identity(self):
        import os
        public = self.root / "public"
        public.mkdir()
        self.git(public, "init", "-q", "-b", "main")
        self.configure(public)
        with patch.object(release, "output", return_value=json.dumps({"login": "example-user", "id": 12345})), \
             patch.dict(os.environ, {"GIT_AUTHOR_EMAIL": "private@example.invalid", "GIT_COMMITTER_EMAIL": "private@example.invalid"}):
            environment = release.public_commit_environment()
        release.sync_public(self.exported(), public, "v0.1.0", commit_env=environment)
        emails = self.git(public, "log", "-1", "--format=%ae%n%ce").stdout.splitlines()
        self.assertEqual(emails, ["12345+example-user@users.noreply.github.com"] * 2)

    def test_bad_version_metadata_is_refused(self):
        for change in [{"version": "../x"}, {"version": "00.1.0"}, {"build": True},
                       {"repository": "someone/another"}, {"minimumSystemVersion": "new"}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                release.metadata(dict(self.info, **change))

class UpdatePolicyTests(unittest.TestCase):
    def test_production_swift_update_policy(self):
        source = (Path(__file__).resolve().parents[1] / "app/Updates.swift").read_text().split("// MARK: - Interface")[0]
        source += r'''
func rejects(_ f: () throws -> Void) { do { try f(); fatalError("Expected refusal") } catch {} }
let payload = Data("verified app download".utf8)
let sha = SHA256.hash(data: payload).map { String(format: "%02x", $0) }.joined()
let filename = "Igloo-0.1.0-universal.zip"
let url = UpdatePolicy.assetURL(tag: "v0.1.0", filename: filename)
let asset = UpdateAsset(architecture: "universal", filename: filename, url: url, size: Int64(payload.count), sha256: sha)
var manifest = UpdateManifest(schemaVersion: 1, repository: UpdatePolicy.repository, tag: "v0.1.0", version: "0.1.0", build: 13,
                              minimumSystemVersion: "14.0", assets: [asset])
var release = GitHubRelease(tag_name: "v0.1.0", html_url: UpdatePolicy.releaseURL("v0.1.0"), body: "Notes", draft: false,
                            prerelease: false, assets: [ReleaseAsset(name: filename, size: asset.size, browser_download_url: url)])
func validate(_ m: UpdateManifest, _ r: GitHubRelease = release, version: String = "0.0.9", build: Int = 12,
              arch: String = "arm64", system: String = "14.0", runtime: String = "bundled") throws -> AvailableUpdate? {
    try UpdatePolicy.validate(m, release: r, architecture: arch, currentVersion: version, currentBuild: build, systemVersion: system, runtime: runtime)
}
precondition(try AppVersion("0.10.0") > AppVersion("0.9.0"))
precondition(try AppVersion("0.1") == AppVersion("0.1.0"))
rejects { _ = try AppVersion("0.1.0-beta") }
rejects { _ = try AppVersion("00.1.0") }
precondition(try validate(manifest)?.asset.architecture == "universal")
precondition(try validate(manifest, version: "0.1.0", build: 13) == nil)
precondition(try validate(manifest, version: "0.1.1", build: 1) == nil)
precondition(try validate(manifest, version: "0.1.0", build: 12) != nil)
var next = manifest
next.version = "0.1.1"; next.tag = "v0.1.1"; next.build = 14
next.assets[0].filename = "Igloo-0.1.1-universal.zip"
next.assets[0].url = UpdatePolicy.assetURL(tag: next.tag, filename: next.assets[0].filename)
var nextRelease = release
nextRelease.tag_name = next.tag; nextRelease.html_url = UpdatePolicy.releaseURL(next.tag)
nextRelease.assets = [ReleaseAsset(name: next.assets[0].filename, size: asset.size, browser_download_url: next.assets[0].url)]
precondition(try validate(next, nextRelease, version: "0.1.0", build: 13)?.manifest.version == "0.1.1")
precondition(try validate(next, nextRelease, version: "0.1.1", build: 14) == nil)
rejects { _ = try validate(manifest, system: "13.0") }
var bad = manifest; bad.repository = "someone/else"
rejects { _ = try validate(bad) }
bad = manifest; bad.assets[0].url = "https://example.com/app.zip"
rejects { _ = try validate(bad) }
bad = manifest; bad.assets[0].sha256 = "bad"
rejects { _ = try validate(bad) }
bad = manifest; bad.assets.append(asset)
rejects { _ = try validate(bad) }
bad = manifest; bad.assets[0].architecture = "arm64"; bad.assets[0].filename = "Igloo-0.1.0-arm64.zip"
rejects { _ = try validate(bad, arch: "x86_64") }
var wrongRelease = release; wrongRelease.prerelease = true
rejects { _ = try validate(manifest, wrongRelease) }
wrongRelease = release; wrongRelease.draft = true
rejects { _ = try validate(manifest, wrongRelease) }
wrongRelease = release; wrongRelease.tag_name = "v0.1.1"
rejects { _ = try validate(manifest, wrongRelease) }
wrongRelease = release; wrongRelease.assets[0].size += 1
rejects { _ = try validate(manifest, wrongRelease) }
var dual = manifest
var systemAsset = asset
systemAsset.runtime = "system"
systemAsset.filename = "Igloo-0.1.0-universal-system-python.zip"
systemAsset.url = UpdatePolicy.assetURL(tag: "v0.1.0", filename: systemAsset.filename)
dual.assets.append(systemAsset)
var dualRelease = release
dualRelease.assets.append(ReleaseAsset(name: systemAsset.filename, size: systemAsset.size, browser_download_url: systemAsset.url))
precondition(try validate(dual, dualRelease)?.asset.runtime == "bundled")
precondition(try validate(dual, dualRelease, runtime: "system")?.asset.runtime == "system")
rejects { _ = try validate(manifest, runtime: "system") }
precondition(!UpdatePolicy.safeTransport(URL(string: "http://github.com/app")!))
precondition(!UpdatePolicy.safeTransport(URL(string: "https://github.com.evil.invalid/app")!))
precondition(!UpdatePolicy.safeTransport(URL(string: "https://user:pass@github.com/app")!))
precondition(UpdatePolicy.safeTransport(URL(string: "https://release-assets.githubusercontent.com/file")!))
let temp = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
try payload.write(to: temp)
try UpdatePolicy.verify(file: temp, asset: asset)
try Data("changed".utf8).write(to: temp)
rejects { try UpdatePolicy.verify(file: temp, asset: asset) }
try FileManager.default.removeItem(at: temp)
print("Production Swift update policy checks passed")
'''
        # precondition uses a nonthrowing autoclosure.
        source = source.replace("precondition(try ", "precondition(try! ")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "main.swift").write_text(source)
            build = subprocess.run(["/usr/bin/swiftc", root / "main.swift", "-o", root / "check"], capture_output=True, text=True)
            self.assertEqual(build.returncode, 0, build.stderr)
            result = subprocess.run([root / "check"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

if __name__ == "__main__":
    unittest.main()
