"""parser 全消息事件处理器。

订阅 ``EventType.ON_MESSAGE_RECEIVED``：
- 提取消息文本（含 JSON 卡片中嵌入的 URL）
- 走白/黑名单 → 关键词正则 → 多 Bot 仲裁 → 防抖 → 解析 → 发送
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Any

from src.app.plugin_system.api.adapter_api import get_adapter
from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BaseEventHandler
from src.core.components.types import EventType
from src.core.models.message import Message
from src.kernel.event import EventDecision

from ..core.arbiter import ArbiterContext
from ..core.sender import SendContext
from ..core.utils import extract_json_url

if TYPE_CHECKING:
    from ..plugin import ParserPlugin


logger = get_logger("parser.message_handler")


class ParserMessageHandler(BaseEventHandler):
    """全消息扫描入口。

    与原 AstrBot 版本 ``on_message`` 等价，但：
    - 通过 ``EventType.ON_MESSAGE_RECEIVED`` 事件订阅
    - 通过 ``napcat_adapter`` 完成仲裁回调
    - 通过 ``MessageSender.send_parse_result`` 完成发送
    """

    handler_name = "parser_message"
    handler_description = "通用链接解析消息扫描入口"

    weight: int = 50
    intercept_message: bool = False
    init_subscribe = [EventType.ON_MESSAGE_RECEIVED]

    NAPCAT_ADAPTER_SIGNATURE = "napcat_adapter:adapter:napcat_adapter"

    # ------------------------------------------------------------------
    # 事件入口
    # ------------------------------------------------------------------

    async def execute(  # type: ignore[override]
        self,
        event_name: str,
        params: dict[str, Any],
    ) -> tuple[EventDecision, dict[str, Any]]:
        # 解析+下载+发送可能耗时几十秒，远超 EventBus 的 5s 超时。
        # 把整条链路丢到后台 task，立即返回，让事件流不被卡。
        asyncio.create_task(self._safe_process(params))
        return EventDecision.SUCCESS, params

    async def _safe_process(self, params: dict[str, Any]) -> None:
        try:
            await self._process(params)
        except Exception as e:
            logger.error(f"[parser] 消息处理出错: {e}", exc_info=True)

    # ------------------------------------------------------------------
    # 主处理流程
    # ------------------------------------------------------------------

    async def _process(self, params: dict[str, Any]) -> None:
        plugin = self._plugin()
        if plugin is None or plugin.cfg is None:
            return

        message = params.get("message")
        envelope = params.get("envelope") or {}
        adapter_signature = params.get("adapter_signature") or ""

        if not isinstance(message, Message):
            return

        cfg = plugin.cfg
        stream_id = message.stream_id
        if not stream_id:
            return

        # 白/黑名单
        if cfg.whitelist and stream_id not in cfg.whitelist:
            return
        if cfg.blacklist and stream_id in cfg.blacklist:
            return

        # 抽取文本（兼容 JSON 卡片）
        text = self._extract_text(message, envelope)
        if not text:
            return

        # @机制：消息开头 @了其他 bot，跳过
        if self._is_at_other_bot(envelope, message):
            return

        # 关键词 + 正则双重判定
        keyword, searched = self._match_keyword(plugin.key_pattern_list, text)
        if searched is None:
            return
        logger.debug(f"[parser] 匹配结果: {keyword}, {searched.group(0)}")

        # 仲裁机制（仅群聊 + 启用了仲裁器）
        if (
            cfg.arbiter
            and message.chat_type == "group"
            and adapter_signature == self.NAPCAT_ADAPTER_SIGNATURE
            and plugin.arbiter is not None
        ):
            if not await self._arbiter_compete(plugin, envelope):
                logger.debug("[parser] Bot 在仲裁中输了，跳过解析")
                return
            logger.debug("[parser] Bot 在仲裁中胜出，准备解析…")

        # 链接防抖
        link = searched.group(0)
        if plugin.debouncer is not None and plugin.debouncer.hit_link(stream_id, link):
            logger.warning(f"[parser] 链接 {link} 在防抖时间内，跳过解析")
            return

        # 解析
        parse_res = await plugin.parser_map[keyword].parse(keyword, searched)

        # 资源 ID 防抖
        try:
            resource_id = parse_res.get_resource_id()
        except Exception:
            resource_id = ""
        if (
            resource_id
            and plugin.debouncer is not None
            and plugin.debouncer.hit_resource(stream_id, resource_id)
        ):
            logger.warning(
                f"[parser] 资源 {resource_id} 在防抖时间内，跳过发送"
            )
            return

        # 发送
        if plugin.sender is None:
            return
        ctx = self._build_send_context(message, envelope)
        await plugin.sender.send_parse_result(ctx, parse_res)

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def _plugin(self) -> "ParserPlugin | None":
        plugin = self.plugin
        # 避免循环导入：通过 plugin_name 字符串识别
        if getattr(plugin, "plugin_name", "") != "parser":
            return None
        return plugin  # type: ignore[return-value]

    @staticmethod
    def _extract_text(message: Message, envelope: dict[str, Any]) -> str:
        """从 message 取纯文本，附加从 JSON 段中提取的 URL。"""
        text = (message.processed_plain_text or "").strip()
        if text:
            return text

        content = message.content
        if isinstance(content, str) and content:
            return content

        # 尝试从 envelope 段中找 JSON 卡片
        seg = envelope.get("message_segment")
        segments: list[Any]
        if isinstance(seg, dict):
            segments = [seg]
        elif isinstance(seg, list):
            segments = seg
        else:
            segments = []

        for s in segments:
            if not isinstance(s, dict):
                continue
            if s.get("type") in ("json", "Json"):
                data = s.get("data")
                if isinstance(data, str):
                    extracted = extract_json_url(data)
                    if extracted:
                        return extracted
        return ""

    @staticmethod
    def _is_at_other_bot(envelope: dict[str, Any], message: Message) -> bool:
        """判断消息是否专门 @ 了其他 bot（首段为 at + 非 self_id）。"""
        seg = envelope.get("message_segment")
        if isinstance(seg, list) and seg:
            first = seg[0]
        elif isinstance(seg, dict):
            first = seg
        else:
            return False

        if not isinstance(first, dict):
            return False
        if first.get("type") != "at":
            return False

        # mofox-wire 的 at 段 data 形如 "<nickname>:<user_id>"
        data = first.get("data")
        target_id = ""
        if isinstance(data, str) and ":" in data:
            target_id = data.rsplit(":", 1)[-1]
        elif isinstance(data, dict):
            target_id = str(data.get("qq") or data.get("user_id") or "")

        if not target_id:
            return False

        # 取 bot 自身 user_id
        msg_info = envelope.get("message_info") or {}
        additional = msg_info.get("additional_config") or {}
        self_id = (
            str(additional.get("self_id") or "")
            or str(envelope.get("self_id") or "")
        )
        if not self_id:
            return False
        return target_id != self_id

    @staticmethod
    def _match_keyword(
        patterns: list[tuple[str, re.Pattern[str]]],
        text: str,
    ) -> tuple[str, re.Match[str] | None]:
        for kw, pat in patterns:
            if kw not in text:
                continue
            if m := pat.search(text):
                return kw, m
        return "", None

    async def _arbiter_compete(
        self,
        plugin: "ParserPlugin",
        envelope: dict[str, Any],
    ) -> bool:
        """通过 napcat adapter 完成 set_msg_emoji_like / fetch_emoji_like 仲裁。"""
        adapter = get_adapter(self.NAPCAT_ADAPTER_SIGNATURE)
        if adapter is None or not hasattr(adapter, "send_napcat_api"):
            logger.debug("[parser] napcat adapter 不可用，跳过仲裁")
            return True

        msg_info = envelope.get("message_info") or {}
        additional = msg_info.get("additional_config") or {}

        try:
            message_id = int(additional.get("message_id") or msg_info.get("message_id") or 0)
            msg_time = int(additional.get("time") or msg_info.get("time") or 0)
            self_id = int(additional.get("self_id") or 0)
        except (TypeError, ValueError):
            return True
        if not (message_id and self_id):
            return True

        bot = _NapcatArbiterBot(adapter)
        if plugin.arbiter is None:
            return True
        try:
            return await plugin.arbiter.compete(
                bot=bot,
                ctx=ArbiterContext(
                    message_id=message_id,
                    msg_time=msg_time,
                    self_id=self_id,
                ),
            )
        except Exception as e:
            logger.warning(f"[parser] 仲裁器异常: {e}")
            return True

    @staticmethod
    def _build_send_context(message: Message, envelope: dict[str, Any]) -> SendContext:
        msg_info = envelope.get("message_info") or {}
        additional = msg_info.get("additional_config") or {}
        group_info = msg_info.get("group_info") or {}
        user_info = msg_info.get("user_info") or {}

        self_id = str(additional.get("self_id") or "")
        target_group_id: str | None = None
        target_user_id: str | None = None
        if message.chat_type == "group":
            target_group_id = str(group_info.get("group_id") or "") or None
        elif message.chat_type == "private":
            target_user_id = str(user_info.get("user_id") or "") or None

        return SendContext(
            stream_id=message.stream_id,
            platform=message.platform or "",
            chat_type=message.chat_type or "",
            self_id=self_id,
            target_group_id=target_group_id,
            target_user_id=target_user_id,
        )


# ----------------------------------------------------------------------
# napcat 仲裁桥接
# ----------------------------------------------------------------------


class _NapcatArbiterBot:
    """把 EmojiLikeArbiter 期望的 ``bot.set_msg_emoji_like`` /
    ``bot.fetch_emoji_like`` 转发到 napcat adapter 的 send_napcat_api。
    """

    def __init__(self, adapter: Any):
        self._adapter = adapter

    async def set_msg_emoji_like(self, **kwargs: Any) -> Any:
        return await self._adapter.send_napcat_api(
            "set_msg_emoji_like", kwargs, timeout=10.0
        )

    async def fetch_emoji_like(self, **kwargs: Any) -> Any:
        return await self._adapter.send_napcat_api(
            "fetch_emoji_like", kwargs, timeout=10.0
        )
