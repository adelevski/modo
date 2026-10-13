"""Routing-provider contracts and a small Valhalla HTTP adapter."""

from __future__ import annotations

import json
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from math import isfinite
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from geographiclib.geodesic import Geodesic

Coordinate = tuple[float, float]
DEFAULT_MAX_SNAP_DISTANCE_KILOMETERS = 1.0
DEFAULT_MAX_MATRIX_PAIRS = 2500


class RoutingProviderError(RuntimeError):
    """A routing provider could not complete a request."""


class RoutingCoverageError(RoutingProviderError):
    """A coordinate is outside coverage or cannot reach the requested target."""


def _valhalla_error(action, document, *, http_status=None):
    message = str(document.get("error", "request failed"))
    code = document.get("error_code", "unknown")
    detail = f"Valhalla {action} failed ({code}): {message}"
    lowered = message.lower()
    if code in {170, 171} or any(
        phrase in lowered
        for phrase in ("unconnected regions", "no path", "outside coverage")
    ):
        return RoutingCoverageError(detail)
    if http_status is not None:
        detail = f"{detail} [HTTP {http_status}]"
    return RoutingProviderError(detail)


def _decode_polyline6(shape):
    if not isinstance(shape, str) or not shape:
        raise RoutingProviderError("Valhalla route geometry is missing")
    coordinates = []
    index = latitude = longitude = 0
    try:
        while index < len(shape):
            deltas = []
            for _ in range(2):
                value = shift = 0
                while True:
                    chunk = ord(shape[index]) - 63
                    index += 1
                    value |= (chunk & 0x1F) << shift
                    shift += 5
                    if chunk < 0x20:
                        break
                deltas.append(~(value >> 1) if value & 1 else value >> 1)
            latitude += deltas[0]
            longitude += deltas[1]
            coordinates.append((latitude / 1_000_000, longitude / 1_000_000))
    except (IndexError, ValueError) as error:
        raise RoutingProviderError("Valhalla route geometry is invalid") from error
    return tuple(coordinates)


@dataclass(frozen=True)
class ProviderRoute:
    """One provider route, with geometry when the provider returned it."""

    origin: Coordinate
    destination: Coordinate
    travel_time_seconds: float
    coordinates: tuple[Coordinate, ...]


def _coordinate(value, label="coordinate") -> Coordinate:
    try:
        latitude, longitude = value
        latitude, longitude = float(latitude), float(longitude)
    except (OverflowError, TypeError, ValueError) as error:
        raise ValueError(f"{label} must be a latitude/longitude pair") from error
    if (
        not isfinite(latitude)
        or not isfinite(longitude)
        or not -90 <= latitude <= 90
        or not -180 <= longitude <= 180
    ):
        raise ValueError(f"{label} is out of range")
    return latitude, longitude


