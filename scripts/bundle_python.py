"""Bundle pinned, relocatable Python runtimes. Build-time dependency only."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile

def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""): h.update(chunk)
    return h.hexdigest()

def bundle(root, app, architecture):
    root, app = Path(root), Path(app)
    policy = json.loads((root / "scripts/python-runtime.json").read_text())
    targets = ["arm64", "x86_64"] if architecture == "universal" else [architecture]
    cache = Path.home() / ".cache/igloo/python-runtime"
    cache.mkdir(parents=True, exist_ok=True)
    for target in targets:
        asset = policy["assets"][target]
        archive = cache / (asset["sha256"] + ".tar.gz")
        if not archive.exists() or digest(archive) != asset["sha256"]:
            with tempfile.TemporaryDirectory(dir=cache) as temporary:
                download = Path(temporary) / "python.tar.gz"
                subprocess.run(["/usr/bin/curl", "--fail", "--location", "--silent", "--show-error",
                                "--proto", "=https", "--max-filesize", str(asset["size"]), "--output",
                                str(download), asset["url"]], check=True)
                if download.stat().st_size != asset["size"] or digest(download) != asset["sha256"]:
                    raise ValueError("The pinned Python runtime failed verification.")
                download.replace(archive)
        destination = app / "Contents/Resources/python" / target
        if destination.exists(): shutil.rmtree(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as temporary:
            with tarfile.open(archive) as tar:
                tar.extractall(temporary, filter="data")
            shutil.move(str(Path(temporary) / "python"), destination)
        if not (destination / "bin/python3").is_file():
            raise ValueError("The Python runtime is incomplete.")
    for target in ["arm64", "x86_64"]:
        stale = app / "Contents/Resources/python" / target
        if target not in targets and stale.exists(): shutil.rmtree(stale)

if __name__ == "__main__":
    bundle(*sys.argv[1:])
