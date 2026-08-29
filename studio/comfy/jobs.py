"""Shared in-memory job store for ComfyUI submissions. The progress watcher in
ollama_client polls this dict to emit WebSocket events during generation."""

import asyncio
import logging
from enum import Enum

from studio.comfy import client as comfy_client
from studio.comfy.client import JobCancelled  # re-exported: callers catch it from here

logger = logging.getLogger(__name__)


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


#: Terminal states — a job in one of these can no longer be cancelled.
_FINISHED = {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}

jobs: dict[str, dict] = {}


async def run_job(job_id: str, workflow: dict, progress_listener=None):
    """Background task that monitors a ComfyUI job until completion. Opens the
    ComfyUI WS BEFORE queuing the prompt to avoid losing the early `executing`
    events that pin progress to the active sampler.

    `progress_listener(p: float)` is called for every progress update with this
    job's value (0..1). The caller uses it to forward progress to the WebSocket
    scoped to that job's own conversation — no polling, no cross-conv leak."""
    try:
        jobs[job_id]["status"] = JobStatus.RUNNING

        async def on_progress(p: float):
            jobs[job_id]["progress"] = p
            if progress_listener is not None:
                try:
                    await progress_listener(p)
                except Exception as e:
                    logger.warning("Progress listener for %s raised: %s", job_id, e)

        def on_queued(prompt_id: str):
            # Back-filled here rather than after the await: `cancel_job` needs the id
            # WHILE the job runs, and submit_and_wait only returns once it is over.
            jobs[job_id]["prompt_id"] = prompt_id

        prompt_id = await comfy_client.submit_and_wait(
            workflow, progress_callback=on_progress, on_queued=on_queued,
        )

        if jobs[job_id].get("cancelled"):
            jobs[job_id]["status"] = JobStatus.CANCELLED
            return

        result = await comfy_client.get_result(prompt_id)
        if result is None:
            jobs[job_id]["status"] = JobStatus.FAILED
            jobs[job_id]["error"] = "No output found in ComfyUI history"
            return

        jobs[job_id]["status"] = JobStatus.COMPLETED
        jobs[job_id]["progress"] = 1.0
        jobs[job_id]["result"] = result

    except JobCancelled:
        logger.info("Job %s interrupted", job_id)
        jobs[job_id]["status"] = JobStatus.CANCELLED
        jobs[job_id]["error"] = "Generation annulee"

    except Exception as e:
        logger.exception("Job %s failed", job_id)
        jobs[job_id]["status"] = JobStatus.FAILED
        jobs[job_id]["error"] = str(e)


async def cancel_job(job_id: str) -> bool:
    """Stop a job: interrupt it in ComfyUI, then drop the watcher task.

    Returns False when there is nothing to stop (unknown job, or already finished) —
    cancelling is idempotent, never an error.

    The watcher task is killed AFTER the ComfyUI call because of the pending case: a
    prompt still waiting in the queue is simply removed from it, so no `executing`
    event will ever arrive and `run_job` would otherwise wait on its WS forever.
    `asyncio.CancelledError` is a BaseException, so it flies past the handlers above
    and leaves the CANCELLED status set here untouched."""
    job = jobs.get(job_id)
    if not job or job.get("status") in _FINISHED:
        return False

    job["cancelled"] = True
    prompt_id = job.get("prompt_id")
    if prompt_id:
        await comfy_client.cancel_prompt(prompt_id)
    else:
        # Sub-second window between spawning the task and ComfyUI accepting the
        # prompt. Nothing to interrupt by id, and a global interrupt is off-limits;
        # dropping the task below is enough (it aborts the POST in flight).
        logger.info("Cancelling job %s before it reached ComfyUI", job_id)

    job["status"] = JobStatus.CANCELLED
    task = job.get("task")
    if task is not None and not task.done():
        task.cancel()
    return True
