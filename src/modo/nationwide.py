"""Bounded nationwide candidate-area estimation over a routing provider."""

from __future__ import annotations

from dataclasses import dataclass
from math import fsum, isfinite, sqrt

from geographiclib.geodesic import Geodesic

from .providers import Coordinate, RoutingCoverageError, _coordinate


@dataclass(frozen=True)
class CoordinateBounds:
    south: float
    west: float
    north: float
    east: float

    def contains(self, coordinate: Coordinate) -> bool:
        latitude, longitude = coordinate
        return (
            self.south <= latitude <= self.north and self.west <= longitude <= self.east
        )


# A deliberately conservative rectangular gate. Provider coverage remains the
# authority for routability inside it; Alaska, Hawaii, and territories are not
# silently snapped into the lower-48 graph.
LOWER_48_BOUNDS = CoordinateBounds(24.396, -124.849, 49.384, -66.885)


@dataclass(frozen=True)
class CandidateEstimate:
    coordinate: Coordinate
    objective_seconds: float
    travel_times_seconds: tuple[float, ...]
    excess_seconds: float

    @property
    def identifier(self):
        """Return a coordinate-derived identifier stable across equal searches."""
        latitude, longitude = self.coordinate
        return f"modo:{latitude:.6f},{longitude:.6f}"


@dataclass(frozen=True)
class PromisingCandidateArea:
    """One spatially distinct catalog catchment around a sampled candidate."""

    identifier: str
    center: Coordinate
    radius_kilometers: float
    objective_seconds: float
    travel_times_seconds: tuple[float, ...]
    excess_seconds: float
    resolution_kilometers: float


