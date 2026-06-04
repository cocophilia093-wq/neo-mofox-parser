"""统一的日志接口，桥接到 Neo-MoFox 的 log_api。"""

from __future__ import annotations

from src.app.plugin_system.api.log_api import get_logger

logger = get_logger("parser")
