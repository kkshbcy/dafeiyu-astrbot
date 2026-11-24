"""Offline QQ handler check for the installed Group Chat Plus in the AstrBot 4.29.4 image."""
import asyncio
import json
import tempfile
import unittest
from itertools import count
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot.api.message_components import At, Plain, Reply
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, Group, MessageMember, MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from data.plugins.astrbot_plugin_group_chat_plus import main as gcp


class EntryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.ids = count(1)
        self.history = []
        self.judge = SimpleNamespace(text_chat=AsyncMock(return_value=SimpleNamespace(completion_text="no")))
        self.context = SimpleNamespace(get_config=lambda **kw: {}, get_registered_star=lambda name: None,
                                       get_provider_by_id=lambda name: self.judge, get_llm_tool_manager=lambda: None,
                                       persona_manager=SimpleNamespace(get_default_persona_v3=AsyncMock(
                                           return_value={"name": "小鱼", "prompt": "本群的小鱼"})))
        schema = json.loads(Path(gcp.__file__).with_name("_conf_schema.json").read_text())
        cfg = {k: v.get("default") for k, v in schema.items() if "default" in v}
        cfg.update(qq_addressed_turns=True, qq_turn_names=["小鱼"], initial_probability=0.01,
                   decision_ai_provider_id="judgment", enable_reply_density_limit=False,
                   reply_density_window_seconds=1, reply_density_max_replies=1,
                   enable_group_wait_window=False, enable_frequency_adjuster=False,
                   enable_private_chat=False, enable_proactive_chat=False, enable_idle_cache_flush=False,
                   enable_message_quality_scoring=False, enable_image_processing=False,
                   enable_attention_mechanism=False, enable_debug_log=False, max_context_messages=0,
                   concurrent_mode="legacy", trigger_keywords=[], enable_duplicate_filter=True)
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        with patch.object(gcp.StarTools, "get_data_dir", return_value=Path(scratch.name)):
            self.plugin = gcp.ChatPlus(self.context, cfg)

        # No platform, DSH or history DB connections; exercise the real group handler
        # through the construction and decoration of AstrBot's provider request.
        async def content(event, *args, **kwargs):
            text = event.get_message_str()
            cached = {"content": text, "sender_id": event.get_sender_id(), "sender_name": event.get_sender_name()}
            return True, text, text, text, [], self.history, cached, False, {}
        self.plugin._process_message_content = content
        self.enterContext(patch.object(gcp.ContextManager, "save_user_message", new_callable=AsyncMock))

    def event(self, *parts, group="g"):
        msg = AstrBotMessage()
        msg.type = MessageType.GROUP_MESSAGE
        msg.message_id = str(next(self.ids))
        msg.self_id = "11"
        msg.group = Group(group_id=group)
        msg.sender = MessageMember(user_id="22", nickname="甲")
        msg.message = list(parts)
        msg.message_str = "".join(p.text for p in parts if isinstance(p, Plain))
        msg.raw_message = {}
        event = AstrMessageEvent(msg.message_str, msg, PlatformMetadata("aiocqhttp", "test", "qq"), group)
        event.is_at_or_wake_command = any(isinstance(p, At) and str(p.qq) == "11" for p in parts)
        return event

    async def consume(self, event, sent=True):
        results = []
        async for result in self.plugin.on_group_message(event):
            self.assertIsInstance(result, gcp.ProviderRequest)
            await self.plugin.on_llm_request(event, result)
            results.append(result)
            event._has_send_oper = sent  # the real OneBot adapter sets this after await send succeeds
        return results

    async def test_actual_entry_recognises_direct_followups_and_releases_failed_send(self):
        self.plugin._check_probability_before_processing = AsyncMock(return_value=False)
        self.plugin._check_ai_decision = AsyncMock(return_value=False)
        event = self.event(Plain("那后来呢？"))
        self.judge.text_chat.return_value.completion_text = "yes"
        results = await self.consume(event)
        self.assertEqual(len(results), 1)
        self.assertEqual(event.get_extra("dfy_qq_turn"), "addressed")
        self.assertIn("正事认真回答", results[0].system_prompt)
        self.plugin._check_probability_before_processing.assert_not_awaited()
        self.plugin._check_ai_decision.assert_not_awaited()
        self.judge.text_chat.reset_mock()
        for kind, parts in [
            ("mention", [At(qq="11"), Plain("在吗")]),
            ("quote", [Reply(id="x", sender_id=11), Plain("为什么")]),
        ]:
            event = self.event(*parts)
            self.assertEqual(len(await self.consume(event)), 1)
            self.assertEqual(event.get_extra("dfy_qq_turn"), kind)
        self.judge.text_chat.assert_not_awaited()
        self.judge.text_chat.return_value.completion_text = "no"
        self.plugin.qq_turn_window.draw = lambda: 0.39
        for _ in range(7):
            self.assertEqual(len(await self.consume(self.event(Plain("群里聊两句")))), 1)
        self.assertEqual(await self.consume(self.event(Plain("群里另一个话题"))), [])
        self.assertEqual(len(await self.consume(self.event(At(qq="11"), Plain("来看一下")))), 1)
        other = self.event(Plain("掉线这句"), group="other")
        self.assertEqual(len(await self.consume(other, sent=False)), 1)
        self.assertNotIn(("qq", "other"), self.plugin.qq_turn_window.pending)
        self.assertNotIn(("qq", "other"), self.plugin.qq_turn_window.sent)

    async def test_qq_self_mention_while_sender_has_open_window(self):
        self.plugin.enable_group_wait_window = True
        self.plugin._maybe_intercept_for_wait_window = AsyncMock(return_value=True)
        event = self.event(At(qq="11"), Plain("回我一下"))
        self.assertEqual(len(await self.consume(event)), 1)
        self.plugin._maybe_intercept_for_wait_window.assert_not_awaited()

    async def test_first_delivered_bubble_starts_window_and_second_counts_once(self):
        event = self.event(At(qq="11"), Plain("听着呢"))
        handler = self.plugin.on_group_message(event)
        await anext(handler)
        try:
            key = ("qq", "g")
            self.assertIn(key, self.plugin.qq_turn_window.pending)
            event._has_send_oper = True
            await self.plugin.after_message_sent(event)
            await self.plugin.after_message_sent(event)
            self.assertNotIn(key, self.plugin.qq_turn_window.pending)
            self.assertEqual(len(self.plugin.qq_turn_window.sent[key]), 1)
        finally:
            await handler.aclose()

    async def test_same_speaker_addition_can_make_the_whole_turn_addressed(self):
        self.plugin.enable_group_wait_window = True
        self.plugin.group_wait_window_timeout_ms = 500
        self.plugin._group_wait_window_max_extra = 1
        self.plugin.pending_cache_max_count = 10
        self.plugin.qq_turn_window.draw = lambda: 0.99
        async def from_burst(**kwargs):
            body = json.loads(kwargs["prompt"])
            return SimpleNamespace(completion_text="yes" if "你怎么想的" in body["followups"] else "no")
        self.judge.text_chat.side_effect = from_burst
        first = asyncio.create_task(self.consume(self.event(Plain("我看完了"))))
        async with asyncio.timeout(2):
            while not self.plugin._group_wait_windows:
                await asyncio.sleep(0.01)
        self.assertEqual(await self.consume(self.event(Plain("你怎么想的"))), [])
        self.assertEqual(len(await first), 1)
        self.judge.text_chat.assert_awaited_once()

    async def test_mention_takes_over_earlier_waiting_turn_without_double_reply(self):
        self.plugin.enable_group_wait_window = True
        self.plugin.group_wait_window_timeout_ms = 500
        self.plugin._group_wait_window_max_extra = 2
        self.plugin.pending_cache_max_count = 10
        first = asyncio.create_task(self.consume(self.event(Plain("我看完了"))))
        async with asyncio.timeout(2):
            while not self.plugin._group_wait_windows:
                await asyncio.sleep(0.01)
        results = await self.consume(self.event(At(qq="11"), Plain("你呢")))
        self.assertEqual(await first, [])
        self.assertEqual(len(results), 1)
        self.assertIn("我看完了", results[0].prompt)
        self.assertIn("你呢", results[0].prompt)
        self.assertEqual(len(self.plugin.qq_turn_window.sent[("qq", "g")]), 1)
        self.judge.text_chat.assert_not_awaited()

    async def test_own_at_among_multiple_mentions_and_clear_name_have_priority(self):
        self.plugin.enable_ignore_at_all = True
        self.plugin.enable_ignore_at_others = True
        self.plugin.qq_turn_window.draw = lambda: 0.99
        self.judge.text_chat.return_value.completion_text = "no"
        for parts in [(At(qq="all"), At(qq="11"), Plain("看看")), (Plain("小鱼，在吗"),)]:
            self.assertEqual(len(await self.consume(self.event(*parts))), 1)
        self.judge.text_chat.assert_not_awaited()

    async def test_actual_public_sleep_phase_and_room_cue_reach_dsh_request(self):
        core = SimpleNamespace(activated=True, star_cls=SimpleNamespace(
            cfg=lambda: {"enabled": True, "wake_hour": 8, "sleep_hour": 23}))
        self.context.get_registered_star = lambda name: core if name == "astrbot_plugin_affective_core" else None
        with patch.object(gcp.qq_group_turns, "time", SimpleNamespace(time=lambda: 57600)):
            # 1970-01-02 00:00 Shanghai
            result, = await self.consume(self.event(At(qq="11"), Plain("我今天好难过")))
        self.assertIn("有些困", result.system_prompt)
        self.assertIn("分清是谁遇到了什么", result.system_prompt)

    async def test_other_quote_and_at_all_are_classified_and_private_ignored(self):
        self.plugin.qq_turn_window.draw = lambda: 0.95
        for part in [Reply(id="x", sender_id=33), At(qq="all")]:
            event = self.event(part, Plain("这个呢"))
            event.is_at_or_wake_command = True
            self.assertEqual(await self.consume(event), [])
            self.assertEqual(event.get_extra("dfy_qq_turn"), "miss")
            self.assertTrue(event.call_llm)  # prevent AstrBot's separate default wake path
            self.assertTrue(json.loads(self.judge.text_chat.await_args.kwargs["prompt"])["mentions_others"])
        event = self.event(Plain("私聊"))
        event.is_private_chat = lambda: True
        self.assertEqual(await self.consume(event), [])
        self.assertIsNone(event.get_extra("dfy_qq_turn"))


if __name__ == "__main__":
    unittest.main()
