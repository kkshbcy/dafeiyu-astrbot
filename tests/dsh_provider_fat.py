"""Offline contract test, run in the pinned AstrBot image with an empty ASTRBOT_ROOT."""
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot.core.provider.entities import ProviderRequest
from plugin.astrbot_plugin_dsh_companion.library import Library
from plugin.astrbot_plugin_dsh_companion.main import Companion, Harness
import json


class EntryTest(unittest.IsolatedAsyncioTestCase):
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
            plugin.context=SimpleNamespace(conversation_manager=manager)
            scope="testqq:FriendMessage:test-actor"
            plugin.config={"owner_private_scopes":[scope]}
            plugin.library=Library(tmp)
            def event(origin):
                extra={}
                return SimpleNamespace(unified_msg_origin=origin, get_sender_id=lambda:"test-actor",
                    message_obj=SimpleNamespace(message_id="test-inbound"),get_platform_name=lambda:"aiocqhttp",
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
