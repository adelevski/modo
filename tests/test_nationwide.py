import io
import json
from math import hypot

import pytest
from geographiclib.geodesic import Geodesic

from modo import NationwideEstimator, RoutingCoverageError, ValhallaHTTPProvider


class PlaneProvider:
    def __init__(self, unreachable=None):
        self.unreachable = unreachable or (lambda point: False)
        self.calls = []

    def matrix(self, origins, targets):
        self.calls.append((tuple(origins), tuple(targets)))
        return tuple(
            tuple(
                None
                if self.unreachable(target)
                else hypot(origin[0] - target[0], origin[1] - target[1]) * 3600
                for target in targets
            )
            for origin in origins
        )


class RejectingBatchProvider(PlaneProvider):
    def __init__(self, unreachable=None):
        super().__init__(unreachable)
        self.singleton_rejections = []

    def matrix(self, origins, targets):
        if any(self.unreachable(target) for target in targets):
            if len(targets) > 1:
                raise RoutingCoverageError("batch contains an unconnected location")
            self.singleton_rejections.append(targets[0])
            raise RoutingCoverageError("location is unconnected")
        return super().matrix(origins, targets)


class PrefilteringPlaneProvider(PlaneProvider):
    def __init__(self, unreachable=None):
        super().__init__(unreachable)
        self.prefilter_calls = []

    def filter_reachable(self, coordinates):
        self.prefilter_calls.append(tuple(coordinates))
        return tuple(not self.unreachable(point) for point in coordinates)


class Valhalla171Opener:
    def __init__(self, *, reject_sources=False):
        self.reject_sources = reject_sources

    def __call__(self, request, timeout):
        document = json.loads(request.data)
        targets = document["targets"]
        if self.reject_sources or any(target["lon"] < -87.5 for target in targets):
            result = {
                "error_code": 171,
                "error": "No suitable edges near location",
            }
        else:
            result = {
                "sources_to_targets": [
                    [
                        {
                            "time": hypot(
                                source["lat"] - target["lat"],
                                source["lon"] - target["lon"],
                            )
                            * 3600,
                            "begin_lat": source["lat"],
                            "begin_lon": source["lon"],
                            "end_lat": target["lat"],
                            "end_lon": target["lon"],
                        }
                        for target in targets
                    ]
                    for source in document["sources"]
                ]
            }
        return Response(json.dumps(result).encode())


class FarSnapOpener:
    def __init__(self, *, reject_sources=False):
        self.reject_sources = reject_sources

    def __call__(self, request, timeout):
        document = json.loads(request.data)
        result = {
            "sources_to_targets": [
                [
                    {
                        "time": hypot(
                            source["lat"] - target["lat"],
                            source["lon"] - target["lon"],
                        )
                        * 3600,
                        "begin_lat": source["lat"]
                        + (0.2 if self.reject_sources else 0),
                        "begin_lon": source["lon"],
                        "end_lat": target["lat"]
                        + (0.2 if target["lon"] < -87.5 else 0),
                        "end_lon": target["lon"],
                    }
                    for target in document["targets"]
                ]
                for source in document["sources"]
            ]
        }
        return Response(json.dumps(result).encode())


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class NoPrefilterValhalla(ValhallaHTTPProvider):
    filter_reachable = None


def test_estimator_refines_near_known_minimax_optimum():
    provider = PlaneProvider()
    estimator = NationwideEstimator(
        provider,
        coarse_candidate_limit=36,
        refinement_rounds=3,
        finalists_per_round=4,
        matrix_target_limit=7,
    )

    result = estimator.estimate(
        [(41.0, -90.0), (41.0, -86.0)],
        objective="maximum",
        tolerance_seconds=1200,
    )

    assert result.approximate is True
    assert result.method == "bounded coarse-to-fine matrix sampling"
    assert result.coordinate == pytest.approx((41.0, -88.0), abs=0.06)
    assert result.final_resolution_km < 10
    assert result.evaluated_candidates <= sum(
        len(targets) for _, targets in provider.calls
    )
    assert result.matrix_requests == len(provider.calls)
    assert all(len(targets) <= 7 for _, targets in provider.calls)
    assert result.candidates[0].excess_seconds == 0
    selected = result.promising_candidates(limit=2, catchment_radius_kilometers=10)
    assert len(selected) <= 2
    assert selected[0].identifier.startswith("modo:41.")
    assert selected[0].radius_kilometers == 10
    assert selected[0].resolution_kilometers == result.final_resolution_km


