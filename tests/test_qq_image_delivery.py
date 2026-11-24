import asyncio
import base64
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from plugin.astrbot_plugin_dsh_companion.qq_images import QQImageDelivery, qq_target


class QQImageDeliveryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.originals = Path(temp.name) / "generated_images"
        self.temp = Path(temp.name) / "temp"
        self.originals.mkdir()
        self.temp.mkdir()
        self.status = AsyncMock(return_value={"online": True, "good": True})
        self.send = AsyncMock(return_value={"message_id": -123})

        async def call_action(action, **kwargs):
            if action == "get_status":
                return await self.status(**kwargs)
            return await self.send(action, **kwargs)

        platform = SimpleNamespace(bot=SimpleNamespace(call_action=call_action), meta=lambda: SimpleNamespace(name="aiocqhttp"))
        self.context = SimpleNamespace(get_platform_inst=lambda _: platform)
        self.records = []
        self.manager = SimpleNamespace(
            list_generation_tasks=lambda **kwargs: [r for r in self.records if r.unified_msg_origin == kwargs["unified_msg_origin"]],
            record_qq_image_receipt=self.record_receipt,
        )
        self.delivery = QQImageDelivery(self.context, self.manager, self.originals, self.temp)

    @staticmethod
    def event(scope="qq:FriendMessage:101", sender="101", group=""):
        return SimpleNamespace(
            unified_msg_origin=scope, get_platform_name=lambda: "aiocqhttp",
            get_platform_id=lambda: "qq", get_sender_id=lambda: sender,
            get_group_id=lambda: group, get_self_id=lambda: "999",
        )

    def record(self, event, name="abc", status="succeeded", count=1):
        paths = []
        for index in range(count):
            path = self.originals / f"gen_{name}_{index}.png"
            path.write_bytes(b"mock image")
            os.link(path, self.temp / path.name)
            paths.append(str(path))
        record = SimpleNamespace(
            task_id=name, status=status, source="LLM工具", error="", created_at=datetime.now(),
            sender_id=event.get_sender_id(), unified_msg_origin=event.unified_msg_origin,
            result_paths=paths, delivered_paths=[], delivered=False, qq_image_receipts={},
        )
        self.records.insert(0, record)
        return record

    def record_receipt(self, task_id, path, message_id):
        record = next(r for r in self.records if r.task_id == task_id)
        record.qq_image_receipts[path] = message_id
        record.delivered_paths.append(path)
        record.delivered = set(record.result_paths) <= set(record.delivered_paths)

    def exists(self, path):
        return Path(path).exists(), (self.temp / Path(path).name).exists()

    async def test_callback_cleans_original_and_link_only_after_group_ack(self):
        event = self.event("qq:GroupMessage:room_202", group="202")
        record = self.record(event)
        path, = record.result_paths

        async def ack(action, **kwargs):
            self.assertEqual(self.exists(path), (True, True))
            self.assertEqual(action, "send_group_msg")
            self.assertEqual(kwargs["group_id"], 202)
            self.assertEqual(kwargs["self_id"], "999")
            self.assertEqual(kwargs["message"][0]["type"], "image")
            return {"message_id": 42}

        self.send.side_effect = ack
        await self.delivery.deliver(record, qq_target(event))
        self.assertEqual(record.qq_image_receipts[path], 42)
        self.assertEqual(self.exists(path), (False, False))
        self.assertIn("已经发到这个聊天", self.delivery.status(event, "出来了吗？"))

    async def test_missing_receipt_keeps_failed_image_and_retry_only_sends_pending_image(self):
        event = self.event()
        record = self.record(event, count=2)
        first, other = record.result_paths
        self.send.side_effect = [{"message_id": 55}, {}]
        await self.delivery.deliver(record, qq_target(event))
        self.assertEqual(self.exists(first), (False, False))
        self.assertEqual(self.exists(other), (True, True))
        self.assertFalse(record.delivered)
        self.assertIn("补发", self.delivery.status(event, "画好了没"))
        # Use a fresh service, as after a restart. The recorded partial receipt is authoritative.
        reloaded = QQImageDelivery(self.context, self.manager, self.originals, self.temp)
        self.send.reset_mock(side_effect=True)
        self.assertIsNone(await reloaded.retry(event))
        self.send.assert_awaited_once()
        self.assertEqual(self.send.call_args.args, ("send_private_msg",))
        self.assertEqual(self.send.call_args.kwargs["user_id"], 101)
        self.assertTrue(record.delivered)
        self.assertEqual(self.exists(other), (False, False))

    async def test_no_duplicate_send_when_callback_and_retry_overlap(self):
        event = self.event()
        record = self.record(event)
        entered, resume = asyncio.Event(), asyncio.Event()

        async def ack(*args, **kwargs):
            entered.set()
            await resume.wait()
            return {"message_id": 456}

        self.send.side_effect = ack
        sending = asyncio.create_task(self.delivery.deliver(record, qq_target(event)))
        await asyncio.wait_for(entered.wait(), timeout=1)
        retrying = asyncio.create_task(self.delivery.retry(event))
        await asyncio.sleep(0)
        resume.set()
        await asyncio.gather(sending, retrying)
        self.send.assert_awaited_once()

    async def test_status_and_retry_are_scoped_and_latest_failure_does_not_hide_older_image(self):
        event = self.event("qq:GroupMessage:202", group="202")
        self.record(event, "old")
        newest = self.record(event, "new", status="failed", count=0)
        self.record(self.event(event.unified_msg_origin, sender="303", group="202"), "foreign", status="running")
        self.assertIn("没弄成", self.delivery.status(event, "出来了吗"))
        self.assertIn("更早", self.delivery.status(event, "出来了吗"))
        self.assertIsNone(self.delivery.status(self.event(), "出来了吗"))
        self.assertIsNone(self.delivery.status(event, "出来了吗？再画一张"))
        self.assertEqual(
            self.delivery.status(self.event(event.unified_msg_origin, sender="303", group="202"), "出来了吗"),
            "上一张还没好呢。",
        )
        self.send.assert_not_awaited()
        self.assertIsNone(await self.delivery.retry(event))
        self.assertEqual(newest.qq_image_receipts, {})

    def test_short_followup_after_image_turn_uses_actual_states(self):
        event = self.event()
        self.record(event, "portrait")
        self.record(event, "second", status="failed", count=0)
        history = [
            {"role": "assistant", "tool_calls": [{"function": {"name": "generate_image"}}]},
            {"role": "tool", "content": "生图任务已提交"},
            {"role": "assistant", "content": [{"type": "text", "text": "我在等它出来。"}]},
        ]
        for text in ("还没有吗", "还没好吗？", "好了没呀", "有了吗？"):
            with self.subTest(text=text):
                answer = self.delivery.status(event, text, contexts=history)
                self.assertIn("没弄成", answer)
                self.assertIn("补发", answer)
                self.assertNotIn("排队", answer)
        # A genuine topic change takes precedence over an older drawing.
        history += [{"role": "user", "content": "我的快递到了吗？"},
                    {"role": "assistant", "content": "我刚帮你看了物流。"}]
        self.assertIsNone(self.delivery.status(event, "还没有吗", contexts=history))
        self.send.assert_not_awaited()

    def test_free_form_followup_can_use_scoped_state_after_original_was_delivered(self):
        event = self.event()
        old = self.record(event, "portrait")
        self.record_receipt(old.task_id, old.result_paths[0], 123)
        self.record(event, "again", status="failed", count=0)
        summary = self.delivery.chat_context(event)
        self.assertIn("没弄成", summary)
        self.assertIn("已经发", summary)
        self.assertNotIn(str(self.originals), summary)
        self.assertFalse(self.delivery.chat_context(self.event("qq:GroupMessage:202", group="202")))

    async def test_connection_failure_and_outside_paths_do_not_delete(self):
        event = self.event()
        record = self.record(event)
        path, = record.result_paths
        self.send.side_effect = TimeoutError
        await self.delivery.deliver(record, qq_target(event))
        self.assertEqual(self.exists(path), (True, True))
        self.assertFalse(record.delivered)
        outside = self.originals.parent / Path(path).name
        outside.write_bytes(b"keep")
        record.result_paths[:] = [str(outside)]
        self.send.reset_mock()
        await self.delivery.deliver(record, qq_target(event))
        self.send.assert_not_awaited()
        self.assertTrue(outside.exists())

    async def test_offline_account_keeps_original_and_retry_sends_same_bytes_after_login(self):
        event = self.event()
        record = self.record(event)
        path, = record.result_paths
        original = Path(path).read_bytes()
        self.status.return_value = {"online": False, "good": True}
        await self.delivery.deliver(record, qq_target(event))
        self.status.assert_awaited_once_with(self_id="999")
        self.send.assert_not_awaited()
        self.assertFalse(record.delivered)
        self.assertEqual(self.exists(path), (True, True))

        self.status.return_value = {"online": True, "good": True}
        async def ack(action, **kwargs):
            payload = kwargs["message"][0]["data"]["file"].removeprefix("base64://")
            self.assertEqual(base64.b64decode(payload), original)
            self.assertEqual(self.exists(path), (True, True))
            return {"message_id": 123}
        self.send.side_effect = ack
        self.assertIsNone(await self.delivery.retry(event))
        self.send.assert_awaited_once()
        self.assertTrue(record.delivered)
        self.assertEqual(self.exists(path), (False, False))

    async def test_cleanup_replays_confirmed_files_without_resend(self):
        event = self.event()
        record = self.record(event)
        path, = record.result_paths
        self.record_receipt(record.task_id, path, 987)
        await self.delivery.deliver(record, qq_target(event))
        self.assertEqual(self.exists(path), (False, False))
        self.send.assert_not_awaited()
