import asyncio
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

PATCH = Path(__file__).resolve().parents[1] / "deploy" / "patches" / "group_chat_plus"
sys.path.insert(0, str(PATCH))
try:
    import qq_group_turns as turns
finally:
    sys.path.pop(0)


class GroupTurnsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = Mock(return_value=100.0)
        self.draw = Mock(return_value=0.399)
        self.window = turns.ReplyWindow(clock=self.clock, draw=self.draw)
        self.slots = asyncio.Semaphore(2)
        self.provider = SimpleNamespace(text_chat=AsyncMock(
            return_value=SimpleNamespace(completion_text="no")))
        self.room = "group/a"
        self.event = SimpleNamespace(get_platform_id=lambda: "qq", get_group_id=lambda: self.room,
                                     get_self_id=lambda: "fish", get_sender_id=lambda: "a",
                                     get_sender_name=lambda: "甲", unified_msg_origin="group/a")
        self.history = [
            SimpleNamespace(sender=SimpleNamespace(user_id="fish", nickname="小鱼"), message_str="然后呢？"),
            SimpleNamespace(sender=SimpleNamespace(user_id="b", nickname="乙"), message_str="我们聊别的"),
        ]

    async def decide(self, *, direct="", text="收到", names=("小鱼",), followups=(), other=False):
        return await turns.decide(self.window, self.event, self.history, text, followups,
                                  direct=direct, names=names, other=other, provider=self.provider,
                                  slots=self.slots, timeout=10, probability=0.4, seconds=300, limit=10)

    async def test_direct_and_followup_even_after_ten_and_no_random_draw(self):
        for _ in range(10):
            reason, claim = await self.decide(direct="mention")
            self.assertEqual(reason, "mention")
            self.window.finish(claim, sent=True)
        self.provider.text_chat.assert_not_awaited()
        self.assertEqual((await self.decide())[0], "full")
        self.provider.text_chat.return_value.completion_text = "yes"
        for direct, expected in [("", "addressed"), ("quote", "quote")]:
            reason, claim = await self.decide(direct=direct)
            self.assertEqual(reason, expected)
            self.window.finish(claim, sent=True)
        self.draw.assert_not_called()

    async def test_exact_draw_boundary_and_no_reroll(self):
        self.draw.side_effect = [0.4, 0.399999, 0.9]
        self.assertEqual(await self.decide(), ("miss", None))
        reason, claim = await self.decide()
        self.assertEqual(reason, "ambient")
        self.window.finish(claim, sent=True)
        self.assertEqual(await self.decide(), ("miss", None))
        self.assertEqual(self.draw.call_count, 3)

    async def test_concurrent_last_slot_failure_release_rolling_expiry_and_groups(self):
        for _ in range(9):
            _, claim = await self.decide(direct="mention")
            self.window.finish(claim, sent=True)
            self.window.finish(claim, sent=True)
        results = await asyncio.gather(*(self.decide() for _ in range(5)))
        self.assertCountEqual([r for r, _ in results], ["ambient"] + ["full"] * 4)
        self.window.finish(next(c for _, c in results if c), sent=False)
        reason, claim = await self.decide()
        self.assertEqual(reason, "ambient")
        self.window.finish(claim, sent=True)
        self.event.unified_msg_origin = "group/a/other-member"
        self.assertEqual((await self.decide())[0], "full")
        self.room = "group/b"
        self.assertEqual((await self.decide())[0], "ambient")
        self.room = "group/a"
        self.clock.return_value = 399.99
        self.assertEqual((await self.decide())[0], "full")
        self.clock.return_value = 400.0
        self.assertEqual((await self.decide())[0], "ambient")

    async def test_address_classifier_distinguishes_speakers_and_current_followups(self):
        await self.decide(text="昨天说的呢", followups=["你读到哪章啦"], other=True)
        call = self.provider.text_chat.await_args.kwargs
        body = json.loads(call["prompt"])
        self.assertEqual(body["history"][0]["speaker"], "她")
        self.assertNotEqual(body["history"][1]["speaker"], "她")
        self.assertEqual(body["current"], "昨天说的呢")
        self.assertEqual(body["followups"], ["你读到哪章啦"])
        self.assertTrue(body["mentions_others"])
        self.assertIsNone(call["func_tool"])
        self.assertEqual(call["contexts"], [])

    async def test_judge_failure_preserves_clear_vocative_only(self):
        self.provider.text_chat.side_effect = TimeoutError()
        self.draw.return_value = 0.9
        self.assertEqual((await self.decide(text="小鱼，在吗"))[0], "addressed")
        self.assertEqual((await self.decide(text="我听说小鱼在这"))[0], "miss")
        self.assertEqual((await self.decide(text="小鱼，在吗", other=True))[0], "miss")
        self.provider.text_chat.side_effect = None
        self.provider.text_chat.return_value.completion_text = "yes or no"
        self.assertEqual((await self.decide(text="这个怎么回事"))[0], "miss")

    def test_expression_changes_with_energy_route_and_room(self):
        neutral = turns.expression_hint("addressed", "daytime", "平静")
        sleepy = turns.expression_hint("addressed", "sleeping", "开心")
        sad = turns.expression_hint("ambient", "daytime", "难过")
        self.assertNotEqual(neutral, sleepy)
        self.assertIn("困", sleepy)
        self.assertIn("群友", sad)
        self.assertNotIn("困", neutral)
        self.assertNotEqual(neutral, turns.expression_hint("ambient", "daytime", "平静"))

    def test_reload_keeps_the_rolling_window(self):
        context = SimpleNamespace()
        first = turns.ReplyWindow.for_context(context)
        _, claim = first.reserve("g", "mention", 0.4, 300, 1)
        reloaded = turns.ReplyWindow.for_context(context)
        self.assertEqual(reloaded.reserve("g", "", 0.4, 300, 1), ("full", None))

        first.finish(claim, sent=True)
        self.assertEqual(reloaded.reserve("g", "", 0.4, 300, 1), ("full", None))

    def test_qq_defaults_independent_of_legacy_settings_and_invalid_values(self):
        self.assertEqual(turns.qq_limits({"initial_probability": 0.3, "enable_reply_density_limit": False}),
                         (0.4, 300, 10))
        self.assertEqual(turns.qq_limits({"qq_ambient_probability": "nan", "qq_reply_limit": None,
                                         "qq_reply_window_seconds": "invalid"}), (0.4, 300, 10))

    async def test_mentions_do_not_queue_behind_semantic_checks(self):
        async with self.slots, self.slots:
            queued = asyncio.create_task(self.decide())
            await asyncio.sleep(0)
            self.assertEqual((await self.decide(direct="mention"))[0], "mention")
            self.provider.text_chat.assert_not_awaited()
        self.assertEqual((await queued)[0], "ambient")
