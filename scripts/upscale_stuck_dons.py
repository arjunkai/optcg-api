"""
AI-upscale the DONs flagged as STUCK_NO_SOURCE by audit_don_resolution.py
and upload the results to R2.

Uses the local realesrgan-ncnn-vulkan binary (GPU-accelerated on any Vulkan-
capable card, falls back to CPU). No external API, no cost, no rate limits.

Setup:
  1. Download the precompiled binary from
     https://github.com/xinntao/Real-ESRGAN/releases (Windows/Linux/Mac)
  2. Unzip into data/bin/esrgan/ so that
     data/bin/esrgan/realesrgan-ncnn-vulkan(.exe) exists.
  3. pip install Pillow

Usage:
  python scripts/upscale_stuck_dons.py               # all STUCK_NO_SOURCE
  python scripts/upscale_stuck_dons.py --dry-run     # list targets only
  python scripts/upscale_stuck_dons.py DON-005 ...   # specific ids
"""

from __future__ import annotations

import io
import json
import platform
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).parent))
from crop_don_images import clip_to_rounded_card  # noqa: E402

BUCKET = "optcg-images"
AUDIT_PATH = Path("data/don_resolution_audit.json")
API_BASE = "https://optcg-api.arjunbansal-ai.workers.dev"

ESRGAN_DIR = Path("data/bin/esrgan")
ESRGAN_BIN = ESRGAN_DIR / (
    "realesrgan-ncnn-vulkan.exe" if platform.system() == "Windows"
    else "realesrgan-ncnn-vulkan"
)
MODEL = "realesrgan-x4plus-anime"
SCALE = 4


def load_stuck() -> list[dict]:
    if not AUDIT_PATH.exists():
        sys.exit(f"Missing {AUDIT_PATH}. Run scripts/audit_don_resolution.py first.")
    rows = json.loads(AUDIT_PATH.read_text(encoding="utf-8"))
    return [r for r in rows if r["status"] == "STUCK_NO_SOURCE"]


def fetch_current(don_id: str) -> bytes:
    req = urllib.request.Request(
        f"{API_BASE}/images/{don_id}",
        headers={"User-Agent": "Mozilla/5.0 (optcg upscaler)"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def upscale_locally(src: Path, dst: Path) -> None:
    result = subprocess.run(
        [str(ESRGAN_BIN.resolve()),
         "-i", str(src.resolve()),
         "-o", str(dst.resolve()),
         "-n", MODEL,
         "-s", str(SCALE)],
        cwd=str(ESRGAN_DIR.resolve()),  # binary needs models/ relative
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"realesrgan failed: {result.stderr[:300]}")


def upload_to_r2(data: bytes, key: str) -> bool:
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(data)
        tmp = f.name
    try:
        result = subprocess.run(
            ["npx", "wrangler", "r2", "object", "put", f"{BUCKET}/{key}",
             "--file", tmp, "--content-type", "image/png", "--remote"],
            shell=True,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"    UPLOAD ERROR: {result.stderr[:200]}")
            return False
        return True
    finally:
        Path(tmp).unlink(missing_ok=True)


def main() -> None:
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    ids = [a for a in args if a.startswith("DON-")]

    if not dry_run and not ESRGAN_BIN.exists():
        sys.exit(f"Missing {ESRGAN_BIN}. See setup notes at top of this file.")

    stuck = load_stuck()
    if ids:
        stuck = [r for r in stuck if r["id"] in ids]
    print(f"Targets: {len(stuck)} stuck DONs{' (dry-run)' if dry_run else ''}")
    for t in stuck:
        print(f"  {t['id']:8} {t['width']}x{t['height']}  {t['name']}")
    if dry_run:
        return

    ok = fail = 0
    workdir = Path(tempfile.mkdtemp(prefix="optcg_upscale_"))
    try:
        for i, t in enumerate(stuck, start=1):
            don_id = t["id"]
            prefix = f"[{i:2}/{len(stuck)}] {don_id}"
            try:
                raw = fetch_current(don_id)
                src = workdir / f"{don_id}-in.png"
                dst = workdir / f"{don_id}-out.png"
                src.write_bytes(raw)

                print(f"{prefix}  upscaling {SCALE}x via realesrgan...")
                upscale_locally(src, dst)

                img = Image.open(dst)
                cleaned = clip_to_rounded_card(img)
                buf = io.BytesIO()
                cleaned.save(buf, "PNG")

                if upload_to_r2(buf.getvalue(), f"cards/{don_id}.png"):
                    print(f"{prefix}  OK   {cleaned.size[0]}x{cleaned.size[1]}")
                    ok += 1
                else:
                    fail += 1
            except Exception as e:
                print(f"{prefix}  FAIL  {e}")
                fail += 1
            time.sleep(0.1)
    finally:
        # clean up temp files
        for p in workdir.glob("*"):
            p.unlink(missing_ok=True)
        workdir.rmdir()

    print()
    print(f"Done: {ok} ok, {fail} failed")
    if fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
