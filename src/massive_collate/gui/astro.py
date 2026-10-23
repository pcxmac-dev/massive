"""Where the Sun, Moon and planets are: for the Markets globe (the point on Earth each body is
overhead) and its orrery and moon views.

Paul Schlyter's method ("How to compute planetary positions"): Keplerian elements with secular
terms, the Moon's 12 largest longitude perturbations and Jupiter/Saturn/Uranus mutual
perturbations. Accuracy is a few arcminutes for the planets and the Moon - far finer than a dot
on the globe. Coordinates are geocentric, equinox of date.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

DEG = math.pi / 180


def _rev(x: float) -> float:
    return x % 360.0


def day_number(t: datetime) -> float:
    """Days since 2000 Jan 0.0 UT (Schlyter's d)."""
    jd = t.astimezone(timezone.utc).timestamp() / 86400.0 + 2440587.5
    return jd - 2451543.5


@dataclass(frozen=True)
class Elements:
    N: float  # longitude of the ascending node
    i: float  # inclination
    w: float  # argument of perihelion
    a: float  # semi-major axis (AU; Earth radii for the Moon)
    e: float  # eccentricity
    M: float  # mean anomaly


def elements(body: str, d: float) -> Elements:
    E = {
        "sun": (0.0, 0.0, 282.9404 + 4.70935e-5 * d, 1.0, 0.016709 - 1.151e-9 * d, 356.0470 + 0.9856002585 * d),
        "moon": (125.1228 - 0.0529538083 * d, 5.1454, 318.0634 + 0.1643573223 * d, 60.2666, 0.054900,
                 115.3654 + 13.0649929509 * d),
        "mercury": (48.3313 + 3.24587e-5 * d, 7.0047 + 5.00e-8 * d, 29.1241 + 1.01444e-5 * d, 0.387098,
                    0.205635 + 5.59e-10 * d, 168.6562 + 4.0923344368 * d),
        "venus": (76.6799 + 2.46590e-5 * d, 3.3946 + 2.75e-8 * d, 54.8910 + 1.38374e-5 * d, 0.723330,
                  0.006773 - 1.302e-9 * d, 48.0052 + 1.6021302244 * d),
        "mars": (49.5574 + 2.11081e-5 * d, 1.8497 - 1.78e-8 * d, 286.5016 + 2.92961e-5 * d, 1.523688,
                 0.093405 + 2.516e-9 * d, 18.6021 + 0.5240207766 * d),
        "jupiter": (100.4542 + 2.76854e-5 * d, 1.3030 - 1.557e-7 * d, 273.8777 + 1.64505e-5 * d, 5.20256,
                    0.048498 + 4.469e-9 * d, 19.8950 + 0.0830853001 * d),
        "saturn": (113.6634 + 2.38980e-5 * d, 2.4886 - 1.081e-7 * d, 339.3939 + 2.97661e-5 * d, 9.55475,
                   0.055546 - 9.499e-9 * d, 316.9670 + 0.0334442282 * d),
        "uranus": (74.0005 + 1.3978e-5 * d, 0.7733 + 1.9e-8 * d, 96.6612 + 3.0565e-5 * d, 19.18171 - 1.55e-8 * d,
                   0.047318 + 7.45e-9 * d, 142.5905 + 0.011725806 * d),
        "neptune": (131.7806 + 3.0173e-5 * d, 1.7700 - 2.55e-7 * d, 272.8461 - 6.027e-6 * d, 30.05826 + 3.313e-8 * d,
                    0.008606 + 2.15e-9 * d, 260.2471 + 0.005995147 * d),
    }[body]
    N, i, w, a, e, M = E
    return Elements(_rev(N), i, _rev(w), a, e, _rev(M))


def _kepler(el: Elements) -> tuple[float, float]:
    """(distance, true anomaly in degrees)."""
    M = el.M * DEG
    E = M + el.e * math.sin(M) * (1 + el.e * math.cos(M))
    for _ in range(10):
        dE = (E - el.e * math.sin(E) - M) / (1 - el.e * math.cos(E))
        E -= dE
        if abs(dE) < 1e-12:
            break
    x = el.a * (math.cos(E) - el.e)
    y = el.a * math.sqrt(1 - el.e * el.e) * math.sin(E)
    return math.hypot(x, y), math.atan2(y, x) / DEG


def _ecliptic(el: Elements, r: float, v: float) -> tuple[float, float, float]:
    N, i, vw = el.N * DEG, el.i * DEG, (v + el.w) * DEG
    return (r * (math.cos(N) * math.cos(vw) - math.sin(N) * math.sin(vw) * math.cos(i)),
            r * (math.sin(N) * math.cos(vw) + math.cos(N) * math.sin(vw) * math.cos(i)),
            r * math.sin(vw) * math.sin(i))


def _lonlat(x: float, y: float, z: float) -> tuple[float, float, float]:
    return _rev(math.atan2(y, x) / DEG), math.atan2(z, math.hypot(x, y)) / DEG, math.sqrt(x * x + y * y + z * z)


def _xyz(lon: float, lat: float, r: float) -> tuple[float, float, float]:
    return (r * math.cos(lon * DEG) * math.cos(lat * DEG), r * math.sin(lon * DEG) * math.cos(lat * DEG),
            r * math.sin(lat * DEG))


def sun_ecliptic(d: float) -> tuple[float, float, float]:
    """Geocentric ecliptic x, y, z of the Sun (AU)."""
    el = elements("sun", d)
    r, v = _kepler(el)
    lon = (v + el.w) * DEG
    return r * math.cos(lon), r * math.sin(lon), 0.0


def heliocentric(body: str, d: float) -> tuple[float, float, float]:
    """Heliocentric ecliptic x, y, z (AU) of a planet, with the main mutual perturbations."""
    el = elements(body, d)
    r, v = _kepler(el)
    x, y, z = _ecliptic(el, r, v)
    if body not in ("jupiter", "saturn", "uranus"):
        return x, y, z
    lon, lat, r = _lonlat(x, y, z)
    Mj, Ms, Mu = elements("jupiter", d).M, elements("saturn", d).M, elements("uranus", d).M
    s = lambda deg: math.sin(deg * DEG)  # noqa: E731
    c = lambda deg: math.cos(deg * DEG)  # noqa: E731
    if body == "jupiter":
        lon += (-0.332 * s(2 * Mj - 5 * Ms - 67.6) - 0.056 * s(2 * Mj - 2 * Ms + 21) + 0.042 * s(3 * Mj - 5 * Ms + 21)
                - 0.036 * s(Mj - 2 * Ms) + 0.022 * c(Mj - Ms) + 0.023 * s(2 * Mj - 3 * Ms + 52) - 0.016 * s(Mj - 5 * Ms - 69))
    elif body == "saturn":
        lon += (0.812 * s(2 * Mj - 5 * Ms - 67.6) - 0.229 * c(2 * Mj - 4 * Ms - 2) + 0.119 * s(Mj - 2 * Ms - 3)
                + 0.046 * s(2 * Mj - 6 * Ms - 69) + 0.014 * s(Mj - 3 * Ms + 32))
        lat += -0.020 * c(2 * Mj - 4 * Ms - 2) + 0.018 * s(2 * Mj - 6 * Ms - 49)
    else:
        lon += 0.040 * s(Ms - 2 * Mu + 6) + 0.035 * s(Ms - 3 * Mu + 33) - 0.015 * s(Mj - Mu + 20)
    return _xyz(lon, lat, r)


def moon_ecliptic(d: float) -> tuple[float, float, float]:
    """Geocentric ecliptic longitude, latitude (degrees) and distance (Earth radii) of the Moon."""
    el = elements("moon", d)
    r, v = _kepler(el)
    lon, lat, r = _lonlat(*_ecliptic(el, r, v))
    sun = elements("sun", d)
    Ms, Mm = sun.M, el.M
    Ls, Lm = _rev(sun.M + sun.w), _rev(el.M + el.w + el.N)
    D, F = _rev(Lm - Ls), _rev(Lm - el.N)
    s = lambda deg: math.sin(deg * DEG)  # noqa: E731
    c = lambda deg: math.cos(deg * DEG)  # noqa: E731
    lon += (-1.274 * s(Mm - 2 * D) + 0.658 * s(2 * D) - 0.186 * s(Ms) - 0.059 * s(2 * Mm - 2 * D)
            - 0.057 * s(Mm - 2 * D + Ms) + 0.053 * s(Mm + 2 * D) + 0.046 * s(2 * D - Ms) + 0.041 * s(Mm - Ms)
            - 0.035 * s(D) - 0.031 * s(Mm + Ms) - 0.015 * s(2 * F - 2 * D) + 0.011 * s(Mm - 4 * D))
    lat += (-0.173 * s(F - 2 * D) - 0.055 * s(Mm - F - 2 * D) - 0.046 * s(Mm + F - 2 * D)
            + 0.033 * s(F + 2 * D) + 0.017 * s(2 * Mm + F))
    r += -0.58 * c(Mm - 2 * D) - 0.46 * c(2 * D)
    return _rev(lon), lat, r


def obliquity(d: float) -> float:
    return 23.4393 - 3.563e-7 * d


def equatorial(x: float, y: float, z: float, d: float) -> tuple[float, float]:
    """Ecliptic x, y, z -> right ascension, declination (degrees)."""
    e = obliquity(d) * DEG
    xe, ye, ze = x, y * math.cos(e) - z * math.sin(e), y * math.sin(e) + z * math.cos(e)
    return _rev(math.atan2(ye, xe) / DEG), math.atan2(ze, math.hypot(xe, ye)) / DEG


def gmst(t: datetime) -> float:
    """Greenwich mean sidereal time in degrees."""
    jd = t.astimezone(timezone.utc).timestamp() / 86400.0 + 2440587.5
    T = (jd - 2451545.0) / 36525
    return _rev(280.46061837 + 360.98564736629 * (jd - 2451545.0) + 0.000387933 * T * T)


def separation(ra1: float, dec1: float, ra2: float, dec2: float) -> float:
    """Angle between two directions on the sky, degrees."""
    a = (math.sin(dec1 * DEG) * math.sin(dec2 * DEG)
         + math.cos(dec1 * DEG) * math.cos(dec2 * DEG) * math.cos((ra1 - ra2) * DEG))
    return math.acos(max(-1.0, min(1.0, a))) / DEG


PLANETS = ("mercury", "venus", "mars", "jupiter", "saturn", "uranus", "neptune")
SYMBOLS = {"sun": "☉", "moon": "☾", "mercury": "☿", "venus": "♀", "mars": "♂", "jupiter": "♃", "saturn": "♄",
           "uranus": "♅", "neptune": "♆"}
AU_KM = 149_597_870.7
EARTH_RADIUS_KM = 6378.14


def sky(t: datetime | None = None) -> dict:
    """Every body's right ascension/declination (the client turns these into the point on Earth
    it is overhead, as the Earth rotates), distance, elongation from the Sun, the Moon's phase,
    and heliocentric ecliptic positions for the orrery."""
    t = (t or datetime.now(timezone.utc)).astimezone(timezone.utc)
    d = day_number(t)
    xs, ys, zs = sun_ecliptic(d)
    sun_ra, sun_dec = equatorial(xs, ys, zs, d)
    bodies = [{"id": "sun", "name": "Sun", "symbol": SYMBOLS["sun"], "ra": sun_ra, "dec": sun_dec,
               "distanceKm": math.hypot(xs, ys) * AU_KM, "elongation": 0.0}]
    lon, lat, rm = moon_ecliptic(d)
    mra, mdec = equatorial(*_xyz(lon, lat, 1.0), d)
    elong = separation(mra, mdec, sun_ra, sun_dec)
    phase_angle = 180 - elong  # adequate for the Moon (the Sun is ~390 times further)
    illuminated = (1 + math.cos(phase_angle * DEG)) / 2
    # Position angle of the bright limb (from north towards east), Meeus 48.5.
    chi = math.atan2(math.cos(sun_dec * DEG) * math.sin((sun_ra - mra) * DEG),
                     math.sin(sun_dec * DEG) * math.cos(mdec * DEG)
                     - math.cos(sun_dec * DEG) * math.sin(mdec * DEG) * math.cos((sun_ra - mra) * DEG)) / DEG
    sun_lon = _rev(math.atan2(ys, xs) / DEG)
    waxing = _rev(lon - sun_lon) < 180
    bodies.append({"id": "moon", "name": "Moon", "symbol": SYMBOLS["moon"], "ra": mra, "dec": mdec,
                   "distanceKm": rm * EARTH_RADIUS_KM, "elongation": elong, "illuminated": illuminated,
                   "brightLimb": _rev(chi), "waxing": waxing, "phaseName": _phase_name(illuminated, waxing)})
    orrery = {"earth": [-xs, -ys, 0.0]}
    for p in PLANETS:
        xh, yh, zh = heliocentric(p, d)
        orrery[p] = [xh, yh, zh]
        xg, yg, zg = xh + xs, yh + ys, zh + zs
        ra, dec = equatorial(xg, yg, zg, d)
        bodies.append({"id": p, "name": p.capitalize(), "symbol": SYMBOLS[p], "ra": ra, "dec": dec,
                       "distanceKm": math.sqrt(xg * xg + yg * yg + zg * zg) * AU_KM,
                       "elongation": separation(ra, dec, sun_ra, sun_dec)})
    return {"at": t.isoformat(), "gmst": gmst(t), "bodies": bodies, "orrery": orrery}


def _phase_name(k: float, waxing: bool) -> str:
    if k < 0.02:
        return "New moon"
    if k > 0.98:
        return "Full moon"
    if abs(k - 0.5) < 0.04:
        return "First quarter" if waxing else "Last quarter"
    side = "Waxing" if waxing else "Waning"
    return f"{side} {'crescent' if k < 0.5 else 'gibbous'}"


def subpoint(ra: float, dec: float, t: datetime) -> tuple[float, float]:
    """(longitude, latitude) on Earth where a body at ra/dec is overhead at time t."""
    lon = ((ra - gmst(t)) + 540) % 360 - 180
    return lon, dec
