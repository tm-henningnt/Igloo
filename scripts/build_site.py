#!/usr/bin/env python3
"""Stage the static GitHub Pages site without development files or Git history."""
import argparse
from pathlib import Path
import shutil
ASSETS = {
    "assets/README-header.png": "README-header.png",
    "assets/IglooIcon.png": "IglooIcon.png",
    "assets/web/favicon.ico": "favicon.ico",
    "assets/web/favicon-16.png": "favicon-16.png",
    "assets/web/favicon-32.png": "favicon-32.png",
    "assets/web/apple-touch-icon.png": "apple-touch-icon.png",
    "docs/screenshots/overview.png": "overview.png",
    "docs/screenshots/overview-light.png": "overview-light.png",
    "docs/screenshots/space-explorer.png": "space-explorer.png",
}
def build(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if destination.exists():
        raise ValueError("Choose a new output folder; existing files are never replaced.")
    if destination == source or (source in destination.parents and destination.parent != source):
        raise ValueError("Choose an output folder outside source subdirectories.")
    destination.mkdir(parents=True)
    (destination / "assets").mkdir()
    for name in ("index.html", "styles.css", "app.js"):
        shutil.copy2(source / "site" / name, destination / name)
    for name, target in ASSETS.items():
        shutil.copy2(source / name, destination / "assets" / target)
    (destination / ".nojekyll").touch()
    return destination
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    print(build(Path(__file__).resolve().parent.parent, parser.parse_args().output))
