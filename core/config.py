"""parser 插件配置定义。

包含两部分：
1. ParserBaseConfig：Neo-MoFox BaseConfig，存储标准字段（toml）
2. ParserItem / ParserConfigContainer：动态读取 parsers_config.json 的解析器配置
3. PluginConfig：运行时配置容器（统一封装路径 / parser map / 派生字段），
   是 core 模块统一访问入口
"""

from __future__ import annotations

import json
import zoneinfo
from pathlib import Path
from typing import Any, ClassVar

from src.app.plugin_system.base import BaseConfig, Field, SectionBase, config_section

from ._log import logger


# ============================================================================
# 1. BaseConfig：基础字段（toml）
# ============================================================================


class ParserBaseConfig(BaseConfig):
    """parser 插件主配置。

    存储除 parsers_template 外的所有标量配置；
    parsers_template 仍以独立 JSON 文件保存（动态平台 schema）。
    """

    config_name: ClassVar[str] = "config"
    config_description: ClassVar[str] = "通用链接解析插件主配置"

    @config_section("filter")
    class FilterSection(SectionBase):
        """白/黑名单与解析开关。"""

        whitelist: list[str] = Field(
            default_factory=list,
            description="解析白名单。填写 stream_id；非空时仅这些会话生效，优先级高于黑名单",
        )
        blacklist: list[str] = Field(
            default_factory=list,
            description="解析黑名单。填写 stream_id，可在会话中用 /开启解析、/关闭解析 修改",
        )

    @config_section("arbiter")
    class ArbiterSection(SectionBase):
        """多 Bot 仲裁机制开关。"""

        enable: bool = Field(
            default=True,
            description="基于 QQ 表情贴的多 Bot 仲裁机制；保持开启可避免群内多 Bot 重复解析",
        )

    @config_section("debounce")
    class DebounceSection(SectionBase):
        """防抖。"""

        interval: int = Field(
            default=300,
            description="同一会话内重复链接的防抖秒数，0 表示禁用",
        )

    @config_section("source")
    class SourceSection(SectionBase):
        """资源限制。"""

        max_size: int = Field(
            default=90,
            description="允许下载的音视频最大体积，单位 MB",
        )
        max_minute: int = Field(
            default=15,
            description="允许下载的音视频最大时长，单位分钟",
        )

    @config_section("send")
    class SendSection(SectionBase):
        """发送策略。"""

        audio_to_file: bool = Field(
            default=True,
            description="是否将音频以文件形式上传，而不是语音形式",
        )
        single_heavy_render_card: bool = Field(
            default=False,
            description="单条重媒体（视频/音频/文件）是否额外渲染信息卡片",
        )
        forward_threshold: int = Field(
            default=2,
            description="生成消息条数达到该阈值则合并转发",
        )
        show_download_fail_tip: bool = Field(
            default=True,
            description="是否发送下载失败相关提示",
        )

    @config_section("network")
    class NetworkSection(SectionBase):
        """网络相关。"""

        download_timeout: int = Field(
            default=280,
            description="下载请求超时秒数",
        )
        download_retry_times: int = Field(
            default=2,
            description="下载失败重试次数",
        )
        common_timeout: int = Field(
            default=15,
            description="普通请求超时秒数",
        )
        proxy: str = Field(
            default="",
            description="代理地址，例如 http://127.0.0.1:7890；留空直连",
        )

    @config_section("clean")
    class CleanSection(SectionBase):
        """缓存清理。"""

        cron: str = Field(
            default="30 2 * * *",
            description="自动清理缓存的 Cron 表达式（分 时 日 月 周），留空禁用",
        )

    @config_section("misc")
    class MiscSection(SectionBase):
        """杂项。"""

        timezone: str = Field(
            default="Asia/Shanghai",
            description="调度器时区",
        )

    filter: FilterSection = Field(default_factory=FilterSection)
    arbiter: ArbiterSection = Field(default_factory=ArbiterSection)
    debounce: DebounceSection = Field(default_factory=DebounceSection)
    source: SourceSection = Field(default_factory=SourceSection)
    send: SendSection = Field(default_factory=SendSection)
    network: NetworkSection = Field(default_factory=NetworkSection)
    clean: CleanSection = Field(default_factory=CleanSection)
    misc: MiscSection = Field(default_factory=MiscSection)


# ============================================================================
# 2. parsers_template 配置（动态字段）
# ============================================================================


class ParserItem:
    """单个平台解析器的配置项。

    与 BaseConfig 解耦，直接基于 dict 提供属性访问；
    支持的字段视各平台 default_template.json 而定。
    """

    __slots__ = ("_data",)

    def __init__(self, data: dict[str, Any]):
        self._data = data

    @property
    def name(self) -> str:
        return str(self._data.get("__template_key", ""))

    def __getattr__(self, key: str) -> Any:
        if key.startswith("_"):
            raise AttributeError(key)
        if key in self._data:
            return self._data[key]
        # 未配置字段，返回 None，避免抛错
        return None


class ParserConfigContainer:
    """parsers_template list[dict] 的 dict 视图。"""

    def __init__(self, nodes: list[dict[str, Any]]):
        self._nodes: dict[str, ParserItem] = {}
        for node in nodes:
            key = node.get("__template_key")
            if not key:
                logger.warning(f"[parser] template 缺少 __template_key，已跳过: {node}")
                continue
            if key in self._nodes:
                logger.warning(f"[parser] template {key} 重复，覆盖")
            self._nodes[key] = ParserItem(node)

    def __getattr__(self, name: str) -> ParserItem:
        if name.startswith("_"):
            raise AttributeError(name)
        if name in self._nodes:
            return self._nodes[name]
        raise AttributeError(name)

    def platforms(self) -> list[str]:
        return list(self._nodes.keys())

    def enabled_platforms(self) -> list[str]:
        return [k for k, v in self._nodes.items() if getattr(v, "enable", True)]


