"""Offline contract test, run in the pinned AstrBot image with an empty ASTRBOT_ROOT."""
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot.core.provider.entities import ProviderRequest
from plugin.astrbot_plugin_dsh_companion.library import Library
from plugin.astrbot_plugin_dsh_companion.main import Companion, Harness
import json


class EntryTest(unittest.IsolatedAsyncioTestCase):
    async def test_qq_identity_prefix_and_elliptical_question_use_task_records(self):
        from datetime import datetime
        from plugin.astrbot_plugin_dsh_companion.qq_images import QQImageDelivery

        plugin = object.__new__(Companion)
        plugin.config = {"owner_private_scopes": []}
        scope = "qq:FriendMessage:101"
        event = SimpleNamespace(
            message_str="[主人的朋友 来客(101)] 还没有吗",
            message_obj=SimpleNamespace(message_str="还没有吗", message_id="msg1", raw_message={}),
            get_platform_name=lambda: "aiocqhttp", get_platform_id=lambda: "qq", get_self_id=lambda: "999",
            get_sender_id=lambda: "101", get_group_id=lambda: "", unified_msg_origin=scope,
        )
        records = [SimpleNamespace(
            task_id="second", status="failed", sender_id="101", source="LLM工具",
            created_at=datetime.now(), delivered=False, result_paths=[], delivered_paths=[], qq_image_receipts={},
        )]
        manager = SimpleNamespace(list_generation_tasks=lambda **kwargs: records if kwargs["unified_msg_origin"] == scope else [])
        with tempfile.TemporaryDirectory() as tmp:
            delivery = QQImageDelivery(None, manager, tmp, tmp)
            star = SimpleNamespace(activated=True, star_cls=SimpleNamespace(qq_image_delivery=delivery))
            plugin.context = SimpleNamespace(get_registered_star=lambda name: star)
            req = ProviderRequest(prompt="带聊天历史的提示词", contexts=[
                {"role": "assistant", "content": [{"type": "text", "text": "等自画像画好就发。"}]},
            ])
            req.conversation = SimpleNamespace(cid="c")
            await plugin.bind_turn(event, req)
            provider = Harness({"id": "fat", "type": "dsh_companion_acp"}, {})
            with patch("asyncio.open_unix_connection", side_effect=AssertionError("进度查询不应提交到 DSH")):
                answer = await provider.text_chat(prompt=req.prompt, session_id=req.session_id)
            self.assertIn("失败", answer.completion_text)
            self.assertNotIn("排队", answer.completion_text)

            event.message_obj.message_str = "刚刚忙了半天，那幅大作到底怎么样啦"
            await plugin.bind_turn(event, req)
            self.assertIn("最近一次", req.system_prompt)
            self.assertIn("失败", req.system_prompt)

    async def test_tool_loop_carries_the_actual_receipt_back_to_the_model(self):
        from astrbot.core.agent.hooks import BaseAgentRunHooks
        from astrbot.core.agent.run_context import ContextWrapper
        from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
        from mcp.types import CallToolResult, TextContent
        for receipt in ("图片已生成", "生成失败", "供应商结果未知"):
            with self.subTest(receipt=receipt), tempfile.TemporaryDirectory() as tmp:
                path = str(Path(tmp) / "tool.sock")
                calls, sent = [], []
                async def answer(reader, writer):
                    packet = json.loads(await reader.readline())
                    results = [m["content"] for m in packet["recent"] if m["role"] == "tool"]
                    sent.append(results)
                    result = {"ok":True, "call":{"name":"generate_image","id":"first","args":{"prompt":"云"}}}
                    if results:
                        result = {"ok":True,"text":"回执：" + results[-1]}
                    writer.write(json.dumps(result,ensure_ascii=False).encode() + b"\n")
                    await writer.drain()
                    writer.close()
                class Executor:
                    @classmethod
                    async def execute(cls, tool, run_context, **args):
                        calls.append(tool.name)
                        yield CallToolResult(content=[TextContent(type="text",text=receipt)])
                server=await asyncio.start_unix_server(answer,path)
                previous=os.environ.get("DAFEIYU_DSH_SOCKET")
                os.environ["DAFEIYU_DSH_SOCKET"]=path
                try:
                    plugin=object.__new__(Companion)
                    plugin.config={"owner_private_scopes":[]}
                    plugin._new_conversation_lock=asyncio.Lock()
                    plugin.context=SimpleNamespace(get_registered_star=lambda _: None, conversation_manager=SimpleNamespace(
                        get_curr_conversation_id=AsyncMock(return_value="fat-cid")))
                    event=SimpleNamespace(unified_msg_origin="test:GroupMessage:room",message_obj=SimpleNamespace(
                        message_id="message",raw_message={"post_type":"message","sub_type":"normal"}),
                        message_str="画云",get_platform_name=lambda:"aiocqhttp",get_sender_id=lambda:"member")
                    tools=ToolSet()
                    tools.add_tool(FunctionTool(name="generate_image",description="FAT",
                        parameters={"type":"object","properties":{"prompt":{"type":"string"}}}))
                    req=ProviderRequest(prompt="画云",system_prompt="FAT",func_tool=tools)
                    await plugin.bind_turn(event,req)
                    runner=ToolLoopAgentRunner()
                    provider=Harness({"id":"fat","type":"dsh_companion_acp"},{})
                    await runner.reset(provider=provider, request=req, run_context=ContextWrapper(context=None),
                        tool_executor=Executor, agent_hooks=BaseAgentRunHooks(), streaming=False)
                    async for _ in runner.step_until_done(3):
                        pass
                    self.assertEqual(calls,["generate_image"])
                    self.assertEqual(sent[0],[])
                    self.assertIn(receipt,runner.get_final_llm_resp().completion_text)
                    if receipt != "图片已生成":
                        self.assertNotIn("图片已生成",runner.get_final_llm_resp().completion_text)
                finally:
                    server.close()
                    await server.wait_closed()
                    if previous is None: os.environ.pop("DAFEIYU_DSH_SOCKET",None)
                    else: os.environ["DAFEIYU_DSH_SOCKET"]=previous

    async def test_onebot_temporary_chat_isolated_before_astrbot_loads_history(self):
        from aiocqhttp import Event
        from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import AiocqhttpAdapter
        adapter = object.__new__(AiocqhttpAdapter)
        def incoming(subtype, group):
            return Event({"post_type":"message","message_type":"private","sub_type":subtype,"self_id":999,
                          "user_id":101,"sender":{"user_id":101,"group_id":group},"message_id":93,
                          "time":1,"message":[{"type":"text","data":{"text":"hi"}}],"raw_message":"hi"})
        friend = await adapter.convert_message(incoming("friend", 0))
        temporary = await adapter.convert_message(incoming("group", 505))
        other = await adapter.convert_message(incoming("group", 506))
        self.assertEqual(friend.session_id, "101")
        self.assertEqual(temporary.sender.user_id, friend.sender.user_id)
        self.assertNotEqual(friend.session_id, temporary.session_id)
        self.assertNotEqual(temporary.session_id, other.session_id)

    async def test_event_hooks_provider_and_private_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "test.sock")
            captured = []
            async def serve(reader, writer):
                packet = json.loads(await reader.readline())
                captured.append(packet)
                value = {"ok":True, "text":"记住了"}
                if any(t["name"]=="generate_image" for t in packet["tools"]):
                    value = {"ok":True,"call":{"name":"generate_image","args":{"prompt":"云"},"id":"a"}}
                writer.write(json.dumps(value,ensure_ascii=False).encode()+b"\n")
                await writer.drain()
                writer.close()
            server=await asyncio.start_unix_server(serve,path)
            self.addAsyncCleanup(server.wait_closed)
            self.addCleanup(server.close)
            os.environ["DAFEIYU_DSH_SOCKET"]=path
            self.addCleanup(os.environ.pop,"DAFEIYU_DSH_SOCKET",None)
            plugin=object.__new__(Companion)
            plugin._new_conversation_lock=asyncio.Lock()
            manager=SimpleNamespace(get_curr_conversation_id=AsyncMock(return_value=None),
                                    new_conversation=AsyncMock(return_value="current-cid"))
            plugin.context=SimpleNamespace(conversation_manager=manager, get_registered_star=lambda _: None)
            scope="testqq:FriendMessage:test-actor"
            plugin.config={"owner_private_scopes":[scope]}
            plugin.library=Library(tmp)
            def event(origin):
                extra={}
                return SimpleNamespace(unified_msg_origin=origin, get_sender_id=lambda:"test-actor",
                    message_obj=SimpleNamespace(message_id="test-inbound",raw_message={"post_type":"message","sub_type":"friend"}),
                    message_str="今天读第二章",
                    get_platform_name=lambda:"aiocqhttp",
                    get_extra=extra.get,set_extra=extra.__setitem__,stop_event=lambda:extra.update(stopped=True))
            owner=event(scope)
            primary=Harness({"id":"test-provider","type":"dsh_companion_acp"}, {})
            def toolset():
                tools=ToolSet()
                for n in ("df_reading_progress","generate_image","run_shell"):
                    tools.add_tool(FunctionTool(name=n,description=n,parameters={"type":"object","properties":{}}))
                return tools
            req=ProviderRequest(prompt="今天读第二章",session_id=scope,system_prompt="心里牵挂",func_tool=toolset())
            await plugin.bind_turn(owner,req)
            result=await primary.text_chat(prompt=req.prompt,session_id=req.session_id,system_prompt=req.system_prompt,
                func_tool=req.func_tool,conversation_id="thread-a")
            self.assertEqual(result.tools_call_name,["generate_image"])
            self.assertEqual(captured[-1]["system"],["心里牵挂"])
            self.assertEqual({t["name"] for t in captured[-1]["tools"]},{"df_reading_progress","generate_image"})
            with self.assertRaisesRegex(RuntimeError,"已经提交"):
                await primary.text_chat(prompt=req.prompt,session_id=req.session_id,func_tool=req.func_tool,conversation_id="thread-a")
            group=event("testqq:GroupMessage:test-room")
            await plugin.bind_turn(group,req)
            await primary.text_chat(prompt="群聊",session_id=req.session_id,func_tool=req.func_tool)
            self.assertTrue(captured[-1]["binding"])
            self.assertEqual({t["name"] for t in captured[-1]["tools"]},{"generate_image"})
            with self.assertRaises(PermissionError):
                await plugin.progress(group,"一本书","秘密")
            await plugin.progress(owner,"一本书","第二章")
            self.assertEqual(await plugin.progress(owner,"一本书"),"第二章")
            self.assertEqual(Library(tmp).progress("一本书"),"第二章")
            await plugin.accept_once(owner)
            duplicate=event(scope)
            await plugin.accept_once(duplicate)
            self.assertTrue(duplicate.get_extra("stopped"))
            await primary.text_chat(prompt="是否接话",session_id=scope,func_tool=req.func_tool)
            self.assertEqual(captured[-1]["tools"],[])
            self.assertIsNone(captured[-1]["binding"])
            server.close()


if __name__=="__main__":
    unittest.main()
