"""Static website staging and release-link boundary checks."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("build_site", ROOT / "scripts/build_site.py")
site = importlib.util.module_from_spec(spec)
spec.loader.exec_module(site)

class SiteTests(unittest.TestCase):
    def test_comparison_has_no_visual_labels_and_keeps_accessible_slider(self):
        html = (ROOT / "site/index.html").read_text()
        css = (ROOT / "site/styles.css").read_text()
        self.assertNotIn("appearance-label", html)
        self.assertNotIn("appearance-label", css)
        self.assertIn('aria-label="Compare light and dark mode"', html)
        self.assertIn('aria-valuetext="50% light mode, 50% dark mode"', html)
        self.assertIn('aria-describedby="appearance-help"', html)
        self.assertIn("igloo review weekly-review", html)
        self.assertNotIn("igloo plan weekly-review", html)

    def download_fixture(self, fixture):
        script = r'''
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const fixture = JSON.parse(process.argv[3]);
const html = fs.readFileSync(process.argv[2], "utf8");
const elements = {};
for (const id of ["release-status", "download-standard", "download-system"]) {
  const match = html.match(new RegExp('<[^>]+id="' + id + '"[^>]*>([\\s\\S]*?)</(?:p|a)>'));
  assert.ok(match, "The production page must have " + id);
  elements[id] = {
    href: (match[0].match(/href="([^"]+)"/) || [])[1],
    textContent: match[1].replace(/<[^>]*>/g, "")
  };
}
const requests = [];
const context = vm.createContext({
  AbortSignal,
  module: { exports: {} },
  fetch: async (url, options) => {
    requests.push({ url, options });
    if (fixture.networkError) throw new TypeError("Fixture connection failure");
    return {
      ok: fixture.status === 200, status: fixture.status,
      json: async () => {
        assert.equal(fixture.status, 200, "Error responses must not be parsed as releases");
        if (fixture.invalidJSON) throw new SyntaxError("Fixture malformed JSON");
        return fixture.release;
      }
    };
  }
});
// Load the production script without a document so its automatic startup does
// not race the explicitly awaited request. Every fetch is a local fixture.
vm.runInContext(fs.readFileSync(process.argv[1], "utf8"), context);
context.document = { getElementById: id => elements[id] };
(async () => {
  await vm.runInContext("showDownloads()", context);
  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, "https://api.github.com/repos/tm-henningnt/Igloo/releases/latest");
  assert.equal(requests[0].options.credentials, "omit");
  assert.ok(requests[0].options.signal instanceof AbortSignal);
  process.stdout.write(JSON.stringify({ elements }));
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
        result = subprocess.run(["node", "-e", script, str(ROOT / "site/app.js"),
                                 str(ROOT / "site/index.html"), json.dumps(fixture)],
                                check=True, capture_output=True, text=True, timeout=15)
        return json.loads(result.stdout)

    def assert_release_fallback(self, fixture, message):
        result = self.download_fixture(fixture)
        elements = result["elements"]
        self.assertEqual(elements["release-status"]["textContent"], message)
        for name in ("download-standard", "download-system"):
            self.assertEqual(elements[name]["href"], "https://github.com/tm-henningnt/Igloo/releases")
            self.assertIn("releases", elements[name]["textContent"].lower())

    @unittest.skipUnless(shutil.which("node"), "Node is needed for the website JavaScript checks")
    def test_no_release_404_keeps_releases_links(self):
        self.assert_release_fallback({"status": 404},
            "No public release is available yet. View releases on GitHub.")

    @unittest.skipUnless(shutil.which("node"), "Node is needed for the website JavaScript checks")
    def test_api_unavailable_http_status_keeps_releases_links(self):
        for status in (301, 401, 403, 429, 500, 503):
            with self.subTest(status=status):
                self.assert_release_fallback({"status": status},
                    "Release downloads are temporarily unavailable. View releases on GitHub.")

    @unittest.skipUnless(shutil.which("node"), "Node is needed for the website JavaScript checks")
    def test_api_network_error_keeps_releases_links(self):
        self.assert_release_fallback({"networkError": True},
            "Release downloads are temporarily unavailable. View releases on GitHub.")

    @unittest.skipUnless(shutil.which("node"), "Node is needed for the website JavaScript checks")
    def test_unreadable_release_keeps_releases_links(self):
        for fixture in ({"status": 200, "invalidJSON": True}, {"status": 200, "release": {}}):
            with self.subTest(fixture=fixture):
                self.assert_release_fallback(fixture,
                    "Release downloads are temporarily unavailable. View releases on GitHub.")

    @unittest.skipUnless(shutil.which("node"), "Node is needed for the website JavaScript checks")
    def test_available_release_shows_verified_downloads(self):
        base = "https://github.com/tm-henningnt/Igloo/releases"
        names = ("Igloo-0.2.0-universal.dmg", "Igloo-0.2.0-universal-system-python.dmg")
        release = {"tag_name": "v0.2.0", "html_url": base + "/tag/v0.2.0",
                   "draft": False, "prerelease": False,
                   "assets": [{"name": name, "state": "uploaded", "size": 100,
                               "browser_download_url": base + "/download/v0.2.0/" + name}
                              for name in names]}
        elements = self.download_fixture({"status": 200, "release": release})["elements"]
        self.assertEqual(elements["release-status"]["textContent"], "Igloo 0.2.0 · Apple silicon and Intel Macs")
        self.assertEqual(elements["download-standard"]["href"], release["assets"][0]["browser_download_url"])
        self.assertEqual(elements["download-standard"]["textContent"], "Download Igloo 0.2.0 for Mac ↓")
        self.assertEqual(elements["download-system"]["href"], release["assets"][1]["browser_download_url"])
        self.assertEqual(elements["download-system"]["textContent"], "Download 0.2.0 · system Python →")

    def test_export_contains_only_site_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = site.build(ROOT, Path(temporary) / "site")
            files = {p.relative_to(output).as_posix() for p in output.rglob("*") if p.is_file()}
            self.assertEqual(files, {"index.html", "styles.css", "app.js", ".nojekyll",
                                    *("assets/" + name for name in site.ASSETS.values())})
            self.assertNotIn(".git", files)

    def test_appearance_screenshots_have_matching_dimensions(self):
        import struct
        images = [(ROOT / "docs/screenshots" / name).read_bytes()
                  for name in ("overview.png", "overview-light.png")]
        self.assertTrue(all(image.startswith(b"\x89PNG\r\n\x1a\n") for image in images))
        dimensions = [struct.unpack(">II", image[16:24]) for image in images]
        self.assertEqual(dimensions[0], dimensions[1])
        self.assertGreaterEqual(dimensions[0][0], 1440)
        self.assertNotEqual(images[0], images[1])

    def test_existing_destination_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            (output / "keep").write_text("untouched")
            with self.assertRaises(ValueError):
                site.build(ROOT, output)
            self.assertEqual((output / "keep").read_text(), "untouched")

    @unittest.skipUnless(shutil.which("node"), "Node is needed for the website JavaScript checks")
    def test_downloads_are_published_exact_repo_assets(self):
        script = """
