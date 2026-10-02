from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

from helperme.assistant.host.supervisor import HostSupervisor
from helperme.assistant.session_metadata import SessionLineageStore


class EditAttachmentsTest(IsolatedAsyncioTestCase):
    async def test_edit_uses_explicit_refs_instead_of_restoring_original_attachments(self):
        for refs in (("retained-image",), ()):
            with self.subTest(refs=refs):
                host = object.__new__(HostSupervisor)
                host.locks = {}
                host.models = None
                host._lineage = SessionLineageStore(None, "lineage.json")
                host.store = SimpleNamespace(fork_before_message=AsyncMock(
                    return_value=SimpleNamespace(artifact_refs=("removed-file", "retained-image")),
                ))
                host.select = AsyncMock()
                host.compact = SimpleNamespace(application=AsyncMock())
                host.accept_input = AsyncMock(return_value="view")

                result = await host.fork_and_accept_input(
                    "owner", "source", "message", "edited",
                    child_session_id="child", delivery_id="delivery", artifact_refs=refs,
                )

                self.assertEqual(result, "view")
                host.accept_input.assert_awaited_once_with(
                    "child", "edited", delivery_id="delivery", source="user", artifact_refs=refs,
                )
