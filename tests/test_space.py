"""Exercise the production Swift allocation model with synthetic project data."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

class SpaceTests(unittest.TestCase):
    def test_space_allocation_excludes_nested_candidates_and_preserves_totals(self):
        source=Path(__file__).resolve().parents[1]/"app"
        model=(source/"Igloo.swift").read_text().split("struct TrashItem:")[0]
        model+=(source/"SpaceViews.swift").read_text().split("struct SpaceSector:")[0]
        fixture=dict(id="/fixture",name="Fixture",path="/fixture",common="/fixture/.git",category="Example",
                     size=1000,git=True,ageDays=None,lastCommit="",remoteStatus="",verifiedAt="",remotes=[],
                     active=False,activityKnown=False,processes=[],branches=[],worktrees=[],changes=[],
                     stashes=[],refs=[],localFiles=[],ignoredFiles=[],candidates=[
                         dict(id="a",path="/fixture/node_modules",size=400,kind="node_modules",owner="/fixture",eligible=True,reason=""),
                         dict(id="b",path="/fixture/node_modules/build",size=200,kind="build",owner="/fixture",eligible=True,reason=""),
                         dict(id="c",path="/fixture/build",size=100,kind="build",owner="/fixture",eligible=False,reason="review")],
                     holds=[],uncovered=0,unknown=0,decision="Undecided",note="")
        # JSON is encoded again as a Swift quoted string.
        literal=json.dumps(json.dumps(fixture))
        model+="\nvar p = try JSONDecoder().decode(Project.self, from: "+literal+".data(using: .utf8)!)\n"
        model+="""
let parts = SpaceData.slices(p)
precondition(parts.reduce(Int64(0)) { $0 + $1.size } == 1000)
precondition(parts.first { $0.kind == "Other project files" }!.size == 500)
let cleanup = SpaceData.slices(p, cleanupOnly: true)
precondition(cleanup.count == 1 && cleanup[0].size == 400)
precondition(SpaceData.age(p) == "Unknown age")
precondition(SpaceData.activity(p) == "Activity unknown")
p.candidates[0].size = 2000
precondition(SpaceData.slices(p).reduce(Int64(0)) { $0 + $1.size } == 1000)
p.projectType = "Native app"
p.languages = ["Swift", "Python"]
var other = p
other.id = "/other"
other.projectType = "Scripts / automation"
other.languages = ["Python"]
let combined = SpaceData.slices(p) + SpaceData.slices(other)
for dimension in ["Projects", "Project type", "Technology"] {
    let groups = SpaceData.groups(combined, by: dimension)
    precondition(groups.count == 2)
    precondition(groups.reduce(Int64(0)) { $0 + $1.size } == 2000)
}
p.size = 0
precondition(SpaceData.slices(p).isEmpty)
precondition(IglooPalette.agePosition("Under 30 days") == 0)
precondition(IglooPalette.agePosition("1 year or older") == 1)
precondition(IglooPalette.agePosition("Unknown age") == nil)
precondition(IglooPalette.agePosition("30–89 days")! < IglooPalette.agePosition("90–364 days")!)
print("Swift space allocation and Igloo temperature checks passed")
"""
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);script=root/"main.swift";script.write_text(model)
            build=subprocess.run(["/usr/bin/swiftc",str(script),"-o",str(root/"check")],capture_output=True,text=True)
            self.assertEqual(build.returncode,0,build.stderr)
            run=subprocess.run([str(root/"check")],capture_output=True,text=True)
            self.assertEqual(run.returncode,0,run.stderr)

if __name__=="__main__": unittest.main()
