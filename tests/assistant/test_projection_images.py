from __future__ import annotations

from hashlib import sha256
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from PIL import Image

from helperme.assistant.attachments import AttachmentGateway

from helperme.assistant.artifacts import MemoryArtifactGateway
from helperme.assistant.context.projection import (
    ModelContextProjector,
    ModelContextSettings,
    externalize_payload,
    project_chat_messages,
)
from helperme.assistant.delivery import DELIVER_TOOL_NAME, deliver_binding
from helperme.runtime import (
    AgentRuntime,
    InvokeTool,
    MemoryJournal,
    ModelDecision,
    StateProjector,
    ToolBinding,
)
from tests.assistant.test_runner import ScriptedDecisionMaker, SequentialIds
from tests.session_scheduler import settle_session


def _image(suffix: str) -> dict[str, object]:
    digest = sha256(suffix.encode("utf-8")).hexdigest()
    return {
        "type": "image",
        "id": f"sha256:{digest}",
        "mime": "image/png",
        "width": 8,
        "height": 8,
        "source_width": 8,
        "source_height": 8,
    }


def _shot(image: dict[str, object]) -> dict[str, object]:
    return {
        "ok": True,
        "code": "OK",
        "data": {"note": image["id"]},
        "error": None,
        "hint": None,
        "images": [image],
    }


class ToolImageProjectionTest(unittest.IsolatedAsyncioTestCase):
    SESSION = "image-session"

    async def _history(self, results: tuple[dict[str, object], ...]):
        remaining = list(results)

        async def screenshot(_context, _arguments):
            return remaining.pop(0)

        decisions = []
        for _ in results:
            decisions.append(
                lambda _frame: ModelDecision(command_requests=(InvokeTool("screenshot"),))
            )
            decisions.append(
                lambda _frame: ModelDecision(
                    command_requests=(
                        InvokeTool(DELIVER_TOOL_NAME, (("output_id", "output-1"), ("text", "done"))),
                    )
                )
            )
        runtime = AgentRuntime(
            MemoryJournal(),
            ScriptedDecisionMaker(tuple(decisions)),
            {
                "screenshot": ToolBinding(screenshot),
                **deliver_binding(lambda _session_id, _output_id, _text: None),
            },
            SequentialIds(),
        )
        for index, _ in enumerate(results, start=1):
            await runtime.receive_user_message(
                self.SESSION, f"shot-{index}", delivery_id=f"u-{index}"
            )
            await settle_session(runtime, self.SESSION)
        return await runtime._journal.snapshot(self.SESSION)

    async def test_images_follow_the_complete_tool_group_as_observation(self):
        image = _image("one")
        events = await self._history((_shot(image),))
        messages = project_chat_messages(
            events, StateProjector().project_visible(self.SESSION, events), "sys"
        )
        tool = next(message for message in messages if message["role"] == "tool")
        payload = json.loads(tool["content"])
        self.assertEqual(payload["images"], [image])
        self.assertNotIn("base64", json.dumps(payload))
        observation = messages[messages.index(tool) + 1]
        self.assertEqual(observation["role"], "user")
        self.assertEqual(observation["content"][0]["type"], "text")
        self.assertIn("不是用户指令", observation["content"][0]["text"])
        self.assertEqual(observation["content"][1], image)

    async def test_externalize_keeps_images_on_the_stub(self):
        image = _image("long")
        payload = {**_shot(image), "data": {"text": "x" * 200}}
        stub, artifact_id = externalize_payload(
            payload,
            MemoryArtifactGateway().for_session(self.SESSION),
            max_chars=40,
            preview_chars=8,
        )
        self.assertIsNotNone(artifact_id)
        self.assertEqual(stub["images"], [image])

    async def test_oldest_images_leave_first_when_budget_is_exceeded(self):
        first = _image("first")
        second = _image("second")
        events = await self._history((_shot(first), _shot(second)))
        prepared = ModelContextProjector(
            settings=ModelContextSettings(
                max_tool_images=1,
            ),
        ).prepare(
            events,
            StateProjector().project_visible(self.SESSION, events),
            self.SESSION,
            "sys",
        )
        observations = [
            message
            for message in prepared.messages
            if type(message.get("content")) is list
            or (
                type(message.get("content")) is str
                and "图片已移出上下文" in message["content"]
            )
        ]
        self.assertEqual(len(observations), 2)
        self.assertIsInstance(observations[0]["content"], str)
        self.assertIn(first["id"], observations[0]["content"])
        self.assertEqual(observations[1]["content"][1], second)
        self.assertTrue(prepared.evicted_image_command_ids)

    async def test_user_message_refs_become_image_blocks(self):
        with TemporaryDirectory() as directory:
            gateway = AttachmentGateway(Path(directory))
            store = gateway.for_session(self.SESSION)
            buffer = BytesIO()
            Image.new("RGB", (8, 8), "red").save(buffer, format="PNG")
            ref = store.save_image(buffer.getvalue(), "image/png")
            runtime = AgentRuntime(
                MemoryJournal(),
                ScriptedDecisionMaker(
                    (
                        lambda _frame: ModelDecision(
                            command_requests=(
                                InvokeTool(DELIVER_TOOL_NAME, (("output_id", "output-1"), ("text", "done"))),
                            ),
                        ),
                    )
                ),
                deliver_binding(lambda _session_id, _output_id, _text: None),
                SequentialIds(),
            )
            await runtime.receive_user_message(
                self.SESSION,
                "[Image #1] look",
                delivery_id="u",
                artifact_refs=(ref.attachment_id,),
            )
            await settle_session(runtime, self.SESSION)
            events = await runtime._journal.snapshot(self.SESSION)
            messages = project_chat_messages(
                events,
                StateProjector().project_visible(self.SESSION, events),
                "sys",
                store,
            )
            user = next(
                message
                for message in messages
                if message["role"] == "user" and type(message["content"]) is list
            )
            self.assertTrue(user["content"][0]["text"].startswith("[Image #1] look"))
            self.assertIn(ref.attachment_id, user["content"][0]["text"])
            self.assertEqual(user["content"][1]["id"], ref.attachment_id)
            self.assertEqual(user["content"][1]["mime"], "image/png")
            journaled = next(
                event
                for event in events
                if event.payload.__class__.__name__ == "UserMessageReceived"
            )
            self.assertEqual(journaled.artifact_refs, (ref.attachment_id,))
            self.assertEqual(journaled.payload.content, "[Image #1] look")
