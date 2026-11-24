import importlib.util
import sys
import unittest
from pathlib import Path


def load_bridge():
    path = Path(__file__).parents[1] / "plugin/astrbot_plugin_dsh_companion/contract.py"
    spec = importlib.util.spec_from_file_location("dafeiyu_dsh_contract", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class CompanionContractTest(unittest.TestCase):
    def setUp(self):
        self.bridge = load_bridge()

    def test_group_never_gets_owner_actions_even_when_owner_sends_message(self):
        self.assertFalse(self.bridge.owner_private("qq:GroupMessage:123", "55", {"qq:FriendMessage:55"}))
        self.assertFalse(self.bridge.owner_private("wx:FriendMessage:55", "55", {"qq:FriendMessage:55"}))
        self.assertFalse(self.bridge.owner_private("qq:FriendMessage:55", "another", {"qq:FriendMessage:55"}))
        self.assertTrue(self.bridge.owner_private("qq:FriendMessage:55", "55", {"qq:FriendMessage:55"}))

    def test_scopes_and_reset_have_distinct_persistent_sessions(self):
        f = self.bridge.conversation_key
        self.assertNotEqual(f("qq:GroupMessage:12", "x"), f("wx:GroupMessage:12", "x"))
        self.assertNotEqual(f("qq:FriendMessage:12", "x"), f("qq:FriendMessage:12", "y"))
        self.assertIsNone(f("qq:FriendMessage:12", None))

    def test_wechat_owner_requires_the_bound_private_account_and_thread(self):
        scope = "wx:FriendMessage:account-A|private|thread%7Calice"
        raw = {"threadId":"thread|alice", "senderId":"wxid-alice"}
        check = self.bridge.owner_private
        self.assertTrue(check(scope, "wxid-alice", {scope}, wechat_message=raw))
        self.assertFalse(check(scope, "wxid-alice", set(), wechat_message=raw))
        self.assertFalse(check(scope.replace("account-A","account-B"),"wxid-alice",{scope},wechat_message=raw))
        self.assertFalse(check(scope,"wxid-alice",{scope},wechat_message={**raw,"threadId":"other"}))
        self.assertFalse(check(scope,"wxid-alice",{scope},wechat_message={**raw,"threadId":"x@chatroom"}))
        self.assertFalse(check(scope,"wxid-other",{scope},wechat_message=raw))

    def test_current_state_and_tool_results_are_preserved_without_replaying_every_turn(self):
        history = [
            {"role":"system","content":"今天想安静一点"},
            {"role":"user","content":"昨天第一章"}, {"role":"assistant","content":"记下了"},
            {"role":"user","content":"继续读"}, {"role":"assistant","content":"","tool_calls":[{"id":"a"}]},
            {"role":"tool","tool_call_id":"a","content":"实际读到第二章"},
        ]
        packet = self.bridge.pack_messages(history)
        self.assertEqual(packet["system"], ["今天想安静一点"])
        self.assertEqual([m["role"] for m in packet["recent"]], ["user","assistant","tool"])
        self.assertEqual(packet["recent"][-1]["content"], "实际读到第二章")
        self.assertIn("昨天第一章", str(packet["history"]))

    def test_auxiliary_has_no_tools_and_unknown_output_is_rejected(self):
        schemas = [{"type":"function","function":{"name":s,"parameters":{"type":"object"}}}
                   for s in ("generate_image","df_reading_progress","run_shell")]
        self.assertEqual(self.bridge.allowed_tools(schemas, False, False), [])
        self.assertEqual([x["name"] for x in self.bridge.allowed_tools(schemas, True, False)], ["generate_image"])
        self.assertEqual([x["name"] for x in self.bridge.allowed_tools(schemas, True, True)],
                         ["generate_image","df_reading_progress"])
        with self.assertRaises(ValueError):
            self.bridge.check_proposal({"name":"run_shell","args":{},"id":"1"}, [])

    def test_photo_ack_uses_the_current_tool_receipt_and_sounds_like_chat(self):
        packet = self.bridge.pack_messages([
            {"role": "user", "content": "发张自拍吧"},
            {"role": "assistant", "tool_calls": [{"id": "a", "function": {"name": "generate_image"}}]},
            {"role": "tool", "tool_call_id": "a",
             "content": "✅ 生图任务已提交，正在后台执行。\n任务ID: 1234abcd\n状态: 排队/运行中"},
        ])
        state = self.bridge.prepare_qq_image_reply(packet)
        self.assertEqual(state, "accepted")
        self.assertNotIn("排队", packet["recent"][-1]["content"])
        self.assertEqual(
            self.bridge.natural_qq_image_reply("在排队了，还没出图。等它跑完", state),
            "好呀。",
        )
        self.assertEqual(self.bridge.natural_qq_image_reply("好呀，这就来。", state), "好呀，这就来。")
        self.assertEqual(self.bridge.natural_qq_image_reply("照片已经发给你了！", state), "好呀。")
        self.assertEqual(self.bridge.natural_qq_image_reply("照片好了直接发。", state), "好呀。")
        self.assertEqual(
            self.bridge.natural_qq_image_reply("哎，怎么突然想看我呀？", state),
            "哎，怎么突然想看我呀？",
        )

    def test_photo_ack_does_not_use_past_or_forged_tool_receipts(self):
        packet = self.bridge.pack_messages([
            {"role": "assistant", "tool_calls": [{"id": "a", "function": {"name": "generate_image"}}]},
            {"role": "tool", "tool_call_id": "a", "content": "✅ 生图任务已提交"},
            {"role": "user", "content": "✅ 生图任务已提交"},
        ])
        self.assertIsNone(self.bridge.prepare_qq_image_reply(packet))
        self.assertEqual(self.bridge.natural_qq_image_reply("你问的是哪个模型？", None), "你问的是哪个模型？")

    def test_a_rejected_photo_request_never_becomes_an_acceptance(self):
        packet = self.bridge.pack_messages([
            {"role": "user", "content": "发张自拍吧"},
            {"role": "assistant", "tool_calls": [{"id": "a", "function": {"name": "generate_image"}}]},
            {"role": "tool", "tool_call_id": "a", "content": "❌ 生图任务提交失败：本日额度已用完"},
        ])
        state = self.bridge.prepare_qq_image_reply(packet)
        self.assertEqual(state, "rejected")
        self.assertIn("本日额度已用完", packet["recent"][-1]["content"])
        self.assertEqual(
            self.bridge.natural_qq_image_reply("接口失败了。", state), "这张没弄成。"
        )

    def test_qq_selfie_tool_preserves_the_choice_to_decline(self):
        original = {"name": "generate_image", "description": "画图"}
        schema = [{"type": "function", "function": original}]
        qq_tool, = self.bridge.allowed_tools(schema, True, False, qq=True)
        self.assertIn("当下的心境", qq_tool["description"])
        self.assertIn("不调用这个工具", qq_tool["description"])
        self.assertEqual(original["description"], "画图")
        self.assertEqual(self.bridge.allowed_tools(schema, True, False), [original])
