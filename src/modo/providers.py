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

Coordinate = tuple[float, float]


class RoutingProviderError(RuntimeError):
    """A routing provider could not complete a request."""


class RoutingCoverageError(RoutingProviderError):
    """A coordinate is outside coverage or cannot reach the requested target."""


def _valhalla_error(action, document, *, http_status=None):
    message = str(document.get("error", "request failed"))
    code = document.get("error_code", "unknown")
    detail = f"Valhalla {action} failed ({code}): {message}"
    lowered = message.lower()
    if code == 170 or any(
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
    into each matrix call, and the small LRU avoids repeating identical calls
    during an interactive private session.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        costing: str = "auto",
        timeout_seconds: float = 10,
        cache_size: int = 128,
        opener: Callable = urlopen,
    ):
        endpoint = str(endpoint).rstrip("/")
        if not endpoint.startswith(("http://", "https://")):
            raise ValueError("endpoint must use http or https")
        if not isinstance(costing, str) or not costing:
            raise ValueError("costing must be a nonempty string")
        if not isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if (
            isinstance(cache_size, bool)
            or not isinstance(cache_size, int)
            or cache_size < 0
        ):
            raise ValueError("cache_size must be a nonnegative integer")
        self.endpoint = endpoint
        self.costing = costing
        self.timeout_seconds = float(timeout_seconds)
        self.cache_size = cache_size
        self._opener = opener
        self._cache = OrderedDict()
        self._cache_lock = Lock()
        self.request_count = 0

    @staticmethod
    def _locations(coordinates: Sequence[Coordinate], label: str):
        if not coordinates:
            raise ValueError(f"{label} must not be empty")
        return [
            {"lat": latitude, "lon": longitude}
            for latitude, longitude in (
                _coordinate(value, f"{label}[{index}]")
                for index, value in enumerate(coordinates)
            )
        ]

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
        if not isinstance(result, dict):
            raise RoutingProviderError("Valhalla returned an invalid response")
        if result.get("error"):
            raise _valhalla_error(action, result)
        self.request_count += 1
        if self.cache_size:
            with self._cache_lock:
                self._cache[key] = result
                while len(self._cache) > self.cache_size:
                    self._cache.popitem(last=False)
        return result

    def matrix(
        self, origins: Sequence[Coordinate], targets: Sequence[Coordinate]
    ) -> tuple[tuple[float | None, ...], ...]:
        """Return seconds from every origin to every target.

        Unreachable cells are ``None`` so a bounded estimator can discard only
        those candidates. Malformed or dimensionally inconsistent responses
        fail the entire request.
        """
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
        rows = result.get("sources_to_targets")
        if not isinstance(rows, list):
            raise RoutingProviderError("Valhalla matrix response has no rows")
        if rows and isinstance(rows[0], dict):
            nested = [[None for _ in target_locations] for _ in source_locations]
            for cell in rows:
                try:
                    nested[int(cell["from_index"])][int(cell["to_index"])] = cell
                except (IndexError, KeyError, TypeError, ValueError) as error:
                    raise RoutingProviderError(
                        "Valhalla matrix cell has invalid indexes"
                    ) from error
            rows = nested
        if len(rows) != len(source_locations) or any(
            not isinstance(row, list) or len(row) != len(target_locations)
            for row in rows
        ):
            raise RoutingProviderError(
                "Valhalla matrix response has invalid dimensions"
            )
        parsed = []
        for row in rows:
            parsed_row = []
            for cell in row:
                value = cell.get("time") if isinstance(cell, dict) else None
                if value is None:
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
        return ProviderRoute(origin, destination, seconds, tuple(coordinates))
