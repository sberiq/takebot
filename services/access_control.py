"""Shared access control for the constructor and its child bots."""

from __future__ import annotations

from aiogram import types
from aiogram.dispatcher.middlewares.base import BaseMiddleware


class GlobalBanMiddleware(BaseMiddleware):
    def __init__(
        self,
        database,
        *,
        exempt_user_id: int | None = None,
        message: str = "❌ Вы заблокированы и не можете пользоваться ботом.",
    ):
        self.database = database
        self.exempt_user_id = exempt_user_id
        self.message = message

    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if (
            user
            and user.id != self.exempt_user_id
            and await self.database.is_user_globally_banned(user.id)
        ):
            if isinstance(event, types.Message):
                await event.answer(self.message)
            elif isinstance(event, types.CallbackQuery):
                await event.answer("Доступ запрещён.", show_alert=True)
            return None
        return await handler(event, data)


class InstanceOwnerMiddleware(BaseMiddleware):
    """Keep a self-hosted constructor private to its configured owner by default."""

    def __init__(self, owner_id: int, *, enabled: bool = True):
        self.owner_id = owner_id
        self.enabled = enabled

    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if self.enabled and user and user.id != self.owner_id:
            if isinstance(event, types.Message):
                await event.answer("Этот экземпляр предназначен для владельца, который его разместил.")
            elif isinstance(event, types.CallbackQuery):
                await event.answer("Этот экземпляр закрыт для других пользователей.", show_alert=True)
            return None
        return await handler(event, data)
