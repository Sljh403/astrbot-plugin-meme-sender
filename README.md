# astrbot-plugin-meme-sender

AstrBot 表情包 / 本地图库自动配图插件。它会读取聊天上下文，从图库中挑选与当前回复语义、情绪和话题适配的图片或表情包，追加在文字回复后面发送；不替换、不删除原有文字。

## 功能介绍

- **语境与情绪双评分**：分别识别用户情绪和 bot 即将发送的回复情绪；图片只按 **bot 情绪 + 语境** 评分，不会因为用户生气就挑一张生气的图。
- **固定话题直发**：晚安、早安、再见、生日快乐等强绑定话题的表情，在语境命中且不与 bot 情绪明显冲突时直接发送，并带冷却防刷屏。
- **多图优先级**：多个候选同时达标时，先比综合分，再比使用次数（少的优先），最后随机；避免总是同一张图。
- **手动标签优先**：`/media add`、`/media import` 可直接写情绪、作用、情境、固定话题；手动标签素材不会调用图片分析模型。
- **动态标签库**：不限制固定情绪/作用列表，模型输出的新标签会自动写入本地标签库，可在状态里查看数量。
- **图库管理**：聊天中直接添加、服务器路径导入、查看、试发、删除、重新分析；SHA-256 去重。
- **表情包同步**：从 SnowLuma / OneBot 网关同步账号收藏表情，支持保留 QQ 原生表情样式发送。
- **图文联合分析**：提取图中文字（OCR）与画面内容，判断真实含义、情绪、作用、推荐情境，结构化写入 SQLite。
- **文字优先**：图片只追加到回复消息链，永远不会替换或删除 AI 已经生成的文字。
- **默认模型**：插件采用智能默认模型选取方式，无需手动干预。不绑定 Provider/模型名，跟随 AstrBot 当前会话的全局模型；失败时退化为本地启发式标注。

## 工作原理

```text
图片入库
  ├─ 聊天图片 / 服务器路径 / 收藏表情
  ├─ 文件校验 + SHA-256 去重
  ├─ 手动标签？──是──> 直接写入手动标签，跳过模型
  └─ 否 ──> 写入 media.db，保存原图
        ↓
自动触发分析（仅未手动标注的素材）
  ├─ OCR：逐字提取图中文字
  ├─ 画面：场景、人物、物体、氛围
  ├─ 语义：真实含义、情绪、作用、推荐情境
  └─ 固定话题：trigger_topic（如 晚安 / 早安 / 生日快乐）
        ↓
结构化存储
  ├─ description / ocr_text / text_meaning
  ├─ scene / interpretation / role
  ├─ scenarios / emotions / keywords / trigger_topic / safe
  └─ is_manual_tagged / usage_count / analysis_source
        ↓
上下文检索匹配
  ├─ 识别用户情绪
  ├─ 推导 bot 回复情绪
  ├─ 固定话题命中检查（带冷却）
  ├─ 语境分 + 情绪分 + 基础分
  └─ 多图排序：综合分 > 使用次数 > 随机
        ↓
决策发送
  ├─ 话题命中且不冲突 -> 直接发送
  ├─ 语境或情绪 >= 0.80 -> 直接发送
  ├─ 综合分 > 0.40 -> 80% 概率发送
  └─ 否则不配图
```

## 安装

1. 将本目录放入 AstrBot 插件目录，例如：

   ```text
   AstrBot/data/plugins/astrbot_plugin_airz_media/
   ```

2. 在 AstrBot 管理面板启用插件，或将插件名加入 `plugin_set`：

   ```json
   ["airz_media"]
   ```

3. 重载插件，确认日志出现：

   ```text
   [AIRZ-MEDIA] loaded gallery=... stickers=... db=...
   ```

4. 发送 `/media status` 检查图库、标签库和发送规则。

## 命令

| 命令 | 说明 |
| --- | --- |
| `/media add [描述]` | 带图发送，把图片加入图库；可附加手动标签 |
| `/media import <路径> [标签]` | 从服务器路径导入图片 |
| `/media list [image\|sticker]` | 查看素材、OCR、含义、话题、手动标记 |
| `/media sync [数量]` | 同步账号收藏表情（需网关支持） |
| `/media send <id>` | 试发一个素材 |
| `/media delete <id>` | 删除素材 |
| `/media status` | 查看图库、动态标签库、权重、阈值、话题冷却 |
| `/media on` / `/media off` | 开关自动配图 |
| `/media reanalyze` | 重新分析未手动标注的素材 |
| `/media help` | 查看帮助 |

`/表情`、`/图库` 是 `/media` 的别名。

## 手动标签

手动标签优先于模型分析；只要出现任意一个手动标签，入库时就不会调用图片分析模型。

```text
/media add 猫猫偷笑 --emotion 开心,害羞 --role 撒娇 --scenario 对方说晚安时 --topic 晚安
/media import /data/pics/night.png --emotion 温柔 --role 晚安问候 --scenario 睡前聊天 --topic 晚安
```

支持的参数：

