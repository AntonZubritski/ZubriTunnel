#!/usr/bin/env python3
"""Rebuild icon assets from a pre-generated favicon set.

Source layout (from realfavicongenerator.net or similar):
  C:\\Users\\a.zubr\\Downloads\\favicon\\
    favicon.ico                          (16 + 32 + 48 hand-tuned)
    favicon-96x96.png                    (96)
    web-app-manifest-192x192.png         (192)
    web-app-manifest-512x512.png         (512)
    apple-touch-icon.png                 (180)
"""
import io
import struct
from pathlib import Path
from PIL import Image

ROOT = Path(__file__).resolve().parent
SRC = Path(r"C:\Users\a.zubr\Downloads\favicon")


def load_ico_sizes(p: Path) -> dict[int, bytes]:
    """Extract each embedded image from an .ico as PNG bytes (or BMP if not PNG)."""
    data = p.read_bytes()
    n = struct.unpack("<H", data[4:6])[0]
    out = {}
    for i in range(n):
        e = data[6 + i * 16 : 6 + (i + 1) * 16]
        w, h, _, _, _, bpp, sz, off = struct.unpack("<BBBBHHII", e)
        size = w or 256
        img_data = data[off : off + sz]
        # Re-export as standalone PNG so it embeds cleanly into our new .ico
        try:
            img = Image.open(io.BytesIO(img_data))
            buf = io.BytesIO()
            img.save(buf, format="PNG", optimize=True)
            out[size] = buf.getvalue()
        except Exception:
            out[size] = img_data
    return out


def png_resize(src_png: Path, target: int) -> bytes:
    """Resize an existing PNG to target pixel size, return PNG bytes."""
    img = Image.open(src_png).convert("RGBA")
    if img.size != (target, target):
        img = img.resize((target, target), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def make_ico(by_size: dict[int, bytes]) -> bytes:
    sizes = sorted(by_size.keys())
    n = len(sizes)
    header = struct.pack("<HHH", 0, 1, n)
    entries = b""
    images = b""
    offset = 6 + 16 * n
    for s in sizes:
        png = by_size[s]
        w_byte = 0 if s >= 256 else s
        h_byte = 0 if s >= 256 else s
        entries += struct.pack("<BBBBHHII", w_byte, h_byte, 0, 0, 1, 32, len(png), offset)
        images += png
        offset += len(png)
    return header + entries + images


def make_icns(by_size: dict[int, bytes]) -> bytes:
    type_for_size = {
        16:  b"icp4", 32: b"icp5", 64: b"icp6",
        128: b"ic07", 256: b"ic08", 512: b"ic09", 1024: b"ic10",
    }
    parts = []
    for s, png in sorted(by_size.items()):
        if s not in type_for_size:
            continue
        elem = type_for_size[s] + struct.pack(">I", 8 + len(png)) + png
        parts.append(elem)
    body = b"".join(parts)
    return b"icns" + struct.pack(">I", 8 + len(body)) + body


def main():
    if not SRC.exists():
        raise SystemExit(f"source not found: {SRC}")

    print(f"source: {SRC}")
    favicon = load_ico_sizes(SRC / "favicon.ico")
    print(f"  favicon.ico has sizes: {sorted(favicon.keys())}")

    src_512 = SRC / "web-app-manifest-512x512.png"
    src_192 = SRC / "web-app-manifest-192x192.png"
    src_96  = SRC / "favicon-96x96.png"

    # Build PNG bytes for every needed size
    pngs: dict[int, bytes] = {}
    # tiny sizes → use hand-tuned versions from favicon.ico
    for s in (16, 32, 48):
        if s in favicon:
            pngs[s] = favicon[s]
    # 64 → resize from 96 (less downscale)
    pngs[64] = png_resize(src_96, 64)
    # 96 → use as-is
    pngs[96] = src_96.read_bytes()
    # 128 → from 192
    pngs[128] = png_resize(src_192, 128)
    # 192 → as-is
    pngs[192] = src_192.read_bytes()
    # 256 → from 512
    pngs[256] = png_resize(src_512, 256)
    # 512 → as-is
    pngs[512] = src_512.read_bytes()
    # 1024 → upscale 512 (no source larger; lanczos is fine)
    pngs[1024] = png_resize(src_512, 1024)

    print(f"  prepared sizes: {sorted(pngs.keys())}")

    # Save 256-px PNG into both folders for the GUI's iconphoto fallback
    for d in (ROOT / "windows", ROOT / "mac"):
        d.mkdir(exist_ok=True)
        (d / "icon.png").write_bytes(pngs[256])

    # Windows .ico — include sizes 16-256 (Windows uses these for various contexts)
    ico_sizes = {s: pngs[s] for s in (16, 32, 48, 64, 96, 128, 256) if s in pngs}
    (ROOT / "windows" / "icon.ico").write_bytes(make_ico(ico_sizes))

    # Mac .icns — uses specific sizes
    icns_sizes = {s: pngs[s] for s in (16, 32, 64, 128, 256, 512, 1024) if s in pngs}
    (ROOT / "mac" / "icon.icns").write_bytes(make_icns(icns_sizes))

    print("\nWritten:")
    for f in (ROOT / "windows" / "icon.ico", ROOT / "windows" / "icon.png",
              ROOT / "mac" / "icon.icns", ROOT / "mac" / "icon.png"):
        print(f"  {f.relative_to(ROOT)} ({f.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
