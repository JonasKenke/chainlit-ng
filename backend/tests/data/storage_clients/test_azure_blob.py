from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from azure.core.exceptions import ResourceNotFoundError

from chainlit.data.storage_clients.azure_blob import AzureBlobStorageClient


@pytest.fixture
def mock_azure_blob():
    with patch(
        "chainlit.data.storage_clients.azure_blob.AsyncBlobServiceClient"
    ) as mock_service_class:
        service = MagicMock()
        container = MagicMock()
        blob = MagicMock()
        blob.delete_blob = AsyncMock()
        service.get_container_client.return_value = container
        container.get_blob_client.return_value = blob
        mock_service_class.from_connection_string.return_value = service
        yield blob


def _make_client() -> AzureBlobStorageClient:
    return AzureBlobStorageClient("container", "account", "key")


@pytest.mark.asyncio
async def test_delete_file_treats_a_missing_blob_as_deleted(mock_azure_blob):
    mock_azure_blob.delete_blob.side_effect = ResourceNotFoundError("missing")

    assert await _make_client().delete_file("test/path/file.txt") is True


@pytest.mark.asyncio
async def test_delete_file_returns_false_for_other_errors(mock_azure_blob):
    mock_azure_blob.delete_blob.side_effect = RuntimeError("permission denied")

    assert await _make_client().delete_file("test/path/file.txt") is False