def test_downstream_candidate_limit_is_capped_at_eight():
    result = NationwideEstimator(PlaneProvider(), coarse_candidate_limit=25).estimate(
        [(41.0, -90.0), (41.0, -86.0)], tolerance_seconds=100_000
    )
    assert (
        len(result.promising_candidates(limit=8, catchment_radius_kilometers=80)) <= 8
    )
    with pytest.raises(ValueError, match="1 through 8"):
        result.promising_candidates(limit=9, catchment_radius_kilometers=80)


def test_catalog_catchment_is_separate_from_sampling_resolution():
    result = NationwideEstimator(
        PlaneProvider(), coarse_candidate_limit=9, refinement_rounds=0
    ).estimate(
        [(47.6062, -122.3321), (25.7617, -80.1918)],
        tolerance_seconds=100_000,
    )
    assert result.final_resolution_km > 160
    selected = result.promising_candidates(catchment_radius_kilometers=80)
    assert selected[0].radius_kilometers == 80
    assert selected[0].resolution_kilometers > 160
    with pytest.raises(ValueError, match="at most 160"):
        result.promising_candidates(catchment_radius_kilometers=161)


def test_downstream_candidates_are_spatially_diverse():
    result = NationwideEstimator(PlaneProvider(), coarse_candidate_limit=36).estimate(
        [(41.0, -90.0), (41.0, -86.0)], tolerance_seconds=100_000
    )
    selected = result.promising_candidates(limit=8, catchment_radius_kilometers=80)
    assert len(selected) > 1
    for position, first in enumerate(selected):
        for second in selected[position + 1 :]:
            distance = Geodesic.WGS84.Inverse(*first.center, *second.center)["s12"]
            assert distance > 80_000


def test_total_objective_uses_sum_and_deterministic_tie_breaking():
    result = NationwideEstimator(PlaneProvider(), coarse_candidate_limit=25).estimate(
        [(41.0, -90.0), (41.0, -86.0)], objective="total"
    )
    assert result.objective == "total"
    assert result.objective_seconds == pytest.approx(14_400)
    assert result.coordinate == (41.0, -90.0)


def test_four_distant_public_city_coordinates_stay_bounded():
    provider = PlaneProvider()
    result = NationwideEstimator(provider, matrix_target_limit=20).estimate(
        [
            (47.6062, -122.3321),  # Seattle
            (40.7128, -74.0060),  # New York
            (25.7617, -80.1918),  # Miami
            (34.0522, -118.2437),  # Los Angeles
        ],
        objective="maximum",
    )

    assert result.evaluated_candidates < 300
    assert result.matrix_requests <= 24
    assert 24.396 <= result.coordinate[0] <= 49.384
    assert -124.849 <= result.coordinate[1] <= -66.885


def test_unreachable_candidates_are_discarded_but_not_fatal():
    provider = PlaneProvider(unreachable=lambda point: point[1] < -88.5)
    result = NationwideEstimator(provider, coarse_candidate_limit=25).estimate(
        [(41, -90), (41, -86)]
    )
    assert result.coordinate[1] >= -88.5


def test_provider_batch_coverage_error_is_split_to_isolate_bad_candidates():
    provider = RejectingBatchProvider(unreachable=lambda point: point[1] < -88.5)
    result = NationwideEstimator(
        provider, coarse_candidate_limit=25, matrix_target_limit=10
    ).estimate([(41, -90), (41, -86)])
    assert result.coordinate[1] >= -88.5
    assert result.matrix_requests > len(provider.calls)


def test_valhalla_171_mixed_batch_keeps_good_candidates():
    provider = NoPrefilterValhalla("http://router.test", opener=Valhalla171Opener())
    result = NationwideEstimator(
        provider,
        coarse_candidate_limit=25,
        refinement_rounds=2,
        matrix_target_limit=10,
    ).estimate([(41, -88), (41, -86)])

    assert result.coordinate == pytest.approx((41, -87), abs=0.08)
    assert result.coordinate[1] >= -87.5
    assert result.matrix_requests == provider.request_count
    assert provider.successful_request_count < provider.request_count


def test_valhalla_171_bad_origins_fail_clearly():
    provider = NoPrefilterValhalla(
        "http://router.test", opener=Valhalla171Opener(reject_sources=True)
    )
    with pytest.raises(RoutingCoverageError, match="no mutually reachable sampled"):
        NationwideEstimator(provider, coarse_candidate_limit=9).estimate(
            [(41, -88), (41, -86)]
        )