@dataclass(frozen=True)
class CandidateAreaResult:
    """A sampled candidate area, never a claim of global road optimality."""

    coordinate: Coordinate
    objective: str
    objective_seconds: float
    travel_times_seconds: tuple[float, ...]
    candidates: tuple[CandidateEstimate, ...]
    evaluated_candidates: int
    matrix_requests: int
    prefilter_requests: int
    final_resolution_km: float
    approximate: bool = True
    method: str = "bounded coarse-to-fine matrix sampling"

    def promising_candidates(self, *, catchment_radius_kilometers, limit=8):
        """Select spatially distinct candidates for a downstream catalog.

        The caller-supplied catchment is an operational catalog-search radius.
        It is intentionally separate from ``final_resolution_km``, which only
        describes the optimizer's final sampling grid.
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 8:
            raise ValueError("candidate limit must be an integer from 1 through 8")
        try:
            radius = float(catchment_radius_kilometers)
        except (TypeError, ValueError) as error:
            raise ValueError(
                "catchment_radius_kilometers must be positive and at most 160"
            ) from error
        if (
            isinstance(catchment_radius_kilometers, bool)
            or not isfinite(radius)
            or not 0 < radius <= 160
        ):
            raise ValueError(
                "catchment_radius_kilometers must be positive and at most 160"
            )
        selected = []
        for candidate in self.candidates:
            if any(
                Geodesic.WGS84.Inverse(*candidate.coordinate, *item.center)["s12"]
                <= radius * 1000
                for item in selected
            ):
                continue
            selected.append(
                PromisingCandidateArea(
                    candidate.identifier,
                    candidate.coordinate,
                    radius,
                    candidate.objective_seconds,
                    candidate.travel_times_seconds,
                    candidate.excess_seconds,
                    self.final_resolution_km,
                )
            )
            if len(selected) == limit:
                break
        return tuple(selected)


class NationwideEstimator:
    """Estimate promising meeting areas using a capped number of matrix calls."""

    def __init__(
        self,
        provider,
        *,
        bounds: CoordinateBounds = LOWER_48_BOUNDS,
        coarse_candidate_limit: int = 81,
        refinement_rounds: int = 2,
        finalists_per_round: int = 6,
        matrix_target_limit: int = 40,
        matrix_pair_limit: int = 2500,
    ):
        for value, label, minimum in (
            (coarse_candidate_limit, "coarse_candidate_limit", 9),
            (refinement_rounds, "refinement_rounds", 0),
            (finalists_per_round, "finalists_per_round", 1),
            (matrix_target_limit, "matrix_target_limit", 1),
            (matrix_pair_limit, "matrix_pair_limit", 1),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ValueError(f"{label} must be an integer of at least {minimum}")
        self.provider = provider
        self.bounds = bounds
        self.coarse_candidate_limit = coarse_candidate_limit
        self.refinement_rounds = refinement_rounds
        self.finalists_per_round = finalists_per_round
        self.matrix_target_limit = matrix_target_limit
        self.matrix_pair_limit = matrix_pair_limit

    def estimate(
        self,
        origins,
        *,
        objective: str = "maximum",
        tolerance_seconds: float = 900,
    ) -> CandidateAreaResult:
        """Return bounded sampled candidates near the best observed score."""
        origins = tuple(
            _coordinate(value, f"origins[{index}]")
            for index, value in enumerate(origins)
        )
        if len(origins) < 2:
            raise ValueError("origins must contain at least two coordinates")
        provider_pair_limit = getattr(
            self.provider, "max_matrix_pairs", self.matrix_pair_limit
        )
        if (
            isinstance(provider_pair_limit, bool)
            or not isinstance(provider_pair_limit, int)
            or provider_pair_limit < 1
        ):
            raise ValueError("provider max_matrix_pairs must be a positive integer")
        pair_limit = min(self.matrix_pair_limit, provider_pair_limit)
        target_limit = min(self.matrix_target_limit, pair_limit // len(origins))
        if target_limit < 1:
            raise ValueError(f"origin count exceeds matrix pair limit ({pair_limit})")
        outside = [
            index
            for index, point in enumerate(origins)
            if not self.bounds.contains(point)
        ]
        if outside:
            joined = ", ".join(str(index) for index in outside)
            raise RoutingCoverageError(f"origins outside lower-48 coverage: {joined}")
        if objective not in {"maximum", "total"}:
            raise ValueError("objective must be 'maximum' or 'total'")
        try:
            tolerance_seconds = float(tolerance_seconds)
        except (TypeError, ValueError) as error:
            raise ValueError("tolerance_seconds must be nonnegative") from error
        if not isfinite(tolerance_seconds) or tolerance_seconds < 0:
            raise ValueError("tolerance_seconds must be nonnegative")

        search_bounds = self._search_bounds(origins)
        candidates, lat_step, lon_step = self._grid(
            search_bounds, self.coarse_candidate_limit
        )
        polygon = self._expanded_origin_hull(origins)
        if polygon:
            candidates = tuple(
                point for point in candidates if self._inside_polygon(point, polygon)
            )
        candidates = self._deduplicate(
            (*origins, self._mean_seed(origins), *candidates)
        )
        scores = {}
        rejected = set()
        request_count = 0
        prefilter_count = 0
        prefilter = getattr(self.provider, "filter_reachable", None)
        origins_checked = False

        def evaluate(batch):
            nonlocal request_count
            if not batch:
                return
            request_count += 1
            try:
                matrix = self.provider.matrix(origins, batch)
            except RoutingCoverageError:
                if len(batch) == 1:
                    rejected.add(batch[0])
                    return
                middle = len(batch) // 2
                evaluate(batch[:middle])
                evaluate(batch[middle:])
                return
            if len(matrix) != len(origins) or any(
                len(row) != len(batch) for row in matrix
            ):
                raise RuntimeError(
                    "routing provider returned invalid matrix dimensions"
                )
            for target_index, point in enumerate(batch):
                times = tuple(row[target_index] for row in matrix)
                if any(value is None for value in times):
                    rejected.add(point)
                    continue
                values = tuple(float(value) for value in times)
                if any(not isfinite(value) or value < 0 for value in values):
                    raise RuntimeError("routing provider returned invalid travel times")
                score = max(values) if objective == "maximum" else fsum(values)
                scores[point] = score, values

        for level in range(self.refinement_rounds + 1):
            fresh = tuple(
                point
                for point in candidates
                if point not in scores and point not in rejected
            )
            if callable(prefilter) and fresh:
                probe = (*(() if origins_checked else origins), *fresh)
                prefilter_count += 1
                reachable = tuple(prefilter(probe))
                if len(reachable) != len(probe):
                    raise RuntimeError(
                        "routing provider returned invalid prefilter dimensions"
                    )
                if not origins_checked:
                    origin_reachable = reachable[: len(origins)]
                    outside = [
                        index
                        for index, is_reachable in enumerate(origin_reachable)
                        if not is_reachable
                    ]
                    if outside:
                        joined = ", ".join(str(index) for index in outside)
                        raise RoutingCoverageError(
                            f"origins outside provider road coverage: {joined}"
                        )
                    origins_checked = True
                for point, is_reachable in zip(probe, reachable, strict=True):
                    if not is_reachable:
                        rejected.add(point)
                fresh = tuple(point for point in fresh if point not in rejected)
            for start in range(0, len(fresh), target_limit):
                batch = fresh[start : start + target_limit]
                evaluate(batch)
            if not scores:
                raise RoutingCoverageError(
                    "origins have no mutually reachable sampled candidate in provider coverage"
                )
            if level == self.refinement_rounds:
                break
            anchors = sorted(scores, key=lambda point: (scores[point][0], point))[
                : self.finalists_per_round
            ]
            lat_step /= 3
            lon_step /= 3
            candidates = self._neighborhoods(anchors, lat_step, lon_step)

        best = min(scores, key=lambda point: (scores[point][0], point))
        best_score, best_times = scores[best]
        resolution = self._resolution_km(best, lat_step, lon_step)
        area = tuple(
            CandidateEstimate(point, score, times, score - best_score)
            for point, (score, times) in sorted(
                scores.items(), key=lambda item: (item[1][0], item[0])
            )
            if score - best_score <= tolerance_seconds
        )
        return CandidateAreaResult(
            best,
            objective,
            best_score,
            best_times,
            area,
            len(scores),
            request_count,
            prefilter_count,
            resolution,
        )

    def _search_bounds(self, origins):
        latitudes = [point[0] for point in origins]
        longitudes = [point[1] for point in origins]
        lat_span = max(max(latitudes) - min(latitudes), 1.0)
        lon_span = max(max(longitudes) - min(longitudes), 1.0)
        lat_pad, lon_pad = lat_span * 0.12, lon_span * 0.12
        return CoordinateBounds(
            max(self.bounds.south, min(latitudes) - lat_pad),
            max(self.bounds.west, min(longitudes) - lon_pad),
            min(self.bounds.north, max(latitudes) + lat_pad),
            min(self.bounds.east, max(longitudes) + lon_pad),
        )

    def _expanded_origin_hull(self, origins):
        points = sorted({(longitude, latitude) for latitude, longitude in origins})
        if len(points) < 3:
            return ()

        def cross(first, middle, last):
            return (middle[0] - first[0]) * (last[1] - first[1]) - (
                middle[1] - first[1]
            ) * (last[0] - first[0])

        lower = []
        for point in points:
            while len(lower) >= 2 and cross(lower[-2], lower[-1], point) <= 0:
                lower.pop()
            lower.append(point)
        upper = []
        for point in reversed(points):
            while len(upper) >= 2 and cross(upper[-2], upper[-1], point) <= 0:
                upper.pop()
            upper.append(point)
        hull = lower[:-1] + upper[:-1]
        if len(hull) < 3:
            return ()
        center_lon = fsum(point[0] for point in hull) / len(hull)
        center_lat = fsum(point[1] for point in hull) / len(hull)
        return tuple(
            (
                max(
                    self.bounds.south,
                    min(self.bounds.north, center_lat + (latitude - center_lat) * 1.12),
                ),
                max(
                    self.bounds.west,
                    min(self.bounds.east, center_lon + (longitude - center_lon) * 1.12),
                ),
            )
            for longitude, latitude in hull
        )

    @staticmethod
    def _inside_polygon(point, polygon):
        latitude, longitude = point
        inside = False
        previous = polygon[-1]
        for current in polygon:
            first_lat, first_lon = previous
            second_lat, second_lon = current
            if (first_lat > latitude) != (second_lat > latitude):
                crossing = (second_lon - first_lon) * (latitude - first_lat) / (
                    second_lat - first_lat
                ) + first_lon
                if longitude < crossing:
                    inside = not inside
            previous = current
        return inside

    @staticmethod
    def _mean_seed(origins):
        return fsum(point[0] for point in origins) / len(origins), fsum(
            point[1] for point in origins
        ) / len(origins)

    @staticmethod
    def _grid(bounds, limit):
        lat_span, lon_span = bounds.north - bounds.south, bounds.east - bounds.west
        aspect = max(lon_span / max(lat_span, 1e-9), 1e-9)
        columns = max(3, int(sqrt(limit * aspect)))
        rows = max(3, int(limit / columns))
        while rows * columns > limit:
            columns -= 1
        lat_step = lat_span / max(rows - 1, 1)
        lon_step = lon_span / max(columns - 1, 1)
        points = tuple(
            (bounds.south + row * lat_step, bounds.west + column * lon_step)
            for row in range(rows)
            for column in range(columns)
        )
        return points, lat_step, lon_step

    def _neighborhoods(self, anchors, lat_step, lon_step):
        return self._deduplicate(
            (
                max(
                    self.bounds.south, min(self.bounds.north, latitude + dy * lat_step)
                ),
                max(self.bounds.west, min(self.bounds.east, longitude + dx * lon_step)),
            )
            for latitude, longitude in anchors
            for dy in (-1, 0, 1)
            for dx in (-1, 0, 1)
        )

    @staticmethod
    def _deduplicate(points):
        return tuple(
            dict.fromkeys((round(lat, 6), round(lon, 6)) for lat, lon in points)
        )

    @staticmethod
    def _resolution_km(center, lat_step, lon_step):
        latitude, longitude = center
        corner = (latitude + lat_step / 2, longitude + lon_step / 2)
        return round(Geodesic.WGS84.Inverse(*center, *corner)["s12"] / 1000, 3)
