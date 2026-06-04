"""消息发送器（Neo-MoFox 适配版）。

职责：
- 根据解析结果（ParseResult）规划发送策略
- 控制是否渲染卡片、是否强制合并转发
- 通过 Neo-MoFox send_api 发送独立消息
- 通过 napcat adapter 发送 OneBot v11 合并转发

策略规划（_build_send_plan / _resolve_groups）保持与原版完全一致；
只替换最后一公里的“真正发送动作”。
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from itertools import chain
from pathlib import Path
from typing import Any

from src.app.plugin_system.api.adapter_api import get_adapter
from src.app.plugin_system.api.send_api import (
    send_file,
    send_image,
    send_text,
    send_video,
    send_voice,
)

from ._log import logger
from .config import PluginConfig
from .data import (
    AudioContent,
    DynamicContent,
    FileContent,
    GraphicsContent,
    ImageContent,
    ParseResult,
    SendGroup,
    TextContent,
    VideoContent,
)
from .exception import (
    DownloadException,
    DownloadLimitException,
    SizeLimitException,
    ZeroSizeException,
)
from .render import Renderer


# ============================================================================
# SendContext
# ============================================================================


@dataclass(slots=True)
class SendContext:
    """发送上下文。

    携带 message handler 解析出的会话信息，sender 据此完成
    Neo-MoFox 推送 / OneBot 合并转发。
    """

    stream_id: str
    platform: str
    chat_type: str  # "group" / "private"
    self_id: str
    target_group_id: str | None = None
    target_user_id: str | None = None


# ============================================================================
# OneBot v11 segment helpers (用于合并转发)
# ============================================================================


def _ob_text(text: str) -> dict[str, Any]:
    return {"type": "text", "data": {"text": text}}


def _ob_image(uri: str) -> dict[str, Any]:
    return {"type": "image", "data": {"file": uri}}


def _ob_video(uri: str) -> dict[str, Any]:
    return {"type": "video", "data": {"file": uri}}


def _ob_record(uri: str) -> dict[str, Any]:
    return {"type": "record", "data": {"file": uri}}


# base64 编码上限（字节）。超过此大小的文件跳过发送。
_BASE64_SIZE_LIMIT = 100 * 1024 * 1024  # 100 MB


def _file_to_base64_uri(path: Path) -> str | None:
    """读取本地文件并返回 base64:// URI（napcat 可识别）。
    超过 _BASE64_SIZE_LIMIT 则返回 None。
    """
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size > _BASE64_SIZE_LIMIT or size == 0:
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return "base64://" + base64.b64encode(data).decode("ascii")


# ============================================================================
# MessageSender
# ============================================================================


class MessageSender:
    """消息发送器。

    设计原则：
    - 仅负责“怎么发”，不做解析
    - 通过 SendContext 与上层解耦
    - 失败时降级为纯文本兜底
    """

    NAPCAT_ADAPTER_SIGNATURE = "napcat_adapter:adapter:napcat_adapter"
    FORWARD_NICKNAME = "阿勒忒娅"

    def __init__(self, config: PluginConfig, renderer: Renderer):
        self.cfg = config
        self.renderer = renderer

    # ------------------------------------------------------------------
    # 通用工具
    # ------------------------------------------------------------------

    @staticmethod
    def _to_file_uri(path: Path) -> str:
        if not path.is_absolute():
            path = path.resolve()
        return path.as_uri()

    @staticmethod
    def _to_file_path_str(path: Path) -> str:
        """Neo-MoFox send_video / send_voice 等需要本地路径，而 OneBot 段需要 file:// URI。"""
        if not path.is_absolute():
            path = path.resolve()
        return str(path)

    @staticmethod
    def _iter_contents(result: ParseResult):
        return chain(result.contents, result.repost.contents if result.repost else ())

    # ------------------------------------------------------------------
    # 发送计划
    # ------------------------------------------------------------------

    def _build_send_plan(
        self,
        result: ParseResult,
        contents: list | tuple | None = None,
        *,
        force_merge_override: bool | None = None,
        render_card_override: bool | None = None,
    ) -> dict:
        """根据解析结果生成发送计划（plan）。

        plan 只做策略决策，不做任何 IO。
        """
        light, heavy = [], []

        iterable = contents if contents is not None else self._iter_contents(result)
        for cont in iterable:
            match cont:
                case ImageContent() | GraphicsContent() | TextContent():
                    light.append(cont)
                case VideoContent() | AudioContent() | FileContent() | DynamicContent():
                    heavy.append(cont)
                case _:
                    light.append(cont)

        is_single_heavy = len(heavy) == 1 and not light
        render_card = is_single_heavy and self.cfg.single_heavy_render_card
        if render_card_override is not None:
            render_card = render_card_override

        # napcat adapter 的直发路径（send_video）不支持本地文件视频段，
        # 只有合并转发节点能透传 file:// URI。因此只要存在视频，强制走合并转发。
        has_video = any(
            isinstance(c, (VideoContent, DynamicContent)) for c in heavy
        )

        seg_count = len(light) + len(heavy) + (1 if render_card else 0)
        force_merge = seg_count >= self.cfg.forward_threshold
        if has_video:
            force_merge = True
        if force_merge_override is not None:
            force_merge = force_merge_override

        return {
            "light": light,
            "heavy": heavy,
            "render_card": render_card,
            "preview_card": render_card and not force_merge,
            "force_merge": force_merge,
        }

    def _resolve_groups(self, result: ParseResult) -> list[SendGroup]:
        if result.send_groups:
            return result.send_groups
        return [SendGroup(contents=list(self._iter_contents(result)))]

    # ------------------------------------------------------------------
    # 段构建
    #   - direct_segs: 用于非合并发送，直接调用 send_api 的描述
    #   - forward_segs: OneBot v11 段列表，用于合并转发节点 content
    # ------------------------------------------------------------------

    async def _build_direct_segments(
        self,
        result: ParseResult,
        plan: dict,
    ) -> list[tuple[str, Any]]:
        """构建直接发送的段列表。

        每个元素 (kind, payload):
        - ("text", str)
        - ("image", path: Path)
        - ("video", path: Path)
        - ("voice", path: Path)
        - ("file", path: Path)
        """
        segs: list[tuple[str, Any]] = []

        # 合并转发场景：卡片以图片形式作为合并段；非合并由 _send_preview_card 单独处理
        if plan["render_card"] and plan["force_merge"]:
            if image_path := await self.renderer.render_card(result):
                segs.append(("image", image_path))

        # 轻媒体
        for cont in plan["light"]:
            if isinstance(cont, TextContent):
                if cont.text:
                    segs.append(("text", cont.text))
                continue

            try:
                path: Path = await cont.get_path()
            except (DownloadLimitException, ZeroSizeException):
                continue
            except DownloadException:
                if self.cfg.show_download_fail_tip:
                    segs.append(("text", "此项媒体下载失败"))
                continue

            match cont:
                case ImageContent():
                    segs.append(("image", path))
                case GraphicsContent() as g:
                    segs.append(("image", path))
                    if g.text:
                        segs.append(("text", g.text))
                    if g.alt:
                        segs.append(("text", g.alt))

        # 重媒体
        for cont in plan["heavy"]:
            try:
                path: Path = await cont.get_path()
            except SizeLimitException:
                segs.append(("text", "此项媒体超过大小限制"))
                continue
            except DownloadException:
                if self.cfg.show_download_fail_tip:
                    segs.append(("text", "此项媒体下载失败"))
                continue

            match cont:
                case VideoContent() | DynamicContent():
                    segs.append(("video", path))
                case AudioContent():
                    if self.cfg.audio_to_file:
                        segs.append(("file", path))
                    else:
                        segs.append(("voice", path))
                case FileContent():
                    segs.append(("file", path))

        return segs

    @staticmethod
    def _to_forward_segment(seg: tuple[str, Any]) -> dict[str, Any] | None:
        """把 direct segment 转为 OneBot v11 段（用于合并转发）。

        napcat 常运行于独立容器（Docker/WSL），与 Bot 不共享文件系统，
        file:// 本地路径在 napcat 侧不可达。因此媒体一律就地读取并 base64
        内联（napcat 识别 base64:// 前缀）。超过大小上限的文件跳过发送，
        降级为文本提示。
        """
        kind, payload = seg
        if kind == "text":
            return _ob_text(str(payload))
        path: Path = payload  # type: ignore[assignment]

        if kind == "file":
            # file 在合并转发中无标准映射，降级为文本提示
            return _ob_text(f"[文件] {path.name}")

        b64 = _file_to_base64_uri(path)
        if b64 is None:
            logger.warning(f"[parser] 媒体过大或读取失败，跳过合并段: {path.name}")
            return _ob_text(f"[{kind}] {path.name}（文件过大，未发送）")

        if kind == "image":
            return _ob_image(b64)
        if kind == "video":
            return _ob_video(b64)
        if kind == "voice":
            return _ob_record(b64)
        return None

    # ------------------------------------------------------------------
    # 实际发送
    # ------------------------------------------------------------------

    async def _send_preview_card(
        self,
        ctx: SendContext,
        result: ParseResult,
        plan: dict,
    ) -> None:
        """非合并场景下独立发送预览卡片。"""
        if not plan["preview_card"]:
            return
        image_path = await self.renderer.render_card(result)
        if image_path is None:
            return
        b64 = _file_to_base64_uri(image_path)
        if b64 is None:
            logger.warning("[parser] 预览卡片过大或读取失败，跳过发送")
            return
        await send_image(
            b64,
            stream_id=ctx.stream_id,
            platform=ctx.platform,
        )

    async def _send_direct(
        self,
        ctx: SendContext,
        segs: list[tuple[str, Any]],
    ) -> bool:
        """逐段发送（非合并转发）。"""
        any_sent = False
        for kind, payload in segs:
            try:
                ok = await self._send_one(ctx, kind, payload)
            except Exception as e:
                logger.error(f"[parser] 发送失败 kind={kind}: {e}")
                ok = False
            any_sent = any_sent or ok
        return any_sent

    async def _send_one(
        self,
        ctx: SendContext,
        kind: str,
        payload: Any,
    ) -> bool:
        if kind == "text":
            return await send_text(
                str(payload), stream_id=ctx.stream_id, platform=ctx.platform
            )

        path: Path = payload  # type: ignore[assignment]

        # 媒体文件用 base64 内联，避免跨文件系统（Windows↔Docker）路径不可达
        b64 = _file_to_base64_uri(path)
        if b64 is None:
            logger.warning(f"[parser] 直发媒体过大或读取失败: {path.name}")
            return False

        if kind == "image":
            return await send_image(b64, stream_id=ctx.stream_id, platform=ctx.platform)
        if kind == "video":
            return await send_video(b64, stream_id=ctx.stream_id, platform=ctx.platform)
        if kind == "voice":
            return await send_voice(b64, stream_id=ctx.stream_id, platform=ctx.platform)
        if kind == "file":
            return await send_file(
                self._to_file_path_str(path),
                stream_id=ctx.stream_id,
                platform=ctx.platform,
                file_name=path.name,
            )
        return False

    async def _send_forward(
        self,
        ctx: SendContext,
        segs: list[tuple[str, Any]],
    ) -> bool:
        """通过 napcat adapter 发送 OneBot v11 合并转发。"""
        adapter = get_adapter(self.NAPCAT_ADAPTER_SIGNATURE)
        if adapter is None or not hasattr(adapter, "send_napcat_api"):
            logger.warning(
                "[parser] napcat adapter 不可用，合并转发降级为逐段发送"
            )
            return await self._send_direct(ctx, segs)

        nodes: list[dict[str, Any]] = []
        for seg in segs:
            ob_seg = self._to_forward_segment(seg)
            if ob_seg is None:
                continue
            nodes.append(
                {
                    "type": "node",
                    "data": {
                        "user_id": str(ctx.self_id),
                        "nickname": self.FORWARD_NICKNAME,
                        "content": [ob_seg],
                    },
                }
            )

        if not nodes:
            return False

        if ctx.chat_type == "group" and ctx.target_group_id:
            action = "send_group_forward_msg"
            params: dict[str, Any] = {
                "group_id": int(ctx.target_group_id),
                "messages": nodes,
            }
        elif ctx.chat_type == "private" and ctx.target_user_id:
            action = "send_private_forward_msg"
            params = {
                "user_id": int(ctx.target_user_id),
                "messages": nodes,
            }
        else:
            logger.warning(
                f"[parser] 合并转发缺少目标，降级逐段发送: chat_type={ctx.chat_type}"
            )
            return await self._send_direct(ctx, segs)

        seg_types = [
            s[0] for s in segs
        ]
        logger.info(
            f"[parser] 调用 napcat {action}: group={ctx.target_group_id}, "
            f"nodes={len(nodes)}, seg_types={seg_types}"
        )
        try:
            resp = await adapter.send_napcat_api(action, params, timeout=60.0)
            logger.info(f"[parser] napcat {action} 返回: {resp}")
            return True
        except Exception as e:
            logger.error(f"[parser] 合并转发失败，降级逐段发送: {e}")
            return await self._send_direct(ctx, segs)

    # ------------------------------------------------------------------
    # 文本兜底
    # ------------------------------------------------------------------

    @staticmethod
    def _build_text_fallback(result: ParseResult) -> str:
        lines: list[str] = []
        if result.header:
            lines.append(result.header)
        if result.text:
            lines.append(result.text)
        elif result.extra.get("info"):
            lines.append(str(result.extra["info"]))
        return "\n".join(line for line in lines if line).strip()

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------

    async def _send_group_(
        self,
        ctx: SendContext,
        result: ParseResult,
        group: SendGroup,
    ) -> bool:
        plan = self._build_send_plan(
            result,
            group.contents,
            force_merge_override=group.force_merge,
            render_card_override=group.render_card,
        )
        logger.info(
            f"[parser] send plan: force_merge={plan['force_merge']}, "
            f"render_card={plan['render_card']}, "
            f"light={len(plan['light'])}, heavy={len(plan['heavy'])}"
        )

        await self._send_preview_card(ctx, result, plan)

        segs = await self._build_direct_segments(result, plan)
        if not segs:
            logger.warning("[parser] _build_direct_segments 返回空列表，跳过发送")
            return False

        logger.info(f"[parser] 构建了 {len(segs)} 个段，准备发送 (force_merge={plan['force_merge']})")

        if plan["force_merge"]:
            return await self._send_forward(ctx, segs)
        return await self._send_direct(ctx, segs)

    async def send_parse_result(
        self,
        ctx: SendContext,
        result: ParseResult,
    ) -> None:
        """统一发送入口。

        执行顺序：
        1. 解析 send_groups（默认按 contents 一组）
        2. 每组生成 plan -> 预览卡片 -> 段构建 -> 发送
        3. 全部失败时使用纯文本兜底
        """
        groups = self._resolve_groups(result)
        logger.info(f"[parser] send_parse_result: {len(groups)} 个发送组")

        sent = False
        for group in groups:
            sent = await self._send_group_(ctx, result, group) or sent

        if sent:
            return

        text = self._build_text_fallback(result)
        if not text:
            logger.warning("[parser] 发送结果为空，不执行发送")
            return

        try:
            await send_text(text, stream_id=ctx.stream_id, platform=ctx.platform)
        except Exception as e:
            logger.error(f"[parser] 发送文本兜底失败: {e}")
