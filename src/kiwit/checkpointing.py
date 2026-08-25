from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from .database import DatabaseSettings


@contextmanager
def postgres_checkpointer(settings: DatabaseSettings | None = None, *, setup: bool = False) -> Iterator[Any]:
    """Yield kiwiT's durable LangGraph checkpointer with restricted deserialization."""
    os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")
    try:
        from langgraph.checkpoint.postgres import PostgresSaver
    except ImportError as error:
        raise RuntimeError("install kiwiT with the 'workflow' extra to use durable workflows") from error
    database = settings or DatabaseSettings.from_env()
    with PostgresSaver.from_conn_string(database.url) as checkpointer:
        if setup:
            checkpointer.setup()
        yield checkpointer
