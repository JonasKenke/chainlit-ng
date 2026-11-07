import pytest

from chainlit.data.base import BaseDataLayer


async def test_delete_all_threads_is_not_implemented_by_default():
    with pytest.raises(NotImplementedError, match="Bulk thread deletion"):
        await BaseDataLayer.delete_all_threads(None, "user-id")  # type: ignore[arg-type]