| 参数 | 含义 | 示例 |
| --- | --- | --- |
| `--emotion` / `--情绪` | 情绪标签，可多个，逗号分隔 | `--emotion 开心,害羞` |
| `--role` / `--作用` | 图片作用/使用方式 | `--role 撒娇` |
| `--scenario` / `--情境` | 推荐使用情境，可多个 | `--scenario 对方说晚安时` |
| `--topic` / `--话题` | 固定触发话题，可多个 | `--topic 晚安` |

参数值包含空格时请用英文双引号包起来，例如 `--scenario "对方说晚安时"`。没有手动标签时，才自动调用图片分析模型。

## 配置

配置文件位置：

```text
AstrBot/data/plugin_data/airz_media/config.json
```

默认配置：

```json
{
  "enabled": true,
  "auto_send": true,
  "gallery_dir": "AstrBot/data/plugin_data/airz_media/gallery",
  "sticker_dir": "AstrBot/data/plugin_data/airz_media/stickers",
  "sticker_sync_count": 20,
  "native_sticker": true,
  "min_score": 0.40,
  "base_weight": 0.25,
  "context_weight": 0.45,
  "emotion_weight": 0.30,
  "send_probability": 0.80,
  "direct_fit_threshold": 0.80,
  "allow_group": true,
  "allow_private": true,
  "max_image_mb": 10,
  "safe_filter": false,
  "llm_context": true,
  "llm_timeout": 8.0,
  "topic_trigger": true,
  "topic_cooldown_seconds": 300,
  "owner_ids": [],
  "vision": {
    "timeout": 45
  }
}
```

| 配置项 | 说明 |
| --- | --- |
| `gallery_dir` / `sticker_dir` | 图库/表情缓存目录 |
| `min_score` | 分数兜底阈值，默认 `0.40`；必须严格大于才进入概率发送 |
| `base_weight` | 基础分权重，默认 `0.25` |
| `context_weight` | 语境分权重，默认 `0.45` |
| `emotion_weight` | 情绪分权重，默认 `0.30` |
| `send_probability` | 分数兜底发送概率，默认 `0.80` |
| `direct_fit_threshold` | 语境/情绪直发阈值，默认 `0.80` |
| `llm_context` | 是否调用模型做语境/情绪评分，默认 `true` |
| `llm_timeout` | 评分模型超时秒数，默认 `8.0` |
| `topic_trigger` | 是否启用固定话题直发，默认 `true` |
| `topic_cooldown_seconds` | 同一话题直发冷却秒数，默认 `300` |
| `vision.timeout` | 图片分析超时秒数，默认 `45` |

**关于分析模型**：插件采用智能默认模型选取方式，无需手动干预。代码和配置不包含任何分析模型绑定项；运行时使用 AstrBot 当前会话的默认 Provider，失败则使用本地启发式标注。

## 发送规则

### 1. 基础分

```text
base = 0.25 * emotion
     + 0.15 * keyword
     + 0.25 * semantic
     + 0.25 * scenario
     + 0.10 * role
     + 0.04 * sticker
```

- `emotion`：图片情绪与 **bot 情绪** 的重合度。
- `keyword`：图片关键词在回复文字/上下文中的命中率。
- `semantic`：真实含义、画面、OCR 与回复文字/上下文的最大短句相似度。
- `scenario`：推荐情境的最大短句相似度。
- `role`：图片作用是否出现在回复文字/上下文中。

### 2. 语境分与情绪分

```text
context = 0.70 * llm_context + 0.30 * heuristic_context
emotion = 0.70 * llm_emotion + 0.30 * heuristic_emotion
```

模型分是主信号；模型不可用时完全退化为启发分，不会用 0 分伪造。

### 3. 综合分

```text
final = (0.25 * base + 0.45 * context + 0.30 * emotion) / (0.25 + 0.45 + 0.30)
```

### 4. 决策顺序

1. **固定话题**：上下文命中图片的 `trigger_topic`，且图片情绪与 bot 情绪不严重相反 -> 直接发送；同一话题在冷却时间内不重复触发。
2. **语境直发**：`context >= 0.80`。
3. **情绪直发**：`emotion >= 0.80`。
4. **多图排序**：综合分降序 -> 使用次数少的优先 -> 随机。
5. **分数兜底**：最高综合分 `> 0.40` 时，以 `0.80` 概率发送；否则不配图。

语境或情绪达到直发阈值时忽略分数和概率；分数兜底才使用概率。发送次数和发送间隔不设上限。

## 安全与隐私

- 插件不保存任何 API Key、Provider Token 或模型名。
- 图片、SQLite 数据库、配置文件均在 AstrBot 本地数据目录。
- `.gitignore` 默认排除 `config.json`、`*.db`、`gallery/`、`stickers/`、日志、`.env`、`*.pem` 和密钥文件。
- 图片中的文字只作为语义数据，不会被执行。

## 降级行为

- **模型不可用/超时**：跳过该次模型评分，使用本地启发式；不会阻断文字回复。
- **图片分析失败**：写入 `analysis_source=heuristic`、`ocr_status=failed`，仍可按关键词/文件名参与匹配。
- **手动标签素材**：`analysis_source=manual`、`ocr_status=manual`，不调用图片分析模型。

## 更新日志

见 [CHANGELOG.md](CHANGELOG.md)。

## 许可

MIT

## 声明

该插件支持任意修改，如修改发送逻辑判定，分析模型选取等。转载需附带原地址。
