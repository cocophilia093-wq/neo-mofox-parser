"""parser 插件组件聚合。

按 Neo-MoFox 组件签名规则：
- ``parser:event_handler:parser_message`` —— 全消息扫描入口
- ``parser:command:开启解析`` —— 把当前会话从黑名单移除
- ``parser:command:关闭解析`` —— 把当前会话加入黑名单
- ``parser:command:登录B站`` —— 二维码登录 bilibili
"""

from __future__ import annotations

from .blogin_command import BloginCommand
from .close_parser_command import CloseParserCommand
from .message_handler import ParserMessageHandler
from .open_parser_command import OpenParserCommand

__all__ = [
    "BloginCommand",
    "CloseParserCommand",
    "OpenParserCommand",
    "ParserMessageHandler",
]
