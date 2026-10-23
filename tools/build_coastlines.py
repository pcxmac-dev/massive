"""Build the globe's high-resolution coastline and border tiles from GSHHG.

Source: GSHHG 2.3.7 (Wessel & Smith), LGPL-3.0, binary distribution:
  https://www.soest.hawaii.edu/pwessel/gshhg/gshhg-bin-2.3.7.zip   (119 MB; keep it out of the repo)
We use the "high" resolution (~200 m): gshhs_h.b (shorelines: land, lakes, Antarctic ice front
and grounding line) and wdb_borders_h.b (national borders).

Output: one file per 10 x 10 degree tile, so the globe loads and draws only what is on screen:
  <out>/index.json         tiles present and their point counts
  <out>/<lat0>_<lon0>.bin  "GSH1", then two sections (shorelines, borders), each:
                           uint32 line count; per line: uint32 n, then n x (uint16 lon, uint16 lat)
Points are quantised relative to the tile: value = (deg - origin + MARGIN) / SPAN * 65535, with a
MARGIN either side so a segment's end in the next tile still fits (~34 m steps; the data is ~200 m).
All integers little-endian.

    python tools/build_coastlines.py ~/MassiveData/sources/gshhg-bin-2.3.7.zip OUT_DIR
"""
from __future__ import annotations

import json
import struct
import sys
import zipfile
from pathlib import Path

import numpy as np

TILE = 10           # degrees
MARGIN = 5          # degrees of slack around a tile for segments that cross its edge
SPAN = TILE + 2 * MARGIN
SHORE_LEVELS = {1, 2, 5, 6}   # land, lake, Antarctic ice front, Antarctic grounding line
BORDER_LEVELS = {1}           # national borders


def records(data: bytes):
    """(level, points[n, 2] as lon/lat degrees) for every polygon or line in a GSHHG file."""
    off = 0
    while off + 44 <= len(data):
        pid, n, flag, *_ = struct.unpack_from(">11i", data, off)
        off += 44
        pts = np.frombuffer(data, dtype=">i4", count=2 * n, offset=off).reshape(n, 2).astype(np.float64) / 1e6
        off += 8 * n
        yield flag & 255, pts


def tiles_of(lines, closed: bool):
    """Split each line where it moves to another tile; the segment that crosses keeps its far end."""
    out: dict[tuple[int, int], list[np.ndarray]] = {}
    for pts in lines:
        if closed and len(pts) > 2 and not np.array_equal(pts[0], pts[-1]):
            pts = np.vstack([pts, pts[:1]])
        lon = ((pts[:, 0] + 180) % 360) - 180
        lat = np.clip(pts[:, 1], -90, 89.999999)
        tx = np.floor((lon + 180) / TILE).astype(int)
        ty = np.floor((lat + 90) / TILE).astype(int)
        key = tx * 100 + ty
        start = 0
        for i in range(1, len(pts) + 1):
            if i == len(pts) or key[i] != key[start]:
                end = min(i + 1, len(pts))  # include the next point so there is no gap
                seg = np.column_stack([lon[start:end], lat[start:end]])
                if len(seg) >= 2:
                    k = (int(ty[start]) * TILE - 90, int(tx[start]) * TILE - 180)
                    out.setdefault(k, []).append(seg)
                start = i
    return out


def quantise(seg: np.ndarray, origin: tuple[int, int]) -> np.ndarray:
    lat0, lon0 = origin
    lon = seg[:, 0].copy()
    # keep a line continuous across the antimeridian (only directions matter on the sphere)
    lon = np.where(lon - lon0 > 180 + MARGIN, lon - 360, lon)
    lon = np.where(lon - lon0 < -180 + MARGIN, lon + 360, lon)
    q = np.column_stack([(lon - lon0 + MARGIN) / SPAN, (seg[:, 1] - lat0 + MARGIN) / SPAN])
    if (q < 0).any() or (q > 1).any():
        return None  # a long segment out of reach (rare): left to the lower resolutions
    return np.round(q * 65535).astype("<u2")


def write_section(buf: bytearray, segs: list[np.ndarray], origin) -> int:
    packed = [q for q in (quantise(s, origin) for s in segs) if q is not None]
    buf += struct.pack("<I", len(packed))
    points = 0
    for q in packed:
        buf += struct.pack("<I", len(q))
        buf += q.tobytes()
        points += len(q)
    return points


def main(archive: str, out_dir: str) -> None:
    z = zipfile.ZipFile(archive)
    shores = [p for lvl, p in records(z.read("gshhs_h.b")) if lvl in SHORE_LEVELS]
    borders = [p for lvl, p in records(z.read("wdb_borders_h.b")) if lvl in BORDER_LEVELS]
    t_shore, t_border = tiles_of(shores, closed=True), tiles_of(borders, closed=False)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.bin"):
        old.unlink()
    index = {}
    total = 0
    for key in sorted(set(t_shore) | set(t_border)):
        buf = bytearray(b"GSH1")
        n1 = write_section(buf, t_shore.get(key, []), key)
        n2 = write_section(buf, t_border.get(key, []), key)
        name = f"{key[0]}_{key[1]}"
        (out / f"{name}.bin").write_bytes(bytes(buf))
        index[name] = {"shore": n1, "borders": n2, "bytes": len(buf)}
        total += len(buf)
    json.dump({"source": "GSHHG 2.3.7 high resolution (LGPL-3.0), shorelines + WDBII national borders",
               "tile": TILE, "margin": MARGIN, "tiles": index}, open(out / "index.json", "w"), indent=0)
    print(f"{len(shores)} shorelines, {len(borders)} border lines -> {len(index)} tiles, "
          f"{sum(v['shore'] + v['borders'] for v in index.values()):,} points, {total / 1e6:.1f} MB")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
