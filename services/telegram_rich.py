"""Telegram Rich HTML helpers (Bot API 10.1+) and premium emoji rendering."""

from __future__ import annotations

import html
import re
from typing import Any, Iterable

from aiogram import types
from aiogram.methods import TelegramMethod
from aiogram.exceptions import TelegramBadRequest


class _SendRichMessage(TelegramMethod[Any]):
    """Raw API method fallback for aiogram versions predating Bot API 10.1."""

    __api_method__ = "sendRichMessage"
    __returning__ = Any

    chat_id: int | str
    rich_message: dict[str, Any]
    disable_notification: bool | None = None
    protect_content: bool | None = None
    reply_markup: dict[str, Any] | None = None


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


def _configured_fragment(fragment: str | None) -> str:
    """Preserve known owner-authored Rich HTML; escape plain text fragments."""
    if not fragment:
        return ""
    # The owner explicitly sets these fragments in bot settings. Rich HTML parsing
    # is still performed by Telegram; text from submitters always uses escaping.
    if "<" in fragment and ">" in fragment:
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
) -> Any:
    """Send a Telegram Rich Message through Bot API 10.1+ without lossy fallback."""
    rich_message = {"html": content}
    native_method = getattr(bot, "send_rich_message", None)
    try:
        if native_method:
            return await native_method(
                chat_id=chat_id,
                rich_message=rich_message,
                disable_notification=disable_notification,
                protect_content=protect_content,
            )
        raw = await bot.session.make_request(
            bot,
            _SendRichMessage(
                chat_id=chat_id,
                rich_message=rich_message,
                disable_notification=disable_notification,
                protect_content=protect_content,
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
            )
        raw = await bot.session.make_request(
            bot,
            _SendRichMessage(
                chat_id=chat_id,
                rich_message={"html": fallback_html},
                disable_notification=disable_notification,
                protect_content=protect_content,
            ),
        )
    if isinstance(raw, dict) and "message_id" in raw:
        return types.Message.model_validate(raw)
    return raw
