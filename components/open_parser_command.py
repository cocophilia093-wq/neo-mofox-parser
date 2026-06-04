"""/开启解析 命令组件。

将当前会话从 parser 黑名单移除（仅运营/管理员可用）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.api.send_api import send_text
from src.app.plugin_system.base import BaseCommand, cmd_route
from src.core.components.types import PermissionLevel

if TYPE_CHECKING:
    from ..plugin import ParserPlugin


logger = get_logger("parser.open_parser_command")


class OpenParserCommand(BaseCommand):
    """开启当前会话的链接解析。"""

    command_name: str = "开启解析"
    command_description: str = "将当前会话移出 parser 黑名单（仅管理员）"
    permission_level: PermissionLevel = PermissionLevel.OPERATOR

    @cmd_route()
    async def handle(self) -> tuple[bool, str]:
        plugin: "ParserPlugin" = self.plugin  # type: ignore[assignment]
        cfg = getattr(plugin, "cfg", None)
        if cfg is None:
            await send_text("parser 配置尚未就绪", stream_id=self.stream_id)
            return False, "config not ready"

        cfg.remove_blacklist(self.stream_id)
        await send_text("当前会话的解析已开启", stream_id=self.stream_id)
        logger.info(f"[parser] 已从黑名单移除: {self.stream_id}")
        return True, "ok"
