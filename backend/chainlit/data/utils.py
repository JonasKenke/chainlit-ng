import functools
from collections import deque
from typing import Iterable, Optional

from chainlit.context import context
from chainlit.data.storage_clients.base import BaseStorageClient
from chainlit.session import WebsocketSession


async def delete_storage_objects(
    storage_client: Optional[BaseStorageClient], object_keys: Iterable[str]
) -> None:
    """Delete the storage objects of an erasure before its rows.

    A client reports a missing object as success, so retrying after a partial
    failure re-passes the already-deleted keys without wedging. Any other
    failure raises, keeping the rows so a retry can still locate the objects.
    """
    if storage_client is None:
        return

    failed = [
        object_key
        for object_key in object_keys
        if not await storage_client.delete_file(object_key=object_key)
    ]
    if failed:
        raise RuntimeError(
            f"Failed to delete {len(failed)} storage object(s); aborting erasure"
        )


def queue_until_user_message():
    def decorator(method):
        @functools.wraps(method)
        async def wrapper(self, *args, **kwargs):
            if (
                isinstance(context.session, WebsocketSession)
                and not context.session.has_first_interaction
            ):
                # Queue the method invocation waiting for the first user message
                queues = context.session.thread_queues
                method_name = method.__name__
                if method_name not in queues:
                    queues[method_name] = deque()
                queues[method_name].append((method, self, args, kwargs))

            else:
                # Otherwise, Execute the method immediately
                return await method(self, *args, **kwargs)

        return wrapper

    return decorator
