"""Telegram Rich HTML helpers (Bot API 10.1+) and premium emoji rendering."""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from typing import Any, Iterable

from aiogram import types
from aiogram.methods import TelegramMethod
from aiogram.exceptions import TelegramBadRequest


MAX_RICH_MESSAGE_CHARS = 32768
_HTML_TAG_RE = re.compile(r"<\s*(/?)\s*([a-z][a-z0-9-]*)\b([^>]*)>", re.IGNORECASE)
_LEGACY_HTML_TAGS = {
    "a", "b", "blockquote", "code", "del", "em", "i", "ins", "pre",
    "s", "span", "strike", "strong", "tg-spoiler", "u",
}


class _SendRichMessage(TelegramMethod[Any]):
    """Raw API method fallback for aiogram versions predating Bot API 10.1."""

    __api_method__ = "sendRichMessage"
    __returning__ = Any

    chat_id: int | str
    rich_message: dict[str, Any]
    disable_notification: bool | None = None
    protect_content: bool | None = None
    reply_parameters: dict[str, Any] | None = None
    reply_markup: Any | None = None


def _offset_index(text: str, offset: int) -> int | None:
    """Translate Telegram's UTF-16 offsets to Python code-point indexes."""
    if offset < 0:
        return None
    units = 0
    for index, char in enumerate(text):
        if units == offset:
            return index
        units += 2 if ord(char) > 0xFFFF else 1
        if units > offset:
            return None
    return len(text) if units == offset else None


def _entity_html(entity: types.MessageEntity) -> tuple[str, str] | None:
    kind = str(entity.type)
    pairs = {
        "bold": ("<b>", "</b>"),
        "italic": ("<i>", "</i>"),
        "underline": ("<u>", "</u>"),
        "strikethrough": ("<s>", "</s>"),
        "spoiler": ("<tg-spoiler>", "</tg-spoiler>"),
        "code": ("<code>", "</code>"),
        "blockquote": ("<blockquote>", "</blockquote>"),
        "expandable_blockquote": ("<blockquote expandable>", "</blockquote>"),
    }
    if kind in pairs:
        return pairs[kind]
    if kind == "pre":
        language = getattr(entity, "language", None)
        code_open = f'<code class="language-{html.escape(language, quote=True)}">' if language else ""
        code_close = "</code>" if language else ""
        return f"<pre>{code_open}", f"{code_close}</pre>"
    if kind == "text_link" and getattr(entity, "url", None):
        return f'<a href="{html.escape(entity.url, quote=True)}">', "</a>"
    if kind == "text_mention" and getattr(entity, "user", None):
        return f'<a href="tg://user?id={entity.user.id}">', "</a>"
    if kind == "custom_emoji" and getattr(entity, "custom_emoji_id", None):
        return f'<tg-emoji emoji-id="{html.escape(entity.custom_emoji_id, quote=True)}">', "</tg-emoji>"
    return None


def entities_to_rich_html(text: str, entities: Iterable[types.MessageEntity] | None) -> str:
    """Escape text and preserve Telegram formatting/entities in Rich HTML."""
    if not text:
        return ""
    normalized: list[tuple[int, int, str, str]] = []
    for entity in entities or ():
        tags = _entity_html(entity)
        if not tags:
            continue
        start = _offset_index(text, entity.offset)
        end = _offset_index(text, entity.offset + entity.length)
        if start is None or end is None or start >= end:
            continue
        normalized.append((start, end, tags[0], tags[1]))

    output: list[str] = []
    active: list[tuple[int, int, str, str]] = []
    for index, char in enumerate(text):
        desired = sorted(
            (item for item in normalized if item[0] <= index < item[1]),
            key=lambda item: (item[0], -item[1]),
        )
        common = 0
        while common < min(len(active), len(desired)) and active[common] == desired[common]:
            common += 1
        for item in reversed(active[common:]):
            output.append(item[3])
        for item in desired[common:]:
            output.append(item[2])
        output.append(html.escape(char, quote=False))
        active = desired
    for item in reversed(active):
        output.append(item[3])
    return "".join(output)


