import asyncio
import json
import ssl
import uuid
from dataclasses import asdict
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Union, cast

import aiofiles
import aiohttp
from sqlalchemy import bindparam, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from chainlit.data.base import BaseDataLayer
from chainlit.data.storage_clients.base import BaseStorageClient
from chainlit.data.utils import delete_storage_objects, queue_until_user_message
from chainlit.element import ElementDict
from chainlit.logger import logger
from chainlit.prompt_versioning import (
    MAX_PROMPT_VERSIONS,
    PromptVersionConflict,
    PromptVersionLimitReached,
    enforce_content_limit,
    fingerprint,
    fingerprint_from_values,
    prompt_kind,
)
from chainlit.step import StepDict
from chainlit.types import (
    Feedback,
    FeedbackDict,
    FeedbackThreadLookup,
    PageInfo,
    PaginatedResponse,
    Pagination,
    PromptDict,
    PromptUpdateDict,
    PromptVersionDict,
    PromptVersionSummaryDict,
    ScheduledTaskCompletion,
    ScheduledTaskDict,
    SkillUpdateDict,
    ThreadDict,
    ThreadFilter,
    ThreadGrant,
    ThreadOwner,
)
from chainlit.user import PersistedUser, User

if TYPE_CHECKING:
    from chainlit.element import Element, ElementDict
    from chainlit.step import StepDict


