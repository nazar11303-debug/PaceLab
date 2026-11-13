"""Generate PWA icons (pure standard library, no Pillow needed).

Run with the project venv:

    .venv\\Scripts\\python.exe tools\\make_icons.py
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
OUT_DIR = PROJECT / "static" / "icons"

BACKGROUND = (13, 17, 23, 255)
ACCENT = (252, 76, 2, 255)
WHITE = (255, 255, 255, 255)


def _png(width: int, height: int, rows: list[bytes]) -> bytes:
    raw = b"".join(b"\x00" + row for row in rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        payload = struct.pack(">I", len(data)) + tag + data
        return payload + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def _blend(base: tuple[int, int, int, int], top: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    alpha = top[3] / 255
    return (
        int(top[0] * alpha + base[0] * (1 - alpha)),
        int(top[1] * alpha + base[1] * (1 - alpha)),
        int(top[2] * alpha + base[2] * (1 - alpha)),
        255,
    )


def _inside_triangle(x: float, y: float, size: float) -> bool:
    # рівнобедрений трикутник «play» у центрі кола
    cx, cy = size / 2, size / 2
    x1, y1 = cx - 0.10 * size, cy - 0.15 * size
    x2, y2 = cx - 0.10 * size, cy + 0.15 * size
    x3, y3 = cx + 0.17 * size, cy

    def sign(ax: float, ay: float, bx: float, by: float, px: float, py: float) -> float:
        return (px - bx) * (ay - by) - (ax - bx) * (py - by)

    b1 = sign(x, y, x1, y1, x2, y2) < 0.0
    b2 = sign(x, y, x2, y2, x3, y3) < 0.0
    b3 = sign(x, y, x3, y3, x1, y1) < 0.0
    return b1 == b2 == b3


def render(size: int) -> bytes:
    cx = cy = size / 2
    radius = size * 0.36
    glow = size * 0.45
    rows: list[bytes] = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            dx, dy = x + 0.5 - cx, y + 0.5 - cy
            distance = (dx * dx + dy * dy) ** 0.5
            pixel = BACKGROUND
            if distance <= radius:
                pixel = ACCENT
            elif distance <= glow:
                # м'яке оранжеве світіння навколо кола
                fade = (glow - distance) / (glow - radius)
                pixel = _blend(BACKGROUND, (ACCENT[0], ACCENT[1], ACCENT[2], int(70 * fade)))
            if distance <= radius * 0.98 and _inside_triangle(x + 0.5, y + 0.5, size):
                pixel = WHITE
            row += bytes(pixel)
        rows.append(bytes(row))
    return _png(size, size, rows)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for size in (192, 512):
        path = OUT_DIR / f"icon-{size}.png"
        path.write_bytes(render(size))
        print(f"wrote {path} ({path.stat().st_size} bytes)")

    svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">
  <rect width="512" height="512" rx="96" fill="#0d1117"/>
  <circle cx="256" cy="256" r="184" fill="#fc4c02"/>
  <polygon points="204,178 204,334 334,256" fill="#ffffff"/>
</svg>
"""
    svg_path = OUT_DIR / "icon.svg"
    svg_path.write_text(svg, encoding="utf-8")
    print(f"wrote {svg_path}")


if __name__ == "__main__":
    main()