def rich_html_from_message(message: types.Message) -> str:
    """Convert Telegram text formatting to Rich HTML, or accept explicitly typed HTML."""
    source = message.text or message.caption or ""
    entities = message.entities or message.caption_entities
    if source and entities:
        return entities_to_rich_html(source, entities)
    if source and re.search(r"</?[A-Za-z][^>]*>", source):
        return source
    if source:
        return html.escape(source, quote=False)

    # Telegram Bot API 10.1+ may deliver content composed in its rich editor
    # through Message.rich_message, with neither text nor caption populated.
    # aiogram versions that do not model the field preserve it in model_extra.
    rich_message = getattr(message, "rich_message", None)
    if rich_message is None:
        rich_message = (getattr(message, "model_extra", None) or {}).get("rich_message")
    return _rich_message_to_html(rich_message)


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        result = dump(mode="python", exclude_none=True)
        return result if isinstance(result, dict) else {}
    legacy_dump = getattr(value, "dict", None)
    if callable(legacy_dump):
        result = legacy_dump(exclude_none=True)
        return result if isinstance(result, dict) else {}
    return {}


def _rich_text_to_html(value: Any) -> str:
    if isinstance(value, str):
        return html.escape(value, quote=False)
    if isinstance(value, (list, tuple)):
        return "".join(_rich_text_to_html(item) for item in value)

    data = _as_mapping(value)
    if not data:
        return ""
    kind = str(data.get("type", "")).lower()
    body = _rich_text_to_html(data.get("text", data.get("children", "")))
    tags = {
        "bold": ("<b>", "</b>"),
        "italic": ("<i>", "</i>"),
        "underline": ("<u>", "</u>"),
        "strikethrough": ("<s>", "</s>"),
        "spoiler": ("<tg-spoiler>", "</tg-spoiler>"),
        "marked": ("<mark>", "</mark>"),
        "subscript": ("<sub>", "</sub>"),
        "superscript": ("<sup>", "</sup>"),
        "code": ("<code>", "</code>"),
    }
    if kind in tags:
        return tags[kind][0] + body + tags[kind][1]
    if kind == "url":
        url = data.get("url")
        return f'<a href="{html.escape(str(url), quote=True)}">{body}</a>' if url else body
    if kind in {"email_address", "email"}:
        address = data.get("email_address") or data.get("email")
        return f'<a href="mailto:{html.escape(str(address), quote=True)}">{body or html.escape(str(address))}</a>' if address else body
    if kind in {"phone_number", "phone"}:
        phone = data.get("phone_number") or data.get("phone")
        return f'<a href="tel:{html.escape(str(phone), quote=True)}">{body or html.escape(str(phone))}</a>' if phone else body
    if kind == "text_mention":
        user_id = data.get("user_id")
        user = _as_mapping(data.get("user"))
        user_id = user_id or user.get("id")
        return f'<a href="tg://user?id={html.escape(str(user_id), quote=True)}">{body}</a>' if user_id else body
    if kind == "custom_emoji":
        emoji_id = data.get("custom_emoji_id") or data.get("emoji_id")
        alternative = data.get("alternative_text") or data.get("alt") or body
        if emoji_id:
            return f'<tg-emoji emoji-id="{html.escape(str(emoji_id), quote=True)}">{alternative}</tg-emoji>'
        return alternative
    if kind == "pre":
        language = data.get("language")
        code = f'<code class="language-{html.escape(str(language), quote=True)}">{body}</code>' if language else body
        return f"<pre>{code}</pre>"
    if kind in {"hashtag", "cashtag", "mention", "bot_command", "date_time", "anchor", "reference"}:
        return body or html.escape(str(data.get("text", "")), quote=False)
    # Unknown rich text types should degrade to readable text, never disappear.
    if body:
        return body
    for key in ("alternative_text", "email_address", "phone_number", "url"):
        if data.get(key):
            return html.escape(str(data[key]), quote=False)
    return ""


