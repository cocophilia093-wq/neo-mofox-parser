# 通用链接解析插件 (Parser)

> 原 AstrBot `astrbot_plugin_parser` v1.5.1 → Neo-MoFox 插件迁移版

自动解析 **14 个平台**的链接，渲染信息卡片、下载视频/图片/音频，支持合并转发与多 Bot 仲裁。

## 支持平台

| 平台 | 关键词前缀 |
|---|---|
| Bilibili | `bilibili.com` / `b23.tv` / `B站` / `bili` |
| 抖音 | `douyin.com` |
| 微博 | `weibo.com` / `weibo` |
| 小红书 | `xiaohongshu.com` / `xhslink.com` |
| 知乎 | `zhihu.com` / `zhihu` |
| 快手 | `kuaishou.com` / `kuaishou` |
| Acfun | `acfun.cn` / `acfun` |
| 小黑盒 | `xiaoheihe.com` / `小黑盒` / `max+` |
| YouTube | `youtube.com` / `youtu.be` |
| TikTok | `tiktok.com` |
| Instagram | `instagram.com` |
| Twitter/X | `twitter.com` / `x.com` / `t.co` |
| NetEase Cloud Music | `music.163.com` / `163music` |
| NGA | `nga.cn` / `ngabbs.com` |

## 安装

将 `plugins/parser/` 放入 Neo-MoFox 的 `plugins/` 目录：

```
Neo-MoFox/
└── plugins/
    └── parser/           # ← 整个目录
        ├── manifest.json
        ├── plugin.py
        ├── components/
        ├── core/
        └── resources/
```

启动 Neo-MoFox，框架自动按 `manifest.json` 安装依赖（`yt-dlp` / `apscheduler` / `bilibili-api-python` / `Pillow` 等）。

## 配置

启动后生成 `config/plugins/parser/config.toml`，包含 8 个配置段：

### `[filter]` — 会话过滤

```toml
[filter]
whitelist = []        # 仅处理列表（空=不限制），stream_id 格式 "group_123456"
blacklist = []        # 黑名单列表
```

### `[arbiter]` — 多 Bot 仲裁

```toml
[arbiter]
enable = false        # 开启后多个 Bot 在同群用 Q 表情竞争，只让一个 Bot 解析
```

### `[debounce]` — 去抖

```toml
[debounce]
interval = 30         # 同一链接重复发送时的最小间隔（秒）
```

### `[source]` — 来源限制

```toml
[source]
max_size = 50         # 视频最大大小（MB）
max_minute = 30       # 视频最大时长（分钟）
```

### `[send]` — 发送策略

```toml
[send]
audio_to_file = true               # 音频以文件形式发送（false 则尝试语音）
single_heavy_render_card = false    # 单条消息用重渲染卡
forward_threshold = 5               # 解析结果超过 N 条时走合并转发
show_download_fail_tip = false      # 下载失败时是否提示
```

### `[network]` — 网络

```toml
[network]
download_timeout = 300              # 下载超时（秒）
download_retry_times = 3            # 下载重试次数
common_timeout = 15                 # 通用 HTTP 超时（秒）
proxy = ""                          # 代理地址，如 "http://127.0.0.1:7890"
```

### `[clean]` — 缓存清理

```toml
[clean]
cron = "0 4 * * *"                 # cron 表达式，默认每天凌晨 4 点
```

### `[misc]` — 杂项

```toml
[misc]
timezone = "Asia/Shanghai"
```

### 平台模板（`data/parsers_template.json`）

每个平台单独开关、配置关键词和 cookie：

```json
{
  "parsers_template": [
    {
      "__template_key": "bilibili",
      "enabled": true,
      "match_keys": ["bilibili.com", "b23.tv", "B站", "bili"],
      ...
    }
  ]
}
```

首次启动自动从 `default_template.json` 生成，修改 `data/parsers_template.json` 即时生效。

## 命令

| 命令 | 权限 | 效果 |
|---|---|---|
| `/开启解析` | OPERATOR / OWNER | 当前会话移出黑名单 |
| `/关闭解析` | OPERATOR / OWNER | 当前会话加入黑名单 |
| `/登录B站` / `/登录b站` / `/blogin` | OWNER | 弹出 B 站二维码登录 |

## 使用流程

1. **群/私聊**直接发链接 → 自动匹配关键词 → 解析 → 渲染卡片 → 发送结果
2. **JSON 分享卡**（如 Q 客户端转发）自动提取其中的 URL
3. **视频/图片/音频**自动下载并发送
4. 结果**超过阈值**自动走合并转发（需 napcat 适配器）
5. **多 Bot** 环境下可用仲裁模式避免重复

## 依赖

- `aiohttp` / `aiofiles` — 异步 HTTP 与文件
- `yt-dlp` — 视频/音频下载
- `msgspec` — 高性能序列化
- `tqdm` — 下载进度
- `apscheduler` — 缓存定时清理
- `bilibili-api-python` / `curl_cffi` — B 站 API
- `Pillow` — 图片渲染
- `apilmoji` — Emoji 处理

## 架构

```
plugins/parser/
├── manifest.json                  # 插件元数据
├── plugin.py                      # 入口，生命周期管理
├── default_template.json          # 平台模板默认值
├── components/
│   ├── message_handler.py         # 消息事件处理器（核心入口）
│   ├── open_parser_command.py     # /开启解析 命令
│   ├── close_parser_command.py    # /关闭解析 命令
│   └── blogin_command.py          # /登录B站 命令
├── core/
│   ├── config.py                  # BaseConfig + PluginConfig
│   ├── sender.py                  # 消息发送（含合并转发）
│   ├── render.py                  # 渲染引擎
│   ├── download.py                # 下载器（yt-dlp）
│   ├── arbiter.py                 # 多 Bot 仲裁
│   ├── clean.py                   # 缓存清理（apscheduler）
│   ├── debounce.py                # 去抖
│   ├── parsers/                   # 14 个平台解析器
│   │   ├── bilibili/              #   - 视频/直播/专栏/动态/合集/收藏
│   │   ├── douyin/                #   - 视频/图文
│   │   ├── zhihu/                 #   - 内容/卡片
│   │   ├── weibo.py / xhs.py / ... (其他平台)
│   │   └── example.py             # 解析器开发参考
│   ├── cookie.py                  # Cookie 管理
│   ├── data.py                    # 数据模型
│   ├── utils.py                   # 工具函数（JSON 卡 URL 提取等）
│   └── exception.py               # 异常定义
└── resources/
    ├── HYSongYunLangHeiW-1.ttf    # 渲染字体
    ├── media_button.png           # 媒体按钮图
    └── logos/                     # 各平台 Logo
```

## 排错

- **依赖安装失败**：手动 `uv pip install yt-dlp apscheduler apilmoji bilibili-api-python curl_cffi`
- **B 站解析失败**：未登录，执行 `/登录B站` 扫码登录
- **合并转发无效**：需要 napcat 适配器，其他适配器自动降级直发
- **资源找不到**：检查 `resources/` 目录是否正确，字体文件 `HYSongYunLangHeiW-1.ttf` 必须存在
- **日志查看**：Neo-MoFox `logs/` 目录下的 `parser` 日志