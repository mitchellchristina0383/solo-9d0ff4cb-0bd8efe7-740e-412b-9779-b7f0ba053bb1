"""Tests for the read-only rollout planner and its HTTP endpoint.

The planner is checked against an independent, deliberately naive simulator
(plain set arithmetic over snapshot prefixes, replaying every permutation)
so the subset DP is not merely self-consistent.
"""

from __future__ import annotations

import itertools
from typing import Dict, Iterable, List, Sequence, Set, Tuple

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.rollout import (
    Image,
    InvalidPlanInput,
    Service,
    TooManyServices,
    plan_rollout,
)

client = TestClient(app)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def image(*layers: str, ref: str | None = None) -> Image:
    return Image(diff_ids=tuple(layers), ref=ref)


def prefixes(img: Image) -> Set[Tuple[str, ...]]:
    return {img.diff_ids[:i] for i in range(1, len(img.diff_ids) + 1)}


def naive_usage(
    services: Sequence[Service],
    resident: Sequence[Image],
    usage: Dict[Tuple[str, ...], int],
    switched: Iterable[str],
    extra: Image | None = None,
) -> int:
    """Compute disk usage from scratch: union of snapshot prefixes x size."""
    switched = set(switched)
    snapshots: Set[Tuple[str, ...]] = set()
    for img in resident:
        snapshots |= prefixes(img)
    for service in services:
        snapshots |= prefixes(service.new if service.name in switched else service.old)
    if extra is not None:
        snapshots |= prefixes(extra)
    return sum(usage[snapshot] for snapshot in snapshots)


def naive_permutations(
    services: Sequence[Service],
    resident: Sequence[Image],
    usage: Dict[Tuple[str, ...], int],
) -> List[Tuple[int, Tuple[str, ...]]]:
    """Return (max_peak, order) for every permutation, brute force."""
    results = []
    for order in itertools.permutations(service.name for service in services):
        by_name = {service.name: service for service in services}
        switched: List[str] = []
        peak = naive_usage(services, resident, usage, switched)
        for name in order:
            peak = max(
                peak,
                naive_usage(
                    services, resident, usage, switched, extra=by_name[name].new
                ),
            )
            switched.append(name)
        results.append((peak, order))
    return results


def usage_map(**kwargs: int) -> Dict[Tuple[str, ...], int]:
    """usage_map(**{"c1": 100, "c1+a_old": 50}) -> prefix tuple keys."""
    return {tuple(key.split("+")): value for key, value in kwargs.items()}


TWO_SERVICES = [
    Service("alpha", old=image("c1", "a_old"), new=image("c1", "a_new")),
    Service("beta", old=image("c1", "b_old"), new=image("c1", "b_new")),
]
TWO_USAGE = usage_map(c1=100, **{
    "c1+a_old": 50,
    "c1+a_new": 30,
    "c1+b_old": 60,
    "c1+b_new": 20,
})


# --------------------------------------------------------------------------- #
# disk model
# --------------------------------------------------------------------------- #
def test_shared_prefix_is_counted_once():
    # Both services' new images reuse the exact same snapshot, which itself
    # shares the c1 base snapshot with the old images.
    services = [
        Service("alpha", old=image("c1", "shared"), new=image("c1", "shared")),
        Service("beta", old=image("c1", "b_old"), new=image("c1", "shared")),
    ]
    usage = usage_map(c1=100, **{"c1+shared": 40, "c1+b_old": 60})
    plan = plan_rollout(services, [], usage.items(), budget_bytes=10_000)
    # initial: c1 + shared + b_old = 200; peak adds nothing new for alpha.
    assert plan.initial_usage_bytes == 200
    assert plan.final_usage_bytes == 140
    assert [rnd.service for rnd in plan.rounds] == ["alpha", "beta"]
    assert plan.rounds[0].warmup_added_bytes == 0
    assert plan.rounds[0].peak_bytes == 200


def test_prefix_identity_is_position_sensitive():
    # Same digest, different position => different snapshots.
    services = [
        Service("alpha", old=image("x", "y"), new=image("y", "x")),
    ]
    usage = {
        ("x",): 10,
        ("x", "y"): 20,
        ("y", "x"): 30,
        ("y",): 5,
    }
    plan = plan_rollout(services, [], usage.items(), budget_bytes=10_000)
    assert plan.initial_usage_bytes == 30  # (x,) + (x,y)
    # The new image's prefixes are (y,) and (y,x); (x,y) does not match (y,),
    # so both are new: warm-up adds 5 + 30 = 35.
    assert plan.rounds[0].warmup_added_bytes == 35
    assert plan.rounds[0].peak_bytes == 65


