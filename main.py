from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import random
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

import httpx
from PIL import Image as PILImage

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Plain, Record
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

PLUGIN_NAME = "airz_media"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
FORMAT_EXT = {
    "JPEG": ".jpg",
    "MPO": ".jpg",
    "PNG": ".png",
    "GIF": ".gif",
    "WEBP": ".webp",
}
MIME_BY_EXT = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
IMAGE_MAX_PIXELS = 50_000_000

EMOTION_LEXICON: dict[str, list[str]] = {
    "开心": ["开心", "高兴", "快乐", "哈哈", "笑死", "好耶", "太棒", "喜欢", "可爱", "嘿嘿", "乐"],
    "无语": ["无语", "服了", "离谱", "难绷", "沉默", "呃", "6", "……", "..."],
    "疑惑": ["疑惑", "为什么", "怎么", "吗", "？", "?", "不懂", "啥", "真的假的"],
    "难过": ["难过", "伤心", "哭", "累", "烦", "emo", "委屈", "不想"],
    "鼓励": ["加油", "抱抱", "别怕", "没事", "支持", "你可以", "会好的", "拍拍"],
    "调侃": ["调侃", "狗头", "乐", "笑死", "绷不住", "阴阳", "整活", "就这"],
    "生气": ["生气", "气死", "怒", "滚", "别烦", "讨厌"],
    "惊讶": ["惊讶", "卧槽", "竟然", "居然", "天啊", "没想到", "震惊"],
    "困倦": ["困", "睡", "晚安", "睡觉", "熬夜", "想睡"],
    "害羞": ["害羞", "脸红", "不好意思", "羞"],
    "温柔": ["温柔", "轻声", "抱抱", "安慰"],
    "委屈": ["委屈", "欺负", "不理我"],
}
STOPWORDS = {
    "图片",
    "表情",
    "表情包",
    "照片",
    "截图",
    "聊天",
    "配图",
    "image",
    "images",
    "img",
    "pic",
    "gif",
    "png",
    "jpg",
    "jpeg",
    "webp",
    "test",
    "测试",
    "未命名",
    "文件",
}
# 只用于给旧素材做兜底推断；新素材的 trigger_topic 由分析模型自由输出。
TOPIC_TRIGGERS = (
    "晚安",
    "早安",
    "再见",
    "拜拜",
    "生日快乐",
    "新年快乐",
    "加油",
    "抱抱",
    "谢谢",
    "对不起",
)


def _db_default(root: Path) -> dict[str, Any]:
    return {
        "enabled": True,
        "auto_send": True,
        "gallery_dir": str(root / "gallery"),
        "sticker_dir": str(root / "stickers"),
        "sticker_sync_count": 20,
        "native_sticker": True,
        "min_score": 0.40,
        "base_weight": 0.25,
        "context_weight": 0.45,
        "emotion_weight": 0.30,
        "send_probability": 0.80,
        "direct_fit_threshold": 0.80,
        "allow_group": True,
        "allow_private": True,
        "max_image_mb": 10,
        "safe_filter": False,
        "llm_context": True,
        "llm_timeout": 8.0,
        "topic_trigger": True,
        "topic_cooldown_seconds": 300,
        "owner_ids": [],
        "vision": {"timeout": 45},
    }


def _merge_config(default: dict[str, Any], loaded: dict[str, Any]) -> dict[str, Any]:
    out = dict(default)
    for key, value in loaded.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge_config(out[key], value)
        else:
            out[key] = value
    return out


def _json_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(x) for x in value if str(x).strip()]
    try:
        parsed = json.loads(str(value or "[]"))
        if isinstance(parsed, list):
            return [str(x) for x in parsed if str(x).strip()]
    except Exception:
        pass
    return []


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict):
                text = item.get("text")
                if text:
                    parts.append(str(text))
            elif item:
                parts.append(str(item))
        return " ".join(parts)
    if value is None:
        return ""
    return str(value)


def _grams(text: str) -> set[str]:
    clean = re.sub(r"\s+", "", text or "")
    if len(clean) < 2:
        return {clean} if clean else set()
    return {clean[i : i + 2] for i in range(len(clean) - 1)}


def _dice(a: str, b: str) -> float:
    ga, gb = _grams(a), _grams(b)
    if not ga or not gb:
        return 0.0
    return 2.0 * len(ga & gb) / (len(ga) + len(gb))


def _clean_summary(text: str, limit: int = 300) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _unique(items: list[str], limit: int = 12) -> list[str]:
    out: list[str] = []
    for item in items:
        value = str(item or "").strip()
        if value and value not in out:
            out.append(value)
    return out[:limit]


def _labels(value: Any, limit: int = 8) -> list[str]:
    """接受字符串 / JSON 数组 / 逗号分隔文本，保留模型自由生成的标签。"""
    if isinstance(value, list):
        raw = value
    elif isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                parsed = json.loads(text)
                raw = parsed if isinstance(parsed, list) else [text]
            except Exception:
                raw = re.split(r"[,，、;；/|]+", text)
        else:
            raw = re.split(r"[,，、;；/|]+", text)
    else:
        raw = []
    return _unique([str(x).strip() for x in raw if str(x).strip()], limit)


def _segments(text: str) -> list[str]:
    return [
        part.strip()
        for part in re.split(r"[\n\r。！？!?；;]+", str(text or ""))
        if part.strip()
    ]


def _best_dice(needle: str, *texts: str) -> float:
    """在整段文本和每个短句/分行上取最大相似度，避免长上下文稀释短触发词。"""
    best = 0.0
    for text in texts:
        if not text:
            continue
        best = max(best, _dice(needle, text))
        for segment in _segments(text):
            best = max(best, _dice(needle, segment))
    return min(1.0, best)


def _infer_topics(text: str) -> list[str]:
    return _unique([topic for topic in TOPIC_TRIGGERS if topic in str(text or "")], 8)


def _emotion_overlap(candidate: list[str], target: list[str]) -> float:
    cand = {x for x in candidate if x and x != "中性"}
    targ = {x for x in target if x and x != "中性"}
    if not cand or not targ:
        return 0.0
    return min(1.0, len(cand & targ) / max(1, len(cand)))


