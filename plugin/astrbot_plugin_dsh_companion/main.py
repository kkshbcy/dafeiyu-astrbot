import asyncio
import json
import os
import uuid
from collections import OrderedDict

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.provider.entities import LLMResponse, ProviderRequest, ProviderType
from astrbot.core.provider.provider import Provider
from astrbot.core.provider.register import register_provider_adapter, provider_cls_map, provider_registry
from .contract import allowed_tools, check_proposal, conversation_key, owner_private, pack_messages
from .library import Library
from .qq_images import is_image_followup


TYPE = "dsh_companion_acp"
# Only the normal event hook mints a ticket. Background appraisals are isolated.
tickets = OrderedDict()
previous = provider_cls_map.get(TYPE)
if previous and getattr(previous.cls_type, "__module__", "") == __name__:
    provider_cls_map.pop(TYPE)
    if previous in provider_registry:
        provider_registry.remove(previous)


@register_provider_adapter(TYPE, "官方 DSH · ACP", provider_type=ProviderType.CHAT_COMPLETION,
    default_config_tmpl={"id":"dsh_chat", "type":TYPE, "model":"official-dsh", "enable":False,
                         "provider_type":"chat_completion", "modalities":["text","image","tool_use"]})
class Harness(Provider):
    def __init__(self, provider_config, provider_settings):
        super().__init__(provider_config, provider_settings)
        self.set_model("official-dsh")

    def get_current_key(self):
        return ""

    def set_key(self, key):
        if key:
            raise ValueError("DSH uses its signed-in account")

    async def get_models(self):
        return ["official-dsh"]

    async def text_chat(self, prompt=None, session_id=None, image_urls=None, audio_urls=None,
                        func_tool=None, contexts=None, system_prompt=None, tool_calls_result=None,
                        model=None, extra_user_content_parts=None, **kwargs):
        ticket = None if self.provider_config.get("auxiliary") else tickets.get(session_id)
        scope, owner, message_id, fallback_cid, submitted, image_followup, image_status = (
            ticket or ("", False, "", None, set(), False, None)
        )
        if image_status:
            return LLMResponse(role="assistant", completion_text=image_status)
        messages = list(contexts or [])
        if system_prompt:
            messages.insert(0, {"role":"system", "content":system_prompt})
        if prompt is not None:
            request = ProviderRequest(prompt=prompt, image_urls=image_urls or [], audio_urls=audio_urls or [],
                                      extra_user_content_parts=extra_user_content_parts or [])
            messages.append(await request.assemble_context())
        if tool_calls_result:
            results = tool_calls_result if isinstance(tool_calls_result, list) else [tool_calls_result]
            for result in results:
                messages.extend(result.to_openai_messages())
        packet = pack_messages(messages)
        schemas = func_tool.openai_schema() if func_tool else []
        packet["binding"] = conversation_key(scope, kwargs.get("conversation_id") or fallback_cid)
        packet["tools"] = allowed_tools(schemas, bool(ticket), owner)
        if image_followup:
            packet["tools"] = [tool for tool in packet["tools"] if tool["name"] != "generate_image"]
        packet["event"] = message_id
        payload = json.dumps(packet, ensure_ascii=False, separators=(",", ":")).encode()
        if len(payload) > 22 * 1024 * 1024:
            raise ValueError("本轮附件超过 DSH 输入上限")
        socket = os.environ.get("DAFEIYU_DSH_SOCKET", "/run/dafeiyu-dsh/chat.sock")
        async with asyncio.timeout(210):
            reader, writer = await asyncio.open_unix_connection(socket, limit=2 * 1024 * 1024)
            try:
                writer.write(payload + b"\n")
                await writer.drain()
                raw = await reader.readline()
                if not raw:
                    raise RuntimeError("DSH 桥连接中断")
                response = json.loads(raw)
            finally:
                writer.close()
                await writer.wait_closed()
        if not response.get("ok"):
            raise RuntimeError("DSH 未完成本轮：" + str(response.get("error", "unavailable"))[:90])
        if response.get("call"):
            call = check_proposal(response["call"], packet["tools"])
            if call["name"] == "generate_image":
                fingerprint = json.dumps(call["args"], ensure_ascii=False, sort_keys=True)
                if fingerprint in submitted:
                    raise RuntimeError("本轮相同的生图请求已经提交")
                submitted.add(fingerprint)
            return LLMResponse(role="tool", tools_call_args=[call["args"]], tools_call_name=[call["name"]],
                               tools_call_ids=[call["id"]])
        if not isinstance(response.get("text"), str) or not response["text"].strip():
            raise RuntimeError("DSH 没有返回可发送的正文")
        return LLMResponse(role="assistant", completion_text=response["text"])

    async def text_chat_stream(self, *args, **kwargs):
        yield await self.text_chat(*args, **kwargs)


