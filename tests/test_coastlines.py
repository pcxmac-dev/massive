"""tools/build_coastlines.py: GSHHG records into tiles the globe decodes."""
import struct
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import build_coastlines as bc  # noqa: E402


def gshhg(level, pts):
    """One GSHHG record: big-endian 11-int header, then micro-degree lon/lat pairs."""
    head = struct.pack(">11i", 7, len(pts), level, 0, 0, 0, 0, 0, 0, -1, -1)
    return head + np.asarray(np.round(np.array(pts) * 1e6), dtype=">i4").tobytes()


def test_records_reads_level_and_points():
    data = gshhg(1, [(139.5, 35.25), (140.0, 35.5)]) + gshhg(2 | (3 << 8), [(1, 2), (3, 4), (5, 6)])
    recs = list(bc.records(data))
    assert [lvl for lvl, _ in recs] == [1, 2]  # level is the low byte of the flag
    assert np.allclose(recs[0][1], [[139.5, 35.25], [140.0, 35.5]])


def test_a_line_crossing_tiles_is_split_without_a_gap():
    tiles = bc.tiles_of([np.array([[138.0, 35.0], [139.0, 35.0], [141.0, 35.0], [142.0, 35.0]])], closed=False)
    assert set(tiles) == {(30, 130), (30, 140)}
    first, second = tiles[(30, 130)][0], tiles[(30, 140)][0]
    assert first[-1].tolist() == second[0].tolist() == [141.0, 35.0]  # the crossing segment is kept


def test_polygons_are_closed():
    tiles = bc.tiles_of([np.array([[1.0, 1.0], [2.0, 1.0], [2.0, 2.0]])], closed=True)
    seg = tiles[(0, 0)][0]
    assert seg[0].tolist() == seg[-1].tolist()


def test_longitudes_wrap_and_quantise_round_trips_like_the_browser():
    tiles = bc.tiles_of([np.array([[181.5, 10.5], [182.0, 10.75]])], closed=False)
    assert list(tiles) == [(10, -180)]
    q = bc.quantise(tiles[(10, -180)][0], (10, -180))
    # globe.js decodeTile: origin - margin + q / 65535 * span
    lon = -180 - bc.MARGIN + q[:, 0] / 65535 * bc.SPAN
    lat = 10 - bc.MARGIN + q[:, 1] / 65535 * bc.SPAN
    assert np.allclose(lon, [-178.5, -178.0], atol=2e-4) and np.allclose(lat, [10.5, 10.75], atol=2e-4)


def test_segments_out_of_reach_are_dropped():
    assert bc.quantise(np.array([[0.0, 0.0], [30.0, 0.0]]), (0, 0)) is None


def test_tile_file_layout():
    buf = bytearray(b"GSH1")
    n = bc.write_section(buf, [np.array([[1.0, 1.0], [2.0, 2.0]])], (0, 0))
    bc.write_section(buf, [], (0, 0))
    assert n == 2
    assert struct.unpack_from("<II", buf, 4) == (1, 2)  # one line of two points
    assert struct.unpack_from("<I", buf, 4 + 8 + 8)[0] == 0  # empty borders section
    assert len(buf) == 4 + 4 + 4 + 2 * 4 + 4
