"""Build the globe's civil time-zone grid: for every 0.1-degree cell, the IANA time zone
(Europe/London, America/Caracas, Asia/Kolkata...) that is in force there, so the browser can
show each zone's offset right now, daylight saving and every rule change included.

Sources (vendored copies are referenced; nothing is fetched here):
- where the zones are: Natural Earth 1:10m "Time zones" v5.1.2 (public domain), as of 2012:
  https://cdn.jsdelivr.net/gh/nvkelso/natural-earth-vector@v5.1.2/geojson/ne_10m_time_zones.geojson
- countries: world-atlas 2.0.2 countries-50m (Natural Earth, public domain);
- which IANA zones each country has, and their main cities: the tz database (zone.tab and
  iso3166.tab, from the tzdata package).

Each cell gets the IANA zone of its country whose standard offset in 2012 equals Natural
Earth's zone there (the nearest such zone's city when there are several); open sea gets a
nautical Etc/GMT zone. Both polygon sets are rasterised by exact scanline filling at cell
centres (no anti-aliasing), then run-length encoded per row.

    python tools/build_timezones.py NE_TIME_ZONES.geojson COUNTRIES_50M.json OUT.json
"""
from __future__ import annotations

import json
import math
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np

RES = 0.1  # degrees per cell
NE_DATE = datetime(2012, 5, 30, 12)  # Natural Earth's time-zone layer reflects this date
H, W = int(180 / RES), int(360 / RES)

# world-atlas country names that differ from the tz database's iso3166.tab
ALIASES = {
    "United States of America": "US", "Russia": "RU", "Dem. Rep. Congo": "CD", "Congo": "CG",
    "Central African Rep.": "CF", "S. Sudan": "SS", "Côte d'Ivoire": "CI", "Eq. Guinea": "GQ",
    "eSwatini": "SZ", "Bosnia and Herz.": "BA", "Macedonia": "MK", "Czechia": "CZ",
    "Dominican Rep.": "DO", "Falkland Is.": "FK", "Fr. S. Antarctic Lands": "TF", "Solomon Is.": "SB",
    "Iran": "IR", "Syria": "SY", "Laos": "LA", "Vietnam": "VN", "South Korea": "KR", "North Korea": "KP",
    "Korea": "KR", "Dem. Rep. Korea": "KP", "Taiwan": "TW", "Brunei": "BN", "Bolivia": "BO",
    "Venezuela": "VE", "Tanzania": "TZ", "Moldova": "MD", "W. Sahara": "EH", "Palestine": "PS",
    "Myanmar": "MM", "Timor-Leste": "TL", "Cabo Verde": "CV", "São Tomé and Principe": "ST",
    "Faeroe Is.": "FO", "Cayman Is.": "KY", "Turks and Caicos Is.": "TC", "N. Mariana Is.": "MP",
    "Marshall Is.": "MH", "Cook Is.": "CK", "Wallis and Futuna Is.": "WF", "Fr. Polynesia": "PF",
    "Heard I. and McDonald Is.": "HM", "S. Geo. and the Is.": "GS", "Br. Indian Ocean Ter.": "IO",
    "Pitcairn Is.": "PN", "U.S. Virgin Is.": "VI", "British Virgin Is.": "VG", "St. Kitts and Nevis": "KN",
    "St. Vin. and Gren.": "VC", "Antigua and Barb.": "AG", "St. Pierre and Miquelon": "PM",
    "Micronesia": "FM", "Vatican": "VA", "Åland": "AX", "Curaçao": "CW", "Sint Maarten": "SX",
    "St-Martin": "MF", "St-Barthélemy": "BL", "Saint Helena": "SH", "N. Cyprus": "CY", "Somaliland": "SO",
    "Kosovo": "RS", "Siachen Glacier": "IN", "Indian Ocean Ter.": "CX", "Ashmore and Cartier Is.": "AU",
    "Norfolk Island": "NF", "Turkey": "TR", "Türkiye": "TR", "Brunei Darussalam": "BN",
    "United Kingdom": "GB", "Macao": "MO", "Saint Lucia": "LC", "Samoa": "WS", "American Samoa": "AS",
    "Trinidad and Tobago": "TT",
}