class Companion(Star):
    def __init__(self, context: Context, config):
        super().__init__(context)
        self.config = config
        self.library = Library(StarTools.get_data_dir("astrbot_plugin_dsh_companion"))
        self._new_conversation_lock = asyncio.Lock()

    def is_owner(self, event):
        platform = event.get_platform_name()
        raw = getattr(event.message_obj, "raw_message", None)
        scopes = self.config.get("owner_private_scopes", [])
        if platform == "wechat_native":
            return isinstance(raw, dict) and owner_private(event.unified_msg_origin, event.get_sender_id(),
                                                            scopes, wechat_message=raw)
        if platform == "aiocqhttp":
            if not hasattr(raw, "get") or raw.get("post_type") != "message" or raw.get("sub_type") != "friend":
                return False
            return owner_private(event.unified_msg_origin, event.get_sender_id(), scopes)
        return False

    def image_delivery(self):
        star = self.context.get_registered_star("astrbot_plugin_image_generation")
        return getattr(star.star_cls, "qq_image_delivery", None) if star and star.activated else None

    @filter.event_message_type(filter.EventMessageType.ALL, priority=99999)
    async def accept_once(self, event: AstrMessageEvent):
        if event.get_platform_name() not in {"aiocqhttp", "wechat_native"} or event.get_extra("dfy_admitted"):
            return
        if not self.library.first_delivery(event.unified_msg_origin, getattr(event.message_obj, "message_id", None)):
            event.stop_event()
            return
        event.set_extra("dfy_admitted", True)

    @filter.on_llm_request(priority=-50000)
    async def bind_turn(self, event: AstrMessageEvent, req):
        platform = event.get_platform_name()
        if platform not in {"aiocqhttp", "wechat_native"}:
            return
        scope = event.unified_msg_origin
        text = event.message_str if platform == "aiocqhttp" else ""
        image_followup = is_image_followup(text)
        image_status = None
        if image_followup and (delivery := self.image_delivery()):
            image_status = delivery.status(event, text)
        conversation = getattr(req.conversation, "cid", None)
        if not conversation:
            # Group Chat Plus submits its own history without a Conversation object.
            # Track AstrBot's selected conversation so /new and /switch still work.
            manager = self.context.conversation_manager
            async with self._new_conversation_lock:
                conversation = await manager.get_curr_conversation_id(scope)
                if not conversation:
                    conversation = await manager.new_conversation(scope)
        ticket = "dfy:" + uuid.uuid4().hex
        tickets[ticket] = (
            scope, self.is_owner(event), str(event.message_obj.message_id or ""), conversation,
            set(), image_followup, image_status,
        )
        if len(tickets) > 2048:
            tickets.popitem(last=False)
        req.session_id = ticket

    @filter.command("补发生图")
    async def retry_image(self, event: AstrMessageEvent):
        if event.get_platform_name() != "aiocqhttp":
            return
        event.stop_event()
        delivery = self.image_delivery()
        if delivery is None:
            yield event.plain_result("生图暂时不可用。")
        elif message := await delivery.retry(event):
            yield event.plain_result(message)

    @filter.llm_tool(name="df_reading_progress")
    async def progress(self, event: AstrMessageEvent, title: str, note: str = ""):
        """读取或保存主人私聊的阅读、游戏和工作进度。只有真实已知进展才能写入。
        Args:
            title(string): 书、游戏或工作事项名称。
            note(string): 留空查询。保存时填写准确的进度和来源。
        """
        if not self.is_owner(event):
            raise PermissionError("仅主人私聊可用")
        return self.library.progress(title, note)

    @filter.llm_tool(name="df_list_books")
    async def list_books(self, event: AstrMessageEvent):
        """列出主人私有书架里可阅读的 PDF、Markdown 和纯文本文档。"""
        if not self.is_owner(event):
            raise PermissionError("仅主人私聊可用")
        return "\n".join(self.library.titles()) or "书架目前为空。"

    @filter.llm_tool(name="df_read_book")
    async def read_book(self, event: AstrMessageEvent, name: str, part: int = 1):
        """实际读取私有书架里的一页 PDF 或一段文本，取到原文后再讨论。
        Args:
            name(string): 书架列出的准确文件名。
            part(number): 页或片段序号，从 1 开始。
        """
        if not self.is_owner(event):
            raise PermissionError("仅主人私聊可用")
        return await asyncio.to_thread(self.library.read, name, int(part))
