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
            return_value=SimpleNamespace(completion_text='{"turn":"reaction","quote":"跑完五公里"}')))
        self.room = "group/a"
        self.event = SimpleNamespace(get_platform_id=lambda: "qq", get_group_id=lambda: self.room,
                                     get_self_id=lambda: "fish", get_sender_id=lambda: "a",
                                     get_sender_name=lambda: "甲", unified_msg_origin="group/a",
                                     set_extra=Mock())
        self.history = [
            SimpleNamespace(sender=SimpleNamespace(user_id="fish", nickname="小鱼"), message_str="然后呢？"),
            SimpleNamespace(sender=SimpleNamespace(user_id="b", nickname="乙"), message_str="我们聊别的"),
        ]

    async def decide(self, *, direct="", text="我今天跑完五公里啦", names=("小鱼",), followups=(), other=False):
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
        self.provider.text_chat.return_value.completion_text = '{"turn":"addressed"}'
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

    async def test_unknown_or_unsupported_topic_stays_silent_even_on_a_winning_draw(self):
        self.draw.return_value = 0
        for verdict in [
            '{"turn":"silent"}',
            '{"turn":"history","quote":"周五下午三点集合"}',
            '{"turn":"history","quote":"昨天说的呢"}',
            '{"turn":"reaction","quote":"不存在的分享"}',
            '{"turn":"common"}', 'no', 'yes', '{}', '[]',
        ]:
            with self.subTest(verdict=verdict):
                self.provider.text_chat.return_value.completion_text = verdict
                self.assertEqual(await self.decide(text="昨天说的呢"), ("silent", None))
        self.assertFalse(self.window.pending)
        self.assertFalse(self.window.sent)
        self.draw.assert_not_called()

    async def test_pending_group_plan_needs_history_even_if_judge_suggests_common_knowledge(self):
        for kind in ("common", "reaction"):
            for question, topic in [("咱们群什么时候抽奖？", "抽奖"), ("团建日期敲定没？", "团建")]:
                self.provider.text_chat.return_value.completion_text = json.dumps({"turn":kind,"quote":topic})
                self.assertEqual(await self.decide(text=question), ("silent", None))
        self.draw.assert_not_called()

    async def test_history_quote_requires_a_statement_including_its_surrounding_words(self):
        self.provider.text_chat.return_value.completion_text = '{"turn":"history","quote":"周三"}'
        for message in ["有人知道这周三抽不抽奖吗？", "听说是周三，没确认", "周三？还在等通知。"]:
            self.history[1].message_str = message
            self.assertEqual(await self.decide(text="抽奖时间定了吗？"), ("silent", None))
        self.draw.assert_not_called()
        self.history[1].message_str = "奖金还没定？时间已经定了，周三抽奖。"
        self.assertEqual((await self.decide(text="抽奖时间定了吗？"))[0], "ambient")

    async def test_public_fact_stable_knowledge_and_specific_reaction_can_qualify(self):
        self.history[1].message_str = "我确认过，周六三点在南门集合。"
        for kind, quote, question in [
            ("history", "周六三点在南门集合", "集合地点定了吗？"),
            ("common", "公历闰年", "公历闰年怎么算？"),
            ("reaction", "终于跑完五公里", "我今天终于跑完五公里了！"),
        ]:
            self.provider.text_chat.return_value.completion_text = json.dumps({"turn":kind,"quote":quote})
            reason, claim = await self.decide(text=question)
            self.assertEqual(reason, "ambient")
            self.assertIsNotNone(claim)
            self.window.finish(claim, sent=False)
        self.assertEqual(self.provider.text_chat.await_count, 3)
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
        self.draw.return_value = 0
        self.assertEqual((await self.decide(text="小鱼，在吗"))[0], "addressed")
        self.assertEqual((await self.decide(text="我听说小鱼在这"))[0], "silent")
        self.assertEqual((await self.decide(text="小鱼，在吗", other=True))[0], "silent")
        self.provider.text_chat.side_effect = None
        self.provider.text_chat.return_value.completion_text = "yes or no"
        self.assertEqual((await self.decide(text="这个怎么回事"))[0], "silent")
        self.provider = None
        self.assertEqual((await self.decide())[0], "silent")
        self.draw.assert_not_called()

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

    async def test_cancelled_wait_is_released_and_rooms_do_not_merge(self):
        bursts = turns.SpeakerBursts()
        options = {"seconds": 1, "max_extra": 3, "max_users": 5}
        first = asyncio.create_task(bursts.collect(("qq", "a", "user"), "原话", direct=False, **options))
        await asyncio.sleep(0)
        self.assertEqual(await bursts.collect(("qq", "b", "user"), "别群", direct=True, **options), ["别群"])
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertEqual(bursts.waiting, {})
        self.assertEqual(await bursts.collect(("qq", "a", "user"), "另起", direct=True, **options), ["另起"])

