"""FastAPI service: merge two consecutive OCI diff layers into one, and plan
a one-at-a-time image rollout against the node's unpacked-snapshot budget.

Uploads are streamed to private spool files first; the merged tar is only
produced (and therefore downloadable) when both input layers have been fully
validated.  Any invalid input yields HTTP 400 and no artifact.

``POST /plan`` is read-only: it computes a rollout order from the submitted
OCI configs and snapshot usage without touching any image or snapshot.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from typing import List, Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.background import BackgroundTask

from .merger import InvalidLayer, LayerMerger
from .rollout import (
    MAX_SERVICES,
    Image,
    PlanInputError,
    Service,
    plan_rollout,
)

MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB per layer
READ_CHUNK = 1024 * 1024

app = FastAPI(
    title="OCI Layer Merger",
    version="1.1.0",
    description=(
        "Combine two consecutive, uncompressed OCI diff layers (ordered "
        "lower then upper) into a single reusable diff layer. Regular files, "
        "directories and whiteouts (including opaque directory markers) are "
        "supported. Upload both tars as multipart/form-data and download the "
        "merged uncompressed tar on success.\n\n"
        "`POST /plan` is a read-only rollout check: submit up to 10 services "
        "with their old/new OCI image configs, the configs of the resident "
        "images, the exclusive byte size of every unpacked snapshot and the "
        "unpacked-disk budget. It returns an order that switches one service "
        "at a time (both versions resident during warm-up) while keeping the "
        "peak within budget, or reports that no such order exists."
    ),
)


class HealthResponse(BaseModel):
    status: str


# --------------------------------------------------------------------------- #
# rollout planning (read-only)
# --------------------------------------------------------------------------- #
class OciRootfs(BaseModel):
    """`rootfs` section of a real OCI image config."""

    model_config = ConfigDict(extra="allow")

    type: str = Field(default="layers", description="Layer media type family.")
    diff_ids: List[str] = Field(
        ...,
        min_length=1,
        description="Ordered uncompressed layer digests (sha256:...).",
    )


class OciImageConfig(BaseModel):
    """An OCI image config, or just its ``diff_ids``.

    Real configs from a registry have ``rootfs.diff_ids`` nested under
    ``rootfs`` and many unrelated fields (``architecture``, ``config``,
    ``history``, ...); those are accepted and ignored.  For convenience the
    digest list may also be supplied directly as ``diff_ids``.
    """

    model_config = ConfigDict(extra="allow")

    diff_ids: Optional[List[str]] = Field(
        default=None,
        min_length=1,
        description="Convenience shortcut for rootfs.diff_ids.",
    )
    rootfs: Optional[OciRootfs] = Field(
        default=None,
        description="Standard OCI rootfs section carrying diff_ids.",
    )
    ref: Optional[str] = Field(
        default=None,
        description="Optional human-readable image reference echoed back.",
    )

    @model_validator(mode="after")
    def _must_carry_diff_ids(self) -> "OciImageConfig":
        if self.rootfs is not None:
            if (
                self.diff_ids is not None
                and list(self.diff_ids) != list(self.rootfs.diff_ids)
            ):
                raise ValueError(
                    "diff_ids and rootfs.diff_ids disagree; supply only one"
                )
            return self
        if self.diff_ids is None:
            raise ValueError(
                "image config must contain rootfs.diff_ids (standard OCI) "
                "or diff_ids (shortcut)"
            )
        return self

    def resolved_diff_ids(self) -> List[str]:
        if self.rootfs is not None:
            return list(self.rootfs.diff_ids)
        assert self.diff_ids is not None
        return list(self.diff_ids)

    def to_image(self) -> Image:
        return Image(diff_ids=tuple(self.resolved_diff_ids()), ref=self.ref)


class ServiceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, description="Unique service name.")
    old: OciImageConfig = Field(..., description="Currently running image config.")
    new: OciImageConfig = Field(..., description="Replacement image config.")


class SnapshotUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    diff_ids_prefix: List[str] = Field(
        ...,
        min_length=1,
        description=(
            "Complete diff_ids prefix identifying one snapshot "
            "(e.g. [\"sha256:a\", \"sha256:b\"]). A snapshot is shared when "
            "two images have the same prefix."
        ),
    )
    exclusive_bytes: int = Field(
        ...,
        ge=0,
        description="Bytes owned only by this snapshot, counted once.",
    )


class PlanRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "services": [
                    {
                        "name": "orders",
                        "old": {
                            "diff_ids": ["sha256:base", "sha256:orders-old"],
                            "ref": "registry/orders:1",
                        },
                        "new": {
                            "diff_ids": ["sha256:base", "sha256:orders-new"],
                            "ref": "registry/orders:2",
                        },
                    },
                    {
                        "name": "billing",
                        "old": {
                            "diff_ids": ["sha256:base", "sha256:billing-old"],
                            "ref": "registry/billing:7",
                        },
                        "new": {
                            "diff_ids": ["sha256:base", "sha256:billing-new"],
                            "ref": "registry/billing:8",
                        },
                    },
                ],
                "resident_images": [
                    {
                        "diff_ids": ["sha256:host", "sha256:daemon"],
                        "ref": "registry/node-agent:3",
                    }
                ],
                "snapshots": [
                    {"diff_ids_prefix": ["sha256:base"], "exclusive_bytes": 1200000000},
                    {"diff_ids_prefix": ["sha256:host"], "exclusive_bytes": 400000000},
                    {
                        "diff_ids_prefix": ["sha256:host", "sha256:daemon"],
                        "exclusive_bytes": 90000000,
                    },
                    {
                        "diff_ids_prefix": ["sha256:base", "sha256:orders-old"],
                        "exclusive_bytes": 250000000,
                    },
                    {
                        "diff_ids_prefix": ["sha256:base", "sha256:orders-new"],
                        "exclusive_bytes": 260000000,
                    },
                    {
                        "diff_ids_prefix": ["sha256:base", "sha256:billing-old"],
                        "exclusive_bytes": 80000000,
                    },
                    {
                        "diff_ids_prefix": ["sha256:base", "sha256:billing-new"],
                        "exclusive_bytes": 70000000,
                    },
                ],
                "budget_bytes": 2300000000,
            }
        },
    )

    services: List[ServiceConfig] = Field(
        ...,
        max_length=MAX_SERVICES,
        description=f"At most {MAX_SERVICES} services to switch.",
    )
    resident_images: List[OciImageConfig] = Field(
        default_factory=list,
        description="Images that stay running during the whole rollout.",
    )
    snapshots: List[SnapshotUsage] = Field(
        ...,
        description="Exclusive usage for every referenced snapshot prefix.",
    )
    budget_bytes: int = Field(
        ...,
        ge=0,
        description="Unpacked-disk budget in bytes.",
    )


class RoundResponse(BaseModel):
    round: int
    service: str
    pre_round_usage_bytes: int
    warmup_added_bytes: int
    peak_bytes: int
    headroom_bytes: int
    retired_bytes: int
    post_retire_usage_bytes: int


class PlanResponse(BaseModel):
    feasible: bool
    reason: Optional[str]
    budget_bytes: int
    initial_usage_bytes: int
    final_usage_bytes: int
    order: List[str]
    rounds: List[RoundResponse]
    max_usage_bytes: Optional[int]
    minimum_max_usage_bytes: int
    shortfall_bytes: int



def _cleanup(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _spool_upload(upload: UploadFile, workdir: str) -> str:
    """Stream an upload into a private spool file, enforcing the size cap."""
    if not upload.filename:
        raise HTTPException(status_code=400, detail="missing upload filename")
    safe_name = os.path.basename(upload.filename).replace("/", "_") or "layer.tar"
    spool = os.path.join(workdir, safe_name)
    total = 0
    try:
        with open(spool, "wb") as dst:
            while True:
                chunk = upload.file.read(READ_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"layer exceeds {MAX_UPLOAD_BYTES} byte limit",
                    )
                dst.write(chunk)
    finally:
        upload.file.close()
    if total == 0:
        raise HTTPException(status_code=400, detail=f"{safe_name} is empty")
    return spool


@app.get("/health", response_model=HealthResponse, tags=["health"])
def health() -> HealthResponse:
    return HealthResponse(status="ok")


@app.post(
    "/plan",
    response_model=PlanResponse,
    tags=["rollout"],
    responses={
        200: {"description": "Feasible or infeasible rollout assessment"},
        400: {
            "description": (
                "Semantically unusable input: missing snapshot usage, "
                "conflicting usage, duplicate service names, non-numeric size"
            )
        },
        422: {"description": "Schema validation error (e.g. more than 10 services)"},
    },
)
def plan(request: PlanRequest) -> PlanResponse:
    """Read-only: order a one-at-a-time rollout within the unpacked budget."""
    services = [
        Service(
            name=service.name,
            old=service.old.to_image(),
            new=service.new.to_image(),
        )
        for service in request.services
    ]
    try:
        plan = plan_rollout(
            services=services,
            resident_images=[image.to_image() for image in request.resident_images],
            snapshot_usage=[
                (tuple(snapshot.diff_ids_prefix), snapshot.exclusive_bytes)
                for snapshot in request.snapshots
            ],
            budget_bytes=request.budget_bytes,
        )
    except PlanInputError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc

    return PlanResponse(
        feasible=plan.feasible,
        reason=plan.reason,
        budget_bytes=plan.budget_bytes,
        initial_usage_bytes=plan.initial_usage_bytes,
        final_usage_bytes=plan.final_usage_bytes,
        order=list(plan.order),
        rounds=[
            RoundResponse(
                round=rnd.round_number,
                service=rnd.service,
                pre_round_usage_bytes=rnd.pre_round_usage_bytes,
                warmup_added_bytes=rnd.warmup_added_bytes,
                peak_bytes=rnd.peak_bytes,
                headroom_bytes=rnd.headroom_bytes,
                retired_bytes=rnd.retired_bytes,
                post_retire_usage_bytes=rnd.post_retire_usage_bytes,
            )
            for rnd in plan.rounds
        ],
        max_usage_bytes=plan.max_usage_bytes,
        minimum_max_usage_bytes=plan.minimum_max_usage_bytes,
        shortfall_bytes=plan.shortfall_bytes,
    )


@app.post(
    "/merge",
    tags=["layers"],
    responses={
        200: {
            "content": {"application/x-tar": {}},
            "description": "Merged uncompressed layer tar",
        },
        400: {"description": "Invalid input layer"},
        413: {"description": "Upload too large"},
    },
)
def merge_layers(
    lower: UploadFile = File(..., description="lower layer tar, applied first"),
    upper: UploadFile = File(..., description="upper layer tar, applied second"),
) -> FileResponse:
    """Merge two ordered diff layers and return the combined layer tar."""
    workdir = tempfile.mkdtemp(prefix="layermerger-req-")
    try:
        lower_path = _spool_upload(lower, workdir)
        upper_path = _spool_upload(upper, workdir)
        output_path = os.path.join(workdir, "merged-layer.tar")

        try:
            with LayerMerger() as merger:
                merger.add_layer(lower_path, origin=1)
                merger.add_layer(upper_path, origin=2)
                with open(output_path, "wb") as out:
                    merger.write_tar(out)
        except InvalidLayer as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except (OSError, EOFError) as exc:
            raise HTTPException(status_code=400, detail=f"unreadable tar: {exc}") from exc

        return FileResponse(
            output_path,
            media_type="application/x-tar",
            filename="merged-layer.tar",
            background=BackgroundTask(_cleanup, workdir),
        )
    except HTTPException:
        _cleanup(workdir)
        raise
    except Exception:
        _cleanup(workdir)
        raise