# ============================================================================
# 3. PluginConfig：运行时聚合容器
# ============================================================================


class PluginConfig:
    """运行时聚合配置容器（core 模块的统一访问入口）。

    属性映射保持与原 AstrBot 版本完全兼容：
    - whitelist / blacklist / arbiter / debounce_interval
    - source_max_size / source_max_minute / max_size / max_duration
    - audio_to_file / single_heavy_render_card / forward_threshold
    - show_download_fail_tip
    - download_timeout / download_retry_times / common_timeout / proxy
    - clean_cron / parser / parsers_template
    - data_dir / plugin_dir / cache_dir / cookie_dir
    - timezone / emoji_cdn / emoji_style / admins_id
    """

    _PLUGIN_NAME = "parser"

    def __init__(
        self,
        base: ParserBaseConfig,
        *,
        plugin_dir: Path,
        data_dir: Path,
        admins_id: list[str] | None = None,
    ):
        self.base = base

        # ---------- 基础字段 ----------
        self.whitelist: list[str] = list(base.filter.whitelist)
        self.blacklist: list[str] = list(base.filter.blacklist)

        self.arbiter: bool = bool(base.arbiter.enable)

        self.debounce_interval: int = int(base.debounce.interval)

        self.source_max_size: int = int(base.source.max_size)
        self.source_max_minute: int = int(base.source.max_minute)
        self.max_duration: int = self.source_max_minute * 60
        self.max_size: int = self.source_max_size * 1024 * 1024

        self.audio_to_file: bool = bool(base.send.audio_to_file)
        self.single_heavy_render_card: bool = bool(base.send.single_heavy_render_card)
        self.forward_threshold: int = int(base.send.forward_threshold)
        self.show_download_fail_tip: bool = bool(base.send.show_download_fail_tip)

        self.download_timeout: int = int(base.network.download_timeout)
        self.download_retry_times: int = int(base.network.download_retry_times)
        self.common_timeout: int = int(base.network.common_timeout)
        self.proxy: str | None = base.network.proxy or None

        self.clean_cron: str = base.clean.cron

        # ---------- 时区 ----------
        try:
            self.timezone = zoneinfo.ZoneInfo(base.misc.timezone or "Asia/Shanghai")
        except Exception:
            self.timezone = zoneinfo.ZoneInfo("Asia/Shanghai")

        # ---------- 内置常量 ----------
        self.emoji_cdn = (
            "https://cdn.jsdelivr.net/npm/emoji-datasource-facebook@14.0.0/img/facebook/64/"
        )
        self.emoji_style = "FACEBOOK"

        # ---------- 路径 ----------
        self.plugin_dir = plugin_dir
        self.data_dir = data_dir
        self.cache_dir = self.data_dir / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cookie_dir = self.data_dir / "cookies"
        self.cookie_dir.mkdir(parents=True, exist_ok=True)

        # parsers_template 单独存储为 JSON
        self._parsers_template_file = self.data_dir / "parsers_template.json"
        self._default_template_file = self.plugin_dir / "default_template.json"

        # ---------- 解析器模板 ----------
        self.parsers_template: list[dict[str, Any]] = self._load_parsers_template()
        self.parser = ParserConfigContainer(self.parsers_template)

        # ---------- 主人 / 管理员 ID ----------
        self.admins_id: list[str] = list(admins_id or [])

    # ---------- parsers_template I/O ----------

    def _load_parsers_template(self) -> list[dict[str, Any]]:
        # 用户层文件优先
        if self._parsers_template_file.exists():
            try:
                with self._parsers_template_file.open(encoding="utf-8-sig") as f:
                    return json.loads(f.read())
            except Exception as e:
                logger.error(f"[parser] 加载 parsers_template 失败，回退默认: {e}")

        # 回退到插件随附的 default_template.json
        try:
            with self._default_template_file.open(encoding="utf-8-sig") as f:
                template = json.loads(f.read())
                logger.info(f"[parser] 加载默认模板: {self._default_template_file}")
        except Exception as e:
            logger.error(f"[parser] 加载默认模板失败: {e}")
            template = []

        # 持久化默认模板到用户目录
        self._save_parsers_template(template)
        return template

    def _save_parsers_template(self, template: list[dict[str, Any]]) -> None:
        try:
            self._parsers_template_file.parent.mkdir(parents=True, exist_ok=True)
            self._parsers_template_file.write_text(
                json.dumps(template, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.error(f"[parser] 保存 parsers_template 失败: {e}")

    # ---------- 黑名单 I/O ----------

    def _save_blacklist(self) -> None:
        """把当前黑名单回写到 BaseConfig 并落盘。"""
        try:
            self.base.filter.blacklist = list(self.blacklist)
            path = self.base.get_default_path()
            if path is not None:
                self.base.save(path)
        except Exception as e:
            logger.error(f"[parser] 保存黑名单失败: {e}")

    def add_blacklist(self, stream_id: str) -> None:
        if stream_id not in self.blacklist:
            self.blacklist.append(stream_id)
            self._save_blacklist()

    def remove_blacklist(self, stream_id: str) -> None:
        if stream_id in self.blacklist:
            self.blacklist.remove(stream_id)
            self._save_blacklist()
