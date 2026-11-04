"""Render every evidence/*.txt to terminal-style PNGs in evidence/screenshots/.
Files with several "=== name ===" sections are split into one PNG per section so no image is huge.
Usage: python scripts/render_evidence.py [name-substring ...]"""
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVID = ROOT / "evidence"
OUT = EVID / "screenshots"
OUT.mkdir(exist_ok=True)
SKIP = {"token.txt"}
MAX_LINES = 140


def chunks(lines):
    """Split on '=== name ===' headers; a too-long section is cut into parts."""
    secs, cur, name = [], [], "main"
    for l in lines:
        m = re.match(r"^=== (.+?) ===", l)
        if m and cur and any(x.strip() for x in cur):
            secs.append((name, cur))
            cur = []
        if m:
            name = m.group(1)
        cur.append(l)
    if cur:
        secs.append((name, cur))
    out = []
    for name, ls in secs:
        if len(ls) <= MAX_LINES:
            out.append((name, ls))
        else:
            for i in range(0, len(ls), MAX_LINES):
                out.append((f"{name}-part{i // MAX_LINES + 1}", ls[i:i + MAX_LINES]))
    return out


def main(filters):
    tmp = Path(tempfile.mkdtemp())
    for f in sorted(EVID.glob("*.txt")):
        if f.name in SKIP or (filters and not any(x in f.name for x in filters)):
            continue
        lines = f.read_text(encoding="utf-8", errors="replace").replace("\r", "").split("\n")
        parts = chunks(lines)
        for name, ls in parts:
            slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-")
            stem = f.stem if len(parts) == 1 else f"{f.stem}--{slug}"
            src = tmp / f"{stem}.txt"
            src.write_text("\n".join(ls).rstrip() + "\n", encoding="utf-8")
            png = OUT / f"terminal-{stem}.png"
            subprocess.run(["node", str(ROOT / "scripts" / "shoot.mjs"), "txt", str(src), str(png),
                            f"evidence/{f.name}" + ("" if len(parts) == 1 else f"  [{name}]")],
                           cwd=str(ROOT), check=True, capture_output=True)
            print("rendered", png.relative_to(ROOT))


if __name__ == "__main__":
    main(sys.argv[1:])
