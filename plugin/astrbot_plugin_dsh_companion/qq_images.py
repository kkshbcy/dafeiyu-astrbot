"""Confirmed delivery of a generated image to its original QQ conversation."""

import asyncio
import base64
import logging
import re
from dataclasses import dataclass
from pathlib import Path


logger = logging.getLogger(__name__)
FOLLOWUP = re.compile(
    r"(?:(?:刚才)?(?:(?:那|这|上(?:一)?)(?:张(?:图|自画像)?|次生图)|自画像|图)[，, ]*)?"
    r"(?:还没)?(?:出来|画好|生成好|发出(?:来)?)(?:了)?(?:吗|没|没有)"
    r"[？?。！!～~ ]*"
)
EXPLICIT_STATUS = re.compile(r"(?:查(?:一下|下)?|看看)?(?:生图|画图|自画像|图片)(?:的)?进度[？?。！! ]*")


@dataclass(frozen=True)
class QQTarget:
    origin: str
    sender: str
    platform: str
    account: str
    kind: str
    peer: int

    async def send(self, context, message):
        adapter = context.get_platform_inst(self.platform)
        if not adapter or adapter.meta().name != "aiocqhttp":
            raise RuntimeError("QQ adapter unavailable")
        return await adapter.bot.call_action(
            f"send_{self.kind}_msg",
            self_id=self.account,
            **{("group_id" if self.kind == "group" else "user_id"): self.peer},
            message=message,
        )


def qq_target(event):
    if event.get_platform_name() != "aiocqhttp":
        return None
    origin = event.unified_msg_origin
    parts = origin.split(":", 2)
    if len(parts) != 3:
        return None
    platform, kind, session = parts
    if platform != event.get_platform_id():
        return None
    sender, group, account = event.get_sender_id(), event.get_group_id(), event.get_self_id()
    if not all(re.fullmatch(r"[1-9][0-9]*", value or "") for value in (sender, account)):
        return None
    if kind == "GroupMessage" and group == session.rsplit("_", 1)[-1] and re.fullmatch(r"[1-9][0-9]*", group or ""):
        return QQTarget(origin, sender, platform, account, "group", int(group))
    if kind == "FriendMessage" and not group and session == sender:
        return QQTarget(origin, sender, platform, account, "private", int(sender))
    return None


def is_image_followup(text):
    return bool(FOLLOWUP.fullmatch(text.strip()) or EXPLICIT_STATUS.fullmatch(text.strip()))


class QQImageDelivery:
    def __init__(self, context, manager, originals, temp):
        self.context = context
        self.manager = manager
        self.originals = Path(originals).resolve()
        self.temp = Path(temp).resolve()
        self.in_flight = set()

    def _local_path(self, record, path):
        path = Path(path)
        parent = path.parent.resolve()
        if (parent not in (self.originals, self.temp)
                or not path.name.startswith(f"gen_{record.task_id}_") or path.is_symlink()):
            return None
        return parent / path.name

    def cleanup_confirmed(self, record):
        for raw_path in record.qq_image_receipts:
            path = self._local_path(record, raw_path)
            if path is None:
                continue
            for candidate in {path, self.temp / path.name}:
                try:
                    if not candidate.is_symlink():
                        candidate.unlink(missing_ok=True)
                except OSError:
                    logger.warning("生图 %s 已发送，文件清理失败", record.task_id, exc_info=True)

    def _matching(self, target):
        if target is None:
            return []
        records = self.manager.list_generation_tasks(unified_msg_origin=target.origin, limit=None)
        return [r for r in records if r.sender_id == target.sender and r.source in {"LLM工具", "指令"}]

    def _pending(self, record):
        if record.status != "succeeded":
            return []
        return list(dict.fromkeys(p for p in record.result_paths if p not in record.delivered_paths
                                  and p not in record.qq_image_receipts))

    def _can_retry(self, record):
        return any(path and path.is_file()
                   for path in (self._local_path(record, p) for p in self._pending(record)))

    def status(self, event, text):
        text = text.strip()
        if not is_image_followup(text):
            return None
        records = self._matching(qq_target(event))
        if not records:
            if EXPLICIT_STATUS.fullmatch(text):
                return "我没找到你在这个聊天里提交的生图任务。"
            return None
        latest = records[0]
        if latest.status in {"queued", "running", "cancelling", "cancelled"}:
            return {
                "queued": "上一张还在排队，画好会发到这里。",
                "running": "上一张还在画，画好会发到这里。",
                "cancelling": "上一张正在取消。",
                "cancelled": "上一张已经取消了。",
            }[latest.status]
        if latest.status == "failed":
            message = "上一张生成失败了。"
            if any(self._can_retry(r) for r in records[1:]):
                message += "更早有画好但没发完的图，发「/补发生图」可以补发。"
            return message
        if latest.task_id in self.in_flight:
            return "上一张画好了，正在发到这里。"
        if self._can_retry(latest):
            return "上一张画好了，还有图片没发成功。发「/补发生图」可以补发。"
        if self._pending(latest):
            return "上一张画好了，但服务器上找不到可补发的文件。"
        return "上一张已经发到这个聊天里了。" if latest.delivered else "上一张没有可发送的图片。"

    async def deliver(self, record, target):
        if (target is None or target.origin != record.unified_msg_origin
                or target.sender != record.sender_id or record.task_id in self.in_flight):
            return
        self.in_flight.add(record.task_id)
        try:
            self.cleanup_confirmed(record)
            for raw_path in self._pending(record):
                path = self._local_path(record, raw_path)
                if not path or not path.is_file():
                    logger.warning("生图 %s 找不到可投递的文件", record.task_id)
                    continue
                try:
                    data = await asyncio.to_thread(lambda: base64.b64encode(path.read_bytes()).decode("ascii"))
                    async with asyncio.timeout(60):
                        receipt = await target.send(
                            self.context, [{"type": "image", "data": {"file": "base64://" + data}}]
                        )
                    message_id = receipt.get("message_id") if isinstance(receipt, dict) else None
                    if type(message_id) is not int or message_id == 0:
                        raise ValueError("Missing OneBot message_id")
                    self.manager.record_qq_image_receipt(record.task_id, raw_path, message_id)
                except Exception as exc:
                    # OneBot errors may contain a full payload or recipient data.
                    logger.warning("生图 %s 的 QQ 投递未确认，留图供补发（%s）", record.task_id, type(exc).__name__)
                    continue
                logger.info("生图 %s 收到 QQ 投递回执", record.task_id)
                self.cleanup_confirmed(record)
        finally:
            self.in_flight.discard(record.task_id)

    async def retry(self, event):
        target = qq_target(event)
        records = self._matching(target)
        record = next((r for r in records if self._can_retry(r)), None)
        if record is None:
            return "这个聊天里没有你发起的待补发生图。"
        if record.task_id in self.in_flight:
            return "这张图正在发，稍等一下。"
        await self.deliver(record, target)
        if self._pending(record):
            return "还有图没发成功，原图留着，可以稍后再补发。"
        return None