class MediaStore:
    def __init__(self, path: Path):
        self.path = str(path)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with closing(self._connect()) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS media (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    path TEXT NOT NULL,
                    url TEXT DEFAULT '',
                    sha256 TEXT NOT NULL UNIQUE,
                    description TEXT DEFAULT '',
                    emotions TEXT DEFAULT '[]',
                    keywords TEXT DEFAULT '[]',
                    ocr_text TEXT DEFAULT '',
                    text_meaning TEXT DEFAULT '',
                    scene TEXT DEFAULT '',
                    interpretation TEXT DEFAULT '',
                    role TEXT DEFAULT '',
                    scenarios TEXT DEFAULT '[]',
                    trigger_topic TEXT DEFAULT '[]',
                    is_manual_tagged INTEGER DEFAULT 0,
                    ocr_status TEXT DEFAULT '',
                    external_id TEXT DEFAULT '',
                    source TEXT DEFAULT 'local',
                    safe INTEGER DEFAULT 1,
                    enabled INTEGER DEFAULT 1,
                    analysis_source TEXT DEFAULT 'heuristic',
                    created_at INTEGER NOT NULL,
                    last_used INTEGER DEFAULT 0,
                    use_count INTEGER DEFAULT 0,
                    usage_count INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS tags (
                    kind TEXT NOT NULL,
                    value TEXT NOT NULL,
                    count INTEGER DEFAULT 0,
                    first_seen INTEGER NOT NULL,
                    last_seen INTEGER NOT NULL,
                    PRIMARY KEY (kind, value)
                );
                CREATE INDEX IF NOT EXISTS idx_media_kind ON media(kind);
                CREATE INDEX IF NOT EXISTS idx_media_enabled ON media(enabled);
                """
            )
            # 旧库平滑升级：新增列只做 ADD COLUMN，不破坏已有素材。
            existing = {row["name"] for row in conn.execute("PRAGMA table_info(media)").fetchall()}
            for name, ddl in (
                ("ocr_text", "TEXT DEFAULT ''"),
                ("text_meaning", "TEXT DEFAULT ''"),
                ("scene", "TEXT DEFAULT ''"),
                ("interpretation", "TEXT DEFAULT ''"),
                ("role", "TEXT DEFAULT ''"),
                ("scenarios", "TEXT DEFAULT '[]'"),
                ("trigger_topic", "TEXT DEFAULT '[]'"),
                ("is_manual_tagged", "INTEGER DEFAULT 0"),
                ("ocr_status", "TEXT DEFAULT ''"),
                ("usage_count", "INTEGER DEFAULT 0"),
            ):
                if name not in existing:
                    conn.execute(f"ALTER TABLE media ADD COLUMN {name} {ddl}")
            # 旧库把原有 use_count 同步到 usage_count，避免排序失真。
            conn.execute("UPDATE media SET usage_count=use_count WHERE usage_count=0 AND use_count>0")
            conn.commit()

    def get(self, media_id: int) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM media WHERE id=?", (media_id,)).fetchone()
        return dict(row) if row else None

    def get_by_sha(self, sha256: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM media WHERE sha256=?", (sha256,)).fetchone()
        return dict(row) if row else None

    def add(
        self,
        *,
        kind: str,
        path: str,
        sha256: str,
        description: str = "",
        url: str = "",
        external_id: str = "",
        source: str = "local",
    ) -> tuple[dict[str, Any], bool]:
        now = int(time.time())
        with closing(self._connect()) as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO media
                (kind, path, url, sha256, description, external_id, source, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (kind, path, url, sha256, description, external_id, source, now),
            )
            conn.commit()
            created = cur.rowcount > 0
            row = conn.execute("SELECT * FROM media WHERE sha256=?", (sha256,)).fetchone()
        return (dict(row) if row else {}), created

    def update_meta(
        self,
        media_id: int,
        *,
        description: str | None = None,
        url: str | None = None,
        external_id: str | None = None,
    ) -> None:
        fields: list[str] = []
        values: list[Any] = []
        for name, value in (("description", description), ("url", url), ("external_id", external_id)):
            if value is not None and str(value).strip():
                fields.append(f"{name}=?")
                values.append(str(value))
        if not fields:
            return
        values.append(media_id)
        with closing(self._connect()) as conn:
            conn.execute(f"UPDATE media SET {', '.join(fields)} WHERE id=?", values)
            conn.commit()

    def update_analysis(
        self,
        media_id: int,
        *,
        description: str,
        emotions: list[str],
        keywords: list[str],
        source: str,
        safe: bool,
        ocr_text: str = "",
        text_meaning: str = "",
        scene: str = "",
        interpretation: str = "",
        role: str = "",
        scenarios: list[str] | None = None,
        trigger_topic: list[str] | None = None,
        is_manual_tagged: bool = False,
        ocr_status: str = "",
    ) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                UPDATE media
                SET description=?, emotions=?, keywords=?, analysis_source=?, safe=?,
                    ocr_text=?, text_meaning=?, scene=?, interpretation=?, role=?, scenarios=?,
                    trigger_topic=?, is_manual_tagged=?, ocr_status=?
                WHERE id=?
                """,
                (
                    description,
                    json.dumps(emotions, ensure_ascii=False),
                    json.dumps(keywords, ensure_ascii=False),
                    source,
                    1 if safe else 0,
                    ocr_text,
                    text_meaning,
                    scene,
                    interpretation,
                    role,
                    json.dumps(scenarios or [], ensure_ascii=False),
                    json.dumps(trigger_topic or [], ensure_ascii=False),
                    1 if is_manual_tagged else 0,
                    ocr_status,
                    media_id,
                ),
            )
            conn.commit()

    def list_media(self, kind: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
        sql = "SELECT * FROM media"
        params: list[Any] = []
        if kind:
            sql += " WHERE kind=?"
            params.append(kind)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with closing(self._connect()) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def list_enabled(self, limit: int = 300) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM media WHERE enabled=1 ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def all_media(self, limit: int = 100) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT * FROM media ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def delete(self, media_id: int) -> dict[str, Any] | None:
        row = self.get(media_id)
        if not row:
            return None
        with closing(self._connect()) as conn:
            conn.execute("DELETE FROM media WHERE id=?", (media_id,))
            conn.commit()
        return row

    def mark_used(self, media_id: int) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "UPDATE media SET last_used=?, use_count=use_count+1, "
                "usage_count=COALESCE(usage_count, 0)+1 WHERE id=?",
                (int(time.time()), media_id),
            )
            conn.commit()

    def register_tags(self, kind: str, values: list[str]) -> None:
        now = int(time.time())
        items = _unique([str(v).strip() for v in values if str(v).strip()], 20)
        if not items:
            return
        with closing(self._connect()) as conn:
            for value in items:
                conn.execute(
                    """
                    INSERT INTO tags (kind, value, count, first_seen, last_seen)
                    VALUES (?, ?, 1, ?, ?)
                    ON CONFLICT(kind, value) DO UPDATE SET
                        count=count+1, last_seen=excluded.last_seen
                    """,
                    (kind, value, now, now),
                )
            conn.commit()

    def tag_counts(self) -> dict[str, int]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT kind, COUNT(*) AS n FROM tags GROUP BY kind").fetchall()
        return {str(row["kind"]): int(row["n"]) for row in rows}

    def manual_count(self) -> int:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM media WHERE is_manual_tagged=1").fetchone()
        return int(row["n"]) if row else 0

    def counts(self) -> dict[str, int]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT kind, COUNT(*) AS n FROM media GROUP BY kind"
            ).fetchall()
        out = {"image": 0, "sticker": 0}
        for row in rows:
            out[str(row["kind"])] = int(row["n"])
        return out