def tz_tables():
    import tzdata

    base = os.path.join(os.path.dirname(tzdata.__file__), "zoneinfo")
    names = {}
    for line in open(os.path.join(base, "iso3166.tab"), encoding="utf-8"):
        if line.strip() and not line.startswith("#"):
            code, name = line.rstrip("\n").split("\t")[:2]
            names[name] = code
    zones: dict[str, list[tuple[str, float, float]]] = {}
    for line in open(os.path.join(base, "zone.tab"), encoding="utf-8"):
        if line.strip() and not line.startswith("#"):
            code, coord, tz = line.rstrip("\n").split("\t")[:3]
            lat, lon = _iso6709(coord)
            zones.setdefault(code, []).append((tz, lat, lon))
    return names, zones


def _iso6709(s: str) -> tuple[float, float]:
    i = max(s.rfind("+"), s.rfind("-"))
    def deg(part, d):
        sign = -1 if part[0] == "-" else 1
        p = part[1:]
        return sign * (int(p[:d]) + int(p[d:d + 2]) / 60 + (int(p[d + 2:d + 4]) / 3600 if len(p) > d + 2 else 0))
    return deg(s[:i], 2), deg(s[i:], 3)


def standard_offset_2012(tz: str) -> float:
    t = NE_DATE.replace(tzinfo=ZoneInfo(tz))
    return round(((t.utcoffset() - (t.dst() or t.utcoffset() * 0)).total_seconds()) / 3600, 2)


def fill(grid: np.ndarray, value: int, rings: list[np.ndarray]) -> None:
    """Even-odd scanline fill at cell centres (holes included)."""
    e = np.vstack([np.column_stack([r, np.roll(r, -1, axis=0)]) for r in rings])
    e = e[e[:, 1] != e[:, 3]]
    if not len(e):
        return
    ymin, ymax = np.minimum(e[:, 1], e[:, 3]), np.maximum(e[:, 1], e[:, 3])
    row_lo = max(0, int(np.floor((90 - ymax.max()) / RES)))
    row_hi = min(H - 1, int(np.ceil((90 - ymin.min()) / RES)))
    for row in range(row_lo, row_hi + 1):
        y = 90 - (row + 0.5) * RES
        live = (ymin <= y) & (ymax > y)
        if not live.any():
            continue
        x0, y0, x1, y1 = e[live].T
        xs = np.sort(x0 + (y - y0) * (x1 - x0) / (y1 - y0))
        for left, right in zip(xs[0::2], xs[1::2]):
            c0 = max(0, int(np.ceil((left + 180) / RES - 0.5)))
            c1 = min(W - 1, int(np.floor((right + 180) / RES - 0.5)))
            if c1 >= c0:
                grid[row, c0:c1 + 1] = value


def geojson_rings(geometry: dict) -> list[np.ndarray]:
    polys = geometry["coordinates"] if geometry["type"] == "MultiPolygon" else [geometry["coordinates"]]
    return [np.asarray(r, dtype=float)[:, :2] for poly in polys for r in poly]


def topo_rings(topo: dict, geometry: dict) -> list[np.ndarray]:
    sx, sy = topo["transform"]["scale"]
    tx, ty = topo["transform"]["translate"]
    arcs = []
    for arc in topo["arcs"]:
        pts = np.cumsum(np.asarray(arc, dtype=float), axis=0)
        arcs.append(np.column_stack([pts[:, 0] * sx + tx, pts[:, 1] * sy + ty]))
    def ring(ids):
        out = [arcs[i] if i >= 0 else arcs[~i][::-1] for i in ids]
        return np.vstack(out)
    polys = geometry["arcs"] if geometry["type"] == "MultiPolygon" else [geometry["arcs"]]
    return [ring(r) for poly in polys for r in poly]


