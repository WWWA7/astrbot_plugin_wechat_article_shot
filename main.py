import asyncio
import hashlib
import html
import io
import json
import re
import time
import traceback
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot.core.star.filter.platform_adapter_type import PlatformAdapterType

try:
    import requests
except ImportError:
    requests = None

try:
    from bs4 import BeautifulSoup, NavigableString, Tag
except ImportError:
    BeautifulSoup = None
    NavigableString = None
    Tag = None

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    Image = None
    ImageDraw = None
    ImageFont = None


DATA_DIR = Path("data", "plugins_data", "astrbot_plugin_wechat_article_shot")
DATA_DIR.mkdir(parents=True, exist_ok=True)
SHOT_DIR = DATA_DIR / "shots"
SHOT_DIR.mkdir(parents=True, exist_ok=True)

WECHAT_HOST_KEYWORDS = (
    "mp.weixin.qq.com",
    "weixin.qq.com",
)

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 "
    "Mobile/15E148 Safari/604.1"
)


@register(
    "wechat_article_shot",
    "Cline",
    "自动识别群聊中的微信公众号文章链接或卡片，提取标题、作者、正文文字和图片并在本地生成长图发送到群内。",
    "1.2.0",
)
class WechatArticleShotPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config or {}
        self._recent_tasks: dict[str, float] = {}

    def _get_bool(self, key: str, default: bool = False) -> bool:
        return bool(self.config.get(key, default))

    def _get_int(self, key: str, default: int) -> int:
        try:
            return int(self.config.get(key, default))
        except Exception:
            return default

    def _get_text(self, key: str, default: str = "") -> str:
        value = self.config.get(key, default)
        return default if value is None else str(value)

    def _get_list(self, key: str, default: list[str] | None = None) -> list[str]:
        if default is None:
            default = []
        value = self.config.get(key, default)
        if not isinstance(value, list):
            return default
        return [str(v).strip() for v in value if str(v).strip()]

    @filter.platform_adapter_type(PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AiocqhttpMessageEvent):
        if not self._get_bool("enabled", True):
            return

        if self._is_sender_blocked(event):
            return

        article_url = self._extract_article_url(event)
        if not article_url:
            return

        article_url = self._normalize_wechat_url(article_url)
        if not self._is_valid_wechat_article_url(article_url):
            return

        dedupe_key = self._build_dedupe_key(event, article_url)
        ttl = self._get_int("dedupe_ttl_seconds", 300)

        if self._is_recently_processed(dedupe_key, ttl):
            logger.info(f"[wechat_article_shot] 命中去重，跳过重复处理: {dedupe_key}")
            return

        self._mark_processed(dedupe_key)
        asyncio.create_task(self._process_and_send(event, article_url))

    async def _process_and_send(self, event: AiocqhttpMessageEvent, article_url: str):
        group_id = getattr(event.message_obj, "group_id", "") or event.get_group_id()
        logger.info(f"[wechat_article_shot] 开始处理群 {group_id} 的公众号文章: {article_url}")

        try:
            image_path = await asyncio.to_thread(self._build_local_article_image, article_url)

            if not image_path or not Path(image_path).exists():
                await self._safe_send_text(
                    event,
                    self._get_text(
                        "capture_failed_message",
                        "公众号文章处理失败，请稍后重试。",
                    ),
                )
                return

            await self.context.send_message(
                event.unified_msg_origin,
                MessageChain().message("").file_image(str(image_path)),
            )
            logger.info(f"[wechat_article_shot] 图片发送成功: {image_path}")
        except Exception as e:
            logger.error(f"[wechat_article_shot] 处理失败: {e}")
            logger.error(traceback.format_exc())
            if self._get_bool("send_error_tip", False):
                await self._safe_send_text(event, f"公众号文章处理失败：{e}")

    def _build_local_article_image(self, url: str) -> str | None:
        if requests is None or BeautifulSoup is None or Image is None or ImageDraw is None or ImageFont is None:
            logger.error("[wechat_article_shot] 缺少本地排版所需依赖 requests/bs4/Pillow")
            return None

        article = self._fetch_article_content(url)
        if not article:
            return None

        output_name = f"{int(time.time())}_{hashlib.md5((url + '_local').encode('utf-8')).hexdigest()[:12]}.jpg"
        output_path = SHOT_DIR / output_name

        canvas_width = self._get_int("local_canvas_width", 900)
        padding = self._get_int("local_padding", 48)
        line_spacing = self._get_int("local_line_spacing", 16)
        section_gap = self._get_int("local_section_gap", 28)
        image_gap = self._get_int("local_image_gap", 20)
        max_images = self._get_int("local_max_images", 6)
        bg_color = self._get_text("local_background_color", "#F7F8FA")
        card_color = self._get_text("local_card_color", "#FFFFFF")
        text_color = self._get_text("local_text_color", "#1F2329")
        sub_color = self._get_text("local_sub_text_color", "#667085")
        divider_color = self._get_text("local_divider_color", "#E5E7EB")

        title_font = self._load_font(42, bold=True)
        meta_font = self._load_font(26, bold=False)
        body_font = self._load_font(30, bold=False)
        footer_font = self._load_font(22, bold=False)

        content_width = canvas_width - padding * 2
        body_inner_width = content_width - 32 * 2

        blocks: list[dict[str, Any]] = []
        total_height = padding

        title_lines = self._wrap_text(article["title"], title_font, content_width - 64)
        title_height = self._measure_lines_height(title_lines, title_font, 18)

        meta_text = " · ".join([x for x in [article.get("author"), article.get("publish_time")] if x])
        meta_lines = self._wrap_text(meta_text or "微信公众号文章", meta_font, content_width - 64)
        meta_height = self._measure_lines_height(meta_lines, meta_font, 10)

        total_height += 36 + title_height + 18 + meta_height + 30

        body_blocks = article["blocks"][: self._get_int("local_max_blocks", 120)]
        for block in body_blocks:
            if block["type"] == "text":
                text = block["text"].strip()
                if not text:
                    continue
                lines = self._wrap_text(text, body_font, body_inner_width)
                block_height = self._measure_lines_height(lines, body_font, line_spacing)
                blocks.append({"type": "text", "lines": lines, "height": block_height})
                total_height += block_height + section_gap
            elif block["type"] == "image" and max_images > 0:
                image = self._download_and_resize_image(block["src"], body_inner_width)
                if image:
                    blocks.append({"type": "image", "image": image, "height": image.height})
                    total_height += image.height + image_gap
                    max_images -= 1

        footer_lines = self._wrap_text(f"原文链接：{url}", footer_font, body_inner_width)
        footer_height = self._measure_lines_height(footer_lines, footer_font, 8)
        total_height += 36 + footer_height + 50 + padding

        image = Image.new("RGB", (canvas_width, max(total_height, 400)), bg_color or "#F7F8FA")
        draw = ImageDraw.Draw(image)

        self._draw_rounded_rect(
            draw,
            (padding // 2, padding // 2, canvas_width - padding // 2, total_height - padding // 2),
            radius=28,
            fill=card_color or "#FFFFFF",
        )

        y = padding + 8
        title_x = padding + 32

        y = self._draw_lines(draw, title_lines, title_font, title_x, y, text_color or "#1F2329", 18)
        y += 12
        y = self._draw_lines(draw, meta_lines, meta_font, title_x, y, sub_color or "#667085", 10)
        y += 24
        draw.line(
            [(title_x, y), (canvas_width - title_x, y)],
            fill=divider_color or "#E5E7EB",
            width=2,
        )
        y += 28

        for block in blocks:
            if block["type"] == "text":
                y = self._draw_lines(
                    draw,
                    block["lines"],
                    body_font,
                    title_x,
                    y,
                    text_color or "#1F2329",
                    line_spacing,
                )
                y += section_gap
            elif block["type"] == "image":
                image.paste(block["image"], (title_x, y))
                y += block["height"] + image_gap

        y += 8
        draw.line(
            [(title_x, y), (canvas_width - title_x, y)],
            fill=divider_color or "#E5E7EB",
            width=2,
        )
        y += 24
        self._draw_lines(draw, footer_lines, footer_font, title_x, y, sub_color or "#667085", 8)

        image.save(output_path, format="JPEG", quality=self._get_int("image_quality", 90), optimize=True)
        return str(output_path)

    def _fetch_article_content(self, url: str) -> dict[str, Any] | None:
        try:
            response = requests.get(
                url,
                headers={"User-Agent": MOBILE_UA},
                timeout=self._get_int("http_timeout_seconds", 20),
            )
            response.raise_for_status()
        except Exception as e:
            logger.error(f"[wechat_article_shot] 获取文章页面失败: {e}")
            return None

        html_bytes = response.content
        html_text = None

        for encoding in ("utf-8", "utf-8-sig", response.encoding, response.apparent_encoding, "gb18030"):
            if not encoding:
                continue
            try:
                html_text = html_bytes.decode(encoding, errors="strict")
                break
            except Exception:
                continue

        if html_text is None:
            html_text = html_bytes.decode("utf-8", errors="ignore")

        soup = BeautifulSoup(html_text, "html.parser")

        title = self._extract_first_text(
            soup,
            [
                "#activity-name",
                "h1.rich_media_title",
                "h1",
                "title",
            ],
        ) or "微信公众号文章"

        author = self._extract_first_text(
            soup,
            [
                "#js_name",
                ".wx_follow_nickname",
                ".rich_media_meta_nickname",
            ],
        ) or "公众号"

        publish_time = self._extract_publish_time(soup)
        content_root = soup.select_one("#img-content") or soup.select_one(".rich_media_content") or soup.body
        if not content_root:
            return None

        blocks = self._extract_content_blocks(content_root, url)
        if not blocks:
            text_fallback = content_root.get_text("\n", strip=True)
            text_fallback = self._clean_article_text(text_fallback)
            if text_fallback:
                blocks = [{"type": "text", "text": text_fallback}]

        return {
            "title": self._clean_article_text(title),
            "author": self._clean_article_text(author),
            "publish_time": publish_time,
            "blocks": blocks,
        }

    def _extract_content_blocks(self, root: Any, base_url: str) -> list[dict[str, str]]:
        blocks: list[dict[str, str]] = []
        seen_texts: set[str] = set()
        seen_images: set[str] = set()

        for node in root.descendants:
            if Tag is not None and isinstance(node, Tag):
                if node.name in ("script", "style", "iframe", "svg"):
                    continue

                if node.name in ("img",):
                    src = (
                        node.get("data-src")
                        or node.get("src")
                        or node.get("data-backsrc")
                        or node.get("data-original")
                    )
                    if src:
                        src = urljoin(base_url, src.strip())
                        if src not in seen_images:
                            seen_images.add(src)
                            blocks.append({"type": "image", "src": src})
                    continue

                if node.name in ("p", "section", "blockquote", "li", "h2", "h3", "h4"):
                    text = self._clean_article_text(node.get_text(" ", strip=True))
                    if text and text not in seen_texts and len(text) >= 2:
                        seen_texts.add(text)
                        blocks.append({"type": "text", "text": text})

            elif NavigableString is not None and isinstance(node, NavigableString):
                parent = getattr(node, "parent", None)
                if parent and getattr(parent, "name", "") in ("p", "section", "blockquote", "li", "h2", "h3", "h4"):
                    continue

        return blocks

    def _extract_first_text(self, soup: Any, selectors: list[str]) -> str:
        for selector in selectors:
            node = soup.select_one(selector)
            if node:
                text = node.get_text(" ", strip=True)
                if text:
                    return text
        return ""

    def _extract_publish_time(self, soup: Any) -> str:
        selector_text = self._extract_first_text(
            soup,
            [
                "#publish_time",
                ".rich_media_meta.rich_media_meta_text",
            ],
        )
        if selector_text:
            return selector_text

        text = soup.get_text("\n", strip=True)
        patterns = [
            r"20\d{2}-\d{1,2}-\d{1,2}",
            r"20\d{2}/\d{1,2}/\d{1,2}",
            r"20\d{2}年\d{1,2}月\d{1,2}日",
        ]
        for pattern in patterns:
            m = re.search(pattern, text)
            if m:
                return m.group(0)
        return ""

    def _download_and_resize_image(self, url: str, max_width: int) -> Any | None:
        try:
            response = requests.get(
                url,
                headers={"User-Agent": MOBILE_UA},
                timeout=self._get_int("http_timeout_seconds", 20),
            )
            response.raise_for_status()
            img = Image.open(io.BytesIO(response.content)).convert("RGB")
            width, height = img.size
            if width <= 0 or height <= 0:
                return None
            if width > max_width:
                new_height = int(height * (max_width / width))
                img = img.resize((max_width, new_height))
            return img
        except Exception as e:
            logger.info(f"[wechat_article_shot] 下载正文图片失败，已跳过: {e}")
            return None

    def _load_font(self, size: int, bold: bool = False):
        candidates = []
        if bold:
            candidates.extend(
                [
                    "C:/Windows/Fonts/msyhbd.ttc",
                    "C:/Windows/Fonts/msyhbd.ttf",
                    "C:/Windows/Fonts/SourceHanSansSC-Bold.otf",
                    "C:/Windows/Fonts/simhei.ttf",
                    "C:/Windows/Fonts/simsun.ttc",
                    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
                    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
                    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
                ]
            )
        else:
            candidates.extend(
                [
                    "C:/Windows/Fonts/msyh.ttc",
                    "C:/Windows/Fonts/msyh.ttf",
                    "C:/Windows/Fonts/SourceHanSansSC-Regular.otf",
                    "C:/Windows/Fonts/simsun.ttc",
                    "C:/Windows/Fonts/simhei.ttf",
                    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
                    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
                    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
                ]
            )

        custom_font = self._get_text("font_path", "")
        if custom_font:
            candidates.insert(0, custom_font)

        for path in candidates:
            try:
                if Path(path).exists():
                    logger.info(f"[wechat_article_shot] 使用字体: {path}")
                    return ImageFont.truetype(path, size=size)
            except Exception:
                pass

        logger.error(
            "[wechat_article_shot] 未找到可用中文字体，请在配置中设置 font_path，例如 C:/Windows/Fonts/msyh.ttc"
        )
        raise RuntimeError("未找到可用中文字体，请在插件配置中设置 font_path")

    def _wrap_text(self, text: str, font: Any, max_width: int) -> list[str]:
        text = self._clean_article_text(text)
        if not text:
            return []

        lines: list[str] = []
        paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
        for paragraph in paragraphs:
            current = ""
            for ch in paragraph:
                test = current + ch
                if self._text_width(test, font) <= max_width:
                    current = test
                else:
                    if current:
                        lines.append(current)
                    current = ch
            if current:
                lines.append(current)
        return lines or [""]

    def _text_width(self, text: str, font: Any) -> int:
        try:
            bbox = font.getbbox(text)
            return max(0, bbox[2] - bbox[0])
        except Exception:
            return len(text) * max(getattr(font, "size", 18), 18)

    def _font_height(self, font: Any) -> int:
        try:
            bbox = font.getbbox("测试Ag")
            return max(1, bbox[3] - bbox[1])
        except Exception:
            return max(getattr(font, "size", 20), 20)

    def _measure_lines_height(self, lines: list[str], font: Any, spacing: int) -> int:
        if not lines:
            return 0
        return len(lines) * self._font_height(font) + (len(lines) - 1) * spacing

    def _draw_lines(self, draw: Any, lines: list[str], font: Any, x: int, y: int, fill: Any, spacing: int) -> int:
        font_height = self._font_height(font)
        for line in lines:
            draw.text((x, y), line, font=font, fill=fill)
            y += font_height + spacing
        return y - spacing if lines else y

    def _draw_rounded_rect(self, draw: Any, box: tuple[int, int, int, int], radius: int, fill: Any):
        try:
            draw.rounded_rectangle(box, radius=radius, fill=fill)
        except Exception:
            draw.rectangle(box, fill=fill)

    def _clean_article_text(self, text: str) -> str:
        text = html.unescape(text or "")
        text = text.replace("\xa0", " ")
        text = re.sub(r"\s+\n", "\n", text)
        text = re.sub(r"\n\s+", "\n", text)
        text = re.sub(r"[ \t]{2,}", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def _extract_article_url(self, event: AiocqhttpMessageEvent) -> str | None:
        text = self._collect_text_from_event(event)
        if text:
            url = self._extract_wechat_url_from_text(text)
            if url:
                return url

        try:
            for seg in event.get_messages():
                url = self._extract_url_from_segment(seg)
                if url:
                    return url
        except Exception:
            pass

        raw = getattr(event.message_obj, "raw_message", None)
        url = self._extract_wechat_url_from_any(raw)
        if url:
            return url

        try:
            msg_obj = getattr(event, "message_obj", None)
            url = self._extract_wechat_url_from_any(msg_obj)
            if url:
                return url
        except Exception:
            pass

        return None

    def _collect_text_from_event(self, event: AiocqhttpMessageEvent) -> str:
        parts: list[str] = []

        for attr in ("message_str", "raw_message", "message"):
            try:
                value = getattr(event.message_obj, attr, None)
                if isinstance(value, str):
                    parts.append(value)
            except Exception:
                pass

        try:
            for seg in event.get_messages():
                for attr in ("text", "data", "content", "url", "xml", "json"):
                    value = getattr(seg, attr, None)
                    if isinstance(value, str):
                        parts.append(value)
                    elif isinstance(value, dict):
                        parts.append(json.dumps(value, ensure_ascii=False))
        except Exception:
            pass

        return "\n".join([p for p in parts if p])

    def _extract_url_from_segment(self, seg: Any) -> str | None:
        for attr in ("url", "content", "text", "xml", "json"):
            value = getattr(seg, attr, None)
            if isinstance(value, str):
                url = self._extract_wechat_url_from_text(value)
                if url:
                    return url
            elif isinstance(value, dict):
                url = self._extract_wechat_url_from_any(value)
                if url:
                    return url

        data = getattr(seg, "data", None)
        if isinstance(data, dict):
            return self._extract_wechat_url_from_any(data)
        if isinstance(data, str):
            return self._extract_wechat_url_from_text(data)
        return None

    def _extract_wechat_url_from_any(self, data: Any) -> str | None:
        if data is None:
            return None

        if isinstance(data, str):
            direct = self._extract_wechat_url_from_text(data)
            if direct:
                return direct
            return self._extract_wechat_url_from_xml_or_json_text(data)

        if isinstance(data, dict):
            for key in (
                "url",
                "jumpUrl",
                "jump_url",
                "article_url",
                "content",
                "text",
                "desc",
                "prompt",
                "xml",
                "json",
                "data",
            ):
                if key in data:
                    url = self._extract_wechat_url_from_any(data[key])
                    if url:
                        return url
            for _, value in data.items():
                url = self._extract_wechat_url_from_any(value)
                if url:
                    return url
            return None

        if isinstance(data, (list, tuple, set)):
            for item in data:
                url = self._extract_wechat_url_from_any(item)
                if url:
                    return url
            return None

        try:
            for attr in ("raw_message", "message", "message_str", "xml", "json", "data"):
                value = getattr(data, attr, None)
                if value is not None:
                    url = self._extract_wechat_url_from_any(value)
                    if url:
                        return url
        except Exception:
            pass

        return None

    def _extract_wechat_url_from_text(self, text: str) -> str | None:
        if not text:
            return None

        text = html.unescape(text)
        matches = re.findall(r"https?://[^\s<>\]\)\"']+", text, flags=re.I)
        for candidate in matches:
            candidate = candidate.strip(" \r\n\t<>[]()\"'，。；;、")
            normalized = self._normalize_wechat_url(candidate)
            if self._is_valid_wechat_article_url(normalized):
                return normalized

        return None

    def _extract_wechat_url_from_xml_or_json_text(self, text: str) -> str | None:
        if not text:
            return None

        unescaped = html.unescape(text)

        xml_patterns = [
            r"<url><!\[CDATA\[(https?://[^\]]+)\]\]></url>",
            r"<url>(https?://[^<]+)</url>",
            r"<wechat_url>(https?://[^<]+)</wechat_url>",
        ]
        for pattern in xml_patterns:
            m = re.search(pattern, unescaped, flags=re.I)
            if m:
                normalized = self._normalize_wechat_url(m.group(1))
                if self._is_valid_wechat_article_url(normalized):
                    return normalized

        json_url_patterns = [
            r'"url"\s*:\s*"([^"]+)"',
            r'"jumpUrl"\s*:\s*"([^"]+)"',
            r'"jump_url"\s*:\s*"([^"]+)"',
        ]
        for pattern in json_url_patterns:
            m = re.search(pattern, unescaped, flags=re.I)
            if m:
                value = m.group(1).replace("\\/", "/")
                normalized = self._normalize_wechat_url(value)
                if self._is_valid_wechat_article_url(normalized):
                    return normalized

        return self._extract_wechat_url_from_text(unescaped)

    def _normalize_wechat_url(self, url: str) -> str:
        if not url:
            return url

        url = html.unescape(url).strip()

        try:
            parsed = urlparse(url)
            query = parse_qs(parsed.query)

            for key in ("url", "target", "target_url", "jump", "jump_url"):
                if key in query and query[key]:
                    nested = unquote(query[key][0])
                    if "mp.weixin.qq.com" in nested:
                        url = nested
                        break
        except Exception:
            pass

        url = url.strip(" \r\n\t<>[]()\"'，。；;、")
        return url

    def _is_valid_wechat_article_url(self, url: str) -> bool:
        if not url or not url.startswith(("http://", "https://")):
            return False
        return any(host in url for host in WECHAT_HOST_KEYWORDS)

    def _build_dedupe_key(self, event: AiocqhttpMessageEvent, url: str) -> str:
        group_id = getattr(event.message_obj, "group_id", "") or event.get_group_id()
        mode = self._get_text("dedupe_scope", "group_url").lower()

        if mode == "global_url":
            return f"global:{url}"
        if mode == "group_url_sender":
            sender_id = getattr(getattr(event, "message_obj", None), "sender", None)
            sender_uid = getattr(sender_id, "user_id", "") if sender_id else ""
            sender_uid = (
                sender_uid
                or getattr(event.message_obj, "user_id", "")
                or event.get_sender_id()
            )
            return f"group_sender:{group_id}:{sender_uid}:{url}"
        return f"group:{group_id}:{url}"

    def _cleanup_recent(self, ttl: int):
        now = time.monotonic()
        expired = [k for k, ts in self._recent_tasks.items() if now - ts > ttl]
        for key in expired:
            self._recent_tasks.pop(key, None)

    def _is_recently_processed(self, key: str, ttl: int) -> bool:
        self._cleanup_recent(ttl)
        ts = self._recent_tasks.get(key)
        return ts is not None and time.monotonic() - ts <= ttl

    def _mark_processed(self, key: str):
        self._recent_tasks[key] = time.monotonic()

    def _is_sender_blocked(self, event: AiocqhttpMessageEvent) -> bool:
        blocked_ids = set(self._get_list("blocked_sender_ids", []))
        if not blocked_ids:
            return False

        sender_id = ""
        try:
            sender_id = str(getattr(event.message_obj, "user_id", "") or event.get_sender_id())
        except Exception:
            pass
        return sender_id in blocked_ids

    async def _safe_send_text(self, event: AiocqhttpMessageEvent, text: str):
        text = (text or "").strip()
        if not text:
            return
        try:
            await self.context.send_message(
                event.unified_msg_origin,
                MessageChain().message(text),
            )
        except Exception as e:
            logger.error(f"[wechat_article_shot] 发送文本提示失败: {e}")

    async def terminate(self):
        logger.info("[wechat_article_shot] 插件已停止")
