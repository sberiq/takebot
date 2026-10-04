"""Telegram links for Bot Management Mode and BotFather setup."""

from __future__ import annotations

import re
from urllib.parse import quote

from aiogram import Bot
from aiogram.methods import TelegramMethod


class _GetManagedBotToken(TelegramMethod[str]):
    """Raw Bot API method for aiogram releases without a typed wrapper."""

    __api_method__ = "getManagedBotToken"
    __returning__ = str

    user_id: int


async def get_managed_bot_token(bot: Bot, user_id: int) -> str:
    method = getattr(bot, "get_managed_bot_token", None)
    if method:
        return await method(user_id=user_id)
    return await bot.session.make_request(bot, _GetManagedBotToken(user_id=user_id))


def managed_bot_creation_link(manager_username: str, *, name: str = "", username: str = "") -> str:
    """Build a managed bot creation link for a manager enabled in @BotFather."""
    manager = manager_username.strip().lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", manager):
        raise ValueError("Некорректное имя бота-менеджера")
    parts = [f"https://t.me/newbot/{quote(manager, safe='_')}"]
    suggested = username.strip().lstrip("@")
    if suggested:
        suggested = re.sub(r"[^A-Za-z0-9_]", "", suggested)[:32]
        if suggested and not suggested.lower().endswith("bot"):
            suggested += "bot"
        if suggested:
            parts[0] += "/" + quote(suggested, safe="_")
    params = []
    if name.strip():
        params.append("name=" + quote(name.strip(), safe=""))
    return parts[0] + ("?" + "&".join(params) if params else "")
