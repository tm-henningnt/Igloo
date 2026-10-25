"""Classification uses only disposable files; no project code is executed."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import classification as c

class ClassificationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
    def tearDown(self):self.temp.cleanup()
    def write(self,name,value):
        p=self.root/name;p.parent.mkdir(parents=True,exist_ok=True)
        p.write_text(json.dumps(value) if isinstance(value,dict) else value)
    def test_web_app_keeps_purpose_and_technology_separate(self):
        self.write("package.json",{"dependencies":{"next":"*","react":"*"}})
        self.write("src/app.tsx","");self.write("src/app.css","")
        v=c.detect(self.root)
        self.assertEqual(v["projectType"],"Web app");self.assertIn("TypeScript",v["languages"])
    def test_site_generator_is_website(self):
        self.write("package.json",{"devDependencies":{"@11ty/eleventy":"*"}})
        self.assertEqual(c.detect(self.root)["projectType"],"Website")
    def test_python_cli_and_service_are_distinct(self):
        self.write("pyproject.toml",'[project]\nname="fixture"\ndependencies=[]\n[project.scripts]\nfixture="fixture:main"\n')
        self.assertEqual(c.detect(self.root)["projectType"],"Command-line tool")
        self.write("pyproject.toml",'[project]\nname="fixture"\ndependencies=["fastapi>=0.1"]\n')
        self.assertEqual(c.detect(self.root)["projectType"],"API / service")
    def test_qlik_sdk_does_not_turn_a_service_into_an_extension(self):
        self.write("package.json",{"dependencies":{"@qlik/api":"*","express":"*"}})
        self.assertEqual(c.detect(self.root)["projectType"],"API / service")
        self.write("chart.qext","{}")
        self.assertEqual(c.detect(self.root)["projectType"],"Qlik extension")
    def test_component_package_is_not_mistaken_for_a_web_app(self):
        self.write("package.json",{"exports":"./index.js","peerDependencies":{"react":"*"}})
        self.assertEqual(c.detect(self.root)["projectType"],"Library / package")
    def test_native_and_rust_cli_layouts(self):
        self.write("src/main.rs","")
        self.write("Cargo.toml",'[package]\nname="fixture"\nversion="0.1.0"\n')
        self.assertEqual(c.detect(self.root)["projectType"],"Command-line tool")
        self.write("fixture.xcodeproj/project.pbxproj","");self.write("app.swift","")
        self.assertEqual(c.detect(self.root)["projectType"],"Native app")
        self.assertIn("Swift",c.detect(self.root)["languages"])
    def test_generated_folders_symlinks_and_environment_values_are_not_inputs(self):
        self.write("node_modules/package/entry.swift","")
        self.write("build/generated.ipynb","")
        self.write(".env","synthetic private contents")
        target=self.root/"outside";target.mkdir()
        self.write("outside/index.html","")
        (self.root/"index.html").symlink_to(target/"index.html")
        # A nested docs index is not a root website entry point.
        self.assertEqual(c.detect(self.root)["projectType"],"Unclassified")
        self.assertEqual(c.detect(self.root)["languages"],["HTML"])
    def test_malformed_or_oversized_metadata_falls_back(self):
        self.write("package.json","{broken")
        self.write("pyproject.toml","x"*262145)
        self.assertEqual(c.detect(self.root)["projectType"],"Unclassified")
    def test_unknown_and_empty_projects_are_explicit(self):
        self.assertEqual(c.detect(self.root),{"projectType":"Unclassified","languages":[],"typeEvidence":["No recognized project-purpose metadata"]})
    def test_manual_type_override_survives_snapshot_refresh_and_can_reset(self):
        from tests import test_organizer as fixtures
        f=fixtures.GuardTests();f.setUp()
        try:
            o=fixtures.o
            raw={"root":str(f.root),"projects":[{"id":str(f.repo),"path":str(f.repo),"projectType":"Website"}]}
            o.save(o.STATE/"snapshot.json",raw)
            o.categorize(str(f.repo),"Command-line tool")
            p=o.snapshot()["projects"][0]
            self.assertEqual(p["projectType"],"Command-line tool")
            self.assertEqual(p["detectedType"],"Website")
            o.categorize(str(f.repo),"Automatic")
            self.assertEqual(o.snapshot()["projects"][0]["projectType"],"Website")
            self.assertEqual((o.STATE/"classifications.json").stat().st_mode&0o777,0o600)
            with self.assertRaises(o.Refusal):o.categorize("/unknown","Website")
        finally:f.tearDown()

if __name__=="__main__":unittest.main()
