"""Node id generation, legacy-compatible.

Legacy ids are 10 chars from [a-z0-9] and old .strm URLs embed them, so new
nodes keep the exact same format. Collisions are left to the primary key
(insert retries on conflict), as in the legacy; only ROOT_ID is excluded
explicitly since it is a reserved value inside the alphabet space.
"""

from __future__ import annotations

import secrets

from tgshelf.constants import NODE_ID_ALPHABET, NODE_ID_LENGTH, ROOT_ID


def generate_node_id() -> str:
    while True:
        node_id = "".join(secrets.choice(NODE_ID_ALPHABET) for _ in range(NODE_ID_LENGTH))
        if node_id != ROOT_ID:
            return node_id
