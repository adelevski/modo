from math import hypot

import pytest

from modo import NationwideEstimator, RoutingCoverageError


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
    def matrix(self, origins, targets):
        if any(self.unreachable(target) for target in targets):
            if len(targets) > 1:
                raise RoutingCoverageError("batch contains an unconnected location")
            raise RoutingCoverageError("location is unconnected")
        return super().matrix(origins, targets)


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
    selected = result.promising_candidates(limit=2)
    assert len(selected) <= 2
    assert selected[0].identifier.startswith("modo:41.")
    assert selected[0].radius_kilometers == result.final_resolution_km
    assert 0 < selected[0].radius_kilometers <= 160


def test_downstream_candidate_limit_is_capped_at_eight():
    result = NationwideEstimator(PlaneProvider(), coarse_candidate_limit=25).estimate(
        [(41.0, -90.0), (41.0, -86.0)], tolerance_seconds=100_000
    )
    assert len(result.promising_candidates(limit=8)) <= 8
    with pytest.raises(ValueError, match="1 through 8"):
        result.promising_candidates(limit=9)


def test_downstream_candidates_reject_resolution_over_radius_contract():
    result = NationwideEstimator(
        PlaneProvider(), coarse_candidate_limit=9, refinement_rounds=0
    ).estimate(
        [(47.6062, -122.3321), (25.7617, -80.1918)],
        tolerance_seconds=100_000,
    )
    assert result.final_resolution_km > 160
    with pytest.raises(ValueError, match="160 km"):
        result.promising_candidates()


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

    assert result.evaluated_candidates < 400
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