class SQLAlchemyDataLayer(BaseDataLayer):
    # Opt-in capabilities: the grant table is ensured lazily and the typed
    # feedback/step lookups below are implemented on this layer.
    thread_grants_supported = True
    feedback_authorization_supported = True

    def __init__(
        self,
        conninfo: str,
        connect_args: Optional[dict[str, Any]] = None,
        ssl_require: bool = False,
        storage_provider: Optional[BaseStorageClient] = None,
        user_thread_limit: Optional[int] = 1000,
        show_logger: Optional[bool] = False,
    ):
        self._conninfo = conninfo
        self.user_thread_limit = user_thread_limit
        self.show_logger = show_logger
        # Serialise first-use DDL inside this process. A second process can
        # still win the ALTER, so the column check below also re-reads after a
        # failed statement.
        self._prompt_versioning_lock = asyncio.Lock()
        if connect_args is None:
            connect_args = {}
        if ssl_require:
            # Create an SSL context to require an SSL connection
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
            connect_args["ssl"] = ssl_context
        self.engine: AsyncEngine = create_async_engine(
            self._conninfo, connect_args=connect_args
        )
        self.async_session = sessionmaker(
            bind=self.engine, expire_on_commit=False, class_=AsyncSession
        )  # type: ignore
        if storage_provider:
            self.storage_provider: Optional[BaseStorageClient] = storage_provider
            if self.show_logger:
                logger.info("SQLAlchemyDataLayer storage client initialized")
        else:
            self.storage_provider = None
            logger.warning(
                "SQLAlchemyDataLayer storage client is not initialized and elements will not be persisted!"
            )

    ###### SQL Helpers ######
    async def execute_sql(
        self,
        query: str,
        parameters: dict,
        expanding: Optional[List[str]] = None,
        quiet: bool = False,
        raise_on_error: bool = False,
    ) -> Union[List[Dict[str, Any]], int, None]:
        """Run one statement; ``raise_on_error`` lets a caller tell no-row from failure."""
        parameterized_query = text(query)
        if expanding:
            parameterized_query = parameterized_query.bindparams(
                *(bindparam(name, expanding=True) for name in expanding)
            )
        async with self.async_session() as session:
            try:
                await session.begin()
                result = await session.execute(parameterized_query, parameters)
                await session.commit()
                if result.returns_rows:
                    json_result = [dict(row._mapping) for row in result.fetchall()]
                    clean_json_result = self.clean_result(json_result)
                    assert isinstance(clean_json_result, list) or isinstance(
                        clean_json_result, int
                    )
                    return clean_json_result
                else:
                    return result.rowcount
            except SQLAlchemyError as e:
                await session.rollback()
                if raise_on_error:
                    raise
                if quiet:
                    # The exception text can render query parameters; keep them out of the log.
                    logger.warning(
                        "SQLAlchemy: quiet lookup failed (%s)", type(e).__name__
                    )
                else:
                    logger.warning(f"An error occurred: {e}")
                return None
            except Exception as e:
                await session.rollback()
                if raise_on_error:
                    raise
                if quiet:
                    logger.warning(
                        "SQLAlchemy: quiet lookup failed (%s)", type(e).__name__
                    )
                else:
                    logger.warning(f"An unexpected error occurred: {e}")
                return None

    async def get_current_timestamp(self) -> str:
        return datetime.now().isoformat() + "Z"

    def clean_result(self, obj):
        """Recursively change UUID -> str and serialize dictionaries"""
        if isinstance(obj, dict):
            return {k: self.clean_result(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [self.clean_result(item) for item in obj]
        elif isinstance(obj, uuid.UUID):
            return str(obj)
        return obj

    ###### User ######
    async def get_user(self, identifier: str) -> Optional[PersistedUser]:
        if self.show_logger:
            logger.info(f"SQLAlchemy: get_user, identifier={identifier}")
        return await self._get_user(identifier)

    async def get_user_for_acl(self, identifier: str) -> Optional[PersistedUser]:
        # Authorization path: neither the result nor a DB error may log the identifier.
        return await self._get_user(identifier, quiet=True)

    async def _get_user(
        self, identifier: str, quiet: bool = False
    ) -> Optional[PersistedUser]:
        query = "SELECT * FROM users WHERE identifier = :identifier"
        parameters = {"identifier": identifier}
        result = await self.execute_sql(query=query, parameters=parameters, quiet=quiet)
        if result and isinstance(result, list):
            user_data = result[0]

            # SQLite returns JSON as string, we most convert it. (#1137)
            metadata = user_data.get("metadata", {})
            if isinstance(metadata, str):
                metadata = json.loads(metadata)

            assert isinstance(metadata, dict)
            assert isinstance(user_data["id"], str)
            assert isinstance(user_data["identifier"], str)
            assert isinstance(user_data["createdAt"], str)

            return PersistedUser(
                id=user_data["id"],
                identifier=user_data["identifier"],
                createdAt=user_data["createdAt"],
                metadata=metadata,
            )
        return None

    async def _get_user_identifer_by_id(self, user_id: str) -> str:
        if self.show_logger:
            logger.info(f"SQLAlchemy: _get_user_identifer_by_id, user_id={user_id}")
        query = "SELECT identifier FROM users WHERE id = :user_id"
        parameters = {"user_id": user_id}
        result = await self.execute_sql(query=query, parameters=parameters)

        assert result
        assert isinstance(result, list)

        return result[0]["identifier"]

    async def _get_user_id_by_thread(self, thread_id: str) -> Optional[str]:
        if self.show_logger:
            logger.info(f"SQLAlchemy: _get_user_id_by_thread, thread_id={thread_id}")
        query = """SELECT "userId" FROM threads WHERE id = :thread_id"""
        parameters = {"thread_id": thread_id}
        result = await self.execute_sql(query=query, parameters=parameters)
        if result:
            assert isinstance(result, list)
            return result[0]["userId"]

        return None

    async def create_user(self, user: User) -> Optional[PersistedUser]:
        if self.show_logger:
            logger.info(f"SQLAlchemy: create_user, user_identifier={user.identifier}")
        existing_user: Optional[PersistedUser] = await self.get_user(user.identifier)
        user_dict: Dict[str, Any] = {
            "identifier": str(user.identifier),
            "metadata": json.dumps(user.metadata) or {},
        }
        if not existing_user:  # create the user
            if self.show_logger:
                logger.info("SQLAlchemy: create_user, creating the user")
            user_dict["id"] = str(uuid.uuid4())
            user_dict["createdAt"] = await self.get_current_timestamp()
            query = """INSERT INTO users ("id", "identifier", "createdAt", "metadata") VALUES (:id, :identifier, :createdAt, :metadata)"""
            await self.execute_sql(query=query, parameters=user_dict)
        else:  # update the user
            if self.show_logger:
                logger.info("SQLAlchemy: update user metadata")
            query = """UPDATE users SET "metadata" = :metadata WHERE "identifier" = :identifier"""
            await self.execute_sql(
                query=query, parameters=user_dict
            )  # We want to update the metadata
        return await self.get_user(user.identifier)

    ###### Threads ######
    async def get_thread_author(self, thread_id: str) -> str:
        if self.show_logger:
            logger.info(f"SQLAlchemy: get_thread_author, thread_id={thread_id}")
        query = """SELECT "userIdentifier" FROM threads WHERE "id" = :id"""
        parameters = {"id": thread_id}
        result = await self.execute_sql(query=query, parameters=parameters)
        if isinstance(result, list) and result:
            author_identifier = result[0].get("userIdentifier")
            if author_identifier is not None:
                return author_identifier
        raise ValueError(f"Author not found for thread_id {thread_id}")

    async def get_thread_owner(self, thread_id: str) -> Optional[ThreadOwner]:
        if self.show_logger:
            logger.info(f"SQLAlchemy: get_thread_owner, thread_id={thread_id}")
        query = """SELECT "userId", "userIdentifier" FROM threads WHERE "id" = :id"""
        result = await self.execute_sql(query=query, parameters={"id": thread_id})
        if not isinstance(result, list) or not result:
            return None
        return ThreadOwner(
            user_id=result[0].get("userId"),
            identifier=result[0].get("userIdentifier"),
        )

    async def get_thread(self, thread_id: str) -> Optional[ThreadDict]:
        if self.show_logger:
            logger.info(f"SQLAlchemy: get_thread, thread_id={thread_id}")
        user_threads: Optional[List[ThreadDict]] = await self.get_all_user_threads(
            thread_id=thread_id
        )
        if user_threads:
            return user_threads[0]
        else:
            return None

    async def update_thread(
        self,
        thread_id: str,
        name: Optional[str] = None,
        user_id: Optional[str] = None,
        metadata: Optional[Dict] = None,
        tags: Optional[List[str]] = None,
    ):
        if self.show_logger:
            logger.info(f"SQLAlchemy: update_thread, thread_id={thread_id}")

        user_identifier = None
        if user_id:
            user_identifier = await self._get_user_identifer_by_id(user_id)

        has_updates = (
            metadata is not None
            or name is not None
            or user_id is not None
            or tags is not None
        )

        if metadata is None:
            metadata = {}

        existing = await self.execute_sql(
            query='SELECT "metadata", "userId" FROM threads WHERE "id" = :id',
            parameters={"id": thread_id},
        )

        existing_rows = existing if isinstance(existing, list) else []
        thread_exists = len(existing_rows) > 0
        stored_owner = existing_rows[0].get("userId") if existing_rows else None
        if thread_exists and not has_updates:
            return

        base = {}
        if existing_rows:
            raw = existing_rows[0].get("metadata") or {}
            if isinstance(raw, str):
                try:
                    base = json.loads(raw)
                except json.JSONDecodeError:
                    base = {}
            elif isinstance(raw, dict):
                base = raw
        to_delete = {k for k, v in metadata.items() if v is None}
        incoming = {k: v for k, v in metadata.items() if v is not None}
        base = {k: v for k, v in base.items() if k not in to_delete}
        metadata = {**base, **incoming}

        name_value = name
        if name_value is None and metadata:
            name_value = metadata.get("name")

        is_new_thread = not thread_exists
        created_at_value = await self.get_current_timestamp() if is_new_thread else None

        data = {
            "id": thread_id,
            "createdAt": created_at_value,
            "name": name_value,
            "userId": user_id,
            "userIdentifier": user_identifier,
            "tags": tags,
            "metadata": json.dumps(metadata),
        }
        parameters = {
            key: value for key, value in data.items() if value is not None
        }  # Remove keys with None values
        columns = ", ".join(f'"{key}"' for key in parameters.keys())
        values = ", ".join(f":{key}" for key in parameters.keys())
        updates = ", ".join(
            f'"{key}" = EXCLUDED."{key}"' for key in parameters.keys() if key != "id"
        )
        query = f"""
            INSERT INTO threads ({columns})
            VALUES ({values})
            ON CONFLICT ("id") DO UPDATE
            SET {updates};
        """
        await self.execute_sql(query=query, parameters=parameters)

        # An ownership change invalidates every grant (M3: user_id=None deletes nothing).
        if (
            user_id is not None
            and thread_exists
            and (stored_owner is None or str(stored_owner) != str(user_id))
        ):
            await self._delete_thread_grants(thread_id)

    async def delete_thread(self, thread_id: str):
        if self.show_logger:
            logger.info(f"SQLAlchemy: delete_thread, thread_id={thread_id}")

        elements_query = """SELECT * FROM elements WHERE "threadId" = :id"""
        elements = await self.execute_sql(elements_query, {"id": thread_id})

        if self.storage_provider is not None and isinstance(elements, list):
            await delete_storage_objects(
                self.storage_provider,
                (elem["objectKey"] for elem in elements if elem["objectKey"]),
            )

        # Delete feedbacks/elements/steps/thread
        feedbacks_query = """DELETE FROM feedbacks WHERE "forId" IN (SELECT "id" FROM steps WHERE "threadId" = :id)"""
        elements_query = """DELETE FROM elements WHERE "threadId" = :id"""
        steps_query = """DELETE FROM steps WHERE "threadId" = :id"""
        thread_query = """DELETE FROM threads WHERE "id" = :id"""
        parameters = {"id": thread_id}
        await self._delete_thread_grants(thread_id)
        await self.execute_sql(query=feedbacks_query, parameters=parameters)
        await self.execute_sql(query=elements_query, parameters=parameters)
        await self.execute_sql(query=steps_query, parameters=parameters)
        await self.execute_sql(query=thread_query, parameters=parameters)

    async def delete_all_threads(self, user_id: str) -> int:
        if self.show_logger:
            logger.info(f"SQLAlchemy: delete_all_threads, user_id={user_id}")

        elements_query = """
            SELECT "objectKey" FROM elements
            WHERE "threadId" IN (SELECT "id" FROM threads WHERE "userId" = :user_id)
        """
        elements = await self.execute_sql(elements_query, {"user_id": user_id})
        if elements is None:
            raise RuntimeError("SQLAlchemy: delete_all_threads failed")

        if self.storage_provider is not None and isinstance(elements, list):
            await delete_storage_objects(
                self.storage_provider,
                (elem["objectKey"] for elem in elements if elem["objectKey"]),
            )

        # Delete feedbacks/elements/steps/threads owned by the user
        feedbacks_query = """
            DELETE FROM feedbacks WHERE "forId" IN (
                SELECT "id" FROM steps WHERE "threadId" IN (
                    SELECT "id" FROM threads WHERE "userId" = :user_id
                )
            )
        """
        elements_query = """
            DELETE FROM elements WHERE "threadId" IN (
                SELECT "id" FROM threads WHERE "userId" = :user_id
            )
        """
        steps_query = """
            DELETE FROM steps WHERE "threadId" IN (
                SELECT "id" FROM threads WHERE "userId" = :user_id
            )
        """
        thread_query = """DELETE FROM threads WHERE "userId" = :user_id"""
        parameters = {"user_id": user_id}
        await self._delete_all_thread_grants(user_id)
        deleted_threads = 0
        for query in (feedbacks_query, elements_query, steps_query, thread_query):
            result = await self.execute_sql(query=query, parameters=parameters)
            if not isinstance(result, int):
                raise RuntimeError("SQLAlchemy: delete_all_threads failed")
            deleted_threads = result

        return deleted_threads

    ###### Thread grants ######
    _thread_grants_table_checked: bool = False
    _thread_grants_table_ready: bool = False

    async def _ensure_thread_grants_table(self) -> bool:
        """Create the grants table best-effort. False means grants are unavailable."""
        if self._thread_grants_table_checked:
            return self._thread_grants_table_ready

        created = await self.execute_sql(
            """
            CREATE TABLE IF NOT EXISTS thread_grants (
                "threadId"             TEXT NOT NULL,
                "granteeUserId"        TEXT NOT NULL,
                "granteeUserCreatedAt" TEXT NOT NULL,
                "granteeIdentifier"    TEXT NOT NULL,
                "grantedByUserId"      TEXT NOT NULL,
                "grantedAt"            TEXT NOT NULL,
                "role"                 TEXT NOT NULL DEFAULT 'read',
                PRIMARY KEY ("threadId", "granteeUserId")
            )
            """,
            {},
            quiet=True,
        )
        await self.execute_sql(
            """
            CREATE INDEX IF NOT EXISTS thread_grants_grantee
            ON thread_grants ("granteeUserId", "threadId")
            """,
            {},
            quiet=True,
        )

        if created is None:
            logger.warning(
                "SQLAlchemy: thread_grants table unavailable; thread grants are disabled"
            )
        self._thread_grants_table_ready = created is not None
        self._thread_grants_table_checked = True
        return self._thread_grants_table_ready

    @staticmethod
    def _grant_from_row(row: Dict[str, Any]) -> ThreadGrant:
        return ThreadGrant(
            thread_id=str(row["threadId"]),
            grantee_user_id=str(row["granteeUserId"]),
            grantee_user_created_at=str(row["granteeUserCreatedAt"]),
            grantee_identifier=str(row["granteeIdentifier"]),
            granted_by_user_id=str(row["grantedByUserId"]),
            granted_at=str(row["grantedAt"]),
            role=str(row["role"]),
        )

    async def get_thread_grant(
        self, thread_id: str, grantee_user_id: str
    ) -> Optional[ThreadGrant]:
        # Grant lookups are quiet: an error must not render the grantee id.
        if not await self._ensure_thread_grants_table():
            return None
        result = await self.execute_sql(
            """
            SELECT * FROM thread_grants
            WHERE "threadId" = :thread_id
              AND "granteeUserId" = :grantee_user_id
              AND "role" = 'read'
            """,
            {"thread_id": thread_id, "grantee_user_id": grantee_user_id},
            quiet=True,
        )
        if not isinstance(result, list) or not result:
            return None
        return self._grant_from_row(result[0])

    async def list_thread_grants(self, thread_id: str) -> List[ThreadGrant]:
        if not await self._ensure_thread_grants_table():
            return []
        result = await self.execute_sql(
            """
            SELECT * FROM thread_grants
            WHERE "threadId" = :thread_id
            ORDER BY "grantedAt" ASC
            """,
            {"thread_id": thread_id},
            quiet=True,
        )
        if not isinstance(result, list):
            return []
        return [self._grant_from_row(row) for row in result]

    async def create_thread_grant(self, grant: ThreadGrant) -> None:
        if not await self._ensure_thread_grants_table():
            raise RuntimeError("Thread grants are unavailable: table creation failed")
        try:
            await self.execute_sql(
                """
                INSERT INTO thread_grants (
                    "threadId", "granteeUserId", "granteeUserCreatedAt",
                    "granteeIdentifier", "grantedByUserId", "grantedAt", "role"
                ) VALUES (
                    :thread_id, :grantee_user_id, :grantee_user_created_at,
                    :grantee_identifier, :granted_by_user_id, :granted_at, :role
                )
                ON CONFLICT ("threadId", "granteeUserId") DO NOTHING
                """,
                asdict(grant),
                quiet=True,
                raise_on_error=True,
            )
        except Exception as e:
            # The driver error can render the grantee id; keep it out of the log.
            logger.warning(
                "SQLAlchemy: thread grant create failed (%s)", type(e).__name__
            )
            raise RuntimeError("Failed to create the thread grant") from None

    async def delete_thread_grant(self, thread_id: str, grantee_user_id: str) -> bool:
        if not await self._ensure_thread_grants_table():
            return False
        result = await self.execute_sql(
            'DELETE FROM thread_grants WHERE "threadId" = :thread_id AND "granteeUserId" = :grantee_user_id',
            {"thread_id": thread_id, "grantee_user_id": grantee_user_id},
            quiet=True,
        )
        if result is None:
            # Do not let a silent no-op look like a successful revoke.
            raise RuntimeError("Failed to revoke the thread grant")
        return isinstance(result, int) and result > 0

    async def _delete_thread_grants(self, thread_id: str) -> None:
        """Guarded: a missing grants table must not break thread deletion."""
        if not await self._ensure_thread_grants_table():
            return
        await self.execute_sql(
            'DELETE FROM thread_grants WHERE "threadId" = :thread_id',
            {"thread_id": thread_id},
            quiet=True,
        )

    async def _delete_all_thread_grants(self, user_id: str) -> None:
        """Guarded; cast because threads.id is operator-defined (UUID or TEXT)."""
        if not await self._ensure_thread_grants_table():
            return
        await self.execute_sql(
            """
            DELETE FROM thread_grants WHERE "threadId" IN (
                SELECT CAST(t."id" AS TEXT) FROM threads t WHERE t."userId" = :user_id
            )
            """,
            {"user_id": user_id},
            quiet=True,
        )

    async def list_threads(
        self, pagination: Pagination, filters: ThreadFilter
    ) -> PaginatedResponse:
        if self.show_logger:
            logger.info(
                f"SQLAlchemy: list_threads, pagination={pagination}, filters={filters}"
            )
        if not filters.userId:
            raise ValueError("userId is required")

        shared_with_user_id = filters.sharedWithUserId
        shared_with_user_created_at = filters.sharedWithUserCreatedAt
        if shared_with_user_id:
            try:
                grants_available = await self._ensure_thread_grants_table()
            except Exception:
                grants_available = False
            if not grants_available:
                # Fail closed to the owner-only list; never a partial/foreign set.
                logger.warning(
                    "SQLAlchemy: thread_grants unavailable; listing owned threads only"
                )
                shared_with_user_id = None
                shared_with_user_created_at = None
        else:
            shared_with_user_created_at = None

        all_user_threads: List[ThreadDict] = (
            await self.get_all_user_threads(
                user_id=filters.userId,
                shared_with_user_id=shared_with_user_id,
                shared_with_user_created_at=shared_with_user_created_at,
            )
            or []
        )

        search_keyword = filters.search.lower() if filters.search else None
        feedback_value = int(filters.feedback) if filters.feedback is not None else None

        filtered_threads = []
        for thread in all_user_threads:
            keyword_match = True
            feedback_match = True
            if search_keyword or feedback_value is not None:
                if search_keyword:
                    keyword_match = any(
                        search_keyword in step["output"].lower()
                        for step in thread["steps"]
                        if "output" in step
                    )
                if feedback_value is not None:
                    feedback_match = False  # Assume no match until found
                    for step in thread["steps"]:
                        feedback = step.get("feedback")
                        if feedback and feedback.get("value") == feedback_value:
                            feedback_match = True
                            break
            if keyword_match and feedback_match:
                filtered_threads.append(thread)

        start = 0
        if pagination.cursor:
            for i, thread in enumerate(filtered_threads):
                if (
                    thread["id"] == pagination.cursor
                ):  # Find the start index using pagination.cursor
                    start = i + 1
                    break
        end = start + pagination.first
        paginated_threads = filtered_threads[start:end] or []

        has_next_page = len(filtered_threads) > end
        start_cursor = paginated_threads[0]["id"] if paginated_threads else None
        end_cursor = paginated_threads[-1]["id"] if paginated_threads else None

        return PaginatedResponse(
            pageInfo=PageInfo(
                hasNextPage=has_next_page,
                startCursor=start_cursor,
                endCursor=end_cursor,
            ),
            data=paginated_threads,
        )

    ###### Steps ######
    @queue_until_user_message()
    async def create_step(self, step_dict: "StepDict"):
        await self.update_thread(step_dict["threadId"])

        if self.show_logger:
            logger.info(f"SQLAlchemy: create_step, step_id={step_dict.get('id')}")

        step_dict["showInput"] = (
            str(step_dict.get("showInput", "")).lower()
            if "showInput" in step_dict
            else None
        )
        parameters = {
            key: value
            for key, value in step_dict.items()
            if value is not None and not (isinstance(value, dict) and not value)
        }
        parameters["metadata"] = json.dumps(step_dict.get("metadata", {}))
        parameters["generation"] = json.dumps(step_dict.get("generation", {}))
        columns = ", ".join(f'"{key}"' for key in parameters.keys())
        values = ", ".join(f":{key}" for key in parameters.keys())
        updates = ", ".join(
            f'"{key}" = :{key}' for key in parameters.keys() if key != "id"
        )
        query = f"""
            INSERT INTO steps ({columns})
            VALUES ({values})
            ON CONFLICT (id) DO UPDATE
            SET {updates};
        """
        await self.execute_sql(query=query, parameters=parameters)

    @queue_until_user_message()
    async def update_step(self, step_dict: "StepDict"):
        if self.show_logger:
            logger.info(f"SQLAlchemy: update_step, step_id={step_dict.get('id')}")
        await self.create_step(step_dict)

    async def write_approval(
        self,
        *,
        thread_id: str,
        step_id: str,
        record: Dict[str, Any],
        expected_revision: Optional[int],
    ) -> bool:
        dialect = self.engine.dialect.name
        parameters: Dict[str, Any] = {
            "id": step_id,
            "thread_id": thread_id,
            "approval_id": record.get("id"),
            "record": json.dumps(record),
        }

        if dialect == "sqlite":
            no_pending = (
                "json_extract(metadata, '$.approval.id') IS NULL OR "
                "COALESCE("
                "json_extract(metadata, '$.approval.status'), 'terminal'"
                ") != 'pending' OR ("
                "json_extract(metadata, '$.approval.id') = :approval_id AND "
                "json_extract(metadata, '$.approval.status') = 'pending')"
            )
            revision_match = (
                "json_extract(metadata, '$.approval.revision') = :expected_revision"
            )
            query = f"""
                UPDATE steps
                SET metadata = json_set(
                    COALESCE(metadata, '{{}}'), '$.approval', json(:record)
                )
                WHERE id = :id
                  AND "threadId" = :thread_id
                  AND ({
                no_pending
                if expected_revision is None
                else "(json_extract(metadata, '$.approval.id') = :approval_id AND "
                + revision_match
                + ")"
            })
            """
        elif dialect == "postgresql":
            no_pending = (
                "metadata -> 'approval' IS NULL OR "
                "COALESCE("
                "metadata #>> '{{approval,status}}', 'terminal'"
                ") != 'pending' OR ("
                "metadata #>> '{{approval,id}}' = :approval_id AND "
                "metadata #>> '{{approval,status}}' = 'pending')"
            )
            revision_match = (
                "(metadata #>> '{{approval,revision}}')::int = :expected_revision"
            )
            query = f"""
                UPDATE steps
                SET metadata = jsonb_set(
                    COALESCE(metadata, '{{}}'::jsonb),
                    '{{approval}}',
                    CAST(:record AS jsonb)
                )
                WHERE id = :id
                  AND "threadId" = :thread_id
                  AND ({
                no_pending
                if expected_revision is None
                else "(metadata #>> '{{approval,id}}' = :approval_id AND "
                + revision_match
                + ")"
            })
            """
        else:
            return await super().write_approval(
                thread_id=thread_id,
                step_id=step_id,
                record=record,
                expected_revision=expected_revision,
            )

        if expected_revision is not None:
            parameters["expected_revision"] = expected_revision
        rowcount = await self.execute_sql(query=query, parameters=parameters)
        return isinstance(rowcount, int) and rowcount == 1

    @queue_until_user_message()
    async def delete_step(self, step_id: str):
        if self.show_logger:
            logger.info(f"SQLAlchemy: delete_step, step_id={step_id}")
        # Delete feedbacks/elements/steps
        feedbacks_query = """DELETE FROM feedbacks WHERE "forId" = :id"""
        elements_query = """DELETE FROM elements WHERE "forId" = :id"""
        steps_query = """DELETE FROM steps WHERE "id" = :id"""
        parameters = {"id": step_id}
        await self.execute_sql(query=feedbacks_query, parameters=parameters)
        await self.execute_sql(query=elements_query, parameters=parameters)
        await self.execute_sql(query=steps_query, parameters=parameters)

    async def step_belongs_to_thread(self, thread_id: str, step_id: str) -> bool:
        """Point check used by the feedback gate; no row or any failure denies."""
        try:
            result = await self.execute_sql(
                'SELECT 1 AS "found" FROM steps WHERE "id" = :step_id AND "threadId" = :thread_id',
                {"step_id": step_id, "thread_id": thread_id},
                quiet=True,
                raise_on_error=True,
            )
        except Exception:
            return False
        return isinstance(result, list) and bool(result)

    async def get_step(self, step_id: str) -> Optional["StepDict"]:
        if self.show_logger:
            logger.info(f"SQLAlchemy: get_step, step_id={step_id}")
        steps_feedbacks_query = """
            SELECT
                s."id" AS step_id,
                s."name" AS step_name,
                s."type" AS step_type,
                s."threadId" AS step_threadid,
                s."parentId" AS step_parentid,
                s."streaming" AS step_streaming,
                s."waitForAnswer" AS step_waitforanswer,
                s."isError" AS step_iserror,
                s."metadata" AS step_metadata,
                s."tags" AS step_tags,
                s."input" AS step_input,
                s."output" AS step_output,
                s."createdAt" AS step_createdat,
                s."start" AS step_start,
                s."end" AS step_end,
                s."generation" AS step_generation,
                s."showInput" AS step_showinput,
                s."language" AS step_language,
                f."value" AS feedback_value,
                f."comment" AS feedback_comment,
                f."id" AS feedback_id
            FROM steps s LEFT JOIN feedbacks f ON s."id" = f."forId"
            WHERE s."id" = :step_id
        """
        steps_feedbacks = await self.execute_sql(
            query=steps_feedbacks_query, parameters={"step_id": step_id}
        )

        if not isinstance(steps_feedbacks, list) or not steps_feedbacks:
            return None

        step_feedback = steps_feedbacks[0]

        feedback = None
        if step_feedback["feedback_value"] is not None:
            feedback = FeedbackDict(
                forId=step_feedback["step_id"],
                id=step_feedback.get("feedback_id"),
                value=step_feedback["feedback_value"],
                comment=step_feedback.get("feedback_comment"),
            )
        return StepDict(
            id=step_feedback["step_id"],
            name=step_feedback["step_name"],
            type=step_feedback["step_type"],
            threadId=step_feedback.get("step_threadid", ""),
            parentId=step_feedback.get("step_parentid"),
            streaming=step_feedback.get("step_streaming", False),
            waitForAnswer=step_feedback.get("step_waitforanswer"),
            isError=step_feedback.get("step_iserror"),
            metadata=(
                step_feedback["step_metadata"]
                if step_feedback.get("step_metadata") is not None
                else {}
            ),
            tags=step_feedback.get("step_tags"),
            input=(
                step_feedback.get("step_input", "")
                if step_feedback.get("step_showinput") not in [None, "false"]
                else ""
            ),
            output=step_feedback.get("step_output", ""),
            createdAt=step_feedback.get("step_createdat"),
            start=step_feedback.get("step_start"),
            end=step_feedback.get("step_end"),
            generation=step_feedback.get("step_generation"),
            showInput=step_feedback.get("step_showinput"),
            language=step_feedback.get("step_language"),
            feedback=feedback,
        )

    ###### Feedback ######
    async def get_feedback_thread_id(self, feedback_id: str) -> FeedbackThreadLookup:
        """Typed lookup: only ``not_found`` may lead to an insert (H1b)."""
        try:
            result = await self.execute_sql(
                'SELECT "threadId" FROM feedbacks WHERE "id" = :feedback_id',
                {"feedback_id": feedback_id},
                quiet=True,
                raise_on_error=True,
            )
        except Exception:
            return FeedbackThreadLookup(status="error")
        if not isinstance(result, list):
            return FeedbackThreadLookup(status="error")
        if not result:
            return FeedbackThreadLookup(status="not_found")
        thread_id = result[0].get("threadId")
        return FeedbackThreadLookup(
            status="found", thread_id=str(thread_id) if thread_id is not None else None
        )

    async def upsert_feedback(self, feedback: Feedback) -> str:
        if self.show_logger:
            logger.info(f"SQLAlchemy: upsert_feedback, feedback_id={feedback.id}")
        feedback.id = feedback.id or str(uuid.uuid4())
        feedback_dict = asdict(feedback)
        parameters = {
            key: value for key, value in feedback_dict.items() if value is not None
        }

        # Defense in depth for the route's not_found lookup: a conflicting row
        # in another thread becomes a no-op instead of an overwrite.
        columns = ", ".join(f'"{key}"' for key in parameters.keys())
        values = ", ".join(f":{key}" for key in parameters.keys())
        updates = ", ".join(
            f'"{key}" = :{key}' for key in parameters.keys() if key != "id"
        )
        conflict_guard = ""
        if feedback.threadId is not None:
            parameters["guard_thread_id"] = feedback.threadId
            conflict_guard = ' WHERE feedbacks."threadId" = :guard_thread_id'
        query = f"""
            INSERT INTO feedbacks ({columns})
            VALUES ({values})
            ON CONFLICT (id) DO UPDATE
            SET {updates}{conflict_guard};
        """
        await self.execute_sql(query=query, parameters=parameters)
        return feedback.id

    async def delete_feedback(self, feedback_id: str) -> bool:
        if self.show_logger:
            logger.info(f"SQLAlchemy: delete_feedback, feedback_id={feedback_id}")
        query = """DELETE FROM feedbacks WHERE "id" = :feedback_id"""
        parameters = {"feedback_id": feedback_id}
        await self.execute_sql(query=query, parameters=parameters)
        return True

    ###### Elements ######
    async def get_element(
        self, thread_id: str, element_id: str
    ) -> Optional["ElementDict"]:
        if self.show_logger:
            logger.info(
                f"SQLAlchemy: get_element, thread_id={thread_id}, element_id={element_id}"
            )
        query = """SELECT * FROM elements WHERE "threadId" = :thread_id AND "id" = :element_id"""
        parameters = {"thread_id": thread_id, "element_id": element_id}
        element: Union[List[Dict[str, Any]], int, None] = await self.execute_sql(
            query=query, parameters=parameters
        )
        if isinstance(element, list) and element:
            element_dict: Dict[str, Any] = element[0]
            return ElementDict(
                id=element_dict["id"],
                threadId=element_dict.get("threadId"),
                type=element_dict["type"],
                chainlitKey=element_dict.get("chainlitKey"),
                url=element_dict.get("url"),
                objectKey=element_dict.get("objectKey"),
                name=element_dict["name"],
                props=json.loads(element_dict.get("props", "{}")),
                display=element_dict["display"],
                size=element_dict.get("size"),
                language=element_dict.get("language"),
                page=element_dict.get("page"),
                autoPlay=element_dict.get("autoPlay"),
                playerConfig=element_dict.get("playerConfig"),
                forId=element_dict.get("forId"),
                mime=element_dict.get("mime"),
            )
        else:
            return None

    @queue_until_user_message()
    async def create_element(self, element: "Element"):
        if self.show_logger:
            logger.info(f"SQLAlchemy: create_element, element_id = {element.id}")

        if not self.storage_provider:
            logger.warning(
                "SQLAlchemy: create_element error. No blob_storage_client is configured!"
            )
            return
        if not element.for_id:
            return

        content: Optional[Union[bytes, str]] = None

        if element.path:
            async with aiofiles.open(element.path, "rb") as f:
                content = await f.read()
        elif element.url:
            async with aiohttp.ClientSession() as session:
                async with session.get(element.url) as response:
                    if response.status == 200:
                        content = await response.read()
                    else:
                        content = None
        elif element.content:
            content = element.content
        else:
            raise ValueError("Element url, path or content must be provided")
        if content is None:
            raise ValueError("Content is None, cannot upload file")

        user_id: str = await self._get_user_id_by_thread(element.thread_id) or "unknown"
        file_object_key = f"{user_id}/{element.id}" + (
            f"/{element.name}" if element.name else ""
        )

        if not element.mime:
            element.mime = "application/octet-stream"

        uploaded_file = await self.storage_provider.upload_file(
            object_key=file_object_key,
            data=content,
            mime=element.mime,
            overwrite=True,
            content_disposition=element.get_content_disposition(),
        )
        if not uploaded_file:
            raise ValueError(
                "SQLAlchemy Error: create_element, Failed to persist data in storage_provider"
            )

        element_dict: ElementDict = element.to_dict()

        element_dict["url"] = uploaded_file.get("url")
        element_dict["objectKey"] = uploaded_file.get("object_key")

        element_dict_cleaned = {k: v for k, v in element_dict.items() if v is not None}
        if "props" in element_dict_cleaned:
            element_dict_cleaned["props"] = json.dumps(element_dict_cleaned["props"])

        columns = ", ".join(f'"{column}"' for column in element_dict_cleaned.keys())
        placeholders = ", ".join(f":{column}" for column in element_dict_cleaned.keys())
        updates = ", ".join(
            f'"{column}" = :{column}'
            for column in element_dict_cleaned.keys()
            if column != "id"
        )
        query = f'INSERT INTO elements ({columns}) VALUES ({placeholders}) ON CONFLICT (id) DO UPDATE SET {updates} WHERE "threadId" = excluded."threadId";'
        await self.execute_sql(query=query, parameters=element_dict_cleaned)

    @queue_until_user_message()
    async def delete_element(self, element_id: str, thread_id: Optional[str] = None):
        if self.show_logger:
            logger.info(f"SQLAlchemy: delete_element, element_id={element_id}")

        query = """SELECT * FROM elements WHERE "id" = :id"""
        params = {"id": element_id}
        if thread_id:
            query += ' AND "threadId" = :thread_id'
            params["thread_id"] = thread_id
        elements = await self.execute_sql(query, params)

        if (
            self.storage_provider is not None
            and isinstance(elements, list)
            and len(elements) > 0
            and elements[0]["objectKey"]
        ):
            await self.storage_provider.delete_file(object_key=elements[0]["objectKey"])

        query = """DELETE FROM elements WHERE "id" = :id"""
        params = {"id": element_id}
        if thread_id:
            query += ' AND "threadId" = :thread_id'
            params["thread_id"] = thread_id

        await self.execute_sql(query=query, parameters=params)

    async def get_all_user_threads(
        self,
        user_id: Optional[str] = None,
        thread_id: Optional[str] = None,
        shared_with_user_id: Optional[str] = None,
        shared_with_user_created_at: Optional[str] = None,
    ) -> Optional[List[ThreadDict]]:
        """Fetch all user threads up to self.user_thread_limit, or one thread by id if thread_id is provided.

        ``shared_with_user_id``/``shared_with_user_created_at`` add the grant
        visibility predicate; omit both for the owner-only path.
        """
        if self.show_logger:
            logger.info("SQLAlchemy: get_all_user_threads")
        granted_visibility = ""
        parameters = {
            "user_id": user_id,
            "limit": self.user_thread_limit,
            "thread_id": thread_id,
        }
        if shared_with_user_id and shared_with_user_created_at:
            granted_visibility = """
               OR EXISTS (
                    SELECT 1 FROM thread_grants g
                    WHERE g."threadId" = CAST(t."id" AS TEXT)
                      AND g."granteeUserId" = :shared_with_user_id
                      AND g."granteeUserCreatedAt" = :shared_with_user_created_at
                      AND g."role" = 'read'
               )
            """
            parameters["shared_with_user_id"] = shared_with_user_id
            parameters["shared_with_user_created_at"] = shared_with_user_created_at
        user_threads_query = f"""
            SELECT
                t."id" AS thread_id,
                t."createdAt" AS thread_createdat,
                t."name" AS thread_name,
                t."userId" AS user_id,
                t."userIdentifier" AS user_identifier,
                t."tags" AS thread_tags,
                t."metadata" AS thread_metadata,
                MAX(s."createdAt") AS updatedAt
            FROM threads t
            LEFT JOIN steps s ON t."id" = s."threadId"
            WHERE t."userId" = :user_id OR t."id" = :thread_id
            {granted_visibility}
            GROUP BY
                t."id",
                t."createdAt",
                t."name",
                t."userId",
                t."userIdentifier",
                t."tags",
                t."metadata"
            ORDER BY updatedAt DESC NULLS LAST
            LIMIT :limit
        """
        user_threads = await self.execute_sql(
            query=user_threads_query,
            parameters=parameters,
        )
        if not isinstance(user_threads, list):
            return None
        if not user_threads:
            return []

        thread_ids = [thread["thread_id"] for thread in user_threads]

        steps_feedbacks_query = """
            SELECT
                s."id" AS step_id,
                s."name" AS step_name,
                s."type" AS step_type,
                s."threadId" AS step_threadid,
                s."parentId" AS step_parentid,
                s."streaming" AS step_streaming,
                s."waitForAnswer" AS step_waitforanswer,
                s."isError" AS step_iserror,
                s."metadata" AS step_metadata,
                s."tags" AS step_tags,
                s."input" AS step_input,
                s."output" AS step_output,
                s."createdAt" AS step_createdat,
                s."start" AS step_start,
                s."end" AS step_end,
                s."generation" AS step_generation,
                s."showInput" AS step_showinput,
                s."language" AS step_language,
                f."value" AS feedback_value,
                f."comment" AS feedback_comment,
                f."id" AS feedback_id
            FROM steps s LEFT JOIN feedbacks f ON s."id" = f."forId"
            WHERE s."threadId" IN :thread_ids
            ORDER BY s."createdAt" ASC
        """
        steps_feedbacks = await self.execute_sql(
            query=steps_feedbacks_query,
            parameters={"thread_ids": thread_ids},
            expanding=["thread_ids"],
        )

        elements_query = """
            SELECT
                e."id" AS element_id,
                e."threadId" as element_threadid,
                e."type" AS element_type,
                e."chainlitKey" AS element_chainlitkey,
                e."url" AS element_url,
                e."objectKey" as element_objectkey,
                e."name" AS element_name,
                e."display" AS element_display,
                e."size" AS element_size,
                e."language" AS element_language,
                e."page" AS element_page,
                e."forId" AS element_forid,
                e."mime" AS element_mime,
                e."props" AS props
            FROM elements e
            WHERE e."threadId" IN :thread_ids
        """
        elements = await self.execute_sql(
            query=elements_query,
            parameters={"thread_ids": thread_ids},
            expanding=["thread_ids"],
        )

        thread_dicts = {}
        for thread in user_threads:
            thread_id = thread["thread_id"]
            if thread_id is not None:
                thread_dicts[thread_id] = ThreadDict(
                    id=thread_id,
                    createdAt=thread["thread_createdat"],
                    name=thread["thread_name"],
                    userId=thread["user_id"],
                    userIdentifier=thread["user_identifier"],
                    tags=thread["thread_tags"],
                    metadata=thread["thread_metadata"],
                    steps=[],
                    elements=[],
                )
        # Process steps_feedbacks to populate the steps in the corresponding ThreadDict
        if isinstance(steps_feedbacks, list):
            for step_feedback in steps_feedbacks:
                thread_id = step_feedback["step_threadid"]
                if thread_id is not None:
                    feedback = None
                    if step_feedback["feedback_value"] is not None:
                        feedback = FeedbackDict(
                            forId=step_feedback["step_id"],
                            id=step_feedback.get("feedback_id"),
                            value=step_feedback["feedback_value"],
                            comment=step_feedback.get("feedback_comment"),
                        )
                    step_dict = StepDict(
                        id=step_feedback["step_id"],
                        name=step_feedback["step_name"],
                        type=step_feedback["step_type"],
                        threadId=thread_id,
                        parentId=step_feedback.get("step_parentid"),
                        streaming=step_feedback.get("step_streaming", False),
                        waitForAnswer=step_feedback.get("step_waitforanswer"),
                        isError=step_feedback.get("step_iserror"),
                        metadata=(
                            step_feedback["step_metadata"]
                            if step_feedback.get("step_metadata") is not None
                            else {}
                        ),
                        tags=step_feedback.get("step_tags"),
                        input=(
                            step_feedback.get("step_input", "")
                            if step_feedback.get("step_showinput")
                            not in [None, "false"]
                            else ""
                        ),
                        output=step_feedback.get("step_output", ""),
                        createdAt=step_feedback.get("step_createdat"),
                        start=step_feedback.get("step_start"),
                        end=step_feedback.get("step_end"),
                        generation=step_feedback.get("step_generation"),
                        showInput=step_feedback.get("step_showinput"),
                        language=step_feedback.get("step_language"),
                        feedback=feedback,
                    )
                    # Append the step to the steps list of the corresponding ThreadDict
                    thread_dicts[thread_id]["steps"].append(step_dict)

        if isinstance(elements, list):
            for element in elements:
                thread_id = element["element_threadid"]
                if thread_id is not None:
                    element_url: str | None = None
                    object_key_val = element.get("element_objectkey")
                    if (
                        self.storage_provider is not None
                        and isinstance(object_key_val, str)
                        and object_key_val.strip()
                    ):
                        try:
                            element_url = await self.storage_provider.get_read_url(
                                object_key=object_key_val,
                            )
                        except Exception as e:
                            logger.warning(
                                f"Failed to get read URL for object_key '{object_key_val}': {e}. Falling back to stored URL."
                            )
                            element_url = element.get("element_url")
                    else:
                        element_url = element.get("element_url")
                    element_dict = ElementDict(
                        id=element["element_id"],
                        threadId=thread_id,
                        type=element["element_type"],
                        chainlitKey=element.get("element_chainlitkey"),
                        url=element_url,
                        objectKey=element.get("element_objectkey"),
                        name=element["element_name"],
                        display=element["element_display"],
                        size=element.get("element_size"),
                        language=element.get("element_language"),
                        autoPlay=element.get("element_autoPlay"),
                        playerConfig=element.get("element_playerconfig"),
                        page=element.get("element_page"),
                        props=element.get("props", "{}"),
                        forId=element.get("element_forid"),
                        mime=element.get("element_mime"),
                    )
                    thread_dicts[thread_id]["elements"].append(element_dict)  # type: ignore

        return list(thread_dicts.values())

    async def get_favorite_steps(self, user_id: str) -> List[StepDict]:
        if self.show_logger:
            logger.info(f"SQLAlchemy: get_favorite_steps, user_id={user_id}")

        query = """
                SELECT
                    s."id" AS step_id,
                    s."name" AS step_name,
                    s."type" AS step_type,
                    s."threadId" AS step_threadid,
                    s."parentId" AS step_parentid,
                    s."streaming" AS step_streaming,
                    s."waitForAnswer" AS step_waitforanswer,
                    s."isError" AS step_iserror,
                    s."metadata" AS step_metadata,
                    s."tags" AS step_tags,
                    s."input" AS step_input,
                    s."output" AS step_output,
                    s."createdAt" AS step_createdat,
                    s."start" AS step_start,
                    s."end" AS step_end,
                    s."generation" AS step_generation,
                    s."showInput" AS step_showinput,
                    s."language" AS step_language
                FROM steps s
                         JOIN threads t ON s."threadId" = t.id
                WHERE t."userId" = :user_id
                  AND s."metadata" LIKE :favorite_pattern
                ORDER BY s."createdAt" DESC \
                """

        result = await self.execute_sql(
            query, {"user_id": user_id, "favorite_pattern": '%"favorite": true%'}
        )

        steps = []
        if isinstance(result, list):
            for row in result:
                metadata_raw = row["step_metadata"]
                meta_dict = {}
                if isinstance(metadata_raw, str):
                    try:
                        meta_dict = json.loads(metadata_raw)
                    except Exception:
                        pass
                elif isinstance(metadata_raw, dict):
                    meta_dict = metadata_raw

                if meta_dict.get("favorite"):
                    steps.append(
                        StepDict(
                            id=row["step_id"],
                            name=row["step_name"],
                            type=row["step_type"],
                            threadId=row["step_threadid"],
                            parentId=row["step_parentid"],
                            streaming=row.get("step_streaming", False),
                            waitForAnswer=row.get("step_waitforanswer"),
                            isError=row.get("step_iserror"),
                            metadata=meta_dict,
                            tags=row.get("step_tags"),
                            input=(
                                row.get("step_input", "")
                                if row.get("step_showinput") not in [None, "false"]
                                else ""
                            ),
                            output=row.get("step_output", ""),
                            createdAt=row.get("step_createdat"),
                            start=row.get("step_start"),
                            end=row.get("step_end"),
                            generation=row.get("step_generation"),
                            showInput=row.get("step_showinput"),
                            language=row.get("step_language"),
                            feedback=None,
                        )
                    )
        return steps

    async def close(self) -> None:
        if self.storage_provider:
            await self.storage_provider.close()
        await self.engine.dispose()

    # ---- Prompt Gallery ----

    _prompts_table_checked: bool = False
    _prompt_versioning_checked: bool = False

    async def _ensure_prompts_table(self) -> None:
        """Create the prompts table if it doesn't exist. Only runs once per instance."""
        if self._prompts_table_checked:
            return
        await self.execute_sql(
            """
            CREATE TABLE IF NOT EXISTS prompts (
                "id"          TEXT PRIMARY KEY,
                "userId"      TEXT NOT NULL,
                "title"       TEXT NOT NULL,
                "content"     TEXT NOT NULL,
                "isShared"    BOOLEAN NOT NULL DEFAULT FALSE,
                "createdAt"   TEXT NOT NULL,
                "updatedAt"   TEXT NOT NULL
            )
            """,
            {},
        )
        self._prompts_table_checked = True

    async def _get_prompts_columns(self) -> set[str]:
        """Return the current prompts column names for the active dialect."""
        if self.engine.dialect.name == "sqlite":
            columns = await self.execute_sql("PRAGMA table_info(prompts)", {})
            key = "name"
        else:
            columns = await self.execute_sql(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'prompts'
                """,
                {},
            )
            key = "column_name"
        if not isinstance(columns, list):
            raise RuntimeError("Could not inspect the prompts table for versioning")
        names: set[str] = set()
        for row in columns:
            name = row.get(key)
            if name:
                names.add(str(name))
        return names

    async def _run_transaction(
        self, statements: List[tuple[str, Dict[str, Any]]]
    ) -> List[Any]:
        """Execute statements in one transaction; roll every one back on failure."""
        async with self.async_session() as session:
            try:
                await session.begin()
                results = []
                for query, parameters in statements:
                    results.append(await session.execute(text(query), parameters))
                await session.commit()
                return results
            except Exception:
                await session.rollback()
                raise

    async def _ensure_prompt_versioning(self) -> None:
        """Add version columns and tables in place, then backfill v1 lazily."""
        if self._prompt_versioning_checked:
            return
        async with self._prompt_versioning_lock:
            if self._prompt_versioning_checked:
                return
            await self._ensure_prompt_versioning_locked()

    async def _ensure_prompt_versioning_locked(self) -> None:
        await self._ensure_prompts_table()
        existing = await self._get_prompts_columns()

        # Only prompt versioning columns here. Skill columns stay opt-in so a
        # prompt-only app keeps its old prompts schema untouched.
        for name, ddl in (
            ("contentHash", "TEXT"),
            ("version", "INTEGER NOT NULL DEFAULT 1"),
            ("sourcePromptId", "TEXT"),
            ("pinnedVersionId", "TEXT"),
            ("pinnedContentHash", "TEXT"),
            ("pinnedAt", "TEXT"),
            ("sharedAt", "TEXT"),
            ("sharedVersion", "INTEGER"),
            ("updateSeenVersionId", "TEXT"),
        ):
            if name not in existing:
                result = await self.execute_sql(
                    f'ALTER TABLE prompts ADD COLUMN "{name}" {ddl}', {}
                )
                if result is None:
                    # Another process may have added the column between our
                    # inspection and this ALTER. Accept that race only when
                    # the column is now actually present.
                    refreshed = await self._get_prompts_columns()
                    if name not in refreshed:
                        raise RuntimeError(
                            "Could not add versioning columns to prompts"
                        )
                    existing = refreshed

        result = await self.execute_sql(
            """
            CREATE TABLE IF NOT EXISTS prompt_versions (
                "id"              TEXT PRIMARY KEY,
                "promptId"        TEXT NOT NULL,
                "kind"            TEXT NOT NULL DEFAULT 'prompt',
                "version"         INTEGER NOT NULL,
                "title"           TEXT NOT NULL,
                "description"     TEXT NOT NULL DEFAULT '',
                "content"         TEXT NOT NULL,
                "contentHash"     TEXT NOT NULL,
                "createdAt"       TEXT NOT NULL,
                "createdBy"       TEXT NOT NULL,
                "sourceVersionId" TEXT
            )
            """,
            {},
        )
        if result is None:
            raise RuntimeError("Could not create the prompt_versions table")

        result = await self.execute_sql(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS prompt_versions_prompt_version
            ON prompt_versions ("promptId", "version")
            """,
            {},
        )
        if result is None:
            raise RuntimeError("Could not create the prompt version index")
        result = await self.execute_sql(
            """
            CREATE INDEX IF NOT EXISTS prompt_versions_prompt_created
            ON prompt_versions ("promptId", "createdAt")
            """,
            {},
        )
        if result is None:
            raise RuntimeError("Could not create the prompt version index")

        result = await self.execute_sql(
            """
            CREATE INDEX IF NOT EXISTS prompts_source_prompt
            ON prompts ("sourcePromptId")
            """,
            {},
        )
        if result is None:
            raise RuntimeError("Could not create the pinned prompt index")

        result = await self.execute_sql(
            """
            CREATE TABLE IF NOT EXISTS prompt_update_notices (
                "userId"          TEXT NOT NULL,
                "promptId"        TEXT NOT NULL,
                "notifiedVersion" INTEGER NOT NULL DEFAULT 0,
                "notifiedAt"      TEXT NOT NULL,
                PRIMARY KEY ("userId", "promptId")
            )
            """,
            {},
        )
        if result is None:
            raise RuntimeError("Could not create the prompt_update_notices table")

        await self._materialise_prompt_versions(kind_column="kind" in existing)
        self._prompt_versioning_checked = True

    async def _materialise_prompt_versions(self, kind_column: bool) -> None:
        """Backfill one immutable v1 row for prompts and skills written earlier."""
        query = 'SELECT * FROM prompts WHERE "version" IS NULL OR "version" <= 1'
        rows = await self.execute_sql(query, {})
        if not isinstance(rows, list):
            raise RuntimeError("Could not read prompts for the version backfill")

        for row in rows:
            existing = await self.execute_sql(
                'SELECT "id" FROM prompt_versions WHERE "promptId" = :id LIMIT 1',
                {"id": row["id"]},
            )
            if not isinstance(existing, list):
                raise RuntimeError("Could not inspect prompt versions")
            if existing:
                continue
            kind = (row.get("kind") or "prompt") if kind_column else "prompt"
            if kind not in ("prompt", "skill"):
                kind = "prompt"
            description = (row.get("description") or "") if kind == "skill" else ""
            payload = {
                "kind": kind,
                "title": row["title"],
                "content": row["content"],
                "description": description,
            }
            digest = fingerprint(payload)
            created_at = row.get("createdAt") or row.get("updatedAt")
            # Prompts treat the head hash as the version fingerprint. Skills
            # keep their imported Markdown hash when one exists, so provenance
            # survives the lazy migration.
            content_hash_statement = (
                '"contentHash" = :contentHash'
                if kind == "prompt"
                else '"contentHash" = COALESCE("contentHash", :contentHash)'
            )
            await self._run_transaction(
                [
                    (
                        """
                        INSERT INTO prompt_versions
                        ("id", "promptId", "kind", "version", "title", "description",
                         "content", "contentHash", "createdAt", "createdBy", "sourceVersionId")
                        VALUES (:id, :promptId, :kind, 1, :title, :description,
                                :content, :contentHash, :createdAt, :createdBy, NULL)
                        """,
                        {
                            "id": str(uuid.uuid4()),
                            "promptId": row["id"],
                            "kind": kind,
                            "title": row["title"],
                            "description": description,
                            "content": row["content"],
                            "contentHash": digest,
                            "createdAt": created_at,
                            "createdBy": row["userId"],
                        },
                    ),
                    (
                        f"""
                        UPDATE prompts
                        SET "version" = 1, {content_hash_statement}
                        WHERE "id" = :id
                        """,
                        {"id": row["id"], "contentHash": digest},
                    ),
                ]
            )

    @staticmethod
    def _prompt_from_row(row: Dict[str, Any]) -> PromptDict:
        return PromptDict(
            id=row["id"],
            userId=row["userId"],
            title=row["title"],
            content=row["content"],
            isShared=bool(row["isShared"]),
            createdAt=row["createdAt"],
            updatedAt=row["updatedAt"],
            kind=row.get("kind") or "prompt",
            description=row.get("description") or "",
            enabled=bool(row.get("enabled") or False),
            importedFrom=row.get("importedFrom"),
            importedAt=row.get("importedAt"),
            contentHash=row.get("contentHash"),
            version=int(row.get("version") or 1),
            sourcePromptId=row.get("sourcePromptId"),
            pinnedVersionId=row.get("pinnedVersionId"),
            pinnedContentHash=row.get("pinnedContentHash"),
            pinnedAt=row.get("pinnedAt"),
            sharedAt=row.get("sharedAt"),
            sharedVersion=row.get("sharedVersion"),
            updateSeenVersionId=row.get("updateSeenVersionId"),
        )

    async def create_prompt(self, prompt: PromptDict) -> PromptDict:
        if self.show_logger:
            logger.info(f"SQLAlchemy: create_prompt, id={prompt['id']}")
        await self._ensure_prompt_versioning()
        enforce_content_limit(prompt["content"])
        digest = fingerprint(prompt)
        version_id = str(uuid.uuid4())
        version_values = {
            "id": version_id,
            "promptId": prompt["id"],
            "kind": "prompt",
            "version": 1,
            "title": prompt["title"],
            "description": "",
            "content": prompt["content"],
            "contentHash": digest,
            "createdAt": prompt["createdAt"],
            "createdBy": prompt["userId"],
            "sourceVersionId": None,
        }
        try:
            await self._run_transaction(
                [
                    (
                        """
                        INSERT INTO prompts
                        ("id", "userId", "title", "content", "isShared", "createdAt",
                         "updatedAt", "contentHash", "version", "sourcePromptId",
                         "pinnedVersionId", "pinnedContentHash", "pinnedAt", "sharedAt",
                         "sharedVersion")
                        VALUES (:id, :userId, :title, :content, :isShared, :createdAt,
                                :updatedAt, :contentHash, 1, :sourcePromptId,
                                :pinnedVersionId, :pinnedContentHash, :pinnedAt, :sharedAt,
                                :sharedVersion)
                        """,
                        {
                            "id": prompt["id"],
                            "userId": prompt["userId"],
                            "title": prompt["title"],
                            "content": prompt["content"],
                            "isShared": prompt["isShared"],
                            "createdAt": prompt["createdAt"],
                            "updatedAt": prompt["updatedAt"],
                            "contentHash": digest,
                            "sourcePromptId": prompt.get("sourcePromptId"),
                            "pinnedVersionId": prompt.get("pinnedVersionId"),
                            "pinnedContentHash": prompt.get("pinnedContentHash"),
                            "pinnedAt": prompt.get("pinnedAt"),
                            "sharedAt": prompt.get("sharedAt"),
                            "sharedVersion": prompt.get("sharedVersion"),
                        },
                    ),
                    (
                        """
                        INSERT INTO prompt_versions
                        ("id", "promptId", "kind", "version", "title", "description",
                         "content", "contentHash", "createdAt", "createdBy", "sourceVersionId")
                        VALUES (:id, :promptId, :kind, :version, :title, :description,
                                :content, :contentHash, :createdAt, :createdBy, :sourceVersionId)
                        """,
                        version_values,
                    ),
                ]
            )
        except IntegrityError as exc:
            raise RuntimeError("Could not store the prompt") from exc

        result = dict(prompt)
        result.update(
            {
                "version": 1,
                "contentHash": digest,
                "sourcePromptId": prompt.get("sourcePromptId"),
                "pinnedVersionId": prompt.get("pinnedVersionId"),
                "pinnedContentHash": prompt.get("pinnedContentHash"),
                "pinnedAt": prompt.get("pinnedAt"),
                "sharedAt": prompt.get("sharedAt"),
                "sharedVersion": prompt.get("sharedVersion"),
            }
        )
        return cast(PromptDict, result)

    async def list_prompts(self, user_id: str) -> List[PromptDict]:
        if self.show_logger:
            logger.info(f"SQLAlchemy: list_prompts, user_id={user_id}")
        await self._ensure_prompt_versioning()
        result = await self.execute_sql(
            'SELECT * FROM prompts WHERE "userId" = :user_id ORDER BY "createdAt" DESC',
            {"user_id": user_id},
        )
        if not isinstance(result, list):
            return []
        return [
            self._prompt_from_row(row)
            for row in result
            if (row.get("kind") or "prompt") == "prompt"
        ]

    async def get_prompt(self, prompt_id: str) -> Optional[PromptDict]:
        if self.show_logger:
            logger.info(f"SQLAlchemy: get_prompt, id={prompt_id}")
        await self._ensure_prompt_versioning()
        result = await self.execute_sql(
            'SELECT * FROM prompts WHERE "id" = :id',
            {"id": prompt_id},
        )
        if not isinstance(result, list) or not result:
            return None
        row = result[0]
        if (row.get("kind") or "prompt") != "prompt":
            return None
        return self._prompt_from_row(row)

    async def update_prompt(self, prompt: PromptDict) -> PromptDict:
        """Write the head and, when the fingerprint changes, append a version.

        Both statements run in one transaction so a failed append cannot leave
        the head pointing at content that has no version row.
        """
        if self.show_logger:
            logger.info(f"SQLAlchemy: update_prompt, id={prompt['id']}")
        await self._ensure_prompt_versioning()
        enforce_content_limit(prompt["content"])
        digest = fingerprint(prompt)
        new_version: Optional[int] = None

        async with self.async_session() as session:
            try:
                await session.begin()
                stored = (
                    (
                        await session.execute(
                            text(
                                """
                            SELECT "version", "contentHash", "title", "content"
                            FROM prompts
                            WHERE "id" = :id AND "userId" = :userId
                            """
                            ),
                            {"id": prompt["id"], "userId": prompt["userId"]},
                        )
                    )
                    .mappings()
                    .first()
                )
                if stored is None:
                    await session.rollback()
                    raise RuntimeError("Could not update the prompt")

                unchanged = stored["contentHash"] == digest
                if not unchanged and stored["contentHash"] is None:
                    unchanged = (
                        stored["title"] == prompt["title"]
                        and stored["content"] == prompt["content"]
                    )

                current_version = int(stored["version"] or 1)
                if unchanged:
                    new_version = current_version
                else:
                    count_row = (
                        (
                            await session.execute(
                                text(
                                    "SELECT COUNT(*) AS count FROM prompt_versions "
                                    'WHERE "promptId" = :id'
                                ),
                                {"id": prompt["id"]},
                            )
                        )
                        .mappings()
                        .first()
                    )
                    count = int(count_row["count"]) if count_row else 0
                    if count >= MAX_PROMPT_VERSIONS:
                        await session.rollback()
                        raise PromptVersionLimitReached(
                            f"At most {MAX_PROMPT_VERSIONS} versions per prompt"
                        )
                    max_row = (
                        (
                            await session.execute(
                                text(
                                    'SELECT MAX("version") AS max_version '
                                    'FROM prompt_versions WHERE "promptId" = :id'
                                ),
                                {"id": prompt["id"]},
                            )
                        )
                        .mappings()
                        .first()
                    )
                    new_version = int(max_row["max_version"] or 0) + 1

                await session.execute(
                    text(
                        """
                        UPDATE prompts
                        SET "title" = :title, "content" = :content,
                            "isShared" = :isShared, "contentHash" = :contentHash,
                            "version" = :version, "updatedAt" = :updatedAt,
                            "sourcePromptId" = :sourcePromptId,
                            "pinnedVersionId" = :pinnedVersionId,
                            "pinnedContentHash" = :pinnedContentHash,
                            "pinnedAt" = :pinnedAt,
                            "sharedAt" = COALESCE(:sharedAt, "sharedAt"),
                            "sharedVersion" = COALESCE(:sharedVersion, "sharedVersion")
                        WHERE "id" = :id AND "userId" = :userId
                        """
                    ),
                    {
                        "id": prompt["id"],
                        "userId": prompt["userId"],
                        "title": prompt["title"],
                        "content": prompt["content"],
                        "isShared": prompt["isShared"],
                        "contentHash": digest,
                        "version": new_version,
                        "updatedAt": prompt["updatedAt"],
                        "sourcePromptId": prompt.get("sourcePromptId"),
                        "pinnedVersionId": prompt.get("pinnedVersionId"),
                        "pinnedContentHash": prompt.get("pinnedContentHash"),
                        "pinnedAt": prompt.get("pinnedAt"),
                        "sharedAt": prompt.get("sharedAt"),
                        "sharedVersion": prompt.get("sharedVersion"),
                    },
                )

                if not unchanged:
                    await session.execute(
                        text(
                            """
                            INSERT INTO prompt_versions
                            ("id", "promptId", "kind", "version", "title", "description",
                             "content", "contentHash", "createdAt", "createdBy", "sourceVersionId")
                            VALUES (:id, :promptId, :kind, :version, :title, :description,
                                    :content, :contentHash, :createdAt, :createdBy, :sourceVersionId)
                            """
                        ),
                        {
                            "id": str(uuid.uuid4()),
                            "promptId": prompt["id"],
                            "kind": prompt_kind(prompt),
                            "version": new_version,
                            "title": prompt["title"],
                            "description": prompt.get("description") or "",
                            "content": prompt["content"],
                            "contentHash": digest,
                            "createdAt": prompt["updatedAt"],
                            "createdBy": prompt["userId"],
                            "sourceVersionId": None,
                        },
                    )
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise PromptVersionConflict(
                    "Prompt version was written concurrently"
                ) from exc
            except Exception:
                await session.rollback()
                raise

        result = dict(prompt)
        result.update(
            {
                "version": new_version,
                "contentHash": digest,
                "sourcePromptId": prompt.get("sourcePromptId"),
                "pinnedVersionId": prompt.get("pinnedVersionId"),
                "pinnedContentHash": prompt.get("pinnedContentHash"),
                "pinnedAt": prompt.get("pinnedAt"),
                "sharedAt": prompt.get("sharedAt"),
                "sharedVersion": prompt.get("sharedVersion"),
            }
        )
        return cast(PromptDict, result)

    async def delete_prompt(self, prompt_id: str, user_id: str) -> bool:
        if self.show_logger:
            logger.info(f"SQLAlchemy: delete_prompt, id={prompt_id}")
        await self._ensure_prompt_versioning()
        columns = await self._get_prompts_columns()
        kind_filter = (
            ' AND ("kind" IS NULL OR "kind" = \'prompt\')' if "kind" in columns else ""
        )
        owned = (
            'EXISTS (SELECT 1 FROM prompts WHERE "id" = :id '
            f'AND "userId" = :user_id{kind_filter})'
        )
        results = await self._run_transaction(
            [
                (
                    f'DELETE FROM prompt_update_notices WHERE "promptId" = :id AND {owned}',
                    {"id": prompt_id, "user_id": user_id},
                ),
                (
                    f'DELETE FROM prompt_versions WHERE "promptId" = :id AND {owned}',
                    {"id": prompt_id, "user_id": user_id},
                ),
                (
                    (
                        'DELETE FROM prompts WHERE "id" = :id '
                        f'AND "userId" = :user_id{kind_filter}'
                    ),
                    {"id": prompt_id, "user_id": user_id},
                ),
            ]
        )
        return bool(results and results[-1].rowcount > 0)

    # ---- Prompt versions and pinned-copy notices ----

    async def list_prompt_versions(
        self, prompt_id: str
    ) -> List[PromptVersionSummaryDict]:
        if self.show_logger:
            logger.info(f"SQLAlchemy: list_prompt_versions, prompt_id={prompt_id}")
        await self._ensure_prompt_versioning()
        result = await self.execute_sql(
            """
            SELECT "id", "promptId", "kind", "version", "title", "description",
                   "contentHash", "createdAt", "createdBy", "sourceVersionId"
            FROM prompt_versions
            WHERE "promptId" = :prompt_id
            ORDER BY "version" DESC
            """,
            {"prompt_id": prompt_id},
        )
        if not isinstance(result, list):
            raise RuntimeError("Could not read prompt versions")
        return [
            cast(PromptVersionSummaryDict, {**row, "version": int(row["version"])})
            for row in result
        ]

    async def get_prompt_version(
        self, prompt_id: str, version_id: str
    ) -> Optional[PromptVersionDict]:
        if self.show_logger:
            logger.info(
                f"SQLAlchemy: get_prompt_version, prompt_id={prompt_id}, id={version_id}"
            )
        await self._ensure_prompt_versioning()
        result = await self.execute_sql(
            """
            SELECT * FROM prompt_versions
            WHERE "promptId" = :prompt_id AND "id" = :version_id
            """,
            {"prompt_id": prompt_id, "version_id": version_id},
        )
        if not isinstance(result, list) or not result:
            return None
        row = result[0]
        return cast(
            PromptVersionDict,
            {
                **row,
                "kind": row.get("kind") or "prompt",
                "description": row.get("description") or "",
                "version": int(row["version"]),
            },
        )

    async def _source_head(self, source_prompt_id: str) -> Optional[Dict[str, Any]]:
        result = await self.execute_sql(
            """
            SELECT * FROM prompt_versions
            WHERE "promptId" = :prompt_id
            ORDER BY "version" DESC
            LIMIT 1
            """,
            {"prompt_id": source_prompt_id},
        )
        if not isinstance(result, list):
            raise RuntimeError("Could not read the source prompt versions")
        return result[0] if result else None

    async def list_prompt_updates(self, user_id: str) -> List[PromptUpdateDict]:
        if self.show_logger:
            logger.info(f"SQLAlchemy: list_prompt_updates, user_id={user_id}")
        await self._ensure_prompt_versioning()
        columns = await self._get_prompts_columns()
        kind_filter = (
            ' AND ("kind" IS NULL OR "kind" = \'prompt\')' if "kind" in columns else ""
        )
        owned = await self.execute_sql(
            f"""
            SELECT * FROM prompts
            WHERE "userId" = :user_id AND "sourcePromptId" IS NOT NULL
            {kind_filter}
            """,
            {"user_id": user_id},
        )
        if not isinstance(owned, list):
            raise RuntimeError("Could not read pinned prompts")

        updates: List[PromptUpdateDict] = []
        for prompt in owned:
            source_result = await self.execute_sql(
                'SELECT * FROM prompts WHERE "id" = :id',
                {"id": prompt["sourcePromptId"]},
            )
            if not isinstance(source_result, list) or not source_result:
                continue
            source = source_result[0]
            if not source.get("isShared"):
                continue
            head = await self._source_head(source["id"])
            if head is None:
                continue

            pinned_version: Optional[int] = None
            if prompt.get("pinnedVersionId"):
                pinned = await self.execute_sql(
                    """
                    SELECT "version" FROM prompt_versions
                    WHERE "id" = :id AND "promptId" = :prompt_id
                    """,
                    {
                        "id": prompt["pinnedVersionId"],
                        "prompt_id": source["id"],
                    },
                )
                if isinstance(pinned, list) and pinned:
                    pinned_version = int(pinned[0]["version"])

            noticed = await self.execute_sql(
                """
                SELECT "notifiedVersion" FROM prompt_update_notices
                WHERE "userId" = :user_id AND "promptId" = :prompt_id
                """,
                {"user_id": user_id, "prompt_id": prompt["id"]},
            )
            if not isinstance(noticed, list):
                raise RuntimeError("Could not read prompt update notices")
            notified_version = int(noticed[0]["notifiedVersion"]) if noticed else 0
            local_version = int(prompt.get("version") or 1)
            baseline = max(local_version, pinned_version or 0, notified_version)
            latest_version = int(head["version"])
            if latest_version <= baseline:
                continue
            updates.append(
                PromptUpdateDict(
                    promptId=prompt["id"],
                    title=prompt["title"],
                    sourcePromptId=source["id"],
                    pinnedVersionId=prompt.get("pinnedVersionId"),
                    pinnedVersion=pinned_version,
                    localVersion=local_version,
                    latestVersionId=head["id"],
                    latestVersion=latest_version,
                    notifiedVersion=notified_version,
                )
            )

        updates.sort(key=lambda item: item["latestVersion"], reverse=True)
        return updates

    async def ack_prompt_update(
        self, user_id: str, prompt_id: str, version: int
    ) -> int:
        if self.show_logger:
            logger.info(
                f"SQLAlchemy: ack_prompt_update, prompt_id={prompt_id}, version={version}"
            )
        await self._ensure_prompt_versioning()
        owned = await self.execute_sql(
            """
            SELECT "sourcePromptId", "pinnedVersionId" FROM prompts
            WHERE "id" = :id AND "userId" = :user_id
            """,
            {"id": prompt_id, "user_id": user_id},
        )
        if not isinstance(owned, list):
            raise RuntimeError("Could not read the pinned prompt")
        if not owned or not owned[0].get("sourcePromptId"):
            raise PromptVersionConflict("Prompt is not pinned to a shared source")

        head = await self._source_head(owned[0]["sourcePromptId"])
        if head is None:
            raise PromptVersionConflict("Prompt source has no versions")
        clamped = min(max(int(version), 1), int(head["version"]))

        async with self.async_session() as session:
            try:
                await session.begin()
                row = (
                    (
                        await session.execute(
                            text(
                                """
                            SELECT "notifiedVersion" FROM prompt_update_notices
                            WHERE "userId" = :user_id AND "promptId" = :prompt_id
                            """
                            ),
                            {"user_id": user_id, "prompt_id": prompt_id},
                        )
                    )
                    .mappings()
                    .first()
                )
                if row is None:
                    effective = clamped
                    await session.execute(
                        text(
                            """
                            INSERT INTO prompt_update_notices
                            ("userId", "promptId", "notifiedVersion", "notifiedAt")
                            VALUES (:user_id, :prompt_id, :version, :notified_at)
                            """
                        ),
                        {
                            "user_id": user_id,
                            "prompt_id": prompt_id,
                            "version": effective,
                            "notified_at": await self.get_current_timestamp(),
                        },
                    )
                else:
                    existing = int(row["notifiedVersion"] or 0)
                    effective = max(existing, clamped)
                    if effective > existing:
                        await session.execute(
                            text(
                                """
                                UPDATE prompt_update_notices
                                SET "notifiedVersion" = :version,
                                    "notifiedAt" = :notified_at
                                WHERE "userId" = :user_id AND "promptId" = :prompt_id
                                """
                            ),
                            {
                                "user_id": user_id,
                                "prompt_id": prompt_id,
                                "version": effective,
                                "notified_at": await self.get_current_timestamp(),
                            },
                        )
                await session.commit()
                return effective
            except Exception:
                await session.rollback()
                raise

    # ---- Per-user skills (rows in the prompts table, kind='skill') ----

    _skills_columns_checked: bool = False

    async def _ensure_skills_columns(self) -> None:
        """Add skill columns to an existing prompts table, keeping old rows intact."""
        if self._skills_columns_checked:
            return
        await self._ensure_prompts_table()
        await self._ensure_prompt_versioning()

        if self.engine.dialect.name == "sqlite":
            columns = await self.execute_sql("PRAGMA table_info(prompts)", {})
            if not isinstance(columns, list):
                raise RuntimeError("Could not inspect the prompts table for skills")
            existing = {row.get("name") for row in columns}
        else:
            columns = await self.execute_sql(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'prompts'
                """,
                {},
            )
            if not isinstance(columns, list):
                raise RuntimeError("Could not inspect the prompts table for skills")
            existing = {row.get("column_name") for row in columns}

        for name, ddl in (
            ("kind", "TEXT NOT NULL DEFAULT 'prompt'"),
            ("description", "TEXT NOT NULL DEFAULT ''"),
            ("enabled", "BOOLEAN NOT NULL DEFAULT FALSE"),
            ("importedFrom", "TEXT"),
            ("importedAt", "TEXT"),
            ("contentHash", "TEXT"),
            ("updateSeenVersionId", "TEXT"),
        ):
            if name not in existing:
                result = await self.execute_sql(
                    f'ALTER TABLE prompts ADD COLUMN "{name}" {ddl}', {}
                )
                if result is None:
                    raise RuntimeError("Could not add skill columns to prompts")

        result = await self.execute_sql(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS prompts_skill_name
            ON prompts ("userId", "title") WHERE "kind" = 'skill'
            """,
            {},
        )
        if result is None:
            raise RuntimeError("Could not create the skill index")
        await self._materialise_prompt_versions(kind_column=True)
        self._skills_columns_checked = True

    @staticmethod
    def _skill_from_row(row: Dict[str, Any]) -> PromptDict:
        return PromptDict(
            id=row["id"],
            userId=row["userId"],
            title=row["title"],
            content=row["content"],
            isShared=bool(row.get("isShared", False)),
            createdAt=row["createdAt"],
            updatedAt=row["updatedAt"],
            kind="skill",
            description=row.get("description") or "",
            enabled=bool(row.get("enabled", False)),
            importedFrom=row.get("importedFrom"),
            importedAt=row.get("importedAt"),
            contentHash=row.get("contentHash"),
            version=int(row.get("version") or 1),
            sourcePromptId=row.get("sourcePromptId"),
            pinnedVersionId=row.get("pinnedVersionId"),
            pinnedContentHash=row.get("pinnedContentHash"),
            pinnedAt=row.get("pinnedAt"),
            sharedAt=row.get("sharedAt"),
            sharedVersion=row.get("sharedVersion"),
            updateSeenVersionId=row.get("updateSeenVersionId"),
        )

    async def create_skill(self, skill: PromptDict) -> PromptDict:
        if self.show_logger:
            logger.info(f"SQLAlchemy: create_skill, id={skill['id']}")
        await self._ensure_skills_columns()
        enforce_content_limit(skill["content"])
        digest = fingerprint(skill)
        version_id = str(uuid.uuid4())
        # Imported Markdown hashes are provenance, so keep a supplied hash.
        content_hash = skill.get("contentHash") or digest
        try:
            await self._run_transaction(
                [
                    (
                        """
                        INSERT INTO prompts
                        ("id", "userId", "title", "content", "isShared", "kind",
                         "description", "enabled", "importedFrom", "importedAt",
                         "contentHash", "createdAt", "updatedAt", "version",
                         "sourcePromptId", "pinnedVersionId", "pinnedContentHash",
                         "pinnedAt", "sharedAt", "sharedVersion", "updateSeenVersionId")
                        VALUES (:id, :userId, :title, :content, :isShared, 'skill',
                                :description, :enabled, :importedFrom, :importedAt,
                                :contentHash, :createdAt, :updatedAt, 1,
                                :sourcePromptId, :pinnedVersionId, :pinnedContentHash,
                                :pinnedAt, :sharedAt, :sharedVersion, :updateSeenVersionId)
                        """,
                        {
                            "id": skill["id"],
                            "userId": skill["userId"],
                            "title": skill["title"],
                            "content": skill["content"],
                            "isShared": skill.get("isShared", False),
                            "description": skill.get("description", ""),
                            "enabled": skill.get("enabled", False),
                            "importedFrom": skill.get("importedFrom"),
                            "importedAt": skill.get("importedAt"),
                            "contentHash": content_hash,
                            "createdAt": skill["createdAt"],
                            "updatedAt": skill["updatedAt"],
                            "sourcePromptId": skill.get("sourcePromptId"),
                            "pinnedVersionId": skill.get("pinnedVersionId"),
                            "pinnedContentHash": skill.get("pinnedContentHash"),
                            "pinnedAt": skill.get("pinnedAt"),
                            "sharedAt": skill.get("sharedAt"),
                            "sharedVersion": skill.get("sharedVersion"),
                            "updateSeenVersionId": skill.get("updateSeenVersionId"),
                        },
                    ),
                    (
                        """
                        INSERT INTO prompt_versions
                        ("id", "promptId", "kind", "version", "title", "description",
                         "content", "contentHash", "createdAt", "createdBy", "sourceVersionId")
                        VALUES (:id, :promptId, 'skill', 1, :title, :description,
                                :content, :contentHash, :createdAt, :createdBy, NULL)
                        """,
                        {
                            "id": version_id,
                            "promptId": skill["id"],
                            "title": skill["title"],
                            "description": skill.get("description", ""),
                            "content": skill["content"],
                            "contentHash": digest,
                            "createdAt": skill["createdAt"],
                            "createdBy": skill["userId"],
                        },
                    ),
                ]
            )
        except IntegrityError as exc:
            raise RuntimeError("Could not store the skill") from exc

        result = dict(skill)
        result.update(
            {
                "kind": "skill",
                "contentHash": content_hash,
                "version": 1,
                "sourcePromptId": skill.get("sourcePromptId"),
                "pinnedVersionId": skill.get("pinnedVersionId"),
                "pinnedContentHash": skill.get("pinnedContentHash"),
                "pinnedAt": skill.get("pinnedAt"),
                "sharedAt": skill.get("sharedAt"),
                "sharedVersion": skill.get("sharedVersion"),
                "updateSeenVersionId": skill.get("updateSeenVersionId"),
            }
        )
        return cast(PromptDict, result)

    async def list_skills(self, user_id: str) -> List[PromptDict]:
        if self.show_logger:
            logger.info(f"SQLAlchemy: list_skills, user_id={user_id}")
        await self._ensure_skills_columns()
        result = await self.execute_sql(
            'SELECT * FROM prompts WHERE "userId" = :user_id AND "kind" = \'skill\' ORDER BY "createdAt" DESC',
            {"user_id": user_id},
        )
        if not isinstance(result, list):
            raise RuntimeError("Could not read skills from the database")
        return [self._skill_from_row(row) for row in result]

    async def get_skill(self, skill_id: str, user_id: str) -> Optional[PromptDict]:
        if self.show_logger:
            logger.info(f"SQLAlchemy: get_skill, id={skill_id}")
        await self._ensure_skills_columns()
        result = await self.execute_sql(
            'SELECT * FROM prompts WHERE "id" = :id AND "userId" = :user_id AND "kind" = \'skill\'',
            {"id": skill_id, "user_id": user_id},
        )
        if not isinstance(result, list):
            raise RuntimeError("Could not read the skill from the database")
        if not result:
            return None
        return self._skill_from_row(result[0])

    async def get_shared_skill(self, skill_id: str) -> Optional[PromptDict]:
        if self.show_logger:
            logger.info(f"SQLAlchemy: get_shared_skill, id={skill_id}")
        await self._ensure_skills_columns()
        result = await self.execute_sql(
            """
            SELECT * FROM prompts
            WHERE "id" = :id AND "kind" = 'skill' AND "isShared" = TRUE
            """,
            {"id": skill_id},
        )
        if not isinstance(result, list):
            raise RuntimeError("Could not read the shared skill")
        if not result:
            return None
        return self._skill_from_row(result[0])

    async def update_skill(self, skill: PromptDict) -> PromptDict:
        """Write the skill head and append one version when its fingerprint moves.

        Content, description and title changes disable a skill: an enabled
        skill must be re-reviewed after its body changes. Sharing, pins and
        enable/disable toggles keep the version and only touch the head.
        """
        if self.show_logger:
            logger.info(f"SQLAlchemy: update_skill, id={skill['id']}")
        await self._ensure_skills_columns()
        enforce_content_limit(skill["content"])
        digest = fingerprint(skill)
        new_version: Optional[int] = None
        enabled = bool(skill.get("enabled", False))

        async with self.async_session() as session:
            try:
                await session.begin()
                stored = (
                    (
                        await session.execute(
                            text(
                                """
                                SELECT "version", "title", "description", "content",
                                       "importedFrom", "contentHash"
                                FROM prompts
                                WHERE "id" = :id AND "userId" = :userId
                                  AND "kind" = 'skill'
                                """
                            ),
                            {"id": skill["id"], "userId": skill["userId"]},
                        )
                    )
                    .mappings()
                    .first()
                )
                if stored is None:
                    await session.rollback()
                    raise RuntimeError("Could not update the skill")

                unchanged = (
                    fingerprint_from_values(
                        "skill",
                        stored["title"] or "",
                        stored["content"] or "",
                        stored["description"] or "",
                    )
                    == digest
                )
                if not unchanged:
                    enabled = False

                # Imported Markdown keep their provenance hash; owned and
                # copied skills expose the current version fingerprint.
                head_hash = (
                    skill.get("contentHash") or stored["contentHash"]
                    if stored["importedFrom"]
                    else digest
                )

                current_version = int(stored["version"] or 1)
                if unchanged:
                    new_version = current_version
                else:
                    count_row = (
                        (
                            await session.execute(
                                text(
                                    "SELECT COUNT(*) AS count FROM prompt_versions "
                                    'WHERE "promptId" = :id'
                                ),
                                {"id": skill["id"]},
                            )
                        )
                        .mappings()
                        .first()
                    )
                    count = int(count_row["count"]) if count_row else 0
                    if count >= MAX_PROMPT_VERSIONS:
                        await session.rollback()
                        raise PromptVersionLimitReached(
                            f"At most {MAX_PROMPT_VERSIONS} versions per skill"
                        )
                    max_row = (
                        (
                            await session.execute(
                                text(
                                    'SELECT MAX("version") AS max_version '
                                    'FROM prompt_versions WHERE "promptId" = :id'
                                ),
                                {"id": skill["id"]},
                            )
                        )
                        .mappings()
                        .first()
                    )
                    new_version = int(max_row["max_version"] or 0) + 1

                await session.execute(
                    text(
                        """
                        UPDATE prompts
                        SET "title" = :title, "description" = :description,
                            "content" = :content, "enabled" = :enabled,
                            "isShared" = :isShared, "updatedAt" = :updatedAt,
                            "version" = :version,
                            "contentHash" = COALESCE(:contentHash, "contentHash"),
                            "sourcePromptId" = :sourcePromptId,
                            "pinnedVersionId" = :pinnedVersionId,
                            "pinnedContentHash" = :pinnedContentHash,
                            "pinnedAt" = :pinnedAt,
                            "sharedAt" = COALESCE(:sharedAt, "sharedAt"),
                            "sharedVersion" = COALESCE(:sharedVersion, "sharedVersion"),
                            "updateSeenVersionId" = :updateSeenVersionId
                        WHERE "id" = :id AND "userId" = :userId
                          AND "kind" = 'skill'
                        """
                    ),
                    {
                        "id": skill["id"],
                        "userId": skill["userId"],
                        "title": skill["title"],
                        "description": skill.get("description", ""),
                        "content": skill["content"],
                        "enabled": enabled,
                        "isShared": skill.get("isShared", False),
                        "updatedAt": skill["updatedAt"],
                        "version": new_version,
                        "contentHash": head_hash,
                        "sourcePromptId": skill.get("sourcePromptId"),
                        "pinnedVersionId": skill.get("pinnedVersionId"),
                        "pinnedContentHash": skill.get("pinnedContentHash"),
                        "pinnedAt": skill.get("pinnedAt"),
                        "sharedAt": skill.get("sharedAt"),
                        "sharedVersion": skill.get("sharedVersion"),
                        "updateSeenVersionId": skill.get("updateSeenVersionId"),
                    },
                )

                if not unchanged:
                    await session.execute(
                        text(
                            """
                            INSERT INTO prompt_versions
                            ("id", "promptId", "kind", "version", "title", "description",
                             "content", "contentHash", "createdAt", "createdBy", "sourceVersionId")
                            VALUES (:id, :promptId, 'skill', :version, :title, :description,
                                    :content, :contentHash, :createdAt, :createdBy, NULL)
                            """
                        ),
                        {
                            "id": str(uuid.uuid4()),
                            "promptId": skill["id"],
                            "version": new_version,
                            "title": skill["title"],
                            "description": skill.get("description", ""),
                            "content": skill["content"],
                            "contentHash": digest,
                            "createdAt": skill["updatedAt"],
                            "createdBy": skill["userId"],
                        },
                    )
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise PromptVersionConflict(
                    "Skill version was written concurrently"
                ) from exc
            except Exception:
                await session.rollback()
                raise

        result = dict(skill)
        result.update(
            {
                "kind": "skill",
                "enabled": enabled,
                "version": new_version,
                "contentHash": head_hash,
                "sourcePromptId": skill.get("sourcePromptId"),
                "pinnedVersionId": skill.get("pinnedVersionId"),
                "pinnedContentHash": skill.get("pinnedContentHash"),
                "pinnedAt": skill.get("pinnedAt"),
                "sharedAt": skill.get("sharedAt"),
                "sharedVersion": skill.get("sharedVersion"),
                "updateSeenVersionId": skill.get("updateSeenVersionId"),
            }
        )
        return cast(PromptDict, result)

    async def delete_skill(self, skill_id: str, user_id: str) -> bool:
        if self.show_logger:
            logger.info(f"SQLAlchemy: delete_skill, id={skill_id}")
        await self._ensure_skills_columns()
        owned = (
            'EXISTS (SELECT 1 FROM prompts WHERE "id" = :id '
            'AND "userId" = :user_id AND "kind" = \'skill\')'
        )
        results = await self._run_transaction(
            [
                (
                    f'DELETE FROM prompt_update_notices WHERE "promptId" = :id AND {owned}',
                    {"id": skill_id, "user_id": user_id},
                ),
                (
                    f'DELETE FROM prompt_versions WHERE "promptId" = :id AND {owned}',
                    {"id": skill_id, "user_id": user_id},
                ),
                (
                    (
                        'DELETE FROM prompts WHERE "id" = :id '
                        'AND "userId" = :user_id AND "kind" = \'skill\''
                    ),
                    {"id": skill_id, "user_id": user_id},
                ),
            ]
        )
        return bool(results and results[-1].rowcount > 0)

    async def list_skill_updates(self, user_id: str) -> List[SkillUpdateDict]:
        if self.show_logger:
            logger.info(f"SQLAlchemy: list_skill_updates, user_id={user_id}")
        await self._ensure_skills_columns()
        owned = await self.execute_sql(
            """
            SELECT * FROM prompts
            WHERE "userId" = :user_id AND "kind" = 'skill'
              AND "sourcePromptId" IS NOT NULL
            """,
            {"user_id": user_id},
        )
        if not isinstance(owned, list):
            raise RuntimeError("Could not read pinned skills")

        updates: List[SkillUpdateDict] = []
        for skill in owned:
            source = await self.execute_sql(
                'SELECT * FROM prompts WHERE "id" = :id AND "kind" = \'skill\'',
                {"id": skill["sourcePromptId"]},
            )
            if not isinstance(source, list) or not source:
                continue
            if not source[0].get("isShared"):
                continue
            source_id = source[0]["id"]
            head = await self._source_head(source_id)
            if head is None:
                continue

            versions = await self.execute_sql(
                'SELECT "id", "version" FROM prompt_versions WHERE "promptId" = :id',
                {"id": source_id},
            )
            if not isinstance(versions, list):
                raise RuntimeError("Could not read the source skill versions")
            by_id = {row["id"]: int(row["version"]) for row in versions}

            pinned_version = by_id.get(skill.get("pinnedVersionId"))
            seen_version_id = skill.get("updateSeenVersionId")
            seen_version = by_id.get(seen_version_id)
            # Acknowledging dismisses the toast but keeps the quiet row marker.
            baseline = pinned_version or 0
            latest_version = int(head["version"])
            if latest_version <= baseline:
                continue
            updates.append(
                SkillUpdateDict(
                    skillId=skill["id"],
                    title=skill["title"],
                    sourcePromptId=source_id,
                    pinnedVersionId=skill.get("pinnedVersionId"),
                    pinnedVersion=pinned_version,
                    localVersion=int(skill.get("version") or 1),
                    latestVersionId=head["id"],
                    latestVersion=latest_version,
                    seenVersionId=seen_version_id,
                    seenVersion=seen_version,
                )
            )

        updates.sort(key=lambda item: item["latestVersion"], reverse=True)
        return updates

    async def ack_skill_update(self, user_id: str, skill_id: str, version: int) -> int:
        if self.show_logger:
            logger.info(
                f"SQLAlchemy: ack_skill_update, skill_id={skill_id}, version={version}"
            )
        await self._ensure_skills_columns()
        owned = await self.execute_sql(
            """
            SELECT "sourcePromptId", "updateSeenVersionId" FROM prompts
            WHERE "id" = :id AND "userId" = :user_id AND "kind" = 'skill'
            """,
            {"id": skill_id, "user_id": user_id},
        )
        if not isinstance(owned, list):
            raise RuntimeError("Could not read the pinned skill")
        if not owned or not owned[0].get("sourcePromptId"):
            raise PromptVersionConflict("Skill is not pinned to a shared source")

        source_id = owned[0]["sourcePromptId"]
        source = await self.execute_sql(
            """
            SELECT "isShared" FROM prompts
            WHERE "id" = :id AND "kind" = 'skill'
            """,
            {"id": source_id},
        )
        if not isinstance(source, list) or not source or not source[0].get("isShared"):
            raise PromptVersionConflict("The source skill is no longer available")

        head = await self._source_head(source_id)
        if head is None:
            raise PromptVersionConflict("The source skill has no versions")

        versions = await self.execute_sql(
            'SELECT "id", "version" FROM prompt_versions WHERE "promptId" = :id',
            {"id": source_id},
        )
        if not isinstance(versions, list):
            raise RuntimeError("Could not read the source skill versions")
        seen_id = owned[0].get("updateSeenVersionId")
        seen_version = next(
            (int(row["version"]) for row in versions if row["id"] == seen_id), 0
        )
        clamped = min(max(int(version), 1), int(head["version"]))
        if seen_version >= clamped:
            return seen_version
        target = next((row for row in versions if int(row["version"]) == clamped), None)
        if target is None:
            raise PromptVersionConflict("Skill version was not found")

        async with self.async_session() as session:
            try:
                await session.begin()
                await session.execute(
                    text(
                        """
                        UPDATE prompts
                        SET "updateSeenVersionId" = :version_id
                        WHERE "id" = :id AND "userId" = :user_id
                          AND "kind" = 'skill'
                          AND (
                            "updateSeenVersionId" IS NULL
                            OR COALESCE((
                              SELECT pv."version" FROM prompt_versions pv
                              WHERE pv."promptId" = :source_id
                                AND pv."id" = prompts."updateSeenVersionId"
                            ), 0) < :clamped
                          )
                        """
                    ),
                    {
                        "version_id": target["id"],
                        "id": skill_id,
                        "user_id": user_id,
                        "source_id": source_id,
                        "clamped": clamped,
                    },
                )
                await session.commit()
                return clamped
            except Exception:
                await session.rollback()
                raise

    # ---- Prompt scheduling ----

    _scheduled_tasks_table_checked: bool = False

    async def _ensure_scheduled_tasks_table(self) -> None:
        if self._scheduled_tasks_table_checked:
            return
        await self.execute_sql(
            """
            CREATE TABLE IF NOT EXISTS scheduled_tasks (
                "id" TEXT PRIMARY KEY,
                "userId" TEXT NOT NULL,
                "userIdentifier" TEXT NOT NULL,
                "prompt" TEXT NOT NULL,
                "chatProfile" TEXT,
                "model" TEXT,
                "cron" TEXT NOT NULL,
                "timezone" TEXT NOT NULL,
                "enabled" BOOLEAN NOT NULL DEFAULT TRUE,
                "afterHooks" TEXT NOT NULL DEFAULT '[]',
                "nextRunAt" TEXT NOT NULL,
                "manualRunRequestedAt" TEXT,
                "lastRunAt" TEXT,
                "lastStatus" TEXT,
                "lastError" TEXT,
                "lastThreadId" TEXT,
                "leaseId" TEXT,
                "leaseUntil" TEXT,
                "createdAt" TEXT NOT NULL,
                "updatedAt" TEXT NOT NULL
            )
            """,
            {},
        )
        if self.engine.dialect.name == "sqlite":
            columns = await self.execute_sql("PRAGMA table_info(scheduled_tasks)", {})
            has_model = isinstance(columns, list) and any(
                row.get("name") == "model" for row in columns
            )
        else:
            columns = await self.execute_sql(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'scheduled_tasks' AND column_name = :column
                """,
                {"column": "model"},
            )
            has_model = isinstance(columns, list) and bool(columns)
        if not has_model:
            await self.execute_sql(
                'ALTER TABLE scheduled_tasks ADD COLUMN "model" TEXT', {}
            )

        for column, name in (
            ("userId", "idx_scheduled_tasks_user"),
            ("nextRunAt", "idx_scheduled_tasks_next"),
            ("manualRunRequestedAt", "idx_scheduled_tasks_manual"),
            ("leaseUntil", "idx_scheduled_tasks_lease"),
        ):
            await self.execute_sql(
                f'CREATE INDEX IF NOT EXISTS "{name}" ON scheduled_tasks ("{column}")',
                {},
            )
        self._scheduled_tasks_table_checked = True

    @staticmethod
    def _scheduled_task_from_row(row: Dict[str, Any]) -> "ScheduledTaskDict":
        hooks = row.get("afterHooks", [])
        if isinstance(hooks, str):
            hooks = json.loads(hooks or "[]")
        return ScheduledTaskDict(
            id=row["id"],
            userId=row["userId"],
            userIdentifier=row["userIdentifier"],
            prompt=row["prompt"],
            chatProfile=row.get("chatProfile"),
            model=row.get("model"),
            cron=row["cron"],
            timezone=row["timezone"],
            enabled=bool(row["enabled"]),
            afterHooks=hooks,
            nextRunAt=row["nextRunAt"],
            manualRunRequestedAt=row.get("manualRunRequestedAt"),
            lastRunAt=row.get("lastRunAt"),
            lastStatus=row.get("lastStatus"),
            lastError=row.get("lastError"),
            lastThreadId=row.get("lastThreadId"),
            leaseId=row.get("leaseId"),
            leaseUntil=row.get("leaseUntil"),
            createdAt=row["createdAt"],
            updatedAt=row["updatedAt"],
        )

    async def create_scheduled_task(
        self, task: "ScheduledTaskDict"
    ) -> "ScheduledTaskDict":
        await self._ensure_scheduled_tasks_table()
        await self.execute_sql(
            """
            INSERT INTO scheduled_tasks
            ("id", "userId", "userIdentifier", "prompt", "chatProfile", "model", "cron", "timezone", "enabled", "afterHooks", "nextRunAt", "manualRunRequestedAt", "lastRunAt", "lastStatus", "lastError", "lastThreadId", "leaseId", "leaseUntil", "createdAt", "updatedAt")
            VALUES (:id, :userId, :userIdentifier, :prompt, :chatProfile, :model, :cron, :timezone, :enabled, :afterHooks, :nextRunAt, :manualRunRequestedAt, :lastRunAt, :lastStatus, :lastError, :lastThreadId, :leaseId, :leaseUntil, :createdAt, :updatedAt)
            """,
            {
                **task,
                "model": task.get("model"),
                "afterHooks": json.dumps(task["afterHooks"]),
            },
        )
        return task

    async def list_scheduled_tasks(self, user_id: str) -> List["ScheduledTaskDict"]:
        await self._ensure_scheduled_tasks_table()
        result = await self.execute_sql(
            'SELECT * FROM scheduled_tasks WHERE "userId" = :user_id ORDER BY "nextRunAt"',
            {"user_id": user_id},
        )
        return (
            [self._scheduled_task_from_row(row) for row in result]
            if isinstance(result, list)
            else []
        )

    async def get_scheduled_task(self, task_id: str) -> Optional["ScheduledTaskDict"]:
        await self._ensure_scheduled_tasks_table()
        result = await self.execute_sql(
            'SELECT * FROM scheduled_tasks WHERE "id" = :id', {"id": task_id}
        )
        return (
            self._scheduled_task_from_row(result[0])
            if isinstance(result, list) and result
            else None
        )

    async def update_scheduled_task(
        self, task: "ScheduledTaskDict"
    ) -> "ScheduledTaskDict":
        await self._ensure_scheduled_tasks_table()
        await self.execute_sql(
            """
            UPDATE scheduled_tasks SET "userId"=:userId, "userIdentifier"=:userIdentifier, "prompt"=:prompt,
            "chatProfile"=:chatProfile, "model"=:model, "cron"=:cron, "timezone"=:timezone, "enabled"=:enabled,
            "afterHooks"=:afterHooks, "nextRunAt"=:nextRunAt, "manualRunRequestedAt"=:manualRunRequestedAt,
            "lastRunAt"=:lastRunAt, "lastStatus"=:lastStatus, "lastError"=:lastError, "lastThreadId"=:lastThreadId,
            "updatedAt"=:updatedAt WHERE "id"=:id
            """,
            {
                **task,
                "model": task.get("model"),
                "afterHooks": json.dumps(task["afterHooks"]),
            },
        )
        return task

    async def delete_scheduled_task(self, task_id: str, user_id: str) -> bool:
        await self._ensure_scheduled_tasks_table()
        result = await self.execute_sql(
            'DELETE FROM scheduled_tasks WHERE "id"=:id AND "userId"=:user_id',
            {"id": task_id, "user_id": user_id},
        )
        return isinstance(result, int) and result > 0

    async def request_scheduled_task_run(
        self, task_id: str, user_id: str, requested_at: str
    ) -> bool:
        await self._ensure_scheduled_tasks_table()
        result = await self.execute_sql(
            """
            UPDATE scheduled_tasks
            SET "manualRunRequestedAt"=:requested_at, "updatedAt"=:requested_at
            WHERE "id"=:id AND "userId"=:user_id
              AND "manualRunRequestedAt" IS NULL AND "leaseId" IS NULL
            """,
            {"id": task_id, "user_id": user_id, "requested_at": requested_at},
        )
        return isinstance(result, int) and result == 1

    async def claim_due_scheduled_tasks(
        self, now: str, lease_until: str, worker_id: str, limit: int
    ) -> List["ScheduledTaskDict"]:
        await self._ensure_scheduled_tasks_table()
        result = await self.execute_sql(
            """
            SELECT * FROM scheduled_tasks
            WHERE ("manualRunRequestedAt" IS NOT NULL OR ("enabled" = TRUE AND "nextRunAt" <= :now))
              AND ("leaseUntil" IS NULL OR "leaseUntil" <= :now)
            ORDER BY COALESCE("manualRunRequestedAt", "nextRunAt") LIMIT :limit
            """,
            {"now": now, "limit": max(0, limit)},
        )
        claimed: List[ScheduledTaskDict] = []
        for row in result if isinstance(result, list) else []:
            lease_id = f"{worker_id}:{uuid.uuid4()}"
            updated = await self.execute_sql(
                """
                UPDATE scheduled_tasks SET "leaseId"=:lease_id, "leaseUntil"=:lease_until, "updatedAt"=:now
                WHERE "id"=:id
                  AND ("leaseUntil" IS NULL OR "leaseUntil" <= :now)
                  AND "enabled"=:enabled AND "nextRunAt"=:next_run_at
                  AND (
                    (:manual_run IS NOT NULL AND "manualRunRequestedAt"=:manual_run)
                    OR (:manual_run IS NULL AND "manualRunRequestedAt" IS NULL
                        AND "enabled"=TRUE AND "nextRunAt" <= :now)
                  )
                """,
                {
                    "lease_id": lease_id,
                    "lease_until": lease_until,
                    "now": now,
                    "id": row["id"],
                    "enabled": bool(row["enabled"]),
                    "next_run_at": row["nextRunAt"],
                    "manual_run": row.get("manualRunRequestedAt"),
                },
            )
            if isinstance(updated, int) and updated == 1:
                current = await self.get_scheduled_task(row["id"])
                if current:
                    claimed.append(current)
        return claimed

    async def renew_scheduled_task_lease(
        self, task_id: str, lease_id: str, lease_until: str
    ) -> bool:
        await self._ensure_scheduled_tasks_table()
        result = await self.execute_sql(
            'UPDATE scheduled_tasks SET "leaseUntil"=:until WHERE "id"=:id AND "leaseId"=:lease',
            {"until": lease_until, "id": task_id, "lease": lease_id},
        )
        return isinstance(result, int) and result == 1

    async def complete_scheduled_task_run(
        self, task_id: str, lease_id: str, completion: "ScheduledTaskCompletion"
    ) -> bool:
        await self._ensure_scheduled_tasks_table()
        values = completion.model_dump()
        result = await self.execute_sql(
            """
            UPDATE scheduled_tasks SET "lastRunAt"=:completedAt, "lastStatus"=:status,
            "lastError"=:lastError, "lastThreadId"=:lastThreadId, "nextRunAt"=COALESCE(:nextRunAt, "nextRunAt"),
            "enabled"=COALESCE(:enabled, "enabled"), "manualRunRequestedAt"=NULL,
            "leaseId"=NULL, "leaseUntil"=NULL, "updatedAt"=:completedAt
            WHERE "id"=:id AND "leaseId"=:lease
            """,
            {**values, "id": task_id, "lease": lease_id},
        )
        return isinstance(result, int) and result == 1
