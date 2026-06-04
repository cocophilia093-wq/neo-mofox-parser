"""/登录B站 命令组件。

通过 BilibiliParser.login 触发扫码登录：
- 先发送二维码图片
- 再循环上报扫码状态（已扫描 / 已确认 / 超时 / 成功）

仅 OWNER 可用。
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.api.send_api import send_image, send_text
from src.app.plugin_system.base import BaseCommand, cmd_route
from src.core.components.types import PermissionLevel

if TYPE_CHECKING:
    from ..plugin import ParserPlugin


logger = get_logger("parser.blogin_command")


class BloginCommand(BaseCommand):
    """B 站扫码登录命令。"""

    command_name: str = "登录B站"
    command_description: str = "扫码登录哔哩哔哩，刷新 SESSDATA / bili_jct（仅主人）"
    permission_level: PermissionLevel = PermissionLevel.OWNER

    @classmethod
    def match(cls, parts: list[str]) -> int:
        if not parts:
            return 0
        if parts[0] in ("登录B站", "登录b站", "blogin"):
            return 1
        return 0

    async def _reply(self, text: str) -> None:
        await send_text(text, stream_id=self.stream_id)

    @cmd_route()
    async def handle(self) -> tuple[bool, str]:
        plugin: "ParserPlugin" = self.plugin  # type: ignore[assignment]

        # 找到 BilibiliParser 实例
        bili = None
        for parser in (plugin.parser_map or {}).values():
            if parser.__class__.__name__ == "BilibiliParser":
                bili = parser
                break
        if bili is None or not hasattr(bili, "login"):
            await self._reply("未启用 B 站解析或 BilibiliParser 未初始化")
            return False, "bilibili parser unavailable"

        # 1. 拉二维码图片
        try:
            qr_bytes = await bili.login.login_with_qrcode()
        except Exception as e:
            logger.error(f"[parser] 生成二维码失败: {e}", exc_info=True)
            await self._reply(f"生成二维码失败: {e}")
            return False, "qrcode generate failed"

        # 落盘到 cache，再以 file:// URI 发出
        cfg = getattr(plugin, "cfg", None)
        if cfg is None:
            await self._reply("parser 配置尚未就绪")
            return False, "config not ready"

        qr_path = cfg.cache_dir / f"bili_qrcode_{uuid.uuid4().hex}.png"
        try:
            qr_path.write_bytes(qr_bytes)
            await send_image(qr_path.as_uri(), stream_id=self.stream_id)
        except Exception as e:
            logger.error(f"[parser] 发送二维码失败: {e}", exc_info=True)
            await self._reply(f"发送二维码失败: {e}")
            return False, "qrcode send failed"

        # 2. 轮询扫码状态
        try:
            async for msg in bili.login.check_qr_state():
                await self._reply(msg)
        except Exception as e:
            logger.error(f"[parser] 轮询扫码状态失败: {e}", exc_info=True)
            await self._reply(f"扫码状态轮询失败: {e}")
            return False, "qrcode polling failed"

        return True, "ok"
