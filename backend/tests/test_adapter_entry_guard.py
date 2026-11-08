import importlib
from unittest.mock import AsyncMock, Mock, patch

import pytest

from chainlit.approval import ApprovalPending


@pytest.mark.asyncio
async def test_slack_guard_runs_before_processing(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "secret")
    module = importlib.import_module("chainlit.slack.app")

    with (
        patch.object(module, "guard_pending_approval", side_effect=ApprovalPending()),
        patch.object(module, "get_user", new_callable=AsyncMock) as get_user,
        patch.object(
            module, "download_slack_files", new_callable=AsyncMock
        ) as download,
        patch.object(
            module, "add_reaction_if_enabled", new_callable=AsyncMock
        ) as react,
    ):
        with pytest.raises(ApprovalPending):
            await module.process_slack_message(
                {"user": "u", "channel": "c", "text": "hi"},
                AsyncMock(),
                "thread-1",
            )

    get_user.assert_not_awaited()
    download.assert_not_awaited()
    react.assert_not_awaited()


@pytest.mark.asyncio
async def test_teams_guard_runs_before_processing():
    module = importlib.import_module("chainlit.teams.app")
    turn_context = Mock()
    turn_context.activity.conversation.id = "conversation-1"

    with (
        patch.object(module, "guard_pending_approval", side_effect=ApprovalPending()),
        patch.object(module, "get_user", new_callable=AsyncMock) as get_user,
        patch.object(
            module, "download_teams_files", new_callable=AsyncMock
        ) as download,
    ):
        with pytest.raises(ApprovalPending):
            await module.process_teams_message(turn_context, "thread name")

    get_user.assert_not_awaited()
    download.assert_not_awaited()


@pytest.mark.asyncio
async def test_discord_guard_runs_before_processing():
    module = importlib.import_module("chainlit.discord.app")

    with (
        patch.object(module, "guard_pending_approval", side_effect=ApprovalPending()),
        patch.object(module, "get_user", new_callable=AsyncMock) as get_user,
        patch.object(
            module, "download_discord_files", new_callable=AsyncMock
        ) as download,
    ):
        with pytest.raises(ApprovalPending):
            await module.process_discord_message(
                Mock(), "thread-1", "thread name", Mock()
            )

    get_user.assert_not_awaited()
    download.assert_not_awaited()
