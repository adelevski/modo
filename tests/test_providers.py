import io
import json
from urllib.error import URLError

import pytest

from modo import RoutingCoverageError, RoutingProviderError, ValhallaHTTPProvider


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def matrix_cell(time, source, target):
    if time is None:
        return {"time": None}
    return {
        "time": time,
        "begin_lat": source[0],
        "begin_lon": source[1],
        "end_lat": target[0],
        "end_lon": target[1],
    }


def test_valhalla_matrix_parses_rows_and_caches_identical_request():
    calls = []

    def opener(request, timeout):
        calls.append((request, timeout))
        sources = [(41.0, -87.0), (42.0, -88.0)]
        targets = [(41.5, -87.5), (41.6, -87.6)]
        return Response(
            json.dumps(
                {
                    "sources_to_targets": [
                        [
                            matrix_cell(4, sources[0], targets[0]),
                            matrix_cell(None, sources[0], targets[1]),
                        ],
                        [
                            matrix_cell(7.5, sources[1], targets[0]),
                            matrix_cell(8, sources[1], targets[1]),
                        ],
                    ]
                }
            ).encode()
        )

    provider = ValhallaHTTPProvider(
        "http://router.test/", timeout_seconds=3, opener=opener
    )
    origins = [(41.0, -87.0), (42.0, -88.0)]
    targets = [(41.5, -87.5), (41.6, -87.6)]

    assert provider.matrix(origins, targets) == ((4.0, None), (7.5, 8.0))
    assert provider.matrix(origins, targets) == ((4.0, None), (7.5, 8.0))
    assert len(calls) == 1
    assert provider.request_count == 1
    assert provider.successful_request_count == 1
    assert calls[0][1] == 3
    body = json.loads(calls[0][0].data)
    assert body["verbose"] is True
    assert body["costing"] == "auto"
    assert all(item["search_cutoff"] == 1000 for item in body["sources"])
    assert all(item["search_cutoff"] == 1000 for item in body["targets"])


def test_valhalla_matrix_parses_flat_verbose_cells():
    def opener(request, timeout):
        target = (41.5, -87.5)
        return Response(
            json.dumps(
                {
                    "sources_to_targets": [
                        {
                            "from_index": 1,
                            "to_index": 0,
                            **matrix_cell(7, (42, -88), target),
                        },
                        {
                            "from_index": 0,
                            "to_index": 0,
                            **matrix_cell(4, (41, -87), target),
                        },
                    ]
                }
            ).encode()
        )

    provider = ValhallaHTTPProvider("http://router.test", opener=opener)
    assert provider.matrix([(41, -87), (42, -88)], [(41.5, -87.5)]) == (
        (4.0,),
        (7.0,),
    )


def test_valhalla_matrix_marks_far_snapped_target_unreachable():
    def opener(request, timeout):
        return Response(
            json.dumps(
                {"sources_to_targets": [[matrix_cell(20, (41, -87), (41.2, -87))]]}
            ).encode()
        )

    provider = ValhallaHTTPProvider("http://router.test", opener=opener)
    assert provider.matrix([(41, -87)], [(41, -87)]) == ((None,),)


def test_valhalla_locate_prefilters_mixed_coordinates_in_one_request():
    def opener(request, timeout):
        body = json.loads(request.data)
        assert all(item["search_cutoff"] == 1000 for item in body["locations"])
        return Response(
            json.dumps(
                [
                    {"edges": [{"correlated_lat": 41.0, "correlated_lon": -87.0}]},
                    {"edges": []},
                ]
            ).encode()
        )

    provider = ValhallaHTTPProvider("http://router.test", opener=opener)
    assert provider.filter_reachable([(41, -87), (41, -86)]) == (True, False)
    assert provider.request_count == 1
    assert provider.successful_request_count == 1


def test_valhalla_route_parses_geojson_and_joins_leg_boundaries():
    def opener(request, timeout):
        return Response(
            json.dumps(
                {
                    "trip": {
                        "summary": {"time": 12.5},
                        "legs": [
                            {
                                "shape": {
                                    "type": "LineString",
                                    "coordinates": [[-87.0, 41.0], [-87.1, 41.1]],
                                }
                            },
                            {
                                "shape": {
                                    "type": "LineString",
                                    "coordinates": [[-87.1, 41.1], [-87.2, 41.2]],
                                }
                            },
                        ],
                    }
                }
            ).encode()
        )

    route = ValhallaHTTPProvider("http://router.test", opener=opener).route(
        (41, -87), (41.2, -87.2)
    )
    assert route.travel_time_seconds == 12.5
    assert route.coordinates == ((41.0, -87.0), (41.1, -87.1), (41.2, -87.2))


def test_valhalla_route_decodes_native_polyline6():
    def encode(coordinates):
        previous = [0, 0]
        output = []
        for coordinate in coordinates:
            current = [round(value * 1_000_000) for value in coordinate]
            for index, value in enumerate(current):
                delta = value - previous[index]
                previous[index] = value
                encoded = ~(delta << 1) if delta < 0 else delta << 1
                while encoded >= 0x20:
                    output.append(chr((0x20 | (encoded & 0x1F)) + 63))
                    encoded >>= 5
                output.append(chr(encoded + 63))
        return "".join(output)

    shape = encode([(41.0, -87.0), (41.1, -87.1), (41.2, -87.2)])

    def opener(request, timeout):
        return Response(
            json.dumps(
                {
                    "trip": {
                        "summary": {"time": 12.5},
                        "legs": [{"shape": shape}],
                    }
                }
            ).encode()
        )

    route = ValhallaHTTPProvider("http://router.test", opener=opener).route(
        (41, -87), (41.2, -87.2)
    )
    assert route.coordinates == ((41.0, -87.0), (41.1, -87.1), (41.2, -87.2))


def test_valhalla_transport_failure_is_clear():
    def opener(request, timeout):
        raise URLError("offline")

    provider = ValhallaHTTPProvider("http://router.test", opener=opener)
    with pytest.raises(RoutingProviderError, match="request failed.*offline"):
        provider.matrix([(41, -87)], [(42, -88)])


def test_valhalla_json_error_is_clear():
    def opener(request, timeout):
        return Response(json.dumps({"error_code": 154, "error": "No path"}).encode())

    provider = ValhallaHTTPProvider("http://router.test", opener=opener)
    with pytest.raises(RoutingCoverageError, match=r"failed \(154\): No path"):
        provider.matrix([(41, -87)], [(42, -88)])


def test_valhalla_no_suitable_edges_is_a_coverage_error():
    def opener(request, timeout):
        return Response(
            json.dumps(
                {"error_code": 171, "error": "No suitable edges near location"}
            ).encode()
        )

    provider = ValhallaHTTPProvider("http://router.test", opener=opener)
    with pytest.raises(
        RoutingCoverageError, match=r"failed \(171\): No suitable edges"
    ):
        provider.matrix([(41, -87)], [(42, -88)])
    assert provider.request_count == 1
    assert provider.successful_request_count == 0