def test_far_snapped_sample_is_dropped_without_losing_good_candidates():
    provider = NoPrefilterValhalla("http://router.test", opener=FarSnapOpener())
    result = NationwideEstimator(
        provider,
        coarse_candidate_limit=25,
        refinement_rounds=2,
        matrix_target_limit=10,
    ).estimate([(41, -88), (41, -86)])

    assert result.coordinate == pytest.approx((41, -87), abs=0.08)
    assert result.coordinate[1] >= -87.5


def test_far_snapped_origins_fail_clearly():
    provider = NoPrefilterValhalla(
        "http://router.test", opener=FarSnapOpener(reject_sources=True)
    )
    with pytest.raises(RoutingCoverageError, match="no mutually reachable sampled"):
        NationwideEstimator(provider, coarse_candidate_limit=9).estimate(
            [(41, -88), (41, -86)]
        )


def test_rejected_candidates_are_not_retried_across_refinement_rounds():
    provider = RejectingBatchProvider(unreachable=lambda point: point[1] < -88.5)
    result = NationwideEstimator(
        provider,
        coarse_candidate_limit=25,
        refinement_rounds=3,
        finalists_per_round=8,
        matrix_target_limit=5,
    ).estimate([(41, -90), (41, -86)])

    assert len(provider.singleton_rejections) == len(set(provider.singleton_rejections))
    assert result.coordinate[1] >= -88.5


def test_prefilter_runs_once_per_level_and_keeps_rejected_points_out_of_matrices():
    provider = PrefilteringPlaneProvider(unreachable=lambda point: point[1] < -88.5)
    result = NationwideEstimator(
        provider,
        coarse_candidate_limit=25,
        refinement_rounds=2,
        finalists_per_round=4,
        matrix_target_limit=7,
    ).estimate([(41, -88), (41, -86)])

    assert result.prefilter_requests == 3
    assert len(provider.prefilter_calls) == 3
    assert all(
        target[1] >= -88.5 for _, targets in provider.calls for target in targets
    )


def test_prefilter_reports_origin_indexes_outside_provider_coverage():
    provider = PrefilteringPlaneProvider(unreachable=lambda point: point[1] < -88.5)

    with pytest.raises(RoutingCoverageError, match="road coverage: 0"):
        NationwideEstimator(provider).estimate([(41, -90), (41, -86)])

    assert provider.calls == []


def test_no_mutually_reachable_candidate_fails_clearly():
    provider = PlaneProvider(unreachable=lambda point: True)
    with pytest.raises(RoutingCoverageError, match="no mutually reachable sampled"):
        NationwideEstimator(provider).estimate([(41, -90), (41, -86)])


def test_origin_outside_lower_48_fails_before_provider_call():
    provider = PlaneProvider()
    with pytest.raises(
        RoutingCoverageError, match="origins outside lower-48 coverage: 0"
    ):
        NationwideEstimator(provider).estimate([(61.2181, -149.9003), (41, -87)])
    assert provider.calls == []


def test_large_origin_group_uses_origin_aware_matrix_batches():
    provider = PlaneProvider()
    origins = [(41 + index * 0.0001, -87) for index in range(63)]
    result = NationwideEstimator(provider, coarse_candidate_limit=81).estimate(origins)
    assert result.approximate
    assert provider.calls
    assert all(
        len(sources) * len(targets) <= 2500 for sources, targets in provider.calls
    )
    assert max(len(targets) for _, targets in provider.calls) == 39


def test_estimator_respects_smaller_provider_pair_limit():
    provider = PlaneProvider()
    provider.max_matrix_pairs = 6
    result = NationwideEstimator(
        provider, coarse_candidate_limit=9, refinement_rounds=0
    ).estimate([(41, -90), (41, -88), (41, -86)])
    assert result.approximate
    assert all(len(sources) * len(targets) <= 6 for sources, targets in provider.calls)


def test_excessive_origins_fail_before_prefilter_or_matrix():
    provider = PrefilteringPlaneProvider()
    with pytest.raises(
        ValueError, match="origin count exceeds matrix pair limit \\(2\\)"
    ):
        NationwideEstimator(provider, matrix_pair_limit=2).estimate(
            [(41, -90), (41, -88), (41, -86)]
        )
    assert provider.calls == provider.prefilter_calls == []