class OwnerHelpTest(unittest.IsolatedAsyncioTestCase):
    setUp = GroupTurnsTest.setUp

    async def test_owner_candidate_uses_only_the_public_turn_and_never_rolls(self):
        from weakref import WeakValueDictionary
        self.event.get_platform_id = lambda: 'qq'
        star = SimpleNamespace(activated=True, star_cls=SimpleNamespace(config={
            'owner_private_scopes':['qq:FriendMessage:555','wx:FriendMessage:777']}))
        context = SimpleNamespace(get_registered_star=lambda _:star)
        pending = WeakValueDictionary()
        ticket = turns.owner_mention(context, self.event, {'555'}, pending)
        self.assertIsNotNone(ticket)
        self.assertIsNone(turns.owner_mention(context, self.event, {'777'}, pending))
        self.assertIsNone(turns.owner_mention(context, self.event, {'all'}, pending))
        extras = {}
        self.event.set_extra = extras.__setitem__
        self.provider.text_chat.return_value.completion_text = '{"reply":"群公告第二条有入口。"}'
        reason, claim = await turns.owner_reply(self.window,self.event,self.history,['报名入口在哪？'],ticket,
            provider=self.provider, slots=self.slots, timeout=3, seconds=300, limit=10)
        self.assertEqual(reason,'owner_help')
        self.assertEqual(extras['dfy_qq_owner_answer'],'群公告第二条有入口。')
        self.draw.assert_not_called()
        kw = self.provider.text_chat.await_args.kwargs
        self.assertIsNone(kw['func_tool'])
        self.assertEqual(kw['contexts'],[])
        self.assertIn('报名入口在哪',kw['prompt'])
        self.window.finish(claim,sent=True)
        self.event.get_sender_id=lambda:'555'
        turns.yield_to_sender(self.event, pending)
        self.assertIsNone(turns.owner_mention(context,self.event,{'555'},pending))
        self.assertTrue(ticket[1].is_set())

    async def test_owner_declines_errors_cancel_and_cap_stay_silent(self):
        self.event.set_extra = Mock()
        ticket=('555',asyncio.Event())
        async def ask():
            return await turns.owner_reply(self.window,self.event,[],['你明天来不来？'],ticket,
                provider=self.provider,slots=self.slots,timeout=1,seconds=300,limit=1)
        for text in ['{"reply":null}','yes','{}','{"reply":""}']:
            self.provider.text_chat.return_value.completion_text=text
            self.assertIsNone((await ask())[1]); self.assertFalse(self.window.pending)
        self.provider.text_chat.side_effect=RuntimeError('test')
        self.assertIsNone((await ask())[1])
        self.provider.text_chat.reset_mock()
        _, held=self.window.reserve(turns.room_key(self.event),'mention',1,300,1)
        self.assertEqual((await ask())[0],'full')
        self.provider.text_chat.assert_not_awaited()
        self.window.finish(held,sent=False)
        async def owner_spoke(**_):
            ticket[1].set()
            return SimpleNamespace(completion_text='{"reply":"可以。"}')
        self.provider.text_chat.side_effect=owner_spoke
        self.assertIsNone((await ask())[1])
        self.assertFalse(self.window.pending)
        self.event.set_extra.assert_not_called()
