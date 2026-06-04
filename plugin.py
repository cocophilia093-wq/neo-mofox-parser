"""parser 插件入口（Neo-MoFox 适配版）。

功能继承自 astrbot_plugin_parser：
- 14 平台链接解析（B站 / 抖音 / 微博 / ...）
- 视频/图片/音频下载与合并转发
- 信息卡片渲染、防抖、缓存清理
- 多 Bot 表情贴仲裁

关键差异：
- 使用 Neo-MoFox 的 BasePlugin / BaseConfig / 事件订阅模型
- 通过 send_api / napcat adapter 完成消息推送
- 资源路径基于 ``__file__`` 推导
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from src.app.plugin_system.base import BasePlugin, register_plugin

from .components import (
    BloginCommand,
    CloseParserCommand,
    OpenParserCommand,
    ParserMessageHandler,
)
from .core._log import logger
from .core.arbiter import EmojiLikeArbiter
from .core.clean import CacheCleaner
from .core.config import ParserBaseConfig, PluginConfig
from .core.debounce import Debouncer
from .core.download import Downloader
from .core.parsers import BaseParser, BilibiliParser
from .core.render import Renderer
from .core.sender import MessageSender


@register_plugin
class ParserPlugin(BasePlugin):
    """parser 插件根类。"""

    plugin_name: str = "parser"
    plugin_description: str = "通用链接解析插件，支持 14 平台链接解析与多 Bot 仲裁"
    plugin_version: str = "1.5.1"

    configs: list[type] = [ParserBaseConfig]
    dependent_components: list[str] = []

    def __init__(self, config: ParserBaseConfig | None = None) -> None:
        super().__init__(config)

        # 运行时聚合配置（在 on_plugin_loaded 中创建）
        self.cfg: PluginConfig | None = None

        # 核心服务（在 on_plugin_loaded 中创建）
        self.renderer: Renderer | None = None
        self.downloader: Downloader | None = None
        self.debouncer: Debouncer | None = None
        self.arbiter: EmojiLikeArbiter | None = None
        self.sender: MessageSender | None = None
        self.cleaner: CacheCleaner | None = None

        # 关键词 -> Parser 实例
        self.parser_map: dict[str, BaseParser] = {}
        # 关键词 -> 正则
        self.key_pattern_list: list[tuple[str, re.Pattern[str]]] = []

    def get_components(self) -> list[type]:
        """声明本插件提供的组件。"""
        return [
            ParserMessageHandler,
            OpenParserCommand,
            CloseParserCommand,
            BloginCommand,
        ]

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def on_plugin_loaded(self) -> None:
        """初始化运行时配置与各核心服务。"""
        if not isinstance(self.config, ParserBaseConfig):
            logger.error("[parser] 未加载到 ParserBaseConfig，插件无法初始化")
            return

        plugin_dir = Path(__file__).parent.resolve()
        data_dir = Path("data") / "parser"
        data_dir.mkdir(parents=True, exist_ok=True)

        # 主人 ID 列表（来自全局 bot 配置；这里采用最简单的兜底，避免硬依赖）
        admins_id: list[str] = []
        try:
            from src.config.config import global_config

            admins_id = list(getattr(global_config.bot, "admin_ids", []) or [])
        except Exception:  # pragma: no cover - 主人 ID 仅用于 cookie 登录提示
            pass

        self.cfg = PluginConfig(
            self.config,
            plugin_dir=plugin_dir,
            data_dir=data_dir,
            admins_id=admins_id,
        )

        # 渲染器资源加载（IO 密集，放线程池）
        await asyncio.to_thread(Renderer.load_resources)
        self.renderer = Renderer(self.cfg)

        self.downloader = Downloader(self.cfg)
        self.debouncer = Debouncer(self.cfg)
        self.arbiter = EmojiLikeArbiter()
        self.sender = MessageSender(self.cfg, self.renderer)
        self.cleaner = CacheCleaner(self.cfg)

        # 注册解析器
        self._register_parser()

        logger.info(f"[parser] 已加载 v{self.plugin_version}")

    async def on_plugin_unloaded(self) -> None:
        """关闭所有挂起会话与定时任务。"""
        if self.downloader is not None:
            try:
                await self.downloader.close()
            except Exception as e:
                logger.warning(f"[parser] 关闭 downloader 失败: {e}")

        unique_parsers = set(self.parser_map.values())
        for parser in unique_parsers:
            try:
                await parser.close_session()
            except Exception as e:
                logger.warning(f"[parser] 关闭 parser session 失败: {e}")

        if self.cleaner is not None:
            try:
                await self.cleaner.stop()
            except Exception as e:
                logger.warning(f"[parser] 关闭 cleaner 失败: {e}")

    # ------------------------------------------------------------------
    # 解析器注册（与原插件等价）
    # ------------------------------------------------------------------

    def _register_parser(self) -> None:
        """根据 parsers_template.enable 字段创建并注册各平台解析器。"""
        if self.cfg is None or self.downloader is None:
            return

        enabled_platforms = set(self.cfg.parser.enabled_platforms())
        enabled_classes: list[type[BaseParser]] = []
        enabled_names: list[str] = []

        for cls in BaseParser.get_all_subclass():
            platform_name = cls.platform.name
            if platform_name not in enabled_platforms:
                logger.debug(f"[parser] 平台未启用或未配置: {platform_name}")
                continue

            enabled_classes.append(cls)
            enabled_names.append(platform_name)

            parser = cls(self.cfg, self.downloader)
            for keyword, _ in cls._key_patterns:
                self.parser_map[keyword] = parser

        logger.info(
            f"[parser] 启用平台: {'、'.join(enabled_names) if enabled_names else '无'}"
        )

        patterns: list[tuple[str, re.Pattern[str]]] = []
        for cls in enabled_classes:
            for kw, pat in cls._key_patterns:
                patterns.append((kw, pat if hasattr(pat, "search") else re.compile(pat)))
        patterns.sort(key=lambda x: -len(x[0]))
        self.key_pattern_list = patterns

        logger.debug(
            f"[parser] 关键词-正则对已生成: {[kw for kw, _ in patterns]}"
        )

    # ------------------------------------------------------------------
    # 工具方法（供命令组件调用）
    # ------------------------------------------------------------------

    def get_parser_by_type(self, parser_type: type[BaseParser]) -> BaseParser:
        """按类型查找已注册的 parser 实例。"""
        for parser in self.parser_map.values():
            if isinstance(parser, parser_type):
                return parser
        raise ValueError(f"未找到类型为 {parser_type.__name__} 的 parser 实例")

    def search_key_pattern(
        self, text: str
    ) -> tuple[str, re.Match[str]] | None:
        """在文本中按 keyword + 正则搜索，找到第一条匹配。"""
        for kw, pat in self.key_pattern_list:
            if kw not in text:
                continue
            if m := pat.search(text):
                return kw, m
        return None

    def is_session_allowed(self, stream_id: str) -> bool:
        """白名单 / 黑名单过滤。"""
        if self.cfg is None:
            return False
        if self.cfg.whitelist and stream_id not in self.cfg.whitelist:
            return False
        if self.cfg.blacklist and stream_id in self.cfg.blacklist:
            return False
        return True

    def get_bilibili_parser(self) -> BilibiliParser:
        """便捷访问 BilibiliParser，供登录命令使用。"""
        return self.get_parser_by_type(BilibiliParser)  # type: ignore[return-value]

    def get_extra_text(self, message: Any) -> str:
        """从 Neo-MoFox Message 提取可用于 URL 匹配的文本。

        Neo-MoFox 已经把多段消息合并为 ``processed_plain_text``；
        这里同时尝试从 raw_data 里捕获 OneBot v11 json 段（QQ 卡片）。
        """
        text = getattr(message, "processed_plain_text", "") or ""
        if text:
            return text

        content = getattr(message, "content", "")
        if isinstance(content, str) and content:
            return content

        # 尝试抓 OneBot v11 卡片（json 段）
        raw = getattr(message, "raw_data", None)
        if isinstance(raw, dict):
            try:
                from .core.utils import extract_json_url

                segments = raw.get("message")
                if isinstance(segments, list):
                    for seg in segments:
                        if (
                            isinstance(seg, dict)
                            and seg.get("type") == "json"
                            and isinstance(seg.get("data"), dict)
                        ):
                            data = seg["data"].get("data")
                            if isinstance(data, str):
                                if url := extract_json_url(data):
                                    return url
            except Exception:
                pass

        return ""
