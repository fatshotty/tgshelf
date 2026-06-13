"""Channel semantics: effective channel + cross-channel part forwarding.

ONE implementation of each, replacing the legacy's divergent copies
(four effective-channel walks, three forward_file_between_channels).
"""

from __future__ import annotations

import logging
from typing import Any, Sequence

from tgshelf.core.upload import PartRecord
from tgshelf.telegram.errors import DocIdMismatch, PartMissing

log = logging.getLogger("tgshelf.channels")


def effective_channel(
    node: Any,
    ancestors: Sequence[Any],
    master_channel: int,
    *,
    skip_current: bool = False,
) -> int:
    """The channel a node's files physically live in: the nearest node (self,
    then ancestors closest-first) with a non-NULL channel_id, else the master
    channel from config.

    `ancestors` is root-first (as NodeRepo.ancestors returns). `skip_current`
    ignores the node's own channel — used when computing the channel of a node's
    NEW position during a move, where its stored channel is stale.
    """
    chain = [] if skip_current else [node]
    chain.extend(reversed(ancestors))  # closest ancestor first
    for n in chain:
        if n.channel_id is not None:
            return n.channel_id
    return master_channel


async def forward_parts(
    gateway: Any, parts: Sequence[PartRecord], dest_channel: int
) -> list[PartRecord]:
    """Copy each part to `dest_channel`, returning the new part records.

    Crash-safe ordering (the legacy movebatch's): verify the live message holds
    our document (doc_id), copy it to the destination, record the new
    message_id/doc_id. NO deletion happens here — the caller commits the new
    parts to the DB first, then deletes the originals (delete_originals), so an
    interruption can only leak duplicate copies, never lose data.

    A part already in `dest_channel` is kept as-is (no copy, no duplicate).
    """
    new_parts: list[PartRecord] = []
    for part in parts:
        if part.channel_id == dest_channel:
            new_parts.append(part)
            continue

        ref = await gateway.get_document(part.channel_id, part.message_id)
        if ref is None:
            raise PartMissing(file_path=str(part.message_id), part_idx=part.idx)
        if part.doc_id is not None and ref.doc_id != part.doc_id:
            raise DocIdMismatch(expected=part.doc_id, found=ref.doc_id)

        message_id, doc_id = await gateway.copy_message(
            part.channel_id, part.message_id, dest_channel
        )
        new_parts.append(
            PartRecord(
                idx=part.idx,
                channel_id=dest_channel,
                message_id=message_id,
                doc_id=doc_id,
                size=part.size,
                original_filename=part.original_filename,
            )
        )
    return new_parts


async def delete_originals(gateway: Any, parts: Sequence[PartRecord]) -> None:
    """Best-effort deletion of the original messages after a move's DB commit.

    A message already gone (deleted out-of-band) or an undeletable one never
    fails the move — at worst a duplicate is leaked in the old channel.
    """
    for part in parts:
        try:
            await gateway.delete_message(part.channel_id, part.message_id)
        except Exception as exc:  # noqa: BLE001 - best-effort, never fail the move
            log.warning(
                "could not delete original message %s in channel %s: %s",
                part.message_id, part.channel_id, exc,
            )
