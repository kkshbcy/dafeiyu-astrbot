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
