"""Convert AstrBot's turn to a scoped request for the official Harness ACP profile."""
import base64
import hashlib
import json
import re
from urllib.parse import unquote


PUBLIC = {"generate_image", "query_image_presets", "recall_long_term_memory", "memorize_long_term_memory"}
PRIVATE = {"df_reading_progress", "df_read_book", "df_list_books", "df_goal", "df_curiosity_resolve",
           "df_method", "df_life_status", "df_workshop", "df_interest", "df_recall", "df_knowledge"}
MAX_HISTORY_BYTES = 384 * 1024
MAX_IMAGE_BYTES = 5 * 1024 * 1024
PHOTO_STATUS_LANGUAGE = re.compile(
    r"排队|还没出图|出图|生图|生成|模型|调用|供应商|服务端|后台|任务|接口|工具|跑完|跑图|回执"
    r"|(?i:(?<![a-z])(?:api|ai)(?![a-z]))|(?:好|完)了?[，, ]*(?:我|就|再|直接|马上|给你)+发"
    r"|(?:已经|刚刚|已)(?:给你)?(?:发|[画拍]好)|给你发了"
)
QQ_SELFIE_WILLINGNESS = (
    "别人想看你的自拍或照片时，先按你当下的心境、和对方的关系以及对方的问法决定愿不愿意。"
    "心烦、想独处或觉得突然时，可以随口拒绝、打趣或岔开话题，不调用这个工具，也不编造故障。"
    "想给对方看时再用。"
)


def owner_private(scope, sender, allowed, *, wechat_message=None):
    parts = str(scope).split(":", 2)
    if len(parts) != 3 or parts[1] != "FriendMessage" or not sender or scope not in allowed:
        return False
    if wechat_message is None:
        return parts[2] == str(sender)
    # Native scopes include the bot account and the encoded thread. The trusted
    # inbound sender is separate from the opaque thread identifier.
    if not isinstance(wechat_message, dict):
        return False
    target = parts[2].split("|")
    thread = wechat_message.get("threadId")
    return (len(target) == 3 and bool(target[0]) and target[1] == "private"
            and isinstance(thread, str) and bool(thread) and not thread.endswith("@chatroom")
            and unquote(target[2]) == thread and wechat_message.get("senderId") == str(sender))


def conversation_key(scope, conversation):
    if not scope or not conversation:
        return None
    return hashlib.sha256(json.dumps([scope, conversation], ensure_ascii=False).encode()).hexdigest()


def allowed_tools(schemas, main_turn, owner, *, qq=False):
    permitted = (PUBLIC | PRIVATE if owner else PUBLIC) if main_turn else set()
    tools = [s["function"] for s in schemas if s.get("type") == "function"
             and s.get("function", {}).get("name") in permitted]
    if not qq:
        return tools
    return [
        {**tool, "description": (tool.get("description") or "") + "\n" + QQ_SELFIE_WILLINGNESS}
        if tool["name"] == "generate_image" else tool
        for tool in tools
    ]


def check_proposal(call, available):
    if (not isinstance(call, dict) or call.get("name") not in {x["name"] for x in available}
            or not isinstance(call.get("args"), dict) or not isinstance(call.get("id"), str)
            or not call["id"]):
        raise ValueError("DSH requested a tool unavailable in this conversation")
    return call


def prepare_qq_image_reply(packet):
    """Find a real, current image receipt and phrase it for a chat response."""
    recent = packet["recent"]
    if not recent or recent[-1]["role"] != "tool":
        return None
    receipt = recent[-1]
    call_id = receipt.get("tool_call_id")
    if not call_id or not any(
        call.get("id") == call_id and call.get("function", {}).get("name") == "generate_image"
        for item in recent[:-1] if item["role"] == "assistant"
        for call in item.get("tool_calls", [])
    ):
        return None
    content = receipt["content"]
    if content.startswith("✅ 生图任务已提交") and "\n任务ID:" in content:
        receipt["content"] = "已接下这张图，图片尚未送出。"
        packet["system"].append(
            "自然接对方刚才的话，像平时聊天，可以随口答应或跟着对方的语气轻轻打趣。"
            "无需播报处理进度，也不用承诺弄好后会发送。"
            "没送到的图片不要说已发，未完成的也不要说已经好了。"
        )
        return "accepted"
    if content.startswith("❌ 生图任务提交失败"):
        packet["system"].append("只告诉对方这张没弄成。用聊天语气，避免复述后台错误和技术术语。")
        return "rejected"
    return None


def natural_qq_image_reply(text, state):
    if state and PHOTO_STATUS_LANGUAGE.search(text):
        return "好呀。" if state == "accepted" else "这张没弄成。"
    return text


def _as_dict(message):
    return message if isinstance(message, dict) else message.model_dump(exclude_none=True)


def pack_messages(messages):
    system, history, images = [], [], []
    last_user = max((i for i, m in enumerate(messages) if _as_dict(m).get("role") == "user"), default=-1)
    for i, item in enumerate(messages):
        msg = _as_dict(item)
        role = msg.get("role")
        if role not in {"user", "assistant", "tool", "system", "developer"}:
            continue
        content = msg.get("content", "")
        if isinstance(content, list):
            text_parts = []
            for raw in content:
                part = _as_dict(raw)
                if part.get("type") == "text":
                    text_parts.append(str(part.get("text", "")))
                elif part.get("type") == "image_url":
                    text_parts.append("[图片]")
                    if i == last_user:
                        url = (part.get("image_url") or {}).get("url", "")
                        match = re.fullmatch(r"data:(image/(?:png|jpeg|webp|gif));base64,([A-Za-z0-9+/=]+)", url)
                        if not match or len(images) >= 3 or len(match[2]) > MAX_IMAGE_BYTES * 4 // 3:
                            continue
                        base64.b64decode(match[2], validate=True)
                        images.append({"type": "image", "mimeType": match[1], "data": match[2]})
                else:
                    text_parts.append("[附件]")
            content = "\n".join(text_parts)
        if role in {"system", "developer"}:
            system.append(str(content or ""))
            continue
        record = {k: msg[k] for k in ("name", "tool_calls", "tool_call_id") if msg.get(k)}
        record.update(role=role, content=str(content or ""))
        history.append(record)
    latest = max((i for i, m in enumerate(history) if m["role"] == "user"), default=0)
    recent = history[latest:]
    def size(obj):
        return len(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode())
    if size([system, recent]) > MAX_HISTORY_BYTES:
        raise ValueError("One message or its active instructions exceed the DSH bridge input limit")
    kept, used = [], size(system)
    for item in reversed(history):
        weight = size(item) + 1
        if used + weight > MAX_HISTORY_BYTES:
            break
        kept.append(item)
        used += weight
    return {"system":system, "history":list(reversed(kept)), "recent":recent, "images":images}