class ValhallaHTTPProvider:
    """Route through a Valhalla HTTP service without adding a dependency.

    The adapter is intentionally synchronous. Estimation batches many targets
    into each matrix call. Response caching is disabled by default so a shared
    provider does not retain request coordinates between calculations. An
    explicit cache belongs to one calculation; use the context manager or
    ``clear_cache()`` to discard it on success or failure.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        costing: str = "auto",
        timeout_seconds: float = 10,
        cache_size: int = 0,
        max_matrix_pairs: int = DEFAULT_MAX_MATRIX_PAIRS,
        max_snap_distance_kilometers: float = DEFAULT_MAX_SNAP_DISTANCE_KILOMETERS,
        opener: Callable = urlopen,
    ):
        endpoint = str(endpoint).rstrip("/")
        if not endpoint.startswith(("http://", "https://")):
            raise ValueError("endpoint must use http or https")
        if not isinstance(costing, str) or not costing:
            raise ValueError("costing must be a nonempty string")
        if not isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        try:
            snap_limit = float(max_snap_distance_kilometers)
        except (TypeError, ValueError) as error:
            raise ValueError("max_snap_distance_kilometers must be positive") from error
        if (
            isinstance(max_snap_distance_kilometers, bool)
            or not isfinite(snap_limit)
            or snap_limit <= 0
        ):
            raise ValueError("max_snap_distance_kilometers must be positive")
        if (
            isinstance(cache_size, bool)
            or not isinstance(cache_size, int)
            or cache_size < 0
        ):
            raise ValueError("cache_size must be a nonnegative integer")
        if (
            isinstance(max_matrix_pairs, bool)
            or not isinstance(max_matrix_pairs, int)
            or max_matrix_pairs < 1
        ):
            raise ValueError("max_matrix_pairs must be a positive integer")
        self.endpoint = endpoint
        self.costing = costing
        self.timeout_seconds = float(timeout_seconds)
        self.cache_size = cache_size
        self.max_matrix_pairs = max_matrix_pairs
        self.max_snap_distance_kilometers = snap_limit
        self._opener = opener
        self._cache = OrderedDict()
        self._cache_lock = Lock()
        self.request_count = 0
        self.successful_request_count = 0

    def clear_cache(self):
        """Discard cached request coordinates and provider responses."""
        with self._cache_lock:
            self._cache.clear()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.clear_cache()

    def _locations(self, coordinates: Sequence[Coordinate], label: str):
        if not coordinates:
            raise ValueError(f"{label} must not be empty")
        return [
            {
                "lat": latitude,
                "lon": longitude,
                "search_cutoff": max(
                    1, round(self.max_snap_distance_kilometers * 1000)
                ),
            }
            for latitude, longitude in (
                _coordinate(value, f"{label}[{index}]")
                for index, value in enumerate(coordinates)
            )
        ]

    @staticmethod
    def _snap_distance_kilometers(requested, snapped):
        return Geodesic.WGS84.Inverse(*requested, *snapped)["s12"] / 1000

    def _validate_snap(self, requested, snapped, label):
        distance = self._snap_distance_kilometers(requested, snapped)
        if distance > self.max_snap_distance_kilometers:
            raise RoutingCoverageError(
                f"{label} snapped {distance:.3f} km to the road; maximum is "
                f"{self.max_snap_distance_kilometers:g} km"
            )

    def _request(self, action: str, payload: dict):
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        key = action, encoded
        with self._cache_lock:
            if key in self._cache:
                result = self._cache.pop(key)
                self._cache[key] = result
                return result
        request = Request(
            f"{self.endpoint}/{action}",
            data=encoded,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        self.request_count += 1
        try:
            with self._opener(request, timeout=self.timeout_seconds) as response:
                raw = response.read(16 * 1024 * 1024 + 1)
        except HTTPError as error:
            raw = error.read(4096)
            try:
                document = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                detail = raw.decode("utf-8", "replace")
                raise RoutingProviderError(
                    f"Valhalla {action} failed with HTTP {error.code}: {detail}"
                ) from error
            raise _valhalla_error(action, document, http_status=error.code) from error
        except (TimeoutError, URLError, OSError) as error:
            raise RoutingProviderError(
                f"Valhalla {action} request failed: {error}"
            ) from error
        if len(raw) > 16 * 1024 * 1024:
            raise RoutingProviderError("Valhalla response exceeded 16 MiB")
        try:
            result = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RoutingProviderError("Valhalla returned invalid JSON") from error
        if not isinstance(result, (dict, list)):
            raise RoutingProviderError("Valhalla returned an invalid response")
        if isinstance(result, dict) and result.get("error"):
            raise _valhalla_error(action, result)
        self.successful_request_count += 1
        if self.cache_size:
            with self._cache_lock:
                self._cache[key] = result
                while len(self._cache) > self.cache_size:
                    self._cache.popitem(last=False)
        return result

    def filter_reachable(self, coordinates: Sequence[Coordinate]) -> tuple[bool, ...]:
        """Return which coordinates correlate to an auto-accessible road."""
        coordinates = tuple(
            _coordinate(value, f"coordinates[{index}]")
            for index, value in enumerate(coordinates)
        )
        result = self._request(
            "locate",
            {
                "locations": self._locations(coordinates, "coordinates"),
                "costing": self.costing,
                "verbose": False,
            },
        )
        if not isinstance(result, list) or len(result) != len(coordinates):
            raise RoutingProviderError(
                "Valhalla locate response has invalid dimensions"
            )
        reachable = []
        for index, (requested, item) in enumerate(
            zip(coordinates, result, strict=True)
        ):
            edges = item.get("edges") if isinstance(item, dict) else None
            if not isinstance(edges, list):
                raise RoutingProviderError("Valhalla locate response is invalid")
            if not edges:
                reachable.append(False)
                continue
            edge = edges[0]
            try:
                snapped = _coordinate(
                    (edge["correlated_lat"], edge["correlated_lon"]),
                    "locate snapped coordinate",
                )
            except (KeyError, TypeError, ValueError) as error:
                raise RoutingProviderError(
                    "Valhalla locate response is missing snapped coordinates"
                ) from error
            reachable.append(
                self._snap_distance_kilometers(requested, snapped)
                <= self.max_snap_distance_kilometers
            )
        return tuple(reachable)

    def matrix(
        self, origins: Sequence[Coordinate], targets: Sequence[Coordinate]
    ) -> tuple[tuple[float | None, ...], ...]:
        """Return seconds from every origin to every target.

        Unreachable cells are ``None`` so a bounded estimator can discard only
        those candidates. Malformed or dimensionally inconsistent responses
        fail the entire request.
        """
        origins = tuple(
            _coordinate(value, f"origins[{index}]")
            for index, value in enumerate(origins)
        )
        targets = tuple(
            _coordinate(value, f"targets[{index}]")
            for index, value in enumerate(targets)
        )
        if len(origins) * len(targets) > self.max_matrix_pairs:
            raise ValueError(
                f"matrix exceeds max_matrix_pairs ({self.max_matrix_pairs})"
            )
        source_locations = self._locations(origins, "origins")
        target_locations = self._locations(targets, "targets")
        result = self._request(
            "sources_to_targets",
            {
                "sources": source_locations,
                "targets": target_locations,
                "costing": self.costing,
                "verbose": True,
            },
        )
        rows = result.get("sources_to_targets") if isinstance(result, dict) else None
        if not isinstance(rows, list):
            raise RoutingProviderError("Valhalla matrix response has no rows")
        if rows and isinstance(rows[0], dict):
            nested = [[None for _ in target_locations] for _ in source_locations]
            seen = set()
            for cell in rows:
                if not isinstance(cell, dict):
                    raise RoutingProviderError("Valhalla matrix cell is invalid")
                source_index = cell.get("from_index")
                target_index = cell.get("to_index")
                if (
                    type(source_index) is not int
                    or type(target_index) is not int
                    or not 0 <= source_index < len(origins)
                    or not 0 <= target_index < len(targets)
                ):
                    raise RoutingProviderError(
                        "Valhalla matrix cell has invalid indexes"
                    )
                pair = source_index, target_index
                if pair in seen:
                    raise RoutingProviderError("Valhalla matrix has duplicate cells")
                seen.add(pair)
                nested[source_index][target_index] = cell
            if len(seen) != len(origins) * len(targets):
                raise RoutingProviderError("Valhalla matrix is incomplete")
            rows = nested
        if len(rows) != len(source_locations) or any(
            not isinstance(row, list) or len(row) != len(target_locations)
            for row in rows
        ):
            raise RoutingProviderError(
                "Valhalla matrix response has invalid dimensions"
            )
        parsed = []
        for source_index, row in enumerate(rows):
            parsed_row = []
            for target_index, cell in enumerate(row):
                if not isinstance(cell, dict) or "time" not in cell:
                    raise RoutingProviderError("Valhalla matrix cell is invalid")
                if ("from_index" in cell or "to_index" in cell) and (
                    type(cell.get("from_index")) is not int
                    or type(cell.get("to_index")) is not int
                    or cell["from_index"] != source_index
                    or cell["to_index"] != target_index
                ):
                    raise RoutingProviderError(
                        "Valhalla matrix cell has invalid indexes"
                    )
                value = cell["time"]
                if value is None:
                    parsed_row.append(None)
                    continue
                try:
                    snapped_source = _coordinate(
                        (cell["begin_lat"], cell["begin_lon"]),
                        "matrix snapped source",
                    )
                    snapped_target = _coordinate(
                        (cell["end_lat"], cell["end_lon"]),
                        "matrix snapped target",
                    )
                except (KeyError, ValueError) as error:
                    raise RoutingProviderError(
                        "Valhalla matrix response is missing snapped coordinates"
                    ) from error
                self._validate_snap(
                    origins[source_index],
                    snapped_source,
                    f"origin {source_index}",
                )
                if (
                    self._snap_distance_kilometers(
                        targets[target_index], snapped_target
                    )
                    > self.max_snap_distance_kilometers
                ):
                    parsed_row.append(None)
                    continue
                try:
                    value = float(value)
                except (TypeError, ValueError) as error:
                    raise RoutingProviderError(
                        "Valhalla matrix time is invalid"
                    ) from error
                if not isfinite(value) or value < 0:
                    raise RoutingProviderError("Valhalla matrix time is invalid")
                parsed_row.append(value)
            parsed.append(tuple(parsed_row))
        return tuple(parsed)

    def route(self, origin: Coordinate, destination: Coordinate) -> ProviderRoute:
        """Return a route and GeoJSON-shaped geometry between two coordinates."""
        origin, destination = _coordinate(origin), _coordinate(destination)
        result = self._request(
            "route",
            {
                "locations": self._locations([origin, destination], "locations"),
                "costing": self.costing,
                "shape_format": "polyline6",
            },
        )
        trip = result.get("trip")
        if not isinstance(trip, dict):
            raise RoutingCoverageError("Valhalla found no route")
        try:
            seconds = float(trip["summary"]["time"])
            legs = trip["legs"]
        except (KeyError, TypeError, ValueError) as error:
            raise RoutingProviderError(
                "Valhalla route response is incomplete"
            ) from error
        if (
            not isfinite(seconds)
            or seconds < 0
            or not isinstance(legs, list)
            or not legs
        ):
            raise RoutingProviderError("Valhalla route response is invalid")
        coordinates = []
        for leg in legs:
            shape = leg.get("shape") if isinstance(leg, dict) else None
            if isinstance(shape, str):
                raw_coordinates = _decode_polyline6(shape)
            else:
                raw_coordinates = (
                    shape.get("coordinates") if isinstance(shape, dict) else None
                )
                if not isinstance(raw_coordinates, list):
                    raise RoutingProviderError("Valhalla route geometry is missing")
                raw_coordinates = ((value[1], value[0]) for value in raw_coordinates)
            for value in raw_coordinates:
                point = _coordinate(value, "route coordinate")
                if not coordinates or point != coordinates[-1]:
                    coordinates.append(point)
        if not coordinates:
            raise RoutingProviderError("Valhalla route geometry is missing")
        self._validate_snap(origin, coordinates[0], "route origin")
        self._validate_snap(destination, coordinates[-1], "route destination")
        return ProviderRoute(origin, destination, seconds, tuple(coordinates))