def _rich_block_to_html(value: Any) -> str:
    data = _as_mapping(value)
    if not data:
        return _rich_text_to_html(value)
    kind = str(data.get("type", "")).lower()
    if kind in {"paragraph", "heading", "pre", "divider", "blockquote", "quotation", "block_quotation", "expandable_blockquote", "pullquote", "details", "list", "table"}:
        if kind == "divider":
            return "<hr/>"
        if kind == "heading":
            try:
                size = min(6, max(1, int(data.get("size", 2))))
            except (TypeError, ValueError):
                size = 2
            return f"<h{size}>{_rich_text_to_html(data.get('text', ''))}</h{size}>"
        if kind == "pre":
            return _rich_text_to_html({"type": "pre", "text": data.get("text", ""), "language": data.get("language")})
        if kind in {"blockquote", "quotation", "block_quotation"}:
            return f"<blockquote>{_rich_text_to_html(data.get('text', ''))}</blockquote>"
        if kind == "expandable_blockquote":
            return f"<blockquote expandable>{_rich_text_to_html(data.get('text', ''))}</blockquote>"
        if kind == "pullquote":
            return f"<blockquote>{_rich_text_to_html(data.get('text', ''))}</blockquote>"
        if kind == "details":
            summary = _rich_text_to_html(data.get("summary", ""))
            blocks = "".join(_rich_block_to_html(item) for item in data.get("blocks", []))
            return f"<details><summary>{summary}</summary>{blocks}</details>"
        if kind == "list":
            items = []
            for item in data.get("items", []):
                item_data = _as_mapping(item)
                content = "".join(_rich_block_to_html(part) for part in item_data.get("blocks", []))
                if not content:
                    content = _rich_text_to_html(item_data.get("text", item))
                items.append(f"<li>{content}</li>")
            return "<ul>" + "".join(items) + "</ul>"
        if kind == "table":
            rows = data.get("rows", data.get("cells", []))
            rendered_rows = []
            for row in rows:
                cells = row if isinstance(row, (list, tuple)) else _as_mapping(row).get("cells", [])
                rendered_cells = []
                for cell in cells:
                    cell_data = _as_mapping(cell)
                    tag = "th" if cell_data.get("is_header") else "td"
                    rendered_cells.append(f"<{tag}>{_rich_text_to_html(cell_data.get('text', cell))}</{tag}>")
                rendered_rows.append("<tr>" + "".join(rendered_cells) + "</tr>")
            return "<table>" + "".join(rendered_rows) + "</table>"
        return f"<p>{_rich_text_to_html(data.get('text', ''))}</p>"

    # Unknown block types can still contain useful text in nested fields.
    if data.get("text") is not None:
        return f"<p>{_rich_text_to_html(data['text'])}</p>"
    if data.get("blocks"):
        return "".join(_rich_block_to_html(item) for item in data["blocks"])
    return ""


def _rich_message_to_html(value: Any) -> str:
    data = _as_mapping(value)
    if not data:
        return ""
    blocks = data.get("blocks") or []
    return "".join(_rich_block_to_html(block) for block in blocks)


def rich_message_too_long(content: str) -> bool:
    """Telegram Rich Messages are limited to 32,768 UTF-8 characters."""
    return len(content) > MAX_RICH_MESSAGE_CHARS


def requires_rich_message(content: str | None) -> bool:
    """Whether the content uses tags unsupported by Telegram's legacy HTML parse mode."""
    if not content:
        return False
    for match in _HTML_TAG_RE.finditer(content):
        tag_name = match.group(2).lower()
        attributes = match.group(3).lower()
        if tag_name not in _LEGACY_HTML_TAGS:
            return True
        if tag_name == "blockquote" and "expandable" in attributes:
            return True
        if tag_name == "a" and "name=" in attributes and "href=" not in attributes:
            return True
    return False



