"""Generate browser icons from Igloo's master artwork using macOS image tools."""
from pathlib import Path
import struct
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent.parent


def build(source, destination):
    source, destination = Path(source), Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="igloo-favicons-") as temporary:
        images = []
        for size in (16, 32, 48, 180):
            if size == 180:
                target = destination / "apple-touch-icon.png"
            elif size == 48:
                target = Path(temporary) / "favicon-48.png"
            else:
                target = destination / ("favicon-" + str(size) + ".png")
            subprocess.run(["/usr/bin/sips", "-z", str(size), str(size), str(source),
                            "--out", str(target)], check=True, stdout=subprocess.DEVNULL)
            if size != 180:
                images.append((size, target.read_bytes()))
        header = struct.pack("<HHH", 0, 1, len(images))
        offset = len(header) + 16 * len(images)
        entries = []
        for size, data in images:
            entries.append(struct.pack("<BBBBHHII", size, size, 0, 0, 1, 32, len(data), offset))
            offset += len(data)
        (destination / "favicon.ico").write_bytes(header + b"".join(entries) +
                                                b"".join(data for _, data in images))


if __name__ == "__main__":
    build(ROOT / "assets/IglooIcon.png", ROOT / "assets/web")
