"""Cancellation registry for in-flight generations.

The frontend can only name a generation by the placeholder it drew for it:
`(conversation_id, item_index)`. The cancellable object, on the other hand, is the
ComfyUI job — whose id only exists once that item's turn has come, since a lot of N
items runs strictly one at a time. This module maps one onto the other and, just as
importantly, remembers slots cancelled BEFORE they ever started so `_execute_tasks`
can skip them instead of queueing work the user has already thrown away.

Everything lives in memory: a cancellation only makes sense for a generation this
process is currently running.
"""

from __future__ import annotations

import logging

from studio.comfy.jobs import cancel_job

logger = logging.getLogger(__name__)

#: slot key -> {"job_id": str | None, "cancelled": bool}
_slots: dict[str, dict] = {}


def slot_key(conversation_id: str, item_index: int) -> str:
    return f"{conversation_id}:{int(item_index)}"


def open_slot(conversation_id: str, item_index: int) -> str:
    """Declare that this item is about to run. Keeps an existing entry: the user may
    have hit cancel while an earlier item of the same lot was still rendering."""
    key = slot_key(conversation_id, item_index)
    _slots.setdefault(key, {"job_id": None, "cancelled": False})
    return key


def bind_job(key: str, job_id: str) -> None:
    """Attach the ComfyUI job that now backs this slot."""
    slot = _slots.get(key)
    if slot is not None:
        slot["job_id"] = job_id


def close_slot(key: str) -> None:
    _slots.pop(key, None)


def is_cancelled(key: str) -> bool:
    slot = _slots.get(key)
    return bool(slot and slot["cancelled"])


async def cancel_slot(conversation_id: str, item_index: int) -> dict:
    """Cancel one item of a lot, whether it is rendering or still waiting its turn.

    Returns `{cancelled, running}`: `running` says whether an actual ComfyUI job was
    interrupted, as opposed to a slot merely being marked so it gets skipped. Both
    count as a success — the item will not produce a media either way."""
    key = slot_key(conversation_id, item_index)
    slot = _slots.setdefault(key, {"job_id": None, "cancelled": False})
    slot["cancelled"] = True

    job_id = slot.get("job_id")
    stopped = await cancel_job(job_id) if job_id else False
    logger.info("Cancel requested on slot %s (job=%s, interrupted=%s)", key, job_id, stopped)
    return {"cancelled": True, "running": stopped}


def clear_conversation(conversation_id: str) -> None:
    """Drop every slot of a finished conversation, including ones cancelled before
    they ran (those are never closed by the execution loop — it skipped them)."""
    prefix = f"{conversation_id}:"
    for key in [k for k in _slots if k.startswith(prefix)]:
        _slots.pop(key, None)