def test_resident_images_always_counted():
    resident = [image("r1", "r2")]
    services = [Service("alpha", old=image("c1", "a_old"), new=image("c1", "a_new"))]
    usage = {
        ("r1",): 11,
        ("r1", "r2"): 22,
        ("c1",): 100,
        ("c1", "a_old"): 50,
        ("c1", "a_new"): 30,
    }
    plan = plan_rollout(services, resident, usage.items(), budget_bytes=10_000)
    assert plan.initial_usage_bytes == 11 + 22 + 100 + 50
    assert plan.final_usage_bytes == 11 + 22 + 100 + 30
    assert plan.rounds[0].peak_bytes == 11 + 22 + 100 + 50 + 30


def test_retired_snapshot_shared_with_another_service_stays():
    # alpha and beta old images share the `common` snapshot.  Retiring alpha
    # must not free it because beta's old image still references it.
    services = [
        Service("alpha", old=image("c1", "common"), new=image("c1", "a_new")),
        Service("beta", old=image("c1", "common"), new=image("c1", "b_new")),
    ]
    usage = usage_map(c1=100, **{
        "c1+common": 70,
        "c1+a_new": 10,
        "c1+b_new": 20,
    })
    plan = plan_rollout(services, [], usage.items(), budget_bytes=10_000)
    assert plan.initial_usage_bytes == 170
    first = plan.rounds[0]
    assert first.warmup_added_bytes == 10
    # Nothing is reclaimed after the first switch: `common` is still in use.
    assert first.retired_bytes == 0
    assert first.post_retire_usage_bytes == 180
    last = plan.rounds[-1]
    assert last.retired_bytes == 70  # only the now-unreferenced `common` snapshot
    assert last.post_retire_usage_bytes == 130  # c1 + a_new + b_new


# --------------------------------------------------------------------------- #
# ordering and feasibility
# --------------------------------------------------------------------------- #
def test_order_matters_for_the_peak():
    # alpha-first peak: 210 + 30 = 240; beta-first peak: 210 + 20 = 230.
    brute = naive_permutations(TWO_SERVICES, [], TWO_USAGE)
    assert min(peak for peak, _ in brute) == 230

    plan = plan_rollout(TWO_SERVICES, [], TWO_USAGE.items(), budget_bytes=230)
    assert plan.feasible is True
    assert plan.order == ("beta", "alpha")
    assert plan.max_usage_bytes == 230
    assert plan.minimum_max_usage_bytes == 230

    tight = plan_rollout(TWO_SERVICES, [], TWO_USAGE.items(), budget_bytes=229)
    assert tight.feasible is False
    assert tight.reason == "no_order_fits_budget"
    assert tight.order == () and tight.rounds == ()
    assert tight.minimum_max_usage_bytes == 230
    assert tight.shortfall_bytes == 1


def test_initial_usage_over_budget_is_reported_separately():
    plan = plan_rollout(TWO_SERVICES, [], TWO_USAGE.items(), budget_bytes=100)
    assert plan.feasible is False
    assert plan.reason == "initial_usage_exceeds_budget"
    assert plan.shortfall_bytes == 130  # best achievable peak 230 vs budget 100


