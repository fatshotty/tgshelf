"""HTTP adapter for durable bulk operation jobs."""

from __future__ import annotations

from aiohttp import web

from tgshelf.http.api import _bad_request, _json_body
from tgshelf.http.app import RUNTIME

_STATES = {"queued", "running", "completed", "failed", "interrupted"}


def _timestamp(value) -> str | None:
    return value.isoformat() if value is not None else None


def job_to_dict(job) -> dict:
    return {
        "id": job.id,
        "operation": job.operation,
        "state": job.state,
        "parent_id": job.parent_id,
        "total": job.total,
        "succeeded": job.succeeded,
        "failed": job.failed,
        "skipped": job.skipped,
        "error": job.error,
        "created_at": _timestamp(job.created_at),
        "started_at": _timestamp(job.started_at),
        "finished_at": _timestamp(job.finished_at),
    }


def item_to_dict(item) -> dict:
    return {
        "position": item.position,
        "node_id": item.node_id,
        "source_name": item.source_name,
        "source_path": item.source_path,
        "state": item.state,
        "error": item.error,
        "started_at": _timestamp(item.started_at),
        "finished_at": _timestamp(item.finished_at),
    }


async def create_job(request: web.Request) -> web.Response:
    body = await _json_body(request)
    allowed = {"operation", "node_ids", "parent_id"}
    if set(body) - allowed:
        return _bad_request("invalid job request fields")
    operation = body.get("operation")
    node_ids = body.get("node_ids")
    parent_id = body.get("parent_id")
    if operation not in ("move", "delete"):
        return _bad_request("operation must be move or delete")
    if not isinstance(node_ids, list) or not node_ids or not all(isinstance(node_id, str) for node_id in node_ids):
        return _bad_request("node_ids must be a non-empty list of strings")
    if operation == "move" and not isinstance(parent_id, str):
        return _bad_request("parent_id is required for move")
    if operation == "delete" and parent_id is not None:
        return _bad_request("parent_id is only valid for move")
    job = await request.app[RUNTIME]["job_service"].create(operation, node_ids, parent_id)
    return web.json_response(
        {"id": job.id, "state": job.state, "status_url": f"/api/v1/jobs/{job.id}"},
        status=202,
    )


async def list_jobs(request: web.Request) -> web.Response:
    state = request.query.get("state")
    if state is not None and state not in _STATES:
        return _bad_request("invalid job state")
    try:
        limit = int(request.query.get("limit", "50"))
        offset = int(request.query.get("offset", "0"))
    except ValueError:
        return _bad_request("limit and offset must be integers")
    service = request.app[RUNTIME]["job_service"]
    jobs = await service.list(state=state, limit=limit, offset=offset)
    next_offset = offset + len(jobs) if len(jobs) == min(max(limit, 1), 100) else None
    return web.json_response({"items": [job_to_dict(job) for job in jobs], "next_offset": next_offset})


async def get_job(request: web.Request) -> web.Response:
    detail = await request.app[RUNTIME]["job_service"].get(request.match_info["job_id"])
    if detail is None:
        return web.json_response({"error": "operation job not found"}, status=404)
    job, items = detail
    return web.json_response({**job_to_dict(job), "items": [item_to_dict(item) for item in items]})


def register_job_routes(app: web.Application) -> None:
    app.router.add_post("/api/v1/jobs", create_job)
    app.router.add_get("/api/v1/jobs", list_jobs)
    app.router.add_get("/api/v1/jobs/{job_id}", get_job)