@star.register(
    name=PLUGIN_NAME,
    author="A.I.R Z",
    desc="按聊天语境自动挑选表情包或本地图库图片，支持图库管理与语义标注",
    version="1.6.0",
)
class AirzMediaPlugin(star.Star):
    def __init__(self, context: star.Context, config: Any = None) -> None:
        super().__init__(context, config)
        self.root = Path(get_astrbot_data_path()) / "plugin_data" / "airz_media"
        self.root.mkdir(parents=True, exist_ok=True)
        self.config_path = self.root / "config.json"
        self.config = self._load_config()
        self.gallery_dir = self._resolve_dir(self.config.get("gallery_dir"), "gallery")
        self.sticker_dir = self._resolve_dir(self.config.get("sticker_dir"), "stickers")
        self.store = MediaStore(self.root / "media.db")
        self._contexts: dict[str, list[dict[str, str]]] = {}
        self._topic_last: dict[str, float] = {}
        self._bg_tasks: set[asyncio.Task] = set()
        logger.info(
            "[AIRZ-MEDIA] loaded gallery=%s stickers=%s db=%s",
            self.gallery_dir,
            self.sticker_dir,
            self.root / "media.db",
        )

    # ---------- config / paths ----------
    def _load_config(self) -> dict[str, Any]:
        default = _db_default(self.root)
        loaded: dict[str, Any] = {}
        if self.config_path.exists():
            try:
                loaded = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
                if not isinstance(loaded, dict):
                    loaded = {}
            except Exception as exc:
                logger.warning("[AIRZ-MEDIA] config read failed: %s", exc)
        merged = _merge_config(default, loaded)
        # 代码净化：不再保留任何 Provider/模型绑定；只保留超时。
        vision = merged.get("vision") if isinstance(merged.get("vision"), dict) else {}
        try:
            vision_timeout = float(vision.get("timeout", 45))
        except Exception:
            vision_timeout = 45.0
        merged["vision"] = {"timeout": vision_timeout}
        if not self.config_path.exists() or merged != loaded:
            self._write_config(merged)
        return merged

    def _write_config(self, cfg: dict[str, Any] | None = None) -> None:
        target = cfg or self.config
        tmp = self.config_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(target, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.config_path)

    def _resolve_dir(self, value: Any, fallback: str) -> Path:
        raw = str(value or fallback)
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = self.root / path
        path.mkdir(parents=True, exist_ok=True)
        return path.resolve()

    # ---------- image helpers ----------
    def _max_bytes(self) -> int:
        try:
            return max(1, int(self.config.get("max_image_mb", 10))) * 1024 * 1024
        except Exception:
            return 10 * 1024 * 1024

    def _validate_image(self, data: bytes) -> tuple[str | None, str]:
        if not data:
            return None, "图片内容为空。"
        if len(data) > self._max_bytes():
            return None, f"图片超过 {self.config.get('max_image_mb', 10)}MB。"
        try:
            with PILImage.open(io.BytesIO(data)) as im:
                fmt = im.format
                width, height = im.size
                im.verify()
            if width * height > IMAGE_MAX_PIXELS:
                return None, "图片像素过大（超过 5000 万像素）。"
        except Exception:
            return None, "无法识别为图片，支持 jpg / png / gif / webp。"
        ext = FORMAT_EXT.get(str(fmt or "").upper())
        if not ext:
            return None, "暂不支持该图片格式，支持 jpg / png / gif / webp。"
        return ext, ""

    def _vision_jpeg(self, data: bytes) -> bytes:
        with PILImage.open(io.BytesIO(data)) as im:
            im.seek(0)
            rgb = im.convert("RGB")
            rgb.thumbnail((1024, 1024))
            out = io.BytesIO()
            rgb.save(out, format="JPEG", quality=85)
        return out.getvalue()

    def _analysis_prompt(self, filename: str, desc: str) -> str:
        extra = f"已有描述：{desc}\n" if desc else ""
        return (
            "你是聊天表情包/图片的图文语义分析器。请先逐字识别图中所有文字，再结合文字与画面判断真实含义。\n"
            "只输出一个 JSON 对象，不要 Markdown、不要解释：\n"
            '{"ocr_text":"图中文字原文","ocr_status":"ok|no_text|failed",'
            '"text_meaning":"文字含义/网络梗解读","scene":"画面描述",'
            '"interpretation":"图文结合后的真实含义","emotions":["开心"],'
            '"keywords":["猫","大笑"],"role":"表达情绪","scenarios":["对方说了蠢话时"],'
            '"trigger_topic":["晚安"],"safe":true}\n'
            "规则：\n"
            "1. ocr_text 必须按阅读顺序逐字保留图中所有可读文字，包括表情包配文、字幕、贴纸文字、水印、角落小字；"
            "保留原文、标点和换行，不要翻译、改写、纠错或概括。没有任何文字时填“无文字”；文字模糊无法辨认时填“识别失败”。\n"
            "2. ocr_status：有文字且识别成功=ok；确认无文字=no_text；文字看不清/识别失败=failed。\n"
            "3. text_meaning：解释文字的字面意思；若含网络梗、流行语、缩写（如“蚌埠住了”“yyds”“666”），"
            "必须给出网络语境下的含义；无文字时写“无文字，仅依据画面判断”。\n"
            "4. scene：客观描述画面主体、动作、表情、构图、颜色，不超过120字。\n"
            "5. interpretation：必须同时使用文字和画面判断真实含义，明确指出二者是互补、一致、反差还是反讽；"
            "不要只复述文字，也不要只描述画面。例如画面笑脸+“我没事”通常表示强撑/自嘲；"
            "画面平静+“破防了”通常表示表面平静、内心崩溃。\n"
            "6. emotions 是 1~3 个自由情绪标签，不要局限固定列表；必须准确描述图片本身传达的情绪，"
            "例如 开心、害羞、委屈、得意、困倦、无语、生气、挑衅、温柔、尴尬 等都可以。\n"
            "7. role 是图片的作用/使用情境，用自然语言给出一个短标签，例如 表达情绪、调侃对方、自嘲、"
            "缓和气氛、回应特定话题、庆祝、安慰对方、晚安问候、嘴硬、撒娇 等；不要局限固定列表。\n"
            "8. scenarios 是 1~5 个具体使用情境，例如“对方说晚安时”“自己无奈时”“庆祝时”“安慰对方时”。\n"
            "9. trigger_topic 是该图片强绑定的固定话题，例如 晚安、早安、再见、生日快乐、新年快乐、加油、抱抱；"
            "只有图片本身就是为这个话题服务时才填写，没有则填空数组 []。不要为了多触发而乱填。\n"
            "10. keywords 是 2~8 个便于检索的中文/英文词，尽量包含图中文字或梗词。\n"
            "11. safe 仅在明确包含色情、血腥、暴力或违法内容时为 false；普通照片、截图、文字图和聊天表情包一律为 true。\n"
            f"文件名：{filename}\n{extra}"
        )

    def _parse_analysis(self, content: Any, fallback_desc: str) -> dict[str, Any] | None:
        text = _content_text(content).strip()
        if not text:
            return None
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            return None
        try:
            obj = json.loads(match.group(0))
        except Exception:
            return None
        if not isinstance(obj, dict):
            return None
        desc = str(obj.get("description") or obj.get("desc") or fallback_desc or "聊天配图")
        emotions = _labels(obj.get("emotions") or obj.get("emotion") or [], 3)
        keywords = _labels(obj.get("keywords") or [], 12)
        # OCR 原文只做首尾空白清理，不能摘要、改写或截断。
        ocr_text = str(obj.get("ocr_text") or "").strip()
        ocr_status = str(obj.get("ocr_status") or "").strip().lower()
        if ocr_text in ("无文字", "无", "none", "None", "null", "NULL"):
            ocr_text = "无文字"
            ocr_status = "no_text"
        elif ocr_text in ("识别失败", "无法识别", "看不清"):
            ocr_status = "failed"
        if not ocr_text:
            ocr_text = "无文字"
            ocr_status = "no_text"
        if ocr_status not in ("ok", "no_text", "failed"):
            ocr_status = "no_text" if ocr_text == "无文字" else "ok"

        text_meaning = str(obj.get("text_meaning") or "").strip()
        scene = str(obj.get("scene") or "").strip()
        interpretation = str(obj.get("interpretation") or "").strip()
        role = str(obj.get("role") or "").strip() or "表达情绪"
        scenarios = _labels(obj.get("scenarios") or [], 5)
        trigger_topic = _labels(
            obj.get("trigger_topic") or obj.get("trigger_topics") or obj.get("topics") or [],
            8,
        )

        if not text_meaning:
            text_meaning = "无文字，仅依据画面判断" if ocr_status == "no_text" else "未给出文字解读"
        if not scene:
            scene = desc
        if not interpretation:
            interpretation = scene
        if not scenarios:
            scenarios = ["日常聊天"]
        if not trigger_topic:
            trigger_topic = _infer_topics(" ".join([ocr_text, text_meaning, interpretation, scene]))

        # description 保留为一行摘要，供列表/简单匹配使用；完整字段另存。
        description = str(obj.get("description") or "").strip()
        if not description:
            description = _clean_summary(f"{scene}；{interpretation}", 300)
        if not emotions:
            emotions = ["中性"]
        return {
            "description": _clean_summary(description),
            "emotions": emotions,
            "keywords": keywords,
            "safe": bool(obj.get("safe", True)),
            "source": "vision",
            "ocr_text": ocr_text,
            "ocr_status": ocr_status,
            "text_meaning": text_meaning,
            "scene": scene,
            "interpretation": interpretation,
            "role": role,
            "scenarios": scenarios,
            "trigger_topic": trigger_topic,
        }

    def _heuristic(self, filename: str, desc: str) -> dict[str, Any]:
        stem = Path(filename).stem.replace("_", " ").replace("-", " ")
        text = f"{desc} {stem}".strip()
        emotions = [name for name, words in EMOTION_LEXICON.items() if any(w in text for w in words)]
        if not emotions:
            emotions = ["中性"]
        tokens = re.findall(r"[\u4e00-\u9fff]{2,6}|[A-Za-z0-9]{2,16}", text)
        keywords: list[str] = []
        for token in tokens:
            low = token.lower()
            if low in STOPWORDS or token in STOPWORDS:
                continue
            if token not in keywords:
                keywords.append(token)
        if not keywords:
            keywords = emotions[:]
        topics = _infer_topics(text)
        keywords = _unique(keywords + topics, 12)
        description = _clean_summary(desc or stem or "聊天配图")
        return {
            "description": description,
            "emotions": emotions[:3],
            "keywords": keywords[:12],
            "safe": True,
            "source": "heuristic",
            "ocr_text": "识别失败",
            "ocr_status": "failed",
            "text_meaning": "文字识别失败，仅依据画面/文件名判断",
            "scene": description,
            "interpretation": description,
            "role": "表达情绪",
            "scenarios": ["日常聊天"],
            "trigger_topic": topics,
        }

    async def _analyze_with_default_provider(
        self, data: bytes, filename: str, desc: str, umo: str
    ) -> dict[str, Any] | None:
        """用当前会话的默认模型分析图片，不指定 Provider 或模型名。"""
        timeout = float((self.config.get("vision") or {}).get("timeout", 45))
        data_uri = "data:image/jpeg;base64," + base64.b64encode(self._vision_jpeg(data)).decode()
        try:
            provider = await self.context.get_using_provider_async(umo=umo)
        except Exception as exc:
            logger.info("[AIRZ-MEDIA] default vision provider unavailable: %s", exc)
            return None
        if provider is None:
            logger.info("[AIRZ-MEDIA] no default vision provider; fallback to heuristic")
            return None
        try:
            response = await asyncio.wait_for(
                provider.text_chat(
                    prompt=self._analysis_prompt(filename, desc),
                    image_urls=[data_uri],
                    contexts=[],
                    system_prompt="你是图片语义标注器，只输出严格 JSON。",
                ),
                timeout=timeout,
            )
            text = getattr(response, "completion_text", "") or ""
            parsed = self._parse_analysis(text, desc)
            if parsed:
                logger.info("[AIRZ-MEDIA] analysis by default provider")
                return parsed
            logger.info("[AIRZ-MEDIA] default provider returned no parsable JSON")
        except Exception as exc:
            logger.info("[AIRZ-MEDIA] default vision analysis failed: %s", exc)
        return None

    async def _analyze(self, data: bytes, filename: str, desc: str, umo: str) -> dict[str, Any]:
        result = await self._analyze_with_default_provider(data, filename, desc, umo)
        if result:
            return result
        return self._heuristic(filename, desc)

    # ---------- ingestion ----------
    def _parse_manual_args(self, text: str) -> tuple[str, dict[str, Any]]:
        """解析 /media add|import 的手动标签参数，剩余文本作为描述/路径。"""
        manual: dict[str, Any] = {
            "emotions": [],
            "role": "",
            "scenarios": [],
            "trigger_topic": [],
        }
        pattern = re.compile(
            r"--(emotion|情绪|role|作用|scenario|情境|topic|话题)\s+"
            r"(?:\"([^\"]*)\"|“([^”]*)”|(\S+))",
            re.I,
        )
        for match in pattern.finditer(text or ""):
            flag = match.group(1).lower()
            value = next((g for g in match.groups()[1:] if g is not None), "")
            labels = _labels(value, 8)
            if not labels:
                continue
            if flag in ("emotion", "情绪"):
                manual["emotions"] = _unique(manual["emotions"] + labels, 6)
            elif flag in ("role", "作用"):
                manual["role"] = labels[0]
            elif flag in ("scenario", "情境"):
                manual["scenarios"] = _unique(manual["scenarios"] + labels, 6)
            else:
                manual["trigger_topic"] = _unique(manual["trigger_topic"] + labels, 8)
        cleaned = re.sub(pattern, " ", text or "")
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned, manual

    def _manual_analysis(self, desc: str, filename: str, manual: dict[str, Any]) -> dict[str, Any]:
        emotions = _labels(manual.get("emotions") or [], 6) or ["中性"]
        role = str(manual.get("role") or "").strip() or "表达情绪"
        scenarios = _labels(manual.get("scenarios") or [], 6) or ["日常聊天"]
        topics = _labels(manual.get("trigger_topic") or [], 8)
        description = _clean_summary(desc or Path(filename).stem.replace("_", " ") or "聊天配图")
        if not topics:
            topics = _infer_topics(f"{description} {' '.join(scenarios)}")
        keywords = _unique(emotions + topics + [description], 12)
        return {
            "description": description,
            "emotions": emotions,
            "keywords": keywords,
            "safe": True,
            "source": "manual",
            "ocr_text": "未分析（手动标签）",
            "ocr_status": "manual",
            "text_meaning": "使用手动标签，未调用图片分析模型",
            "scene": description,
            "interpretation": description,
            "role": role,
            "scenarios": scenarios,
            "trigger_topic": topics,
        }

    def _register_analysis_tags(self, analysis: dict[str, Any]) -> None:
        self.store.register_tags("emotion", list(analysis.get("emotions") or []))
        role = str(analysis.get("role") or "").strip()
        if role:
            self.store.register_tags("role", [role])
        self.store.register_tags("scenario", list(analysis.get("scenarios") or []))
        self.store.register_tags("topic", list(analysis.get("trigger_topic") or []))

    def _managed_path(self, path: Path) -> bool:
        try:
            resolved = path.resolve()
            return resolved.is_relative_to(self.gallery_dir) or resolved.is_relative_to(self.sticker_dir)
        except Exception:
            return False

    async def _ingest(
        self,
        data: bytes,
        *,
        kind: str,
        filename: str,
        desc: str,
        source: str,
        umo: str,
        external_id: str = "",
        url: str = "",
        manual: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, bool, str]:
        ext, error = self._validate_image(data)
        if not ext:
            return None, False, error
        manual = manual or {}
        manual_has_tags = bool(
            manual.get("emotions")
            or manual.get("role")
            or manual.get("scenarios")
            or manual.get("trigger_topic")
        )
        sha = hashlib.sha256(data).hexdigest()
        existing = self.store.get_by_sha(sha)
        if existing:
            self.store.update_meta(
                existing["id"],
                description=desc or None,
                external_id=external_id or None,
                url=url or None,
            )
            if manual_has_tags:
                analysis = self._manual_analysis(desc, filename, manual)
                self.store.update_analysis(
                    existing["id"],
                    description=str(analysis["description"]),
                    emotions=list(analysis["emotions"]),
                    keywords=list(analysis["keywords"]),
                    source=str(analysis["source"]),
                    safe=bool(analysis["safe"]),
                    ocr_text=str(analysis.get("ocr_text", "")),
                    text_meaning=str(analysis.get("text_meaning", "")),
                    scene=str(analysis.get("scene", "")),
                    interpretation=str(analysis.get("interpretation", "")),
                    role=str(analysis.get("role", "")),
                    scenarios=list(analysis.get("scenarios", [])),
                    ocr_status=str(analysis.get("ocr_status", "")),
                    trigger_topic=list(analysis.get("trigger_topic", [])),
                    is_manual_tagged=True,
                )
                self._register_analysis_tags(analysis)
                return self.store.get(existing["id"]), False, "已存在，已更新手动标签"
            return self.store.get(existing["id"]), False, "已存在，已更新描述"

        directory = self.sticker_dir if kind == "sticker" else self.gallery_dir
        target = directory / f"{kind}_{sha[:16]}{ext}"
        target.write_bytes(data)
        row, created = self.store.add(
            kind=kind,
            path=str(target),
            sha256=sha,
            description=desc,
            url=url,
            external_id=external_id,
            source=source,
        )
        if not row:
            return None, False, "写入数据库失败。"
        if manual_has_tags:
            analysis = self._manual_analysis(desc, filename, manual)
        else:
            analysis = await self._analyze(data, filename, desc, umo)
        self.store.update_analysis(
            row["id"],
            description=str(analysis["description"]),
            emotions=list(analysis["emotions"]),
            keywords=list(analysis["keywords"]),
            source=str(analysis["source"]),
            safe=bool(analysis["safe"]),
            ocr_text=str(analysis.get("ocr_text", "")),
            text_meaning=str(analysis.get("text_meaning", "")),
            scene=str(analysis.get("scene", "")),
            interpretation=str(analysis.get("interpretation", "")),
            role=str(analysis.get("role", "")),
            scenarios=list(analysis.get("scenarios", [])),
            ocr_status=str(analysis.get("ocr_status", "")),
            trigger_topic=list(analysis.get("trigger_topic", [])),
            is_manual_tagged=manual_has_tags,
        )
        self._register_analysis_tags(analysis)
        return self.store.get(row["id"]), created, ""

    def _can_manage(self, event: AstrMessageEvent) -> bool:
        if not event.get_group_id():
            return True
        if event.is_admin():
            return True
        return str(event.get_sender_id()) in {str(x) for x in (self.config.get("owner_ids") or [])}

    async def _reply(self, event: AstrMessageEvent, text: str):
        await event.send(MessageChain(chain=[Plain(text)]))

    # ---------- commands ----------
    @filter.command("media", alias={"airzmedia", "airz-media"})
    async def media_command(self, event: AstrMessageEvent):
        result = await self._handle_media(event)
        if result is not None:
            event.set_result(result)

    @filter.command("表情")
    async def sticker_command(self, event: AstrMessageEvent):
        result = await self._handle_media(event)
        if result is not None:
            event.set_result(result)

    @filter.command("图库")
    async def gallery_command(self, event: AstrMessageEvent):
        result = await self._handle_media(event)
        if result is not None:
            event.set_result(result)

    async def _handle_media(self, event: AstrMessageEvent):
        raw = (event.message_str or "").strip()
        rest = re.sub(r"^/?\S+\s*", "", raw, count=1).strip()
        parts = rest.split(maxsplit=1)
        sub = parts[0].lower() if parts else "help"
        arg = parts[1].strip() if len(parts) > 1 else ""
        aliases = {
            "帮助": "help",
            "同步": "sync",
            "列表": "list",
            "添加": "add",
            "导入": "import",
            "删除": "delete",
            "发送": "send",
            "状态": "status",
            "重析": "reanalyze",
            "开启": "on",
            "关闭": "off",
        }
        sub = aliases.get(sub, sub)

        if sub in ("help", ""):
            return event.plain_result(self._help_text())
        if sub == "status":
            counts = self.store.counts()
            tags = self.store.tag_counts()
            vision_timeout = float((self.config.get("vision") or {}).get("timeout", 45))
            return event.plain_result(
                f"A.I.R Z 表情图库\n"
                f"图片：{counts['image']} 张｜表情包：{counts['sticker']} 个｜手动标签素材：{self.store.manual_count()} 个\n"
                f"自动发送：{'开' if self.config.get('enabled') and self.config.get('auto_send') else '关'}\n"
                f"图库目录：{self.gallery_dir}\n"
                f"表情目录：{self.sticker_dir}\n"
                f"语义分析：默认模型（跟随 AstrBot 全局）｜超时 {vision_timeout:.0f}s\n"
                f"动态标签库：情绪 {tags.get('emotion', 0)} 个｜作用 {tags.get('role', 0)} 个｜"
                f"情境 {tags.get('scenario', 0)} 个｜话题 {tags.get('topic', 0)} 个\n"
                f"基础/语境/情绪权重：{float(self.config.get('base_weight', 0.25)) * 100:.0f}%/"
                f"{float(self.config.get('context_weight', 0.45)) * 100:.0f}%/"
                f"{float(self.config.get('emotion_weight', 0.30)) * 100:.0f}%｜"
                f"直发：语境或情绪≥{float(self.config.get('direct_fit_threshold', 0.80)):.2f}｜"
                f"兜底：>{float(self.config.get('min_score', 0.40)):.2f} 时 "
                f"{float(self.config.get('send_probability', 0.80)) * 100:.0f}%｜次数/间隔：不限\n"
                f"固定话题触发：{'开' if self.config.get('topic_trigger', True) else '关'}"
                f"（冷却 {int(self.config.get('topic_cooldown_seconds', 300))} 秒）"
            )
        if sub == "list":
            kind = arg.lower().strip()
            if kind in ("表情", "sticker", "表情包"):
                kind = "sticker"
            elif kind in ("图片", "image"):
                kind = "image"
            else:
                kind = ""
            return event.plain_result(self._list_text(kind))
        if sub == "add":
            return await self._cmd_add(event, arg)
        if sub == "import":
            if not self._can_manage(event):
                return event.plain_result("群聊里只有管理员能导入图片。")
            return await self._cmd_import(event, arg)
        if sub == "sync":
            if not self._can_manage(event):
                return event.plain_result("群聊里只有管理员能同步账号表情。")
            return await self._cmd_sync(event, arg)
        if sub == "delete":
            if not self._can_manage(event):
                return event.plain_result("群聊里只有管理员能删除素材。")
            return self._cmd_delete(event, arg)
        if sub == "send":
            return await self._cmd_send(event, arg)
        if sub == "reanalyze":
            if not self._can_manage(event):
                return event.plain_result("群聊里只有管理员能重析图库。")
            return await self._cmd_reanalyze(event)
        if sub in ("on", "off"):
            if not self._can_manage(event):
                return event.plain_result("群聊里只有管理员能切换开关。")
            self.config["enabled"] = sub == "on"
            self._write_config()
            return event.plain_result(f"自动表情/配图已{'开启' if sub == 'on' else '关闭'}。")
        return event.plain_result(self._help_text())

    def _help_text(self) -> str:
        return (
            "A.I.R Z 表情图库\n"
            "/media add [描述] — 带图发送，把这张图加入图库\n"
            "  可加手动标签：--emotion 开心,害羞 --role 撒娇 "
            "--scenario 对方说晚安时 --topic 晚安\n"
            "/media import <服务器路径> — 从服务器路径导入图片，同样支持上述标签\n"
            "/media list [image|sticker] — 查看素材\n"
            "/media sync [数量] — 同步账号收藏表情（SnowLuma 网关）\n"
            "/media send <id> — 试发一个素材\n"
            "/media delete <id> — 删除素材\n"
            "/media status — 查看状态\n"
            "/media on|off — 开关自动发送\n"
            "/media reanalyze — 重新分析未手动标注的素材语义\n"
            "手动标签优先，带手动标签的素材不会调用图片分析模型。\n"
            "也可以用 /表情 或 /图库 代替 /media。"
        )

    def _list_text(self, kind: str) -> str:
        rows = self.store.list_media(kind=kind or None, limit=30)
        if not rows:
            return "素材库还是空的。发送图片并带上 /media add 就能加入图库。"
        lines = ["A.I.R Z 素材库："]
        for row in rows:
            label = "表情" if row["kind"] == "sticker" else "图片"
            desc = _clean_summary(row.get("description") or Path(row["path"]).name, 40)
            emos = "、".join(_json_list(row.get("emotions"))[:3])
            ocr = row.get("ocr_text") or "无文字"
            ocr_status = row.get("ocr_status") or ""
            if ocr_status == "failed":
                ocr = "识别失败（仅看画面）"
            elif ocr_status == "manual":
                ocr = "手动标签"
            true_meaning = _clean_summary(row.get("interpretation") or row.get("description") or "", 60)
            topics = "、".join(_json_list(row.get("trigger_topic"))[:3])
            manual = "｜手动" if row.get("is_manual_tagged") else ""
            lines.append(f"#{row['id']} [{label}] {desc}" + (f"（{emos}）" if emos else "") + manual)
            lines.append(f"  文字：{_clean_summary(ocr, 60)}")
            lines.append(f"  含义：{true_meaning or '暂无'}" + (f"｜话题：{topics}" if topics else ""))
        if len(rows) >= 30:
            lines.append("只显示最近 30 条。")
        return "\n".join(lines)

    async def _cmd_add(self, event: AstrMessageEvent, desc: str):
        if not self._can_manage(event):
            return event.plain_result("群聊里只有管理员能添加图片。")
        desc, manual = self._parse_manual_args(desc)
        components = getattr(event.message_obj, "message", []) or []
        images = [c for c in components if isinstance(c, Image)]
        if not images:
            return event.plain_result("请把图片和指令一起发送，例如：/media add 猫猫开心")
        added: list[str] = []
        errors: list[str] = []
        for comp in images[:5]:
            try:
                path = await comp.convert_to_file_path()
                data = Path(path).read_bytes()
            except Exception as exc:
                errors.append(str(exc))
                continue
            row, created, message = await self._ingest(
                data,
                kind="image",
                filename=Path(path).name,
                desc=desc,
                source="upload",
                umo=event.unified_msg_origin,
                manual=manual,
            )
            if row:
                manual_mark = "（手动标签）" if row.get("is_manual_tagged") else ""
                added.append(f"#{row['id']} {row['description'][:30]}{manual_mark}" + ("" if created else "（已存在）"))
            elif message:
                errors.append(message)
        text = "已加入图库：\n" + "\n".join(added) if added else "没有加入成功。"
        if errors:
            text += "\n失败：" + "；".join(errors[:3])
        return event.plain_result(text)

    async def _cmd_import(self, event: AstrMessageEvent, arg: str):
        clean, manual = self._parse_manual_args(arg)
        if not clean:
            return event.plain_result(
                "用法：/media import /服务器/图片路径.jpg [--emotion 开心,害羞] "
                "[--role 撒娇] [--scenario 对方说晚安时] [--topic 晚安]"
            )
        path_text = clean.split(maxsplit=1)[0]
        path = Path(path_text).expanduser()
        if not path.is_absolute():
            path = self.root / path
        try:
            path = path.resolve()
            if not path.is_file():
                return event.plain_result("找不到这个文件。")
            if path.stat().st_size > self._max_bytes():
                return event.plain_result(f"文件超过 {self.config.get('max_image_mb', 10)}MB。")
            data = path.read_bytes()
        except Exception as exc:
            return event.plain_result(f"读取失败：{exc}")
        desc = path.stem.replace("_", " ")
        row, created, message = await self._ingest(
            data,
            kind="image",
            filename=path.name,
            desc=desc,
            source="import",
            umo=event.unified_msg_origin,
            manual=manual,
        )
        if not row:
            return event.plain_result(message or "导入失败。")
        manual_mark = "（手动标签）" if row.get("is_manual_tagged") else ""
        return event.plain_result(
            f"已导入 #{row['id']}：{row['description'][:40]}{manual_mark}" + ("" if created else "（已存在）")
        )

    async def _cmd_sync(self, event: AstrMessageEvent, arg: str):
        if not getattr(event, "bot", None):
            return event.plain_result("当前平台不支持读取账号收藏表情；可以把表情图片发给我并 /media add。")
        count = int(self.config.get("sticker_sync_count", 20))
        if arg.strip().isdigit():
            count = max(1, min(50, int(arg.strip())))
        try:
            raw = await asyncio.wait_for(
                event.bot.call_action("fetch_custom_face_detail", count=count),
                timeout=45,
            )
            details = self._unwrap_action(raw)
        except Exception as exc:
            return event.plain_result(
                f"同步失败：{exc}\n当前网关可能不支持 fetch_custom_face_detail，"
                "可以把表情图片直接发给我并用 /media add 收录。"
            )
        if not isinstance(details, list) or not details:
            return event.plain_result("没有读到账号收藏表情；可以先在 QQ 里收藏几个表情再试。")
        added = 0
        duplicated = 0
        failed = 0
        async with httpx.AsyncClient(
            timeout=30,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://q.qq.com/"},
        ) as client:
            for item in details[:count]:
                if not isinstance(item, dict):
                    continue
                url = str(item.get("url") or "").strip()
                if url.startswith("//"):
                    url = "https:" + url
                if not url:
                    failed += 1
                    continue
                try:
                    response = await client.get(url)
                    response.raise_for_status()
                    data = response.content
                except Exception:
                    failed += 1
                    continue
                row, created, message = await self._ingest(
                    data,
                    kind="sticker",
                    filename=f"sticker_{item.get('md5') or item.get('emoji_id') or added}.gif",
                    desc=str(item.get("desc") or ""),
                    source="snowluma",
                    umo=event.unified_msg_origin,
                    external_id=str(item.get("emoji_id") or item.get("resId") or ""),
                    url=url,
                )
                if row and created:
                    added += 1
                elif row:
                    duplicated += 1
                else:
                    failed += 1
        return event.plain_result(
            f"同步完成：新增 {added} 个，已存在 {duplicated} 个，失败 {failed} 个。\n"
            "表情已缓存到本地，自动回复时优先按原表情样式发送。"
        )

    def _unwrap_action(self, raw: Any) -> Any:
        if isinstance(raw, list):
            return raw
        if not isinstance(raw, dict):
            return raw
        status = str(raw.get("status") or "").lower()
        retcode = raw.get("retcode")
        if status in ("failed", "error") or retcode not in (None, 0, "0"):
            raise RuntimeError(str(raw.get("message") or raw.get("wording") or "网关返回失败"))
        data = raw.get("data", raw)
        if isinstance(data, dict) and isinstance(data.get("data"), list):
            data = data["data"]
        return data

    def _cmd_delete(self, event: AstrMessageEvent, arg: str):
        try:
            media_id = int(arg.strip())
        except Exception:
            return event.plain_result("用法：/media delete <id>")
        row = self.store.delete(media_id)
        if not row:
            return event.plain_result("没有这个素材。")
        try:
            path = Path(row["path"])
            if self._managed_path(path):
                path.unlink(missing_ok=True)
        except Exception:
            pass
        return event.plain_result(f"已删除 #{media_id}。")

    async def _cmd_send(self, event: AstrMessageEvent, arg: str):
        try:
            media_id = int(arg.strip())
        except Exception:
            return event.plain_result("用法：/media send <id>")
        row = self.store.get(media_id)
        if not row:
            return event.plain_result("没有这个素材。")
        try:
            native = await self._deliver_media(event, row)
        except Exception as exc:
            return event.plain_result(f"发送失败：{exc}")
        return event.plain_result("已发送" + ("（原表情样式）" if native else "。"))

    async def _deliver_media(self, event: AstrMessageEvent, row: dict[str, Any]) -> bool:
        path = Path(row["path"])
        if not path.is_file():
            raise FileNotFoundError("素材文件不存在，可能已被移动。")
        if (
            row.get("kind") == "sticker"
            and self.config.get("native_sticker")
            and row.get("external_id")
            and getattr(event, "bot", None)
        ):
            await self._send_native(event, row)
            return True
        await event.send(MessageChain(chain=[Image.fromFileSystem(str(path))]))
        return False

    async def _send_native(self, event: AstrMessageEvent, row: dict[str, Any]) -> None:
        if not getattr(event, "bot", None):
            raise RuntimeError("当前平台不支持原表情发送")
        emoji_id = str(row.get("external_id") or "").strip()
        if not emoji_id:
            raise RuntimeError("缺少 emoji_id")
        group_id = event.get_group_id()
        params: dict[str, Any] = {"emoji_id": emoji_id}
        if group_id:
            if not str(group_id).isdigit():
                raise RuntimeError("群号格式异常")
            params["group_id"] = int(str(group_id))
        else:
            sender = str(event.get_sender_id())
            if not sender.isdigit():
                raise RuntimeError("用户号格式异常")
            params["user_id"] = int(sender)
        ret = await event.bot.call_action("send_custom_face", **params)
        if isinstance(ret, dict):
            status = str(ret.get("status") or "").lower()
            retcode = ret.get("retcode")
            if status in ("failed", "error") or retcode not in (None, 0, "0"):
                raise RuntimeError(str(ret.get("message") or ret.get("wording") or "网关发送失败"))

    async def _cmd_reanalyze(self, event: AstrMessageEvent):
        rows = self.store.all_media(limit=30)
        if not rows:
            return event.plain_result("素材库还是空的。")
        ok = 0
        failed = 0
        skipped = 0
        for row in rows:
            if row.get("is_manual_tagged"):
                skipped += 1
                continue
            try:
                data = Path(row["path"]).read_bytes()
                analysis = await self._analyze(
                    data,
                    Path(row["path"]).name,
                    str(row.get("description") or ""),
                    event.unified_msg_origin,
                )
                self.store.update_analysis(
                    row["id"],
                    description=str(analysis["description"]),
                    emotions=list(analysis["emotions"]),
                    keywords=list(analysis["keywords"]),
                    source=str(analysis["source"]),
                    safe=bool(analysis["safe"]),
                    ocr_text=str(analysis.get("ocr_text", "")),
                    text_meaning=str(analysis.get("text_meaning", "")),
                    scene=str(analysis.get("scene", "")),
                    interpretation=str(analysis.get("interpretation", "")),
                    role=str(analysis.get("role", "")),
                    scenarios=list(analysis.get("scenarios", [])),
                    ocr_status=str(analysis.get("ocr_status", "")),
                    trigger_topic=list(analysis.get("trigger_topic", [])),
                    is_manual_tagged=False,
                )
                self._register_analysis_tags(analysis)
                ok += 1
            except Exception:
                failed += 1
        return event.plain_result(
            f"重析完成：成功 {ok} 个，失败 {failed} 个，跳过手动标签 {skipped} 个。"
        )

    # ---------- context / selection ----------
    @filter.on_llm_request()
    async def remember_context(self, event: AstrMessageEvent, req: Any):
        umo = event.unified_msg_origin
        messages: list[dict[str, str]] = []
        for item in (getattr(req, "contexts", None) or [])[-10:]:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "")
            if role not in ("user", "assistant"):
                continue
            content = _content_text(item.get("content"))
            if content:
                messages.append({"role": role, "content": _clean_summary(content, 800)})
        prompt = _content_text(getattr(req, "prompt", ""))
        if prompt and (not messages or messages[-1]["content"] != _clean_summary(prompt, 800)):
            messages.append({"role": "user", "content": _clean_summary(prompt, 800)})
        self._contexts[umo] = messages[-10:]
        if len(self._contexts) > 50:
            for key in list(self._contexts.keys())[:-50]:
                self._contexts.pop(key, None)

    def _context_text(self, umo: str, assistant_text: str) -> str:
        lines: list[str] = []
        for item in self._contexts.get(umo, [])[-6:]:
            lines.append(f"{item['role']}: {item['content']}")
        if assistant_text:
            lines.append(f"assistant: {_clean_summary(assistant_text, 600)}")
        return "\n".join(lines)

    def _emotions_in(self, text: str) -> list[str]:
        found = [name for name, words in EMOTION_LEXICON.items() if any(w in text for w in words)]
        return found or ["中性"]

    def _detect_user_emotion(self, text: str) -> list[str]:
        """从用户原话/最近对话中识别用户情绪。"""
        emotions = self._emotions_in(text or "")
        return [e for e in emotions if e != "中性"] or ["中性"]

    def _derive_bot_emotion(
        self,
        user_emotions: list[str],
        reply_text: str,
    ) -> list[str]:
        """根据用户情绪 + 即将发送的回复文字，推导 bot 应表现的情绪。"""
        reply_emos = [e for e in self._emotions_in(reply_text or "") if e != "中性"]
        mapping = {
            "生气": ["安抚", "温和"],
            "难过": ["安慰", "温柔"],
            "无语": ["无奈", "调侃"],
            "疑惑": ["耐心", "温和"],
            "害怕": ["安抚", "温柔"],
            "开心": ["开心", "同喜"],
            "害羞": ["害羞", "温柔"],
            "惊讶": ["惊讶", "调侃"],
            "鼓励": ["鼓励", "温柔"],
            "调侃": ["调侃", "轻松"],
        }
        derived: list[str] = []
        for emotion in user_emotions:
            derived.extend(mapping.get(emotion, []))
        if not reply_emos and not derived:
            derived = ["中性"]
        return _unique(reply_emos + derived, 3) or ["中性"]

    def _topic_list(self, row: dict[str, Any]) -> list[str]:
        topics = _json_list(row.get("trigger_topic"))
        if topics:
            return topics
        text = " ".join(
            [
                str(row.get("ocr_text") or ""),
                str(row.get("text_meaning") or ""),
                str(row.get("interpretation") or ""),
                str(row.get("description") or ""),
                " ".join(_json_list(row.get("keywords"))),
                " ".join(_json_list(row.get("scenarios"))),
            ]
        )
        return _infer_topics(text)

    def _topic_conflict(self, row: dict[str, Any], bot_emotions: list[str]) -> bool:
        """只在图片情绪与 bot 情绪明显相反时判冲突；中性/未知情绪不拦。"""
        candidate = [e for e in _json_list(row.get("emotions")) if e and e != "中性"]
        bot = [e for e in bot_emotions if e and e != "中性"]
        if not candidate or not bot:
            return False
        positive = {"开心", "温柔", "鼓励", "调侃", "得意", "害羞", "同喜", "安慰", "安抚", "轻松"}
        negative = {"生气", "难过", "委屈", "无语", "尴尬", "害怕", "烦", "嫌弃", "冷淡"}
        cand_pol = 1 if any(e in positive for e in candidate) else (-1 if any(e in negative for e in candidate) else 0)
        bot_pol = 1 if any(e in positive for e in bot) else (-1 if any(e in negative for e in bot) else 0)
        return bool(cand_pol and bot_pol and cand_pol != bot_pol)

    def _topic_is_negated(self, text: str) -> bool:
        """识别“晚安安只是变量名/没有晚安意思”这类元语言语境，避免误触发话题直发。"""
        t = str(text or "")
        if not t:
            return False
        if re.search(r"(变量|变量名|字符串|标识符|代码|调试)", t) and re.search(r"(晚安|晚安安)", t):
            return True
        if "没有" in t and any(word in t for word in ("意思", "问候", "含义")):
            return True
        if re.search(r"(不是|并非|不代表).{0,12}(问候|意思|含义|晚安)", t):
            return True
        return False

    def _score(
        self,
        row: dict[str, Any],
        ctx_text: str,
        bot_emotions: list[str],
        reply_text: str = "",
    ) -> float:
        """基础分：只和 bot 情绪比较，回复文字也参与关键词/含义/情境匹配。"""
        emotions = [e for e in _json_list(row.get("emotions")) if e and e != "中性"]
        keywords = _json_list(row.get("keywords"))
        description = str(row.get("description") or "")
        scene = str(row.get("scene") or "")
        interpretation = str(row.get("interpretation") or "")
        text_meaning = str(row.get("text_meaning") or "")
        ocr_text = str(row.get("ocr_text") or "")
        role = str(row.get("role") or "")
        scenarios = _json_list(row.get("scenarios"))
        target = (reply_text or "") + "\n" + (ctx_text or "")
        emo_score = _emotion_overlap(emotions, bot_emotions)
        kw_hits = sum(1 for word in keywords[:8] if word and word in target)
        kw_score = min(1.0, kw_hits / max(1, min(len(keywords), 3))) if keywords else 0.0
        semantic_score = max(
            _best_dice(description, reply_text, ctx_text),
            _best_dice(scene, reply_text, ctx_text),
            _best_dice(interpretation, reply_text, ctx_text),
            _best_dice(text_meaning, reply_text, ctx_text),
            _best_dice(ocr_text, reply_text, ctx_text) if ocr_text not in ("无文字", "识别失败", "未分析（手动标签）") else 0.0,
        )
        scenario_score = max((_best_dice(s, reply_text, ctx_text) for s in scenarios), default=0.0)
        role_bonus = 0.10 if role and (role in target) else 0.0
        score = 0.25 * emo_score + 0.15 * kw_score + 0.25 * semantic_score + 0.25 * scenario_score + role_bonus
        if row.get("kind") == "sticker":
            score += 0.04
        return max(0.0, min(1.0, score))

    def _context_heuristic(self, row: dict[str, Any], ctx_text: str, reply_text: str) -> float:
        """语境启发分：回复文字优先，考察关键词/情境/含义，不看情绪。"""
        target = (reply_text or "") + "\n" + (ctx_text or "")
        keywords = _json_list(row.get("keywords"))
        scenarios = _json_list(row.get("scenarios"))
        interpretation = str(row.get("interpretation") or "")
        text_meaning = str(row.get("text_meaning") or "")
        scene = str(row.get("scene") or "")
        ocr_text = str(row.get("ocr_text") or "")
        reply = reply_text or ctx_text
        kw_hits = sum(1 for word in keywords[:8] if word and word in reply)
        kw_score = min(1.0, kw_hits / max(1, min(len(keywords), 3))) if keywords else 0.0
        scenario_score = max((_best_dice(s, reply_text, ctx_text) for s in scenarios), default=0.0)
        semantic_score = max(
            _best_dice(interpretation, reply_text, ctx_text),
            _best_dice(text_meaning, reply_text, ctx_text),
            _best_dice(scene, reply_text, ctx_text),
            _best_dice(ocr_text, reply_text, ctx_text) if ocr_text not in ("无文字", "识别失败", "未分析（手动标签）") else 0.0,
        )
        # 关键词只占 30%，防止“文字里有同一个词”被当成语境适配。
        return max(0.0, min(1.0, 0.30 * kw_score + 0.35 * scenario_score + 0.35 * semantic_score))

    def _emotion_heuristic(self, row: dict[str, Any], bot_emotions: list[str]) -> float:
        """情绪启发分：图片情绪只与 bot 情绪比较，绝不直接使用用户情绪。"""
        emotions = [e for e in _json_list(row.get("emotions")) if e and e != "中性"]
        return _emotion_overlap(emotions, bot_emotions)

    def _rank_candidates(
        self,
        items: list[dict[str, Any]],
        primary: str,
    ) -> list[dict[str, Any]]:
        """综合分降序；同分用使用次数升序；再同分随机。"""
        return sorted(
            items,
            key=lambda x: (
                -float(x.get(primary, 0.0)),
                -float(x.get("_final", 0.0)),
                int(x.get("_usage_count", 0)),
                random.random(),
            ),
        )

    async def _llm_context_scores(
        self,
        rows: list[dict[str, Any]],
        ctx_text: str,
        reply_text: str = "",
        umo: str = "",
    ) -> dict[str, Any]:
        """让模型分别判断用户情绪、bot 情绪，并给每个候选打语境/情绪分。"""
        empty: dict[str, Any] = {"scores": {}, "user_emotion": [], "bot_emotion": []}
        if not rows:
            return empty
        try:
            try:
                provider = await self.context.get_using_provider_async(umo=umo)
            except TypeError:
                provider = await self.context.get_using_provider_async()
            if not provider:
                return empty
        except Exception:
            return empty
        lines = [
            "你是聊天配图语义与情绪适配评分器。",
            "第一步：判断“用户”当前的真实情绪（如害羞、生气、开心、委屈、调侃、紧张、困倦等）。",
            "第二步：判断 bot 即将发送的回复文字应当表现什么情绪（如安抚、温柔、同喜、委屈、撒娇、调侃等）。",
            "第三步：对每个候选分别打两个 0~1 分。图片情绪只与 bot 情绪比较，绝对不要按用户情绪选图。",
            "1. context：场景、真实含义、交际作用是否适合当前对话；只是字面出现同一个词但含义相反（反讽、玩梗、强撑、自嘲等）必须给 0。",
            "2. emotion：候选的情绪/氛围是否贴合 bot 情绪；情绪不搭、方向相反必须给 0。",
            "评分刻度：0=不符/相反，0.3=弱相关，0.6=比较合适，0.8~1=明确合适。",
            "禁止因为 OCR 原文或关键词与回复文字相同就打高分；必须判断真实含义和情绪是否适配。",
            "每个候选都必须有 context 和 emotion 两个分数。",
            "",
            "用户当前情绪参考（启发式，仅供校准）：",
            "、".join(self._detect_user_emotion(ctx_text)) or "中性",
            "",
            "本次即将发送的回复文字：",
            _clean_summary(reply_text, 600) if reply_text else "（无）",
            "",
            "最近对话：",
            _clean_summary(ctx_text, 1600),
            "",
            "候选：",
        ]
        for index, row in enumerate(rows[:8], start=1):
            emos = "、".join(_json_list(row.get("emotions")))
            ocr = _clean_summary(str(row.get("ocr_text") or "无文字"), 80)
            meaning = _clean_summary(str(row.get("interpretation") or row.get("description") or ""), 100)
            role = str(row.get("role") or "")
            scenes = "、".join(_json_list(row.get("scenarios"))[:2])
            topics = "、".join(self._topic_list(row)[:3])
            lines.append(
                f"{index}. id={row['id']} 类型={'表情' if row['kind'] == 'sticker' else '图片'} "
                f"文字={ocr} 真实含义={meaning} 情感={emos} 作用={role} 情境={scenes} 固定话题={topics}"
            )
        lines += [
            "",
            '只输出 JSON：{"user_emotion":["..."],"bot_emotion":["..."],'
            '"scores":{"候选id":{"context":0.0,"emotion":0.0},...}}，不要输出解释或选择结果。',
        ]
        prompt = "\n".join(lines)
        try:
            response = await asyncio.wait_for(
                provider.text_chat(
                    prompt=prompt,
                    system_prompt="你是严格的语义与情绪适配评分器，只输出 JSON。",
                    contexts=[],
                ),
                timeout=float(self.config.get("llm_timeout", 8.0)),
            )
            text = str(getattr(response, "completion_text", "") or "")
        except Exception as exc:
            logger.info("[AIRZ-MEDIA] LLM context/emotion score failed: %s", exc)
            return empty
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            return empty
        try:
            data = json.loads(match.group(0))
        except Exception:
            return empty
        if not isinstance(data, dict):
            return empty
        user_emotion = _labels(data.get("user_emotion") or data.get("user_emotions") or [], 5)
        bot_emotion = _labels(data.get("bot_emotion") or data.get("bot_emotions") or [], 5)
        raw = data.get("scores")
        if not isinstance(raw, dict):
            if data.get("id") is not None:
                raw = {str(data["id"]): 1.0}
            else:
                return {"scores": {}, "user_emotion": user_emotion, "bot_emotion": bot_emotion}
        by_id = {str(row["id"]): int(row["id"]) for row in rows}
        by_index = {str(i): int(row["id"]) for i, row in enumerate(rows, 1)}
        scores: dict[int, dict[str, float]] = {}
        for key, value in raw.items():
            key_s = str(key)
            mid = by_id.get(key_s) or by_index.get(key_s)
            if mid is None:
                continue
            if isinstance(value, dict):
                c_raw = value.get("context", value.get("semantic", value.get("fit", 0.0)))
                e_raw = value.get("emotion", value.get("mood", c_raw))
                try:
                    ctx_fit = max(0.0, min(1.0, float(c_raw)))
                    emo_fit = max(0.0, min(1.0, float(e_raw)))
                except (TypeError, ValueError):
                    continue
            else:
                try:
                    ctx_fit = emo_fit = max(0.0, min(1.0, float(value)))
                except (TypeError, ValueError):
                    continue
            scores[mid] = {"context": ctx_fit, "emotion": emo_fit}
        return {"scores": scores, "user_emotion": user_emotion, "bot_emotion": bot_emotion}

    async def _pick(
        self,
        ctx_text: str,
        reply_text: str = "",
        umo: str = "",
        current_text: str = "",
    ) -> dict[str, Any] | None:
        """统一算法：固定话题直发 → 语境/情绪直发 → 分数 + 概率兜底。"""
        rows = self.store.list_enabled(limit=300)
        if not rows:
            return None
        user_emotions = self._detect_user_emotion(ctx_text)
        bot_emotions = self._derive_bot_emotion(user_emotions, reply_text)
        scored: list[dict[str, Any]] = []
        for row in rows:
            if self.config.get("safe_filter") and not bool(row.get("safe")):
                continue
            path = Path(row["path"])
            if not path.is_file():
                continue
            base = self._score(row, ctx_text, bot_emotions, reply_text)
            h_ctx = self._context_heuristic(row, ctx_text, reply_text)
            h_emo = self._emotion_heuristic(row, bot_emotions)
            item = dict(row)
            item["_score"] = base
            item["_heuristic_context"] = h_ctx
            item["_heuristic_emotion"] = h_emo
            item["_usage_count"] = int(row.get("usage_count") or row.get("use_count") or 0)
            scored.append(item)
        if not scored:
            return None

        # 固定话题优先：图片本身强绑定话题，且不与 bot 情绪明显冲突。
        # 同一话题冷却期内，相关素材从所有发送路径中排除，避免话题直发被其他分支绕过。
        ctx_topics = set(_infer_topics(ctx_text + "\n" + reply_text))
        now = time.time()
        cooldown = max(0, int(self.config.get("topic_cooldown_seconds", 300)))
        topic_allowed = not self._topic_is_negated((current_text or "") + "\n" + reply_text)
        if self.config.get("topic_trigger", True) and ctx_topics and cooldown > 0:
            blocked_ids: set[int] = set()
            for item in scored:
                for topic in self._topic_list(item):
                    if (
                        topic in ctx_topics
                        and now - float(self._topic_last.get(topic, 0)) < cooldown
                    ):
                        blocked_ids.add(int(item["id"]))
                        break
            if blocked_ids:
                scored = [item for item in scored if int(item["id"]) not in blocked_ids]
                if not scored:
                    return None

        if self.config.get("topic_trigger", True) and topic_allowed:
            topic_hits: list[dict[str, Any]] = []
            for item in scored:
                if self._topic_conflict(item, bot_emotions):
                    continue
                available = [
                    topic for topic in self._topic_list(item)
                    if (
                        topic in ctx_topics
                        or topic in (ctx_text or "")
                        or topic in (reply_text or "")
                    )
                    and now - float(self._topic_last.get(topic, 0)) >= cooldown
                ]
                if available:
                    item["_topic_hit"] = available[0]
                    item["_final"] = float(item["_score"])
                    topic_hits.append(item)
            if topic_hits:
                top = self._rank_candidates(topic_hits, "_score")[0]
                topic = str(top.get("_topic_hit") or "")
                if topic:
                    self._topic_last[topic] = now
                logger.info(
                    "[AIRZ-MEDIA] topic send #%s topic=%s base=%.2f manual=%s bot_emotion=%s",
                    top.get("id"), topic, float(top["_score"]),
                    bool(top.get("is_manual_tagged")), "、".join(bot_emotions),
                )
                return top

        # 取语境/情绪启发分最高的前 8 个交给模型，分别打语境分和情绪分。
        scored.sort(key=lambda x: max(x["_heuristic_context"], x["_heuristic_emotion"]), reverse=True)
        llm_data: dict[str, Any] = {"scores": {}, "user_emotion": [], "bot_emotion": []}
        if self.config.get("llm_context", True):
            llm_data = await self._llm_context_scores(scored[:8], ctx_text, reply_text, umo)
        llm_scores: dict[int, dict[str, float]] = llm_data.get("scores") or {}
        model_bot = _labels(llm_data.get("bot_emotion") or [], 5)
        if model_bot:
            bot_emotions = model_bot
            # bot 情绪变了，重算基础分与情绪启发分。
            for item in scored:
                item["_score"] = self._score(item, ctx_text, bot_emotions, reply_text)
                item["_heuristic_emotion"] = self._emotion_heuristic(item, bot_emotions)

        base_w = float(self.config.get("base_weight", 0.25))
        ctx_w = float(self.config.get("context_weight", 0.45))
        emo_w = float(self.config.get("emotion_weight", 0.30))
        total_w = base_w + ctx_w + emo_w
        if total_w <= 0:
            base_w, ctx_w, emo_w, total_w = 0.25, 0.45, 0.30, 1.0
        fit_threshold = float(self.config.get("direct_fit_threshold", 0.80))

        for item in scored:
            mid = int(item["id"])
            llm = llm_scores.get(mid, {})
            llm_ctx = float(llm.get("context", 0.0))
            llm_emo = float(llm.get("emotion", 0.0))
            h_ctx = float(item["_heuristic_context"])
            h_emo = float(item["_heuristic_emotion"])
            # 模型分是主信号；模型不可用时完全退化为启发分，不伪造 0 分。
            context = 0.70 * llm_ctx + 0.30 * h_ctx if llm else h_ctx
            emotion = 0.70 * llm_emo + 0.30 * h_emo if llm else h_emo
            item["_llm_context"] = llm_ctx
            item["_llm_emotion"] = llm_emo
            item["_context"] = context
            item["_emotion"] = emotion
            item["_final"] = (
                base_w * float(item["_score"]) + ctx_w * context + emo_w * emotion
            ) / total_w
            item["_context_fit"] = context >= fit_threshold
            item["_emotion_fit"] = emotion >= fit_threshold

        direct = [x for x in scored if x.get("_context_fit") or x.get("_emotion_fit")]
        if direct:
            direct.sort(
                key=lambda x: (
                    -max(float(x["_context"]), float(x["_emotion"])),
                    -float(x["_final"]),
                    int(x["_usage_count"]),
                    random.random(),
                )
            )
            top = direct[0]
            logger.info(
                "[AIRZ-MEDIA] direct send #%s context_fit=%s emotion_fit=%s "
                "context=%.2f emotion=%.2f final=%.2f manual=%s "
                "user_emotion=%s bot_emotion=%s",
                top.get("id"), bool(top.get("_context_fit")), bool(top.get("_emotion_fit")),
                float(top["_context"]), float(top["_emotion"]), float(top["_final"]),
                bool(top.get("is_manual_tagged")),
                "、".join(user_emotions), "、".join(bot_emotions),
            )
            return top

        scored.sort(
            key=lambda x: (
                -float(x["_final"]),
                int(x["_usage_count"]),
                random.random(),
            )
        )
        top = scored[0]
        threshold = float(self.config.get("min_score", 0.40))
        probability = float(self.config.get("send_probability", 0.80))
        logger.info(
            "[AIRZ-MEDIA] score pick #%s base=%.2f context=%.2f emotion=%.2f final=%.2f "
            "threshold=%.2f probability=%.2f manual=%s user_emotion=%s bot_emotion=%s",
            top.get("id"), float(top["_score"]), float(top["_context"]), float(top["_emotion"]),
            float(top["_final"]), threshold, probability,
            bool(top.get("is_manual_tagged")),
            "、".join(user_emotions), "、".join(bot_emotions),
        )
        if float(top["_final"]) <= threshold:
            return None
        if random.random() >= max(0.0, min(1.0, probability)):
            logger.info("[AIRZ-MEDIA] score pick #%s skipped by probability", top.get("id"))
            return None
        return top

    @filter.on_decorating_result()
    async def auto_attach_media(self, event: AstrMessageEvent):
        if not self.config.get("enabled") or not self.config.get("auto_send"):
            return
        if event.get_extra("airz_media_handled"):
            return
        result = event.get_result()
        if not result or not result.is_llm_result():
            return
        if any(isinstance(c, (Image, Record)) for c in result.chain):
            return
        text = result.get_plain_text().strip()
        if not text:
            return
        if (event.message_str or "").strip().startswith(("/", "／")):
            return
        if event.get_group_id():
            if not self.config.get("allow_group", True):
                return
        elif not self.config.get("allow_private", True):
            return
        umo = event.unified_msg_origin
        ctx_text = self._context_text(umo, text)
        current = (event.message_str or "").strip()
        if current:
            ctx_text = (ctx_text + "\nuser: " + current).strip()
        chosen = await self._pick(ctx_text, reply_text=text, umo=umo, current_text=current)
        if not chosen:
            return
        self.store.mark_used(int(chosen["id"]))
        event.set_extra("airz_media_handled", True)
        path = Path(chosen["path"])
        use_native = (
            chosen.get("kind") == "sticker"
            and self.config.get("native_sticker")
            and bool(chosen.get("external_id"))
            and bool(getattr(event, "bot", None))
        )
        if use_native:
            task = asyncio.create_task(self._send_native_after(event, chosen))
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)
        else:
            try:
                result.chain.append(Image.fromFileSystem(str(path)))
                logger.info("[AIRZ-MEDIA] attached #%s score=%.2f", chosen["id"], chosen.get("_score", 0))
            except Exception as exc:
                logger.warning("[AIRZ-MEDIA] attach failed: %s", exc)

    async def _send_native_after(self, event: AstrMessageEvent, row: dict[str, Any]) -> None:
        # 等文本消息先被发送，再补发原表情；失败则退化为普通图片。
        # 这里只做“追加”：不删改、不替换原回复的文本组件，
        # 因此无论原生表情发送成功与否，文字内容都照常保留。
        await asyncio.sleep(1.6)
        try:
            await self._send_native(event, row)
        except Exception as exc:
            logger.info("[AIRZ-MEDIA] native sticker failed, fallback image: %s", exc)
            try:
                await event.send(MessageChain(chain=[Image.fromFileSystem(str(row["path"]))]))
            except Exception as send_exc:
                logger.warning("[AIRZ-MEDIA] fallback image failed: %s", send_exc)