def main(ne_src: str, countries_src: str, out: str) -> None:
    names, tz_by_country = tz_tables()
    # 1. Natural Earth zone offsets per cell
    ne = json.load(open(ne_src, encoding="utf-8"))
    offsets = sorted({float(f["properties"]["zone"]) for f in ne["features"]})
    off_index = {z: i + 1 for i, z in enumerate(offsets)}
    ne_grid = np.zeros((H, W), dtype=np.uint8)
    for f in ne["features"]:
        fill(ne_grid, off_index[float(f["properties"]["zone"])], geojson_rings(f["geometry"]))
    # 2. countries per cell
    topo = json.load(open(countries_src, encoding="utf-8"))
    geoms = topo["objects"]["countries"]["geometries"]
    country_grid = np.zeros((H, W), dtype=np.uint16)
    codes = [None]
    unmatched = []
    for g in geoms:
        if g.get("type") not in ("Polygon", "MultiPolygon"):
            continue
        name = g["properties"]["name"]
        code = ALIASES.get(name) or names.get(name)
        if not code:
            unmatched.append(name)
            continue
        codes.append(code)
        fill(country_grid, len(codes) - 1, topo_rings(topo, g))
    # 3. cell -> IANA zone
    zones: list[str] = []
    zone_index: dict[str, int] = {}
    def zid(name):
        if name not in zone_index:
            zones.append(name)
            zone_index[name] = len(zones)
        return zone_index[name]
    std = {tz: standard_offset_2012(tz) for cands in tz_by_country.values() for tz, _, _ in cands}
    out_grid = np.zeros((H, W), dtype=np.uint16)
    lat_c = 90 - (np.arange(H) + 0.5) * RES
    lon_c = -180 + (np.arange(W) + 0.5) * RES
    pairs = np.unique(np.column_stack([country_grid.ravel(), ne_grid.ravel()]), axis=0)
    fallback = 0
    for ci, oi in pairs:
        if oi == 0:
            continue
        z = offsets[oi - 1]
        mask = (country_grid == ci) & (ne_grid == oi)
        code = codes[ci] if ci else None
        cands = [c for c in tz_by_country.get(code, []) if abs(std[c[0]] - z) < 0.01] if code else []
        if not cands and code:  # zone rules differ from Natural Earth's here: the country's zones
            cands = tz_by_country.get(code, [])
            fallback += 1
        if not cands:
            if z == int(z):  # open sea: nautical zone (Etc/GMT has inverted signs)
                out_grid[mask] = zid(f"Etc/GMT{'-' if z > 0 else '+'}{abs(int(z))}" if z else "Etc/GMT")
            else:
                out_grid[mask] = zid(f"fixed:{z}")
            continue
        if len(cands) == 1:
            out_grid[mask] = zid(cands[0][0])
            continue
        rows, cols = np.nonzero(mask)  # several: the nearest zone city (great-circle)
        la, lo = np.radians(lat_c[rows])[:, None], np.radians(lon_c[cols])[:, None]
        cla = np.radians([c[1] for c in cands])[None, :]
        clo = np.radians([c[2] for c in cands])[None, :]
        cosd = np.sin(la) * np.sin(cla) + np.cos(la) * np.cos(cla) * np.cos(lo - clo)
        best = np.argmax(cosd, axis=1)
        ids = np.array([zid(c[0]) for c in cands], dtype=np.uint16)
        out_grid[rows, cols] = ids[best]
    rows_rle = []
    for r in out_grid:
        change = np.flatnonzero(np.diff(r)) + 1
        starts = np.concatenate([[0], change])
        lengths = np.diff(np.concatenate([starts, [W]]))
        rows_rle.append([int(v) for pair in zip(r[starts], lengths) for v in pair])
    # Label anchors: per Natural Earth offset band, a few cells near its nominal meridian.
    anchors = []
    for z, i in off_index.items():
        for lat in (55, 30, 5, -20, -45):
            row = int((90 - lat) / RES)
            cols = np.flatnonzero(ne_grid[row] == i)
            if len(cols):
                lons = cols * RES - 180 + RES / 2
                nominal = ((z * 15 + 180) % 360) - 180
                best = lons[np.argmin(np.abs(((lons - nominal) + 180) % 360 - 180))]
                anchors.append([round(float(best), 2), lat])
    json.dump({
        "source": "Natural Earth 1:10m time zones v5.1.2 + world-atlas countries-50m (public domain) + tz database",
        "res": RES, "width": W, "height": H, "zones": zones, "rows": rows_rle, "anchors": anchors,
    }, open(out, "w", encoding="utf-8"), separators=(",", ":"))
    print(f"{len(zones)} IANA zones, {fallback} country/offset pairs without an exact match, "
          f"unmatched countries: {sorted(set(unmatched))}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3])