class _LegacyCaptionRenderer(HTMLParser):
    """Downgrade Rich HTML to tags accepted by Telegram media captions."""

    _LEGACY_TAGS = {
        "a", "b", "blockquote", "code", "del", "em", "i", "ins", "pre",
        "s", "span", "strike", "strong", "tg-spoiler", "u",
    }
    _HEADINGS = {f"h{level}" for level in range(1, 7)}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.parts: list[str] = []
        self.link_stack: list[bool] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        values = {key.lower(): value for key, value in attrs}
        if tag in self._LEGACY_TAGS:
            if tag == "blockquote":
                self.parts.append("<blockquote>")
            elif tag == "a":
                href = values.get("href")
                self.link_stack.append(bool(href))
                if href:
                    self.parts.append(f'<a href="{html.escape(href, quote=True)}">')
            elif tag == "code":
                language_class = values.get("class", "")
                if language_class.startswith("language-") and re.fullmatch(r"language-[a-zA-Z0-9_+-]+", language_class):
                    self.parts.append(f'<code class="{language_class}">')
                else:
                    self.parts.append("<code>")
            elif tag == "span":
                self.parts.append('<span class="tg-spoiler">' if values.get("class") == "tg-spoiler" else "<span>")
            else:
                self.parts.append(f"<{tag}>")
        elif tag in self._HEADINGS:
            # Telegram media captions do not support Rich HTML heading tags.
            # Keep the heading text, but render it at normal caption size.
            self.parts.append("<b>")
        elif tag == "tg-button":
            href = values.get("url")
            self.link_stack.append(bool(href))
            if href:
                self.parts.append(f'<a href="{html.escape(href, quote=True)}">')
        elif tag == "tg-emoji":
            # Custom emoji are not available in legacy captions; their Unicode
            # alternative text remains readable.
            pass
        elif tag == "summary":
            self.parts.append("<b>")
        elif tag == "li":
            self.parts.append("\n• ")
        elif tag in {"br", "p", "div", "ul", "ol", "details", "table", "tr"}:
            self.parts.append("\n")
        elif tag == "hr":
            self.parts.append("\n──────────\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._LEGACY_TAGS:
            if tag == "a":
                if self.link_stack and self.link_stack.pop():
                    self.parts.append("</a>")
            else:
                self.parts.append(f"</{tag}>")
        elif tag in self._HEADINGS or tag == "summary":
            self.parts.append("</b>")
        elif tag == "tg-button":
            if self.link_stack and self.link_stack.pop():
                self.parts.append("</a>")
        elif tag in {"p", "div", "ul", "ol", "details", "table", "tr", "li"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_entityref(self, name: str) -> None:
        self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.parts.append(f"&#{name};")


def legacy_caption_html(content: str | None) -> str:
    """Render Rich HTML as readable legacy HTML for photo/video captions."""
    if not content:
        return ""
    renderer = _LegacyCaptionRenderer()
    renderer.feed(content)
    renderer.close()
    return "".join(renderer.parts).strip()


def _configured_fragment(fragment: str | None) -> str:
    """Preserve known owner-authored Rich HTML; escape plain text fragments."""
    if not fragment:
        return ""
    # The owner explicitly sets these fragments in bot settings. Rich HTML parsing
    # is still performed by Telegram; text from submitters always uses escaping.
    if _HTML_TAG_RE.search(fragment) or html.unescape(fragment) != fragment:
        return fragment
    return html.escape(fragment, quote=False)


def compose_post_html(
    body: str,
    entities: Iterable[types.MessageEntity] | None = None,
    *,
    header: str | None = None,
    footer: str | None = None,
    header_mode: str = "newline",
) -> str:
    header_html = _configured_fragment(header)
    body_html = entities_to_rich_html(body, entities)
    footer_html = _configured_fragment(footer)
    parts: list[str] = []
    if header_html:
        parts.append(header_html + (" " if header_mode == "inline" else "\n\n"))
    parts.append(body_html)
    if footer_html:
        parts.append("\n\n" + footer_html)
    return "".join(parts)


async def send_rich_html(
    bot: Any,
    *,
    chat_id: int | str,
    content: str,
    disable_notification: bool = False,
    protect_content: bool = False,
    reply_to_message_id: int | None = None,
    reply_markup: Any | None = None,
) -> Any:
    """Send a Telegram Rich Message through Bot API 10.1+ without lossy fallback."""
    rich_message = {"html": content}
    reply_parameters = {"message_id": reply_to_message_id} if reply_to_message_id is not None else None
    native_method = getattr(bot, "send_rich_message", None)
    try:
        if native_method:
            return await native_method(
                chat_id=chat_id,
                rich_message=rich_message,
                disable_notification=disable_notification,
                protect_content=protect_content,
                reply_parameters=reply_parameters,
                reply_markup=reply_markup,
            )
        raw = await bot.session.make_request(
            bot,
            _SendRichMessage(
                chat_id=chat_id,
                rich_message=rich_message,
                disable_notification=disable_notification,
                protect_content=protect_content,
                reply_parameters=reply_parameters,
                reply_markup=reply_markup,
            ),
        )
    except TelegramBadRequest as exc:
        description = str(exc).lower()
        if "tg-emoji" not in content or not any(word in description for word in ("emoji", "premium")):
            raise
        # Custom emoji are restricted for some bot/channel combinations. Preserve
        # their Unicode alternative text and retry only after an explicit rejection.
        fallback_html = re.sub(
            r'<tg-emoji\s+emoji-id="[^"]+">(.*?)</tg-emoji>',
            r"\1",
            content,
            flags=re.DOTALL,
        )
        if fallback_html == content:
            raise
        if native_method:
            return await native_method(
                chat_id=chat_id,
                rich_message={"html": fallback_html},
                disable_notification=disable_notification,
                protect_content=protect_content,
                reply_parameters=reply_parameters,
                reply_markup=reply_markup,
            )
        raw = await bot.session.make_request(
            bot,
            _SendRichMessage(
                chat_id=chat_id,
                rich_message={"html": fallback_html},
                disable_notification=disable_notification,
                protect_content=protect_content,
                reply_parameters=reply_parameters,
                reply_markup=reply_markup,
            ),
        )
    if isinstance(raw, dict) and "message_id" in raw:
        return types.Message.model_validate(raw)
    return raw
