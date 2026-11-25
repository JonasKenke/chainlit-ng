import asyncio
import json
import logging
import os
import random
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Union, cast

import aiofiles
import aiohttp
import boto3  # type: ignore
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from chainlit.context import context
from chainlit.data.base import BaseDataLayer
from chainlit.data.storage_clients.base import BaseStorageClient
from chainlit.data.utils import delete_storage_objects, queue_until_user_message
from chainlit.element import ElementDict
from chainlit.logger import logger
from chainlit.step import StepDict
from chainlit.types import (
    Feedback,
    FeedbackThreadLookup,
    PageInfo,
    PaginatedResponse,
    Pagination,
    ThreadDict,
    ThreadFilter,
    ThreadGrant,
    ThreadOwner,
)
from chainlit.user import PersistedUser, User

if TYPE_CHECKING:
    from mypy_boto3_dynamodb import DynamoDBClient

    from chainlit.element import Element


_logger = logger.getChild("DynamoDB")
_logger.setLevel(logging.WARNING)


class DynamoDBDataLayer(BaseDataLayer):
    # Grants live as GRANT# items in the thread partition; both capabilities are
    # implemented together because the feedback gate depends on the grant gate.
    thread_grants_supported = True
    feedback_authorization_supported = True

    def __init__(
        self,
        table_name: str,
        client: Optional["DynamoDBClient"] = None,
        storage_provider: Optional[BaseStorageClient] = None,
        user_thread_limit: int = 10,
    ):
        if client:
            self.client = client
        else:
            region_name = os.environ.get("AWS_REGION", "us-east-1")
            self.client = boto3.client("dynamodb", region_name=region_name)  # type: ignore

        self.table_name = table_name
        self.storage_provider = storage_provider
        self.user_thread_limit = user_thread_limit

        self._type_deserializer = TypeDeserializer()
        self._type_serializer = TypeSerializer()

    def _get_current_timestamp(self) -> str:
        return datetime.now().isoformat() + "Z"

    def _serialize_item(self, item: dict[str, Any]) -> dict[str, Any]:
        def convert_floats(obj):
            if isinstance(obj, float):
                return Decimal(str(obj))
            elif isinstance(obj, dict):
                return {k: convert_floats(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [convert_floats(v) for v in obj]
            else:
                return obj

        return {
            key: self._type_serializer.serialize(convert_floats(value))
            for key, value in item.items()
        }

    def _deserialize_item(self, item: dict[str, Any]) -> dict[str, Any]:
        def convert_decimals(obj):
            if isinstance(obj, Decimal):
                return float(obj)
            elif isinstance(obj, dict):
                return {k: convert_decimals(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [convert_decimals(v) for v in obj]
            else:
                return obj

        return {
            key: convert_decimals(self._type_deserializer.deserialize(value))
            for key, value in item.items()
        }

    def _update_item(self, key: Dict[str, Any], updates: Dict[str, Any]):
        update_expr: List[str] = []
        expression_attribute_names = {}
        expression_attribute_values = {}

        for index, (attr, value) in enumerate(updates.items()):
            if value is None:
                continue

            k, v = f"#{index}", f":{index}"
            update_expr.append(f"{k} = {v}")
            expression_attribute_names[k] = attr
            expression_attribute_values[v] = value

        self.client.update_item(
            TableName=self.table_name,
            Key=self._serialize_item(key),
            UpdateExpression="SET " + ", ".join(update_expr),
            ExpressionAttributeNames=expression_attribute_names,
            ExpressionAttributeValues=self._serialize_item(expression_attribute_values),
        )

    @property
    def context(self):
        return context

    async def get_user(self, identifier: str) -> Optional["PersistedUser"]:
        _logger.info("DynamoDB: get_user identifier=%s", identifier)
        return await self._get_user(identifier)

    async def get_user_for_acl(self, identifier: str) -> Optional["PersistedUser"]:
        # Authorization path: the caller's identifier must not reach the logs.
        return await self._get_user(identifier)

    async def _get_user(self, identifier: str) -> Optional["PersistedUser"]:
        response = self.client.get_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": f"USER#{identifier}"},
                "SK": {"S": "USER"},
            },
        )

        if "Item" not in response:
            return None

        user = self._deserialize_item(response["Item"])

        return PersistedUser(
            id=user["id"],
            identifier=user["identifier"],
            createdAt=user["createdAt"],
            metadata=user["metadata"],
        )

    async def create_user(self, user: "User") -> Optional["PersistedUser"]:
        _logger.info("DynamoDB: create_user user.identifier=%s", user.identifier)

        ts = self._get_current_timestamp()
        metadata: Dict[Any, Any] = user.metadata  # type: ignore

        item = {
            "PK": f"USER#{user.identifier}",
            "SK": "USER",
            "id": user.identifier,
            "identifier": user.identifier,
            "metadata": metadata,
            "createdAt": ts,
        }

        self.client.put_item(
            TableName=self.table_name,
            Item=self._serialize_item(item),
        )

        return PersistedUser(
            id=user.identifier,
            identifier=user.identifier,
            createdAt=ts,
            metadata=metadata,
        )

    @staticmethod
    def _parse_feedback_id(feedback_id: str) -> Optional[tuple[str, str]]:
        """Split ``THREAD#<thread>::STEP#<step>``; None on a malformed id."""
        prefix, marker = "THREAD#", "::STEP#"
        if not isinstance(feedback_id, str) or not feedback_id.startswith(prefix):
            return None
        rest = feedback_id[len(prefix) :]
        separator = rest.find(marker)
        if separator <= 0:
            return None
        thread_id = rest[:separator]
        step_id = rest[separator + len(marker) :]
        if not step_id or "::" in thread_id or "::" in step_id:
            return None
        return thread_id, step_id

    async def delete_feedback(self, feedback_id: str) -> bool:
        _logger.info("DynamoDB: delete_feedback feedback_id=%s", feedback_id)

        parsed = self._parse_feedback_id(feedback_id)
        if parsed is None:
            return False
        thread_id, step_id = parsed

        self.client.update_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": f"THREAD#{thread_id}"},
                "SK": {"S": f"STEP#{step_id}"},
            },
            UpdateExpression="REMOVE #feedback",
            ExpressionAttributeNames={"#feedback": "feedback"},
        )

        return True

    async def upsert_feedback(self, feedback: Feedback) -> str:
        _logger.info(
            "DynamoDB: upsert_feedback thread=%s step=%s value=%s",
            feedback.threadId,
            feedback.forId,
            feedback.value,
        )

        if not feedback.forId:
            raise ValueError(
                "DynamoDB data layer expects value for feedback.threadId got None"
            )

        feedback.id = f"THREAD#{feedback.threadId}::STEP#{feedback.forId}"
        serialized_feedback = self._type_serializer.serialize(asdict(feedback))

        self.client.update_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": f"THREAD#{feedback.threadId}"},
                "SK": {"S": f"STEP#{feedback.forId}"},
            },
            UpdateExpression="SET #feedback = :feedback",
            ExpressionAttributeNames={"#feedback": "feedback"},
            ExpressionAttributeValues={":feedback": serialized_feedback},
        )

        return feedback.id

    @queue_until_user_message()
    async def create_element(self, element: "Element"):
        _logger.info(
            "DynamoDB: create_element thread=%s step=%s type=%s",
            element.thread_id,
            element.for_id,
            element.type,
        )
        _logger.debug("DynamoDB: create_element: %s", element.to_dict())

        if not element.for_id:
            return

        if not self.storage_provider:
            _logger.warning(
                "DynamoDB: create_element error. No storage_provider is configured!"
            )
            return

        content: Optional[Union[bytes, str]] = None

        if element.content:
            content = element.content

        elif element.path:
            _logger.debug("DynamoDB: create_element reading file %s", element.path)
            async with aiofiles.open(element.path, "rb") as f:
                content = await f.read()

        elif element.url:
            _logger.debug("DynamoDB: create_element http %s", element.url)
            async with aiohttp.ClientSession() as session:
                async with session.get(element.url) as response:
                    if response.status == 200:
                        content = await response.read()
                    else:
                        raise ValueError(
                            f"Failed to read content from {element.url} status {response.status}",
                        )

        else:
            raise ValueError("Element url, path or content must be provided")

        if content is None:
            raise ValueError("Content is None, cannot upload file")

        if not element.mime:
            element.mime = "application/octet-stream"

        context_user = self.context.session.user
        user_folder = getattr(context_user, "id", "unknown")
        file_object_key = f"{user_folder}/{element.thread_id}/{element.id}"

        uploaded_file = await self.storage_provider.upload_file(
            object_key=file_object_key,
            data=content,
            mime=element.mime,
            overwrite=True,
            content_disposition=element.get_content_disposition(element.mime),
        )
        if not uploaded_file:
            raise ValueError(
                "DynamoDB Error: create_element, Failed to persist data in storage_provider",
            )

        element_dict: Dict[str, Any] = element.to_dict()  # type: ignore
        element_dict.update(
            {
                "PK": f"THREAD#{element.thread_id}",
                "SK": f"ELEMENT#{element.id}",
                "url": uploaded_file.get("url"),
                "objectKey": uploaded_file.get("object_key"),
            }
        )

        self.client.put_item(
            TableName=self.table_name,
            Item=self._serialize_item(element_dict),
        )

    async def get_element(
        self, thread_id: str, element_id: str
    ) -> Optional["ElementDict"]:
        _logger.info(
            "DynamoDB: get_element thread=%s element=%s", thread_id, element_id
        )

        response = self.client.get_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": f"THREAD#{thread_id}"},
                "SK": {"S": f"ELEMENT#{element_id}"},
            },
            ConsistentRead=True,
        )

        if "Item" not in response:
            return None

        return self._deserialize_item(response["Item"])  # type: ignore

    @queue_until_user_message()
    async def delete_element(self, element_id: str, thread_id: Optional[str] = None):
        thread_id = thread_id or self.context.session.thread_id
        _logger.info(
            "DynamoDB: delete_element thread=%s element=%s", thread_id, element_id
        )

        self.client.delete_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": f"THREAD#{thread_id}"},
                "SK": {"S": f"ELEMENT#{element_id}"},
            },
        )

    @queue_until_user_message()
    async def create_step(self, step_dict: "StepDict"):
        _logger.info(
            "DynamoDB: create_step thread=%s step=%s",
            step_dict.get("threadId"),
            step_dict.get("id"),
        )
        _logger.debug("DynamoDB: create_step: %s", step_dict)

        item = dict(step_dict)
        item.update(
            {
                # ignore type, dynamo needs these so we want to fail if not set
                "PK": f"THREAD#{step_dict['threadId']}",  # type: ignore
                "SK": f"STEP#{step_dict['id']}",  # type: ignore
            }
        )

        self.client.put_item(
            TableName=self.table_name,
            Item=self._serialize_item(item),
        )

    @queue_until_user_message()
    async def update_step(self, step_dict: "StepDict"):
        _logger.info(
            "DynamoDB: update_step thread=%s step=%s",
            step_dict.get("threadId"),
            step_dict.get("id"),
        )
        _logger.debug("DynamoDB: update_step: %s", step_dict)

        self._update_item(
            key={
                # ignore type, dynamo needs these so we want to fail if not set
                "PK": f"THREAD#{step_dict['threadId']}",  # type: ignore
                "SK": f"STEP#{step_dict['id']}",  # type: ignore
            },
            updates=step_dict,  # type: ignore
        )

    async def write_approval(
        self,
        *,
        thread_id: str,
        step_id: str,
        record: Dict[str, Any],
        expected_revision: Optional[int],
    ) -> bool:
        names = {"#md": "metadata", "#ap": "approval", "#id": "id"}
        values = self._serialize_item({":record": record, ":id": record.get("id")})
        if expected_revision is None:
            names["#status"] = "status"
            condition = (
                "attribute_not_exists(#md.#ap) OR "
                "(#md.#ap.#status <> :pending) OR "
                "(#md.#ap.#id = :id AND #md.#ap.#status = :pending)"
            )
            values.update(self._serialize_item({":pending": "pending"}))
        else:
            names["#revision"] = "revision"
            condition = "#md.#ap.#id = :id AND #md.#ap.#revision = :expected"
            values.update(self._serialize_item({":expected": expected_revision}))

        try:
            self.client.update_item(
                TableName=self.table_name,
                Key=self._serialize_item(
                    {"PK": f"THREAD#{thread_id}", "SK": f"STEP#{step_id}"}
                ),
                UpdateExpression="SET #md.#ap = :record",
                ConditionExpression=condition,
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
            )
            return True
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code != "ConditionalCheckFailedException":
                _logger.warning("DynamoDB: write_approval failed: %s", code)
            return False
        except Exception:
            _logger.exception("DynamoDB: write_approval failed")
            return False

    @queue_until_user_message()
    async def delete_step(self, step_id: str):
        thread_id = self.context.session.thread_id
        _logger.info("DynamoDB: delete_feedback thread=%s step=%s", thread_id, step_id)

        self.client.delete_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": f"THREAD#{thread_id}"},
                "SK": {"S": f"STEP#{step_id}"},
            },
        )

    async def get_thread_author(self, thread_id: str) -> str:
        _logger.info("DynamoDB: get_thread_author thread=%s", thread_id)

        response = self.client.get_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": f"THREAD#{thread_id}"},
                "SK": {"S": "THREAD"},
            },
            ProjectionExpression="userId",
            ConsistentRead=True,
        )

        if "Item" not in response:
            raise ValueError(f"Author not found for thread_id {thread_id}")

        item = self._deserialize_item(response["Item"])
        return item["userId"]

    async def get_thread_owner(self, thread_id: str) -> Optional["ThreadOwner"]:
        _logger.info("DynamoDB: get_thread_owner thread=%s", thread_id)

        response = self.client.get_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": f"THREAD#{thread_id}"},
                "SK": {"S": "THREAD"},
            },
            ProjectionExpression="userId, userIdentifier",
            ConsistentRead=True,
        )

        if "Item" not in response:
            return None

        item = self._deserialize_item(response["Item"])
        return ThreadOwner(
            user_id=item.get("userId"), identifier=item.get("userIdentifier")
        )

    async def get_thread_grant(
        self, thread_id: str, grantee_user_id: str
    ) -> Optional[ThreadGrant]:
        # Quiet lookup: the grantee id must not reach the logs.
        response = self.client.get_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": f"THREAD#{thread_id}"},
                "SK": {"S": f"GRANT#{grantee_user_id}"},
            },
            ConsistentRead=True,
        )

        if "Item" not in response:
            return None

        item = self._deserialize_item(response["Item"])
        if item.get("role") != "read" or not item.get("granteeUserCreatedAt"):
            return None
        return self._grant_from_item(item)

    async def list_thread_grants(self, thread_id: str) -> List[ThreadGrant]:
        items = await self._query_thread_items(thread_id, sk_prefix="GRANT#")
        return [self._grant_from_item(self._deserialize_item(item)) for item in items]

    async def create_thread_grant(self, grant: ThreadGrant) -> None:
        item = {
            "PK": f"THREAD#{grant.thread_id}",
            "SK": f"GRANT#{grant.grantee_user_id}",
            "granteeUserCreatedAt": grant.grantee_user_created_at,
            "granteeIdentifier": grant.grantee_identifier,
            "grantedByUserId": grant.granted_by_user_id,
            "grantedAt": grant.granted_at,
            "role": grant.role,
        }
        try:
            self.client.put_item(
                TableName=self.table_name,
                Item=self._serialize_item(item),
                ConditionExpression="attribute_not_exists(PK)",
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != (
                "ConditionalCheckFailedException"
            ):
                raise
            # A duplicate grant keeps the first generation: no-op.
            return

    async def delete_thread_grant(self, thread_id: str, grantee_user_id: str) -> bool:
        response = self.client.delete_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": f"THREAD#{thread_id}"},
                "SK": {"S": f"GRANT#{grantee_user_id}"},
            },
            ReturnValues="ALL_OLD",
        )
        return "Attributes" in response

    @staticmethod
    def _grant_from_item(item: Dict[str, Any]) -> ThreadGrant:
        return ThreadGrant(
            thread_id=str(item["PK"]).removeprefix("THREAD#"),
            grantee_user_id=str(item["SK"]).removeprefix("GRANT#"),
            grantee_user_created_at=str(item["granteeUserCreatedAt"]),
            grantee_identifier=str(item["granteeIdentifier"]),
            granted_by_user_id=str(item["grantedByUserId"]),
            granted_at=str(item["grantedAt"]),
            role=str(item["role"]),
        )

    async def _delete_thread_grants(self, thread_id: str) -> None:
        grant_items = await self._query_thread_items(thread_id, sk_prefix="GRANT#")
        keys = [{"PK": item["PK"], "SK": item["SK"]} for item in grant_items]
        if keys:
            await self._batch_delete_items(keys)

    async def get_feedback_thread_id(self, feedback_id: str) -> FeedbackThreadLookup:
        # Dynamo has no client-keyed feedback row: the id is the key space.
        parsed = self._parse_feedback_id(feedback_id)
        if parsed is None:
            return FeedbackThreadLookup(status="not_found")
        return FeedbackThreadLookup(status="found", thread_id=parsed[0])

    async def step_belongs_to_thread(self, thread_id: str, step_id: str) -> bool:
        try:
            response = self.client.get_item(
                TableName=self.table_name,
                Key={
                    "PK": {"S": f"THREAD#{thread_id}"},
                    "SK": {"S": f"STEP#{step_id}"},
                },
                ProjectionExpression="PK",
                ConsistentRead=True,
            )
        except Exception:
            _logger.warning("DynamoDB: step owner lookup failed thread=%s", thread_id)
            return False
        return "Item" in response

    async def _query_thread_items(
        self, thread_id: str, sk_prefix: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        names = {"#pk": "PK"}
        values = {":pk": {"S": f"THREAD#{thread_id}"}}
        condition = "#pk = :pk"
        if sk_prefix:
            condition += " AND begins_with(#sk, :sk)"
            names["#sk"] = "SK"
            values[":sk"] = {"S": sk_prefix}
        query_args: Dict[str, Any] = {
            "TableName": self.table_name,
            "KeyConditionExpression": condition,
            "ExpressionAttributeNames": names,
            "ExpressionAttributeValues": values,
            "ConsistentRead": True,
        }
        while True:
            response = self.client.query(**query_args)  # type: ignore
            items.extend(response.get("Items", []))
            if "LastEvaluatedKey" not in response:
                return items
            query_args["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    async def _batch_delete_items(self, keys: List[Dict[str, Any]]):
        batch_item_size = 25
        for i in range(0, len(keys), batch_item_size):
            chunk = [
                {"DeleteRequest": {"Key": key}} for key in keys[i : i + batch_item_size]
            ]
            response = self.client.batch_write_item(
                RequestItems={self.table_name: chunk}  # type: ignore
            )

            backoff_time = 1
            while response.get("UnprocessedItems"):
                backoff_time *= 2
                # Cap the backoff time at 32 seconds & add jitter
                delay = min(backoff_time, 32) + random.uniform(0, 1)
                await asyncio.sleep(delay)

                response = self.client.batch_write_item(
                    RequestItems=response["UnprocessedItems"]
                )

    async def delete_thread(self, thread_id: str):
        _logger.info("DynamoDB: delete_thread thread=%s", thread_id)

        items = await self._query_thread_items(thread_id)
        if not items:
            return

        # Delete the thread's items first and the THREAD marker last, re-querying
        # until the partition holds only the marker: a step or element written
        # while deleting must not be orphaned.
        while True:
            keys = [
                {"PK": item["PK"], "SK": item["SK"]}
                for item in items
                if item["SK"]["S"] != "THREAD"
            ]
            if not keys:
                break
            # Read the keys from every re-query: an element written during the
            # deletion has a blob the initial query never saw, and deleting its
            # row without the blob leaves personal data with no handle.
            object_keys = [
                item["objectKey"]["S"]
                for item in items
                if item.get("SK", {}).get("S", "").startswith("ELEMENT#")
                and item.get("objectKey", {}).get("S")
            ]
            await delete_storage_objects(self.storage_provider, object_keys)
            await self._batch_delete_items(keys)
            items = await self._query_thread_items(thread_id)

        self.client.delete_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": f"THREAD#{thread_id}"},
                "SK": {"S": "THREAD"},
            },
        )

    async def delete_all_threads(self, user_id: str) -> int:
        _logger.info("DynamoDB: delete_all_threads user=%s", user_id)

        # Collect every thread id before deleting anything: deleting while the GSI
        # paginates would shift the result set under the cursor.
        query_args: Dict[str, Any] = {
            "TableName": self.table_name,
            "IndexName": "UserThread",
            "KeyConditionExpression": "#UserThreadPK = :pk",
            "ExpressionAttributeNames": {"#UserThreadPK": "UserThreadPK"},
            "ExpressionAttributeValues": {":pk": {"S": f"USER#{user_id}"}},
        }
        thread_ids: List[str] = []
        while True:
            response = self.client.query(**query_args)  # type: ignore
            for item in response.get("Items", []):
                pk = item.get("PK", {}).get("S")
                if pk:
                    thread_ids.append(pk.removeprefix("THREAD#"))
            if "LastEvaluatedKey" not in response:
                break
            query_args["ExclusiveStartKey"] = response["LastEvaluatedKey"]

        if not thread_ids:
            return 0

        object_keys: List[str] = []
        keys: List[Dict[str, Any]] = []
        deleted_threads = 0
        for thread_id in thread_ids:
            thread_items = await self._query_thread_items(thread_id)
            # A stale GSI row can outlive its base-table partition; only count
            # threads whose THREAD marker is still there.
            if any(item.get("SK", {}).get("S") == "THREAD" for item in thread_items):
                deleted_threads += 1
            for item in thread_items:
                keys.append({"PK": item["PK"], "SK": item["SK"]})
                object_key = item.get("objectKey", {}).get("S")
                if (
                    item.get("SK", {}).get("S", "").startswith("ELEMENT#")
                    and object_key
                ):
                    object_keys.append(object_key)

        await delete_storage_objects(self.storage_provider, object_keys)
        await self._batch_delete_items(keys)

        return deleted_threads

    async def list_threads(
        self, pagination: "Pagination", filters: "ThreadFilter"
    ) -> "PaginatedResponse[ThreadDict]":
        _logger.info("DynamoDB: list_threads filters.userId=%s", filters.userId)

        if filters.feedback:
            _logger.warning("DynamoDB: filters on feedback not supported")

        if filters.sharedWithUserId:
            # v1 does not list granted threads: a grant has no UserThread row,
            # and a SHARED# shadow would break id mapping and cleanup (M5).
            _logger.info(
                "DynamoDB: shared threads are not listed; returning owned threads only"
            )

        paginated_response: PaginatedResponse[ThreadDict] = PaginatedResponse(
            data=[],
            pageInfo=PageInfo(
                hasNextPage=False, startCursor=pagination.cursor, endCursor=None
            ),
        )

        query_args: Dict[str, Any] = {
            "TableName": self.table_name,
            "IndexName": "UserThread",
            "ScanIndexForward": False,
            "Limit": self.user_thread_limit,
            "KeyConditionExpression": "#UserThreadPK = :pk",
            "ExpressionAttributeNames": {
                "#UserThreadPK": "UserThreadPK",
            },
            "ExpressionAttributeValues": {
                ":pk": {"S": f"USER#{filters.userId}"},
            },
        }

        if pagination.cursor:
            query_args["ExclusiveStartKey"] = json.loads(pagination.cursor)

        if filters.search:
            query_args["FilterExpression"] = "contains(#name, :search)"
            query_args["ExpressionAttributeNames"]["#name"] = "name"
            query_args["ExpressionAttributeValues"][":search"] = {"S": filters.search}

        response = self.client.query(**query_args)  # type: ignore

        if "LastEvaluatedKey" in response:
            paginated_response.pageInfo.hasNextPage = True
            paginated_response.pageInfo.endCursor = json.dumps(
                response["LastEvaluatedKey"]
            )

        for item in response["Items"]:
            # Only the THREAD marker is a thread row; a stale GSI-shaped
            # SHARED# shadow must not be parsed as one.
            if item.get("SK", {}).get("S") != "THREAD":
                continue
            deserialized_item: Dict[str, Any] = self._deserialize_item(item)
            thread = ThreadDict(  # type: ignore
                id=deserialized_item["PK"].removeprefix("THREAD#"),
                createdAt=deserialized_item["UserThreadSK"].removeprefix("TS#"),
                name=deserialized_item["name"],
            )
            paginated_response.data.append(thread)

        return paginated_response

    async def get_thread(self, thread_id: str) -> "Optional[ThreadDict]":
        _logger.info("DynamoDB: get_thread thread=%s", thread_id)

        # Get all thread records
        thread_items: List[Any] = []

        cursor: Dict[str, Any] = {}
        while True:
            response = self.client.query(
                TableName=self.table_name,
                KeyConditionExpression="#pk = :pk",
                ExpressionAttributeNames={"#pk": "PK"},
                ExpressionAttributeValues={":pk": {"S": f"THREAD#{thread_id}"}},
                ConsistentRead=True,
                **cursor,
            )

            deserialized_items = map(self._deserialize_item, response["Items"])
            thread_items.extend(deserialized_items)

            if "LastEvaluatedKey" not in response:
                break
            cursor["ExclusiveStartKey"] = response["LastEvaluatedKey"]

        if len(thread_items) == 0:
            return None

        # process accordingly
        thread_dict: Optional[ThreadDict] = None
        steps = []
        elements = []

        for item in thread_items:
            if item["SK"] == "THREAD":
                thread_dict = item

            elif item["SK"].startswith("ELEMENT"):
                if self.storage_provider is not None:
                    try:
                        item["url"] = await self.storage_provider.get_read_url(
                            object_key=item["objectKey"],
                        )
                    except Exception as e:
                        _logger.warning(
                            "Failed to get read URL for element '%s': %s",
                            item.get("id", "unknown"),
                            e,
                        )
                elements.append(item)

            elif item["SK"].startswith("STEP"):
                if "feedback" in item:  # Decimal is not json serializable
                    item["feedback"]["value"] = int(item["feedback"]["value"])
                steps.append(item)

        if not thread_dict:
            if len(thread_items) > 0:
                _logger.warning(
                    "DynamoDB: found orphaned items for thread=%s", thread_id
                )
            return None

        steps.sort(key=lambda i: i["createdAt"])
        thread_dict.update(
            {
                "steps": steps,
                "elements": elements,
            }
        )

        return thread_dict

    async def update_thread(
        self,
        thread_id: str,
        name: Optional[str] = None,
        user_id: Optional[str] = None,
        metadata: Optional[Dict] = None,
        tags: Optional[List[str]] = None,
    ):
        _logger.info("DynamoDB: update_thread thread=%s userId=%s", thread_id, user_id)
        _logger.debug(
            "DynamoDB: update_thread name=%s tags=%s metadata=%s", name, tags, metadata
        )

        if metadata is None:
            metadata = {}

        # An ownership change invalidates every grant (M3: user_id=None deletes
        # nothing). Delete grants before the owner write: if the cleanup fails,
        # the old owner keeps the thread instead of a stale grant surviving a
        # successful ownership flip.
        if user_id is not None:
            stored = self.client.get_item(
                TableName=self.table_name,
                Key={
                    "PK": {"S": f"THREAD#{thread_id}"},
                    "SK": {"S": "THREAD"},
                },
                ProjectionExpression="userId",
                ConsistentRead=True,
            )
            if "Item" in stored:
                stored_owner = self._deserialize_item(stored["Item"]).get("userId")
                if stored_owner is None or str(stored_owner) != str(user_id):
                    await self._delete_thread_grants(thread_id)

        ts = self._get_current_timestamp()

        item = {
            # GSI: UserThread
            "UserThreadSK": f"TS#{ts}",
            #
            "id": thread_id,
            "createdAt": ts,
            "name": name,
            "userId": user_id,
            "userIdentifier": user_id,
            "tags": tags,
            "metadata": metadata,
        }

        if user_id:
            # user_id may be None on subsequent calls, don't update UserThreadPK to "USER#{None}"
            item["UserThreadPK"] = f"USER#{user_id}"

        self._update_item(
            key={
                "PK": f"THREAD#{thread_id}",
                "SK": "THREAD",
            },
            updates=item,
        )

    async def get_favorite_steps(self, user_id: str) -> List["StepDict"]:
        _logger.info("DynamoDB: get_favorite_steps user_id=%s", user_id)

        thread_ids = []
        query_args: Dict[str, Any] = {
            "TableName": self.table_name,
            "IndexName": "UserThread",
            "KeyConditionExpression": "#UserThreadPK = :pk",
            "ExpressionAttributeNames": {"#UserThreadPK": "UserThreadPK"},
            "ExpressionAttributeValues": {":pk": {"S": f"USER#{user_id}"}},
        }

        while True:
            response = self.client.query(**query_args)  # type: ignore
            for item in response.get("Items", []):
                pk = item.get("PK", {}).get("S")
                if pk:
                    thread_ids.append(pk.removeprefix("THREAD#"))

            if "LastEvaluatedKey" not in response:
                break
            query_args["ExclusiveStartKey"] = response["LastEvaluatedKey"]

        favorite_steps: List[Dict[str, Any]] = []

        for thread_id in thread_ids:
            t_query_args: Dict[str, Any] = {
                "TableName": self.table_name,
                "KeyConditionExpression": "#pk = :pk AND begins_with(#sk, :sk_prefix)",
                "FilterExpression": "#metadata.#favorite = :true",
                "ExpressionAttributeNames": {
                    "#pk": "PK",
                    "#sk": "SK",
                    "#metadata": "metadata",
                    "#favorite": "favorite",
                },
                "ExpressionAttributeValues": {
                    ":pk": {"S": f"THREAD#{thread_id}"},
                    ":sk_prefix": {"S": "STEP#"},
                    ":true": {"BOOL": True},
                },
            }

            while True:
                response = self.client.query(**t_query_args)  # type: ignore
                for item in response.get("Items", []):
                    step = self._deserialize_item(item)
                    if "PK" in step:
                        del step["PK"]
                    if "SK" in step:
                        del step["SK"]
                    if "feedback" in step:
                        del step["feedback"]

                    favorite_steps.append(step)

                if "LastEvaluatedKey" not in response:
                    break
                t_query_args["ExclusiveStartKey"] = response["LastEvaluatedKey"]

        favorite_steps.sort(key=lambda x: x.get("createdAt", ""), reverse=True)
        return cast(List["StepDict"], favorite_steps)

    async def close(self) -> None:
        if self.storage_provider:
            await self.storage_provider.close()
        self.client.close()
