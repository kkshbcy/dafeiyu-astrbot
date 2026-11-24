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


def allowed_tools(schemas, main_turn, owner):
    permitted = (PUBLIC | PRIVATE if owner else PUBLIC) if main_turn else set()
    return [s["function"] for s in schemas if s.get("type") == "function"
            and s.get("function", {}).get("name") in permitted]


def check_proposal(call, available):
    if (not isinstance(call, dict) or call.get("name") not in {x["name"] for x in available}
            or not isinstance(call.get("args"), dict) or not isinstance(call.get("id"), str)
            or not call["id"]):
        raise ValueError("DSH requested a tool unavailable in this conversation")
    return call


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
