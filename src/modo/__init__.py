"""Multi Origin Distance Optimizer."""

from math import atan2, cos, degrees, hypot, isfinite, radians, sin

from geographiclib.geodesic import Geodesic
from scipy.optimize import minimize

from .compact import CompactRoadGraph, CompactStaticRoadAnalysis
from .nationwide import (
    LOWER_48_BOUNDS,
    CandidateAreaResult,
    CandidateEstimate,
    CoordinateBounds,
    NationwideEstimator,
    PromisingCandidateArea,
)
from .providers import (
    ProviderRoute,
    RoutingCoverageError,
    RoutingProviderError,
    ValhallaHTTPProvider,
)
from .road import (
    RoadResult,
    RoadRoute,
    RoadTravelTimes,
    StaticRoadAnalysis,
    analyze_coordinates,
    analyze_vertices,
    nearest_vertices,
    optimize_coordinates,
    optimize_vertices,
)

__version__ = "0.3.7"
_GEOD = Geodesic.WGS84


def _points(coordinates):
    try:
        points = [(float(lat), float(lon)) for lat, lon in coordinates]
    except (OverflowError, TypeError, ValueError) as error:
        raise ValueError(
            "coordinates must contain (latitude, longitude) pairs"
        ) from error
    if not points:
        raise ValueError("coordinates must not be empty")
    if any(
        not isfinite(lat) or not isfinite(lon) or abs(lat) > 90 or abs(lon) > 180
        for lat, lon in points
    ):
        raise ValueError("coordinates are out of range")
    return points


def _midpoint(first, second):
    path = _GEOD.Inverse(*first, *second)
    midpoint = _GEOD.Direct(*first, path["azi1"], path["s12"] / 2)
    return midpoint["lat2"], midpoint["lon2"]


def geographic_median(coordinates):
    """Return the WGS84 geographic median as ``(latitude, longitude)``."""
    points = _points(coordinates)
    if len(points) == 1:
        return points[0]
    if len(points) == 2:
        return _midpoint(*points)

    def total_distance(candidate):
        return sum(_GEOD.Inverse(*candidate, *point)["s12"] for point in points)

    x = sum(cos(radians(lat)) * cos(radians(lon)) for lat, lon in points)
    y = sum(cos(radians(lat)) * sin(radians(lon)) for lat, lon in points)
    z = sum(sin(radians(lat)) for lat, _ in points)
    center = (
        points[0]
        if x == y == z == 0
        else (degrees(atan2(z, hypot(x, y))), degrees(atan2(y, x)))
    )
    results = [
        minimize(
            total_distance,
            start,
            method="Powell",
            bounds=((-90, 90), (-180, 180)),
            options={"xtol": 1e-9, "ftol": 1e-12},
        )
        for start in dict.fromkeys([center, *points])
    ]
    valid = [result for result in results if result.success]
    if not valid:
        raise RuntimeError("geographic median did not converge")
    latitude, longitude = min(valid, key=lambda result: result.fun).x
    return float(latitude), float(longitude)


def minimax_center(coordinates):
    """Return the center minimizing the maximum WGS84 distance to a point.

    This uses multi-start numerical optimization and is intended for ordinary
    regional inputs. Antipodal and other global configurations can be non-unique
    or converge to a non-global solution.
    """
    points = _points(coordinates)
    if len(points) == 1:
        return points[0]
    if len(points) == 2:
        return _midpoint(*points)

    def maximum_distance(candidate):
        return max(_GEOD.Inverse(*candidate, *point)["s12"] for point in points)

    x = sum(cos(radians(lat)) * cos(radians(lon)) for lat, lon in points)
    y = sum(cos(radians(lat)) * sin(radians(lon)) for lat, lon in points)
    z = sum(sin(radians(lat)) for lat, _ in points)
    center = (
        min(points)
        if x == y == z == 0
        else (degrees(atan2(z, hypot(x, y))), degrees(atan2(y, x)))
    )
    starts = [center, *sorted(set(points))]
    results = [
        minimize(
            maximum_distance,
            start,
            method="Powell",
            bounds=((-90, 90), (-180, 180)),
            options={"xtol": 1e-9, "ftol": 1e-12},
        )
        for start in starts
    ]
    valid = [result for result in results if result.success]
    if not valid:
        raise RuntimeError("minimax center did not converge")
    latitude, longitude = min(valid, key=lambda result: result.fun).x
    return float(latitude), float(longitude)


__all__ = [
    "LOWER_48_BOUNDS",
    "CandidateAreaResult",
    "CandidateEstimate",
    "CompactRoadGraph",
    "CompactStaticRoadAnalysis",
    "CoordinateBounds",
    "NationwideEstimator",
    "PromisingCandidateArea",
    "ProviderRoute",
    "RoadResult",
    "RoadRoute",
    "RoadTravelTimes",
    "RoutingCoverageError",
    "RoutingProviderError",
    "StaticRoadAnalysis",
    "ValhallaHTTPProvider",
    "analyze_coordinates",
    "analyze_vertices",
    "geographic_median",
    "minimax_center",
    "nearest_vertices",
    "optimize_coordinates",
    "optimize_vertices",
]