const assert = require("node:assert/strict");
const { releaseDownloads } = require(process.argv[1]);
const base = "https://github.com/tm-henningnt/Igloo/releases";
const asset = (suffix = "") => ({
  name: "Igloo-0.1.0-universal" + suffix + ".dmg", state:"uploaded", size:100,
  browser_download_url: base + "/download/v0.1.0/Igloo-0.1.0-universal" + suffix + ".dmg"
});
const release = {tag_name:"v0.1.0", html_url:base+"/tag/v0.1.0", draft:false, prerelease:false,
                 assets:[asset(),asset("-system-python")]};
assert.equal(releaseDownloads(release).version, "0.1.0");
assert.ok(releaseDownloads(release).system.endsWith("-system-python.dmg"));
for (const patch of [{draft:true},{prerelease:true},{tag_name:"v00.1.0"},{assets:[]},
                     {html_url:"https://github.com/someone/else/releases/tag/v0.1.0"}]) {
  assert.equal(releaseDownloads({...release,...patch}), null);
}
const invalid = [{...asset(),browser_download_url:"https://example.org/Igloo.dmg"},
                 {...asset(),size:0}, {...asset(),state:"new"}];
for (const bad of invalid) assert.equal(releaseDownloads({...release,assets:[bad]}),null);
assert.equal(releaseDownloads({...release,assets:[asset(),asset()]}),null);
assert.equal(releaseDownloads({...release,assets:[asset()]}).system,undefined);
"""
        subprocess.run(["node", "-e", script, str(ROOT / "site/app.js")], check=True,
                       capture_output=True, text=True)
