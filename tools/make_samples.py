"""Generate realistic sample activity files (``sample_data/sample_run.gpx`` / ``.fit``).

Run from anywhere:

    .venv\\Scripts\\python tools\\make_samples.py
"""
from __future__ import annotations

import math
import random
import struct
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
SAMPLES = PROJECT / "sample_data"
SAMPLES.mkdir(exist_ok=True)

random.seed(42)

# ---------------------------------------------------------------- GPX ------
START = datetime(2026, 11, 12, 17, 30, 0, tzinfo=timezone.utc)
CENTER_LAT, CENTER_LON = 50.4501, 30.5234
RADIUS_M = 795.0
TOTAL_SEC = 1800
STEP_SEC = 10
TITLE = "Ранковий забіг на 5 км"

points = []
total_steps = TOTAL_SEC // STEP_SEC
for index in range(total_steps + 1):
    fraction = index / total_steps
    theta = 2 * math.pi * fraction
    d_lat = RADIUS_M * math.cos(theta) / 111_320.0
    d_lon = RADIUS_M * math.sin(theta) / (111_320.0 * math.cos(math.radians(CENTER_LAT)))
    altitude = 120 + 12 * math.sin(2 * math.pi * fraction * 1.5) + random.uniform(-0.4, 0.4)
    heart = int(135 + 25 * fraction + 6 * math.sin(2 * math.pi * fraction * 3) + random.uniform(-2, 2))
    points.append(
        {
            "lat": CENTER_LAT + d_lat,
            "lon": CENTER_LON + d_lon,
            "ele": round(altitude, 1),
            "time": START + timedelta(seconds=index * STEP_SEC),
            "hr": heart,
        }
    )

gpx_lines = [
    '<?xml version="1.0" encoding="UTF-8"?>',
    '<gpx version="1.1" creator="RunTracker sample generator" xmlns="http://www.topografix.com/GPX/1/1" '
    'xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1">',
    f"  <metadata><name>{TITLE}</name></metadata>",
    "  <trk>",
    f"    <name>{TITLE}</name>",
    "    <type>running</type>",
    "    <trkseg>",
]
for point in points:
    gpx_lines.append(
        f'      <trkpt lat="{point["lat"]:.6f}" lon="{point["lon"]:.6f}">'
        f'<ele>{point["ele"]:.1f}</ele><time>{point["time"].strftime("%Y-%m-%dT%H:%M:%SZ")}</time>'
        f'<extensions><gpxtpx:TrackPointExtension><gpxtpx:hr>{point["hr"]}</gpxtpx:hr>'
        f"</gpxtpx:TrackPointExtension></extensions></trkpt>"
    )
gpx_lines += ["    </trkseg>", "  </trk>", "</gpx>"]
gpx_path = SAMPLES / "sample_run.gpx"
gpx_path.write_text("\n".join(gpx_lines), encoding="utf-8")
print("GPX written:", gpx_path, gpx_path.stat().st_size, "bytes")

# ---------------------------------------------------------------- FIT ------
FIT_EPOCH = datetime(1989, 12, 31, tzinfo=timezone.utc)


def fit_crc(data: bytes) -> int:
    table = [
        0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
        0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400,
    ]
    crc = 0
    for byte in data:
        tmp = table[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ table[byte & 0xF]
        tmp = table[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ table[(byte >> 4) & 0xF]
    return crc


# record message (global msg num 20), local type 0
record_fields = [
    (253, 4, 0x86),  # timestamp        uint32
    (0, 4, 0x85),    # position_lat     sint32 (semicircles)
    (1, 4, 0x85),    # position_long    sint32
    (2, 2, 0x84),    # altitude         uint16 (offset 500 m, scale 5)
    (3, 1, 0x02),    # heart_rate       uint8
    (5, 4, 0x86),    # distance         uint32 (scale 100)
]
definition = struct.pack("<BBBHB", 0x40, 0, 0, 20, len(record_fields))  # header, reserved, arch, global msg, count
for def_num, size, base_type in record_fields:
    definition += struct.pack("<BBB", def_num, size, base_type)

body = bytearray(definition)
cumulative_m = 0.0
previous = None
for point in points:
    lat = int(round(point["lat"] / 180.0 * 2**31))
    lon = int(round(point["lon"] / 180.0 * 2**31))
    if previous is not None:
        d_lat = (point["lat"] - previous["lat"]) * 111_320
        d_lon = (point["lon"] - previous["lon"]) * 111_320 * math.cos(math.radians(point["lat"]))
        cumulative_m += math.hypot(d_lat, d_lon)
    previous = point
    timestamp = int((point["time"] - FIT_EPOCH).total_seconds())
    body += struct.pack("<B", 0x00)  # data message, local type 0
    body += struct.pack("<I", timestamp)
    body += struct.pack("<i", lat)
    body += struct.pack("<i", lon)
    body += struct.pack("<H", int(round((point["ele"] + 500.0) * 5)))
    body += struct.pack("<B", point["hr"])
    body += struct.pack("<I", int(round(cumulative_m * 100)))

header_prefix = struct.pack("<BBHI4s", 14, 0x10, 2132, len(body), b".FIT")
header = header_prefix + struct.pack("<H", fit_crc(header_prefix))
file_without_crc = header + bytes(body)
fit_path = SAMPLES / "sample_run.fit"
fit_path.write_bytes(file_without_crc + struct.pack("<H", fit_crc(file_without_crc)))
print("FIT written:", fit_path, fit_path.stat().st_size, "bytes")
