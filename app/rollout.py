"""Read-only planning logic for a one-at-a-time image rollout.

Disk model
==========

Each image config carries an ordered list of ``rootfs.diff_ids``.  Every
proper prefix of that list (``[d1]``, ``[d1, d2]``, ...) denotes one unpacked
snapshot directory on the node.  A snapshot is identified by the *complete*
digest sequence prefix, so two images that share ``[d1, d2]`` share that one
snapshot while an image whose layers happen to contain the same digest in a
different position gets a distinct snapshot.

``exclusive_bytes`` is the disk space owned by one snapshot and not by any
other snapshot.  The total usage of a set of images is therefore the sum of
``exclusive_bytes`` over the *union* of their snapshot prefixes; a prefix
referenced by several images is counted exactly once.

Rollout model
=============

Services are switched one at a time.  While a service is warming up its old
and new images are both referenced, so both snapshot sets occupy disk.  Only
after the new version is ready is the old version stopped; snapshots that are
then referenced by nobody (no resident image, no service's active version)
are reclaimed before the next round starts.

For a state ``S`` (the set of services already on their new version) the
active images are:

* all resident images,
* the new image of every service in ``S``,
* the old image of every service not in ``S``.

The peak of switching service ``j`` out of ``S`` is the usage of those active
images plus the new image of ``j`` (the old image of ``j`` is still running).

The planner performs an exact subset DP over at most ``MAX_SERVICES`` services
(2**10 states), minimising the worst disk level over the whole rollout and
including the initial steady state.  A service order is feasible when that
minimum is within the budget.  Because usage depends only on the set of
already-switched services (not on the path taken to reach it), the DP is
exact."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet, Iterable, Optional, Sequence, Tuple

MAX_SERVICES = 10

SnapshotKey = Tuple[str, ...]


class PlanInputError(ValueError):
    """Base class for semantically invalid planning input (HTTP 400)."""

    code = "invalid_plan_input"


class TooManyServices(PlanInputError):
    code = "too_many_services"


class DuplicateServiceName(PlanInputError):
    code = "duplicate_service_name"

    def __init__(self, name: str) -> None:
        super().__init__(f"duplicate service name: {name!r}")
        self.name = name


class ConflictingSnapshotUsage(PlanInputError):
    code = "conflicting_snapshot_usage"

    def __init__(self, key: SnapshotKey) -> None:
        super().__init__(
            "conflicting exclusive_bytes for snapshot prefix "
            + format_snapshot_key(key)
        )
        self.key = key


class MissingSnapshotUsage(PlanInputError):
    code = "missing_snapshot_usage"

    def __init__(self, missing: Sequence[SnapshotKey]) -> None:
        rendered = ", ".join(format_snapshot_key(key) for key in missing)
        super().__init__(f"missing snapshot usage for: {rendered}")
        self.missing = tuple(missing)


class InvalidPlanInput(PlanInputError):
    code = "invalid_plan_input"


def format_snapshot_key(key: SnapshotKey) -> str:
    """Render a snapshot prefix for humans and error payloads."""
    return "[" + " -> ".join(key) + "]"


@dataclass(frozen=True)
class Image:
    """The part of an OCI image config the planner needs."""

    diff_ids: Tuple[str, ...]
    ref: Optional[str] = None

    def snapshot_prefixes(self) -> Tuple[SnapshotKey, ...]:
        return tuple(self.diff_ids[:i] for i in range(1, len(self.diff_ids) + 1))


@dataclass(frozen=True)
class Service:
    name: str
    old: Image
    new: Image


@dataclass(frozen=True)
class Round:
    round_number: int
    service: str
    pre_round_usage_bytes: int
    warmup_added_bytes: int
    peak_bytes: int
    headroom_bytes: int
    retired_bytes: int
    post_retire_usage_bytes: int


@dataclass(frozen=True)
class Plan:
    feasible: bool
    reason: Optional[str]
    budget_bytes: int
    initial_usage_bytes: int
    final_usage_bytes: int
    order: Tuple[str, ...]
    rounds: Tuple[Round, ...]
    max_usage_bytes: Optional[int]
    minimum_max_usage_bytes: int
    shortfall_bytes: int


def _prefix_set(image: Image) -> FrozenSet[SnapshotKey]:
    return frozenset(image.snapshot_prefixes())


def _normalise_usage(
    snapshot_usage: Iterable[Tuple[SnapshotKey, int]],
) -> Dict[SnapshotKey, int]:
    usage: Dict[SnapshotKey, int] = {}
    for key, exclusive_bytes in snapshot_usage:
        if not isinstance(exclusive_bytes, int) or isinstance(exclusive_bytes, bool):
            raise InvalidPlanInput("exclusive_bytes must be an integer")
        if exclusive_bytes < 0:
            raise InvalidPlanInput("exclusive_bytes must be non-negative")
        key = tuple(key)
        if key in usage and usage[key] != exclusive_bytes:
            raise ConflictingSnapshotUsage(key)
        usage[key] = exclusive_bytes
    return usage


def plan_rollout(
    services: Sequence[Service],
    resident_images: Sequence[Image],
    snapshot_usage: Iterable[Tuple[SnapshotKey, int]],
    budget_bytes: int,
) -> Plan:
    """Compute a rollout order that keeps every peak within the budget.

    Raises :class:`PlanInputError` subclasses for semantically unusable input
    (too many services, duplicate service names, conflicting or missing
    snapshot usage).
    """
    if isinstance(budget_bytes, bool) or not isinstance(budget_bytes, int):
        raise InvalidPlanInput("budget_bytes must be an integer")
    if budget_bytes < 0:
        raise InvalidPlanInput("budget_bytes must be non-negative")
    if len(services) > MAX_SERVICES:
        raise TooManyServices(f"at most {MAX_SERVICES} services are supported")

    names = [service.name for service in services]
    seen: set[str] = set()
    for name in names:
        if name in seen:
            raise DuplicateServiceName(name)
        seen.add(name)

    usage = _normalise_usage(snapshot_usage)

    resident_snaps: FrozenSet[SnapshotKey] = frozenset()
    for image in resident_images:
        resident_snaps |= _prefix_set(image)

    old_snaps = [_prefix_set(service.old) for service in services]
    new_snaps = [_prefix_set(service.new) for service in services]

    referenced: set[SnapshotKey] = set(resident_snaps)
    for snaps in old_snaps + new_snaps:
        referenced |= snaps
    missing = sorted(snapshot for snapshot in referenced if snapshot not in usage)
    if missing:
        raise MissingSnapshotUsage(missing)

    # Bitmask arithmetic: a state's referenced snapshots are the union of the
    # resident snapshots, the new images already switched over, and the old
    # images still running for every service not yet switched.
    index_of: Dict[SnapshotKey, int] = {}
    for snapshot in sorted(referenced):
        index_of[snapshot] = len(index_of)
    weights = [usage[snapshot] for snapshot in sorted(referenced)]

    def mask_of(snaps: Iterable[SnapshotKey]) -> int:
        mask = 0
        for snapshot in snaps:
            mask |= 1 << index_of[snapshot]
        return mask

    resident_mask = mask_of(resident_snaps)
    old_masks = [mask_of(snaps) for snaps in old_snaps]
    new_masks = [mask_of(snaps) for snaps in new_snaps]

    count = len(services)
    state_count = 1 << count

    # or_new[state] / or_old[state] = union of the new (resp. old) snapshots
    # of the services selected by `state`.
    or_new = [0] * state_count
    or_old = [0] * state_count
    for state in range(1, state_count):
        lowest = state & -state
        index = lowest.bit_length() - 1
        previous = state ^ lowest
        or_new[state] = or_new[previous] | new_masks[index]
        or_old[state] = or_old[previous] | old_masks[index]

    # Byte-chunk lookup tables: weighted_sum(mask) then costs one lookup per
    # 8 snapshots instead of one Python-level iteration per snapshot, which
    # keeps the 2**10-state DP fast even with hundreds of layers per image.
    table: list[list[int]] = []
    for start in range(0, max(len(weights), 1), 8):
        chunk = (weights[start:start + 8] + [0] * 8)[:8]
        sums = [0] * 256
        for value in range(1, 256):
            lowest = value & -value
            sums[value] = sums[value ^ lowest] + chunk[lowest.bit_length() - 1]
        table.append(sums)

    weight_cache: Dict[int, int] = {}

    def weighted(mask: int) -> int:
        cached = weight_cache.get(mask)
        if cached is not None:
            return cached
        total = 0
        rest = mask
        for sums in table:
            total += sums[rest & 0xFF]
            rest >>= 8
        weight_cache[mask] = total
        return total

    # refs[mask] = bitmask of snapshots referenced by all active images in
    # that state: residents, the new images already switched over, and the
    # old images still running for every service not yet switched.
    full_state = state_count - 1
    refs = [
        resident_mask | or_new[state] | or_old[full_state ^ state]
        for state in range(state_count)
    ]
    state_usage = [weighted(active) for active in refs]

    initial_usage = state_usage[0]
    final_usage = state_usage[state_count - 1]

    def peak_from(state: int, index: int) -> int:
        """Usage while switching ``index`` out of ``state`` (both resident)."""
        return weighted(refs[state] | new_masks[index])

    # Minimax DP: suffix_best[state] is the smallest achievable maximum of
    # the remaining round peaks when starting from ``state``; the initial
    # steady state is folded in only at the very end.  Every transition goes
    # to a numerically larger mask, so a descending sweep visits all
    # successors first.
    suffix_best: list[Optional[int]] = [None] * state_count
    suffix_best[full_state] = 0
    for state in range(full_state - 1, -1, -1):
        chosen: Optional[int] = None
        for index in range(count):
            bit = 1 << index
            if state & bit:
                continue
            rest = suffix_best[state | bit]
            assert rest is not None
            ceiling = max(peak_from(state, index), rest)
            if chosen is None or ceiling < chosen:
                chosen = ceiling
        assert chosen is not None
        suffix_best[state] = chosen

    minimum_max_usage = max(initial_usage, suffix_best[0] or 0)

    # Greedy reconstruction of the lexicographically smallest order that
    # attains the optimum.
    sequence: list[str] = []
    state = 0
    while state != full_state:
        for index in sorted(range(count), key=lambda item: services[item].name):
            bit = 1 << index
            if state & bit:
                continue
            following = suffix_best[state | bit]
            assert following is not None
            if (
                peak_from(state, index) <= minimum_max_usage
                and following <= minimum_max_usage
            ):
                sequence.append(services[index].name)
                state |= bit
                break
        else:  # pragma: no cover - the DP guarantees a feasible step
            raise AssertionError("rollout reconstruction failed")
    optimal_order: Tuple[str, ...] = tuple(sequence)

    if initial_usage > budget_bytes:
        return Plan(
            feasible=False,
            reason="initial_usage_exceeds_budget",
            budget_bytes=budget_bytes,
            initial_usage_bytes=initial_usage,
            final_usage_bytes=final_usage,
            order=(),
            rounds=(),
            max_usage_bytes=None,
            minimum_max_usage_bytes=minimum_max_usage,
            shortfall_bytes=minimum_max_usage - budget_bytes,
        )
    if minimum_max_usage > budget_bytes:
        return Plan(
            feasible=False,
            reason="no_order_fits_budget",
            budget_bytes=budget_bytes,
            initial_usage_bytes=initial_usage,
            final_usage_bytes=final_usage,
            order=(),
            rounds=(),
            max_usage_bytes=None,
            minimum_max_usage_bytes=minimum_max_usage,
            shortfall_bytes=minimum_max_usage - budget_bytes,
        )

    # Replay the optimal order to report the per-round peaks and transitions.
    rounds: list[Round] = []
    mask = 0
    pre_round = initial_usage
    for round_number, name in enumerate(optimal_order, start=1):
        index = names.index(name)
        added = weighted(new_masks[index] & ~refs[mask])
        peak = weighted(refs[mask] | new_masks[index])
        target = mask | (1 << index)
        post_retire = state_usage[target]
        rounds.append(
            Round(
                round_number=round_number,
                service=name,
                pre_round_usage_bytes=pre_round,
                warmup_added_bytes=added,
                peak_bytes=peak,
                headroom_bytes=budget_bytes - peak,
                retired_bytes=pre_round + added - post_retire,
                post_retire_usage_bytes=post_retire,
            )
        )
        mask = target
        pre_round = post_retire

    max_usage = max([initial_usage, *(rnd.peak_bytes for rnd in rounds)])
    return Plan(
        feasible=True,
        reason=None,
        budget_bytes=budget_bytes,
        initial_usage_bytes=initial_usage,
        final_usage_bytes=final_usage,
        order=optimal_order,
        rounds=tuple(rounds),
        max_usage_bytes=max_usage,
        minimum_max_usage_bytes=minimum_max_usage,
        shortfall_bytes=0,
    )