@pytest.mark.parametrize("seed", [1, 7, 42])
def test_dp_optimum_matches_brute_force(seed: int):
    """Randomised cross-check against exhaustive permutation search."""
    import random

    rng = random.Random(seed)
    digests = [f"sha256:{index}" for index in range(8)]
    usage: Dict[Tuple[str, ...], int] = {}
    for length in range(1, 3):
        for combo in itertools.permutations(digests, length):
            usage[combo] = rng.randrange(0, 200)
    resident = [image("sha256:res")]
    for length in range(1, 2):
        for combo in itertools.permutations(digests, length):
            usage[combo] = usage.get(combo, rng.randrange(0, 200))
    usage[("sha256:res",)] = rng.randrange(0, 700)

    services = []
    for number in range(4):
        def random_image() -> Image:
            count = rng.choice([1, 2])
            return image(*rng.sample(digests, count))

        services.append(
            Service(f"svc{number}", old=random_image(), new=random_image())
        )
    # Include every referenced prefix in the usage table.
    referenced = set(usage)
    for service in services:
        referenced |= prefixes(service.old) | prefixes(service.new)
    for snapshot in referenced:
        usage.setdefault(snapshot, rng.randrange(0, 200))

    plan = plan_rollout(services, resident, usage.items(), budget_bytes=10**9)
    brute = naive_permutations(services, resident, usage)
    brute_best = min(peak for peak, _ in brute)
    assert plan.minimum_max_usage_bytes == brute_best
    assert plan.max_usage_bytes == brute_best

    # The reported per-round peaks must be reproducible independently.
    by_name = {service.name: service for service in services}
    switched: List[str] = []
    expected_initial = naive_usage(services, resident, usage, switched)
    assert plan.initial_usage_bytes == expected_initial
    for rnd in plan.rounds:
        expected_peak = naive_usage(
            services, resident, usage, switched, extra=by_name[rnd.service].new
        )
        assert rnd.peak_bytes == expected_peak
        assert rnd.headroom_bytes == 10**9 - expected_peak
        switched.append(rnd.service)
    assert set(switched) == {service.name for service in services}


def test_zero_services_is_trivially_feasible():
    plan = plan_rollout([], [], [], budget_bytes=0)
    assert plan.feasible is True
    assert plan.order == () and plan.rounds == ()
    assert plan.max_usage_bytes == 0


def test_ten_services_are_supported():
    services = [
        Service(
            f"svc{index}",
            old=image(f"old{index}"),
            new=image(f"new{index}"),
        )
        for index in range(10)
    ]
    usage = {("old" + str(i),): 1 for i in range(10)}
    usage.update({("new" + str(i),): 1 for i in range(10)})
    plan = plan_rollout(services, [], usage.items(), budget_bytes=11)
    assert plan.feasible is True
    assert len(plan.rounds) == 10
    # Each warm-up adds exactly one new snapshot; retirements keep up.
    assert plan.max_usage_bytes == 11


# --------------------------------------------------------------------------- #
# invalid input
# --------------------------------------------------------------------------- #
def test_missing_snapshot_usage_is_rejected():
    services = [Service("alpha", old=image("c1", "a_old"), new=image("c1", "a_new"))]
    with pytest.raises(Exception) as excinfo:
        plan_rollout(services, [], [(("c1",), 100)], budget_bytes=1000)
    from app.rollout import MissingSnapshotUsage

    assert isinstance(excinfo.value, MissingSnapshotUsage)
    message = str(excinfo.value)
    assert "[c1 -> a_old]" in message
    assert "[c1 -> a_new]" in message


def test_missing_resident_snapshot_usage_is_rejected():
    from app.rollout import MissingSnapshotUsage

    with pytest.raises(MissingSnapshotUsage):
        plan_rollout([], [image("r1")], [], budget_bytes=1000)


def test_conflicting_duplicate_usage_is_rejected():
    from app.rollout import ConflictingSnapshotUsage

    services = [Service("alpha", old=image("c1"), new=image("c1"))]
    with pytest.raises(ConflictingSnapshotUsage):
        plan_rollout(
            services,
            [],
            [(("c1",), 10), (("c1",), 11)],
            budget_bytes=1000,
        )
    # Identical duplicates are harmless.
    plan = plan_rollout(services, [], [(("c1",), 10), (("c1",), 10)], budget_bytes=10)
    assert plan.feasible is True


def test_duplicate_service_names_rejected():
    from app.rollout import DuplicateServiceName

    services = [
        Service("alpha", old=image("c1"), new=image("c1")),
        Service("alpha", old=image("c2"), new=image("c2")),
    ]
    usage = {("c1",): 1, ("c2",): 1}
    with pytest.raises(DuplicateServiceName):
        plan_rollout(services, [], usage.items(), budget_bytes=100)


def test_more_than_ten_services_rejected():
    services = [
        Service(f"svc{index}", old=image(str(index)), new=image(str(index)))
        for index in range(11)
    ]
    usage = {(str(index),): 1 for index in range(11)}
    with pytest.raises(TooManyServices):
        plan_rollout(services, [], usage.items(), budget_bytes=100)


