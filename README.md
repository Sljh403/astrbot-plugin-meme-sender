# astrbot-plugin-meme-sender

[![Release](https://img.shields.io/github/v/release/Sljh403/astrbot-plugin-meme-sender?label=release&color=blue)](https://github.com/Sljh403/astrbot-plugin-meme-sender/releases)
[![Downloads](https://img.shields.io/github/downloads/Sljh403/astrbot-plugin-meme-sender/total?label=downloads)](https://github.com/Sljh403/astrbot-plugin-meme-sender/releases)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

AstrBot 表情包 / 本地图库自动配图插件。它会读取聊天上下文，从图库中挑选与当前回复语义适配的图片或表情包，追加在文字回复后面发送；不替换、不删除原有文字。

## 功能介绍

- **上下文自动配图**：根据最近对话和本次即将发送的回复文字，自动判断是否需要配图，并从图库中挑选语义适配的素材。
- **图库管理**：支持聊天中直接添加图片、从服务器路径导入、查看、试发、删除、重新分析；使用 SHA-256 校验内容并自动去重。
- **表情包同步**：可从 SnowLuma / OneBot 网关同步账号收藏表情，支持保留 QQ 原生表情样式发送。
- **图文联合分析**：提取图片中的文字（OCR）和画面内容，判断图片的真实含义、情绪、作用及推荐使用情境，结构化写入 SQLite。
- **文字优先**：图片只会追加到回复消息链，永远不会替换或删除 AI 已经生成的文字。
- **默认模型选取**：插件采用智能默认模型选取方式，无需手动干预，自动跟随 AstrBot 当前会话的全局默认模型。

## 工作原理

```text
图片入库
  ├─ 聊天图片 / 服务器路径 / 收藏表情
  ├─ 文件校验 + SHA-256 去重
  └─ 写入 media.db，保存原图
        ↓
自动触发分析
  ├─ OCR：提取图中文字
  ├─ 画面：场景、人物、物体、氛围
  └─ 语义：真实含义、情绪、作用、推荐情境
        ↓
结构化存储
  ├─ description / ocr_text / text_meaning
  ├─ scene / interpretation / role
  └─ scenarios / emotions / keywords / safe
        ↓
上下文检索匹配
  ├─ 基础分：情绪、关键词、画面/含义、情境、作用
  ├─ 语境分：回复文字与候选含义的语义适配
  └─ 加权得到最终分
        ↓
决策发送
  ├─ final >= min_score：追加图片并发送
  └─ 未达标：只发送文字
```

分析失败不会阻塞入库或聊天：插件会退回文件名/描述规则标注，并在素材上标记识别失败。

## 下载安装

### 方式一：GitHub Release（推荐）

前往 [Releases](https://github.com/Sljh403/astrbot-plugin-meme-sender/releases) 下载最新的插件包：

| 格式 | 说明 |
| --- | --- |
| `airz_media-<版本>.zip` | Windows 推荐 |
| `airz_media-<版本>.tar.gz` | Linux 推荐 |

解压后得到一个 `airz_media` 目录，放入 AstrBot 插件目录即可：

```text
<AstrBot 数据目录>/plugins/airz_media/
├── main.py
├── metadata.yaml
└── README.md
```

### 方式二：命令行下载

```bash
# 下载最新 Release 的 zip（需要 gh 或 curl）
gh release download --repo Sljh403/astrbot-plugin-meme-sender --pattern "*.zip"

# 或直接下载源码压缩包
curl -L -o astrbot-plugin-meme-sender.zip   https://github.com/Sljh403/astrbot-plugin-meme-sender/archive/refs/heads/main.zip
```

### 方式三：Git 克隆

```bash
git clone https://github.com/Sljh403/astrbot-plugin-meme-sender.git
```

把仓库内容复制到插件目录并重命名为 `airz_media`。

## 从源码安装

把仓库放到 AstrBot 插件目录，例如：

```bash
<AstrBot 数据目录>/plugins/astrbot_plugin_airz_media/
```

确认 `/AstrBot/data/cmd_config.json` 的 `plugin_set` 中包含插件名：

```json
"plugin_set": ["airz_media"]
```

重启 AstrBot：

```bash
docker restart astrbot
docker logs --tail 100 astrbot | grep -E "airz_media|AIRZ-MEDIA"
```

看到 `Plugin airz_media (1.4.1)` 和 `[AIRZ-MEDIA] loaded` 即加载成功。

## 配置

首次加载会在 AstrBot 数据目录生成 `config.json`：

```text
/AstrBot/data/plugin_data/airz_media/config.json
```

默认配置：

```json
{
  "enabled": true,
  "auto_send": true,
  "gallery_dir": "/AstrBot/data/plugin_data/airz_media/gallery",
  "sticker_dir": "/AstrBot/data/plugin_data/airz_media/stickers",
  "sticker_sync_count": 20,
  "native_sticker": true,
  "min_score": 0.30,
  "context_weight": 0.70,
  "allow_group": true,
  "allow_private": true,
  "max_image_mb": 10,
  "safe_filter": false,
  "llm_context": true,
  "llm_timeout": 6.0,
  "owner_ids": [],
  "vision": {
    "timeout": 45
  }
}
```

关键项：

| 配置 | 说明 |
| --- | --- |
| `enabled` / `auto_send` | 插件总开关 / 自动配图开关 |
| `gallery_dir` | 本地图库目录；默认在插件数据目录下 |
| `sticker_dir` | 收藏表情缓存目录 |
| `sticker_sync_count` | 每次同步收藏表情的数量，默认 20，最大 50 |
| `native_sticker` | 表情包是否走 SnowLuma 原生 `send_custom_face` 发送 |
| `min_score` | 统一发送阈值，默认 `0.30`；达到才配图 |
| `context_weight` | 语境分权重，默认 `0.70`；基础分占其余部分 |
| `allow_group` / `allow_private` | 是否允许群聊 / 私聊自动配图 |
| `max_image_mb` | 单张图片大小上限，默认 10MB |
| `safe_filter` | 为 `true` 时跳过被标记为不安全的素材 |
| `llm_context` | 是否让模型给候选打语义适配分；关闭后仅用本地启发分 |
| `llm_timeout` | 模型评分超时，默认 6 秒 |
| `owner_ids` | 可管理图库的用户 ID 列表 |
| `vision.timeout` | 图片分析超时，默认 45 秒 |

### 发送频率

当前版本**不限制发送次数和发送间隔**，只要最终分达到 `min_score` 就会配图。若需要限流，建议在 AstrBot 的会话策略或 QQ 网关层处理，避免插件内部再引入一套不一致的频率状态。

### 图库路径

宿主机默认对应：

```text
<AstrBot 数据目录>/plugin_data/airz_media/
├── config.json
├── media.db
├── gallery/
└── stickers/
```

可以直接把图片放进 `gallery/`，然后执行 `/media sync` 或重启插件让插件扫描；更推荐用 `/media add` 或 `/media import`，这样会自动校验、去重并触发分析。

## 指令

`/media`、`/表情`、`/图库` 是等价的三个入口。

| 指令 | 说明 |
| --- | --- |
| `/media add [描述]` | 和图片一起发送，把图片加入图库；描述可选 |
| `/media import <服务器路径>` | 从服务器图片路径导入 |
| `/media list [image\|sticker]` | 查看最近素材 |
| `/media sync [数量]` | 同步账号收藏表情 |
| `/media send <id>` | 试发某个素材 |
| `/media delete <id>` | 删除素材 |
| `/media status` | 查看数量、目录、开关和发送规则 |
| `/media on` / `/media off` | 开关自动配图 |
| `/media reanalyze` | 重新分析最近 30 个素材 |

群聊中只有管理员或 `owner_ids` 中的用户可以管理素材；私聊默认允许本人管理。

### 添加一张图

在聊天中发送图片，并附带指令：

```text
/media add 猫猫开心
```

插件会：

1. 校验文件类型和大小；
2. 计算 SHA-256，已存在则更新描述，不重复入库；
3. 复制到 `gallery/`；
4. 调用默认视觉模型分析文字和画面；
5. 写入结构化标签，之后参与上下文匹配。

## 安全

- 插件不读取、不存储、不提交任何模型 API Key。
- 模型密钥由 AstrBot 自身通过环境变量或面板安全配置；仓库和插件配置中不出现真实密钥。
- `config.json`、`media.db`、`gallery/`、`stickers/` 均属于运行时数据，不应提交到公开仓库。
- 仓库 `.gitignore` 已排除配置、数据库、图片、日志、虚拟环境和本地密钥文件。
- 图片分析结果只作为配图依据，不会执行图片中的文字或指令。

## 降级策略

- **图库为空**：不配图，只发送文字；`/media status` 会提示素材库为空。
- **图片分析失败**：退回文件名/描述规则标注，标记 `ocr_status=failed`，不阻塞入库和聊天。
- **默认模型不可用**：跳过模型分析，使用本地启发式标注；自动配图仍可继续，但更依赖已有标签。
- **模型评分超时**：按本地启发分计算，达不到阈值就不配图，避免错误配图。
- **图片文件丢失**：素材在检索阶段被跳过，不影响文字回复。
- **原生表情发送失败**：记录日志并跳过该次表情发送，文字不受影响。

## 兼容性

- AstrBot `>=4.29.0,<5.0.0`
- OneBot v11 / aiocqhttp
- SnowLuma 收藏表情扩展 action 为可选能力；没有该能力时，普通图片和本地图库仍可正常使用。

## 开发检查

```bash
python -m py_compile main.py
```

修改后重启 AstrBot，并确认日志出现：

```text
Plugin airz_media (1.4.1)
[AIRZ-MEDIA] loaded
```

## 声明

> 该插件支持任意修改，如修改发送逻辑判定，分析模型选取等。转载需附带原地址。

原地址：https://github.com/Sljh403/astrbot-plugin-meme-sender
