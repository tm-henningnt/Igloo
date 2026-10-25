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