def test_negative_budget_rejected():
    with pytest.raises(InvalidPlanInput):
        plan_rollout([], [], [], budget_bytes=-1)


# --------------------------------------------------------------------------- #
# HTTP endpoint
# --------------------------------------------------------------------------- #
def _payload(budget: int = 230) -> dict:
    return {
        "services": [
            {
                "name": "alpha",
                "old": {"diff_ids": ["c1", "a_old"], "ref": "alpha:1"},
                "new": {"diff_ids": ["c1", "a_new"], "ref": "alpha:2"},
            },
            {
                "name": "beta",
                "old": {"diff_ids": ["c1", "b_old"]},
                "new": {"diff_ids": ["c1", "b_new"]},
            },
        ],
        "resident_images": [],
        "snapshots": [
            {"diff_ids_prefix": list(key), "exclusive_bytes": value}
            for key, value in TWO_USAGE.items()
        ],
        "budget_bytes": budget,
    }


def test_plan_endpoint_returns_order_and_peaks():
    resp = client.post("/plan", json=_payload())
    assert resp.status_code == 200
    body = resp.json()
    assert body["feasible"] is True
    assert body["order"] == ["beta", "alpha"]
    assert body["initial_usage_bytes"] == 210
    assert body["final_usage_bytes"] == 150
    assert body["max_usage_bytes"] == 230
    assert [rnd["peak_bytes"] for rnd in body["rounds"]] == [230, 200]
    assert [rnd["service"] for rnd in body["rounds"]] == ["beta", "alpha"]


def test_plan_endpoint_reports_infeasible():
    resp = client.post("/plan", json=_payload(budget=229))
    assert resp.status_code == 200
    body = resp.json()
    assert body["feasible"] is False
    assert body["reason"] == "no_order_fits_budget"
    assert body["order"] == [] and body["rounds"] == []
    assert body["shortfall_bytes"] == 1


def test_plan_endpoint_missing_usage_is_400():
    payload = _payload()
    payload["snapshots"] = payload["snapshots"][:-1]
    resp = client.post("/plan", json=payload)
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["code"] == "missing_snapshot_usage"
    assert "c1 -> b_new" in detail["message"]


def test_plan_endpoint_too_many_services_is_422():
    payload = _payload()
    payload["services"] = payload["services"] * 6  # 12 services
    resp = client.post("/plan", json=payload)
    assert resp.status_code == 422


def test_plan_endpoint_rejects_unknown_fields():
    payload = _payload()
    payload["swap_anything"] = True
    resp = client.post("/plan", json=payload)
    assert resp.status_code == 422


def test_plan_endpoint_accepts_real_oci_config_shape():
    """A real config has extra fields and diff_ids nested under rootfs."""
    oci_config = {
        "architecture": "amd64",
        "os": "linux",
        "config": {"Env": ["PATH=/usr/bin"], "Labels": {"v": "1"}},
        "history": [{"created_by": "build"}],
        "rootfs": {"type": "layers", "diff_ids": ["c1", "a_old"]},
        "unrelated": {"nested": True},
    }
    payload = _payload()
    payload["services"][0]["old"] = oci_config
    payload["services"][0]["new"] = {
        "rootfs": {"type": "layers", "diff_ids": ["c1", "a_new"]}
    }
    resp = client.post("/plan", json=payload)
    assert resp.status_code == 200, resp.text
    assert resp.json()["order"] == ["beta", "alpha"]


def test_plan_endpoint_rejects_missing_diff_ids():
    payload = _payload()
    payload["services"][0]["old"] = {"architecture": "amd64"}
    resp = client.post("/plan", json=payload)
    assert resp.status_code == 422


def test_plan_endpoint_rejects_disagreeing_diff_id_forms():
    payload = _payload()
    payload["services"][0]["old"] = {
        "diff_ids": ["c1", "a_old"],
        "rootfs": {"type": "layers", "diff_ids": ["c1", "something-else"]},
    }
    resp = client.post("/plan", json=payload)
    assert resp.status_code == 422


def test_plan_is_listed_in_openapi_and_docs():
    openapi = client.get("/openapi.json").json()
    assert "/plan" in openapi["paths"]
    assert "/merge" in openapi["paths"]
    docs = client.get("/docs")
    assert docs.status_code == 200
    assert "OCI Layer Merger" in docs.text
