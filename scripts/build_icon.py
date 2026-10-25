"""Build a standard macOS ICNS resource from the generated master artwork."""
from pathlib import Path
import subprocess
import sys
import tempfile

def build(source, destination):
    source = Path(source).resolve()
    destination = Path(destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="organizer-icon-") as directory:
        iconset = Path(directory) / "AppIcon.iconset"
        iconset.mkdir()
        for points in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                pixels = points * scale
                suffix = "@2x" if scale == 2 else ""
                target = iconset / f"icon_{points}x{points}{suffix}.png"
                subprocess.run(["/usr/bin/sips", "-z", str(pixels), str(pixels), str(source),
                                "--out", str(target)], check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["/usr/bin/iconutil", "-c", "icns", str(iconset), "-o", str(destination)], check=True)

if __name__ == "__main__":
    build(*sys.argv[1:])
