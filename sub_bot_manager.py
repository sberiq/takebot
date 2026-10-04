"""
Менеджер под-ботов - управление ботами пользователей
"""
import asyncio
import html
import logging
import re
import io
import json
import time
from collections import deque
from typing import Dict, List
import aiosqlite
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton,
    InputMediaPhoto, InputMediaVideo, InputMediaDocument, InputMediaAudio
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

from database import Database
from config import GEMINI_MODEL
from services.ai_moderation import AIModerationService, MAX_MEDIA_BYTES
from services.access_control import GlobalBanMiddleware
from services.telegram_rich import (
    compose_post_html,
    entities_to_rich_html,
    requires_rich_message,
    rich_message_too_long,
    rich_html_from_message,
    send_rich_html,
)

logger = logging.getLogger(__name__)


class UserBroadcast(StatesGroup):
    """Состояния для рассылки в под-боте"""
    waiting_for_message = State()


class BlockUser(StatesGroup):
    """Состояния для блокировки пользователя"""
    waiting_for_user_id = State()


class ChangeFooter(StatesGroup):
    """Состояния для изменения оформления поста"""
    waiting_for_footer = State()
    waiting_for_confirmation = State()


class ChangeHeader(StatesGroup):
    """Состояния для изменения оформления сверху"""
    waiting_for_header = State()
    waiting_for_mode = State()
    waiting_for_confirmation = State()


def entities_to_json(entities: List[types.MessageEntity]) -> str:
    """Сериализовать entities в JSON"""
    if not entities:
        return None
    return json.dumps([{
        'type': e.type,
        'offset': e.offset,
        'length': e.length,
        'url': e.url,
        'user': e.user.id if e.user else None,
        'language': e.language,
        'custom_emoji_id': e.custom_emoji_id
    } for e in entities])


def json_to_entities(json_str: str) -> List[types.MessageEntity]:
    """Десериализовать entities из JSON"""
    if not json_str:
        return None
    try:
        data = json.loads(json_str)
        entities = []
        for e in data:
            entity = types.MessageEntity(
                type=e['type'],
                offset=e['offset'],
                length=e['length'],
                url=e.get('url'),
                language=e.get('language'),
                custom_emoji_id=e.get('custom_emoji_id')
            )
            entities.append(entity)
        return entities
    except:
        return None


class SubBotManager:
    """Менеджер под-ботов"""
    
    def __init__(self, db: Database):
        self.db = db
        self.ai_moderation = AIModerationService(model=GEMINI_MODEL)
        self.bots: Dict[int, Bot] = {}  # sub_bot_id: Bot
        self.dispatchers: Dict[int, Dispatcher] = {}  # sub_bot_id: Dispatcher
        self.tasks: Dict[int, asyncio.Task] = {}  # sub_bot_id: Task
        self.media_groups: Dict[str, list] = {}  # media_group_id: [messages]
        self.user_message_windows: Dict[tuple[int, int], deque] = {}
        self.media_group_rate_keys: Dict[tuple[int, int, str], float] = {}
        self.manager_username: str | None = None
    
    async def start_sub_bot(self, sub_bot_id: int, bot_token: str):
        """Запустить под-бот"""
        if sub_bot_id in self.bots:
            logger.warning(f"Под-бот {sub_bot_id} уже запущен")
            return
        
        bot = None
        try:
            # Создаем бота и диспетчер
            bot = Bot(token=bot_token)
            dp = Dispatcher(storage=MemoryStorage())
            dp.message.outer_middleware(GlobalBanMiddleware(self.db))
            dp.callback_query.outer_middleware(GlobalBanMiddleware(self.db))
            
            # Регистрируем обработчики
            self._register_handlers(dp, sub_bot_id, bot)
            
            # Сохраняем
            self.bots[sub_bot_id] = bot
            self.dispatchers[sub_bot_id] = dp
            
            # Запускаем polling в отдельной задаче
            await bot.delete_webhook()
            task = asyncio.create_task(dp.start_polling(
                bot,
                allowed_updates=["message", "callback_query", "my_chat_member", "chat_member"]
            ))
            self.tasks[sub_bot_id] = task
            
            logger.info(f"Под-бот {sub_bot_id} успешно запущен")
            
        except Exception as e:
            self.tasks.pop(sub_bot_id, None)
            self.dispatchers.pop(sub_bot_id, None)
            self.bots.pop(sub_bot_id, None)
            if bot is not None:
                try:
                    await bot.session.close()
                except Exception:
                    pass
            logger.error("Could not start sub-bot %s (%s)", sub_bot_id, type(e).__name__)
            raise
    
    async def stop_sub_bot(self, sub_bot_id: int):
        """Остановить под-бот"""
        if sub_bot_id not in self.bots:
            return
        
        # Останавливаем задачу
        task = self.tasks.pop(sub_bot_id, None)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.debug("Sub-bot polling task stopped with an error")
        
        # Закрываем бота
        if sub_bot_id in self.bots:
            await self.bots[sub_bot_id].session.close()
            del self.bots[sub_bot_id]
        
        # Удаляем диспетчер
        if sub_bot_id in self.dispatchers:
            del self.dispatchers[sub_bot_id]
        
        logger.info(f"Под-бот {sub_bot_id} остановлен")
    
    async def restart_sub_bot(self, sub_bot_id: int, bot_token: str):
        """Перезапустить под-бот"""
        await self.stop_sub_bot(sub_bot_id)
        await self.start_sub_bot(sub_bot_id, bot_token)

    async def stop_all(self) -> None:
        for sub_bot_id in list(self.bots):
            await self.stop_sub_bot(sub_bot_id)

    async def _authorize_moderation_action(
        self, callback: types.CallbackQuery, bot: Bot, sub_bot_id: int, message_db_id: int
    ) -> tuple[dict | None, dict | None, str | None]:
        """Check the exact review chat, message, bot, and human moderator."""
        if not callback.message:
            return None, None, "Кнопка больше не связана с доступным сообщением"
        bot_info = await self.db.get_sub_bot_by_id(sub_bot_id)
        if not bot_info or callback.message.chat.id != bot_info.get("admin_chat_id"):
            return None, None, "Действие доступно только в настроенном чате модерации"
        item = await self.db.get_message_by_id(message_db_id, sub_bot_id=sub_bot_id)
        if not item or item.get("admin_message_id") != callback.message.message_id:
            return None, None, "Заявка не принадлежит этому сообщению"
        if callback.from_user.id != bot_info["owner_id"]:
            try:
                member = await bot.get_chat_member(callback.message.chat.id, callback.from_user.id)
            except Exception:
                return None, None, "Не удалось проверить права модератора"
            if member.status not in {"creator", "administrator"}:
                return None, None, "Одобрять заявки могут только владелец и администраторы чата"
        return bot_info, item, None

    async def _is_chat_moderator(self, bot: Bot, info: dict, chat_id: int, user_id: int) -> bool:
        if user_id == info.get("owner_id"):
            return True
        try:
            member = await bot.get_chat_member(chat_id, user_id)
        except Exception:
            return False
        return member.status in {"creator", "administrator"}
    
    def _get_utf16_length(self, text: str) -> int:
        return len(text.encode('utf-16-le')) // 2
    
    def _is_valid_html(self, text: str) -> bool:
        """
        Проверяет, является ли текст валидным HTML для Telegram.
        Telegram поддерживает только: <b>, <i>, <u>, <s>, <code>, <pre>, <a>, <span>, <tg-spoiler>
        """
        if not text or '<' not in text or '>' not in text:
            return False
        
        import re
        # Разрешенные теги Telegram
        allowed_tags = ['b', 'i', 'u', 's', 'code', 'pre', 'a', 'span', 'tg-spoiler', 'tg-emoji', 'br']
        
        # Проверяем на пустые теги типа <>
        if re.search(r'<>', text):
            return False
        
        # Проверяем на некорректные символы после < (не буквы, не /, не -)
        # Это может быть проблема с символами типа <^ или <&
        if re.search(r'<[^/a-zA-Z-]', text):
            return False
        
        # Находим все теги (включая атрибуты)
        tag_pattern_full = r'<(/?)([a-zA-Z-]+)([^>]*)>'
        matches = list(re.finditer(tag_pattern_full, text))
        
        if not matches:
            # Если есть < и >, но не найдены валидные теги - это проблема
            # Проверяем, нет ли некорректных символов между < и >
            if re.search(r'<[^>]*[^a-zA-Z/\s-][^>]*>', text):
                return False
            return False  # Есть < и >, но не найдены теги
        
        # Проверяем баланс открывающих и закрывающих тегов
        open_tags = []
        
        for match in matches:
            is_closing = match.group(1) == '/'
            tag_name = match.group(2).lower()
            attributes = match.group(3).strip()
            
            # Проверяем, что тег разрешен
            if tag_name not in allowed_tags:
                return False
            
            # Для тега <a> проверяем наличие href атрибута
            if tag_name == 'a' and not is_closing:
                if not re.search(r'href\s*=', attributes, re.IGNORECASE):
                    return False  # Тег <a> должен иметь href
            if tag_name == 'tg-emoji' and not is_closing:
                if not re.search(r'emoji-id\s*=', attributes, re.IGNORECASE):
                    return False
            
            if is_closing:
                if not open_tags or open_tags[-1] != tag_name:
                    return False  # Неправильный порядок закрытия
                open_tags.pop()
            else:
                open_tags.append(tag_name)
        
        # Все теги должны быть закрыты
        if open_tags:
            return False
        
        return True
    
    def _build_html_from_entities(self, text: str, entities: list[types.MessageEntity]) -> str:
        """Convert Telegram entities without losing UTF-16 offsets or custom emoji."""
        return entities_to_rich_html(text or "", entities or [])
    
    def _build_html_caption(self, header: str, header_mode: str, user_caption: str, footer: str, user_entities: list[types.MessageEntity]) -> str:
        """Собирает итоговый caption в HTML, комбинируя header/footer и user_caption с entities."""
        import html
        
        header_html = ""
        footer_html = ""
        user_html = self._build_html_from_entities(user_caption, user_entities) if user_entities else html.escape(user_caption or "")
        
        if header:
            header_html = header if self._is_valid_html(header) else html.escape(header)
        if footer:
            footer_html = footer if self._is_valid_html(footer) else html.escape(footer)
        
        parts = []
        if header_html:
            parts.append(header_html)
        
        if user_html:
            if header_html and header_mode == 'inline':
                # header и текст в одной строке через пробел
                parts[-1] = parts[-1] + " " + user_html
            else:
                if header_html:
                    parts.append(user_html)
                else:
                    parts.append(user_html)
        
        if footer_html:
            parts.append(footer_html)
        
        # Разделяем абзацами через \n\n, чтобы избежать ошибок parse_mode на <br>
        return "\n\n".join(parts)
    
    def _text_contains_at_start(self, text: str, pattern: str, allow_separators: bool = True) -> bool:
        """
        Проверяет, содержит ли текст pattern в начале (БЕЗ нормализации, сохраняет ВСЕ символы включая невидимые)
        allow_separators: разрешить разделители (\n\n, \n, пробелы) перед/после pattern
        """
        if not text or not pattern:
            return False
        
        # Используем оригинальный pattern БЕЗ strip, чтобы сохранить все символы
        text_lstrip = text.lstrip()  # Убираем только начальные пробелы для проверки
        
        if not text_lstrip:
            return False
        
        # Проверяем точное совпадение в начале (с оригинальным pattern)
        if text_lstrip.startswith(pattern):
            # Проверяем, что после pattern идет разделитель или конец
            pattern_len = len(pattern)
            if len(text_lstrip) == pattern_len:
                return True
            # Проверяем следующий символ - должен быть разделитель
            next_char = text_lstrip[pattern_len:pattern_len+1]
            if next_char in ['\n', ' ', '\t']:
                return True
            # Проверяем \n\n
            if text_lstrip[pattern_len:pattern_len+2] == '\n\n':
                return True
        
        # Проверяем с разделителями (для newline режима)
        if allow_separators:
            # Варианты с разделителями в начале (используем оригинальный pattern)
            variants = [
                pattern,  # Точное совпадение с оригиналом
                '\n' + pattern,
                '\n\n' + pattern,
                ' ' + pattern,
            ]
            for variant in variants:
                if text_lstrip.startswith(variant):
                    # Проверяем что после идет разделитель или конец
                    variant_len = len(variant)
                    if len(text_lstrip) == variant_len:
                        return True
                    next_chars = text_lstrip[variant_len:variant_len+2]
                    if next_chars.startswith('\n') or next_chars.startswith(' '):
                        return True
        
        # Также проверяем вариант inline (pattern + пробел/разделитель)
        if text_lstrip.startswith(pattern + " ") or text_lstrip.startswith(pattern + "\n"):
            return True
        
        return False
    
    def _text_contains_at_end(self, text: str, pattern: str, allow_separators: bool = True) -> bool:
        """
        Проверяет, содержит ли текст pattern в конце (БЕЗ нормализации, сохраняет ВСЕ символы включая невидимые)
        """
        if not text or not pattern:
            return False
        
        # Используем оригинальный pattern БЕЗ strip, чтобы сохранить все символы
        text_rstrip = text.rstrip()  # Убираем только конечные пробелы для проверки
        
        if not text_rstrip:
            return False
        
        # Проверяем точное совпадение в конце (с оригинальным pattern)
        if text_rstrip.endswith(pattern):
            # Проверяем, что перед pattern идет разделитель или начало
            pattern_len = len(pattern)
            if len(text_rstrip) == pattern_len:
                return True
            # Проверяем предыдущий символ - должен быть разделитель
            prev_char = text_rstrip[-(pattern_len + 1):-(pattern_len)] if len(text_rstrip) > pattern_len else ""
            if prev_char in ['\n', ' ', '\t']:
                return True
            # Проверяем \n\n перед pattern
            if len(text_rstrip) >= pattern_len + 2 and text_rstrip[-(pattern_len + 2):-pattern_len] == '\n\n':
                return True
        
        # Проверяем с разделителями (используем оригинальный pattern)
        if allow_separators:
            variants = [
                pattern,  # Точное совпадение с оригиналом
                pattern + '\n',
                pattern + '\n\n',
                pattern + ' ',
                '\n' + pattern,
                '\n\n' + pattern,
                ' ' + pattern,
            ]
            for variant in variants:
                if text_rstrip.endswith(variant):
                    return True
        
        return False
    
    def _remove_duplicate_header_footer(self, text: str, header: str = None, footer: str = None, header_mode: str = 'newline') -> str:
        """
        Удаляет дубликаты header и footer из текста, если они уже есть в похожих местах
        Сохраняет ВСЕ символы включая невидимые и множественные пробелы
        Возвращает очищенный текст
        """
        if not text:
            return text
        
        result = text
        
        # Проверяем и удаляем footer (в конце текста) - БЕЗ нормализации, сохраняем все символы
        if footer:
            # Проверяем, есть ли footer в конце текста (используем оригинальный footer)
            if self._text_contains_at_end(result, footer, allow_separators=True):
                result_stripped = result.rstrip()
                
                # Пробуем удалить footer с различными разделителями (используем оригинальный footer)
                footer_variants = [
                    footer,  # Точное совпадение с оригиналом (сохраняет все символы)
                    '\n\n' + footer,
                    '\n' + footer,
                    ' ' + footer,
                    footer + '\n',
                    footer + ' ',
                    '\n\n' + footer + '\n',
                    '\n' + footer + '\n',
                ]
                
                for footer_variant in footer_variants:
                    if result_stripped.endswith(footer_variant):
                        result = result_stripped[:-len(footer_variant)].rstrip()
                        break
                else:
                    # Если не нашли точное совпадение, пробуем по строкам
                    lines = result_stripped.split('\n')
                    if len(lines) > 0:
                        # Проверяем последнюю строку - ищем оригинальный footer
                        last_line = lines[-1]
                        if last_line.endswith(footer) or footer in last_line:
                            # Удаляем footer из последней строки
                            footer_pos = last_line.rfind(footer)
                            if footer_pos >= 0:
                                remaining = last_line[:footer_pos].rstrip()
                                if remaining:
                                    result = '\n'.join(lines[:-1] + [remaining]).rstrip()
                                else:
                                    result = '\n'.join(lines[:-1]).rstrip()
        
        # Проверяем и удаляем header (в начале текста) - БЕЗ нормализации, сохраняем все символы
        if header:
            # Проверяем, есть ли header в начале текста (используем оригинальный header)
            if self._text_contains_at_start(result, header, allow_separators=(header_mode == 'newline')):
                result_stripped = result.lstrip()
                
                if header_mode == 'inline':
                    # Inline: header + пробел/разделитель + текст
                    # Пробуем различные варианты разделителей (используем оригинальный header)
                    header_variants = [
                        header + " ",
                        header + "\n",
                        header + "\t",
                    ]
                    
                    for header_variant in header_variants:
                        if result_stripped.startswith(header_variant):
                            result = result_stripped[len(header_variant):].lstrip()
                            break
                    else:
                        # Если header в начале без разделителя
                        if result_stripped.startswith(header):
                            result = result_stripped[len(header):].lstrip()
                else:
                    # Newline: header на отдельной строке
                    lines = result_stripped.split('\n')
                    if len(lines) > 0:
                        # Проверяем первую строку - ищем оригинальный header
                        first_line = lines[0]
                        if first_line.startswith(header) or first_line == header:
                            # Удаляем header из первой строки
                            if first_line == header:
                                result = '\n'.join(lines[1:]).lstrip()
                            else:
                                header_pos = first_line.find(header)
                                if header_pos >= 0:
                                    remaining = first_line[header_pos + len(header):].lstrip()
                                    if remaining:
                                        result = remaining + '\n' + '\n'.join(lines[1:]) if len(lines) > 1 else remaining
                                    else:
                                        result = '\n'.join(lines[1:]).lstrip()
                            # Удаляем пустые строки после header
                            while result.startswith('\n'):
                                result = result[1:].lstrip()
        
        return result

    def _combine_parts(self, parts: list) -> tuple[str, list]:
        """
        Parts: list of tuples (text, entities)
        Returns: (full_text, full_entities)
        """
        full_text = ""
        full_entities = []
        current_offset = 0
        
        for text, entities in parts:
            if not text:
                continue
            
            # Append text
            full_text += text
            
            # Append entities
            if entities:
                for entity in entities:
                    # Create new entity with shifted offset
                    new_entity = entity.model_copy()
                    new_entity.offset += current_offset
                    full_entities.append(new_entity)
            
            # Update offset
            current_offset += self._get_utf16_length(text)
            
        return full_text, full_entities

    def _pre_check_spam(
        self, sub_bot_id: int, user_id: int, media_group_id: str | None = None
    ) -> tuple[bool, str]:
        """Bound intake volume per user without splitting Telegram albums."""
        now = time.monotonic()
        key = (sub_bot_id, user_id)
        if media_group_id:
            group_key = (sub_bot_id, user_id, media_group_id)
            seen_at = self.media_group_rate_keys.get(group_key)
            if seen_at is not None and now - seen_at < 30:
                return True, ""
        history = self.user_message_windows.setdefault(key, deque())
        while history and now - history[0] >= 60:
            history.popleft()
        if history and now - history[-1] < 1.5:
            return False, "Слишком часто. Подождите немного и отправьте предложение ещё раз."
        if len(history) >= 8:
            return False, "Достигнут лимит: не более 8 предложений в минуту."
        history.append(now)
        if media_group_id:
            group_key = (sub_bot_id, user_id, media_group_id)
            self.media_group_rate_keys[group_key] = now
        # Keep rate-limit bookkeeping bounded under traffic from many distinct users.
        if len(self.user_message_windows) > 4096:
            active = [
                (active_key, timestamps)
                for active_key, timestamps in self.user_message_windows.items()
                if timestamps and now - timestamps[-1] < 60
            ]
            active.sort(key=lambda item: item[1][-1], reverse=True)
            self.user_message_windows = dict(active[:3072])
        if len(self.media_group_rate_keys) > 2048:
            recent_groups = sorted(
                (
                    (group, seen)
                    for group, seen in self.media_group_rate_keys.items()
                    if now - seen < 60
                ),
                key=lambda item: item[1],
                reverse=True,
            )
            self.media_group_rate_keys = dict(recent_groups[:1536])
        return True, ""
    
    async def check_with_gemini(
        self,
        api_key: str,
        prompt: str,
        text: str = None,
        photo_file=None,
        photo_files: list = None,
        video_path: str = None,
        video_file=None,
        video_files: list = None,
        media_files: list = None,
        unsupported_media: bool = False,
    ) -> bool:
        """Delegate the moderation decision to the isolated multimodal AI service."""
        inputs = list(media_files or [])
        if photo_file is not None:
            inputs.append((photo_file, "image/jpeg"))
        for item in photo_files or []:
            inputs.append((item, "image/jpeg"))
        if video_file is not None:
            inputs.append((video_file, "video/mp4"))
        for item in video_files or []:
            inputs.append(item if isinstance(item, tuple) else (item, "video/mp4"))
        if video_path:
            inputs.append((video_path, None))
        return await self.ai_moderation.approve(
            api_key=api_key,
            policy=prompt,
            text=text,
            media=inputs,
            unsupported_media=unsupported_media,
        )

    async def _download_ai_media(
        self,
        bot: Bot,
        file_id: str,
        mime_type: str | None,
        max_bytes: int = MAX_MEDIA_BYTES,
    ):
        """Download one supported Telegram media item within its remaining byte budget."""
        if not mime_type or not mime_type.startswith(("image/", "video/", "audio/")):
            return None
        if max_bytes <= 0:
            return None
        file_info = await bot.get_file(file_id)
        if file_info.file_size is not None and file_info.file_size > max_bytes:
            return None
        destination = io.BytesIO()
        await bot.download_file(file_info.file_path, destination=destination)
        payload = destination.getvalue()
        if not payload or len(payload) > max_bytes:
            return None
        return payload, mime_type

    async def _process_media_group_delayed(self, bot: Bot, sub_bot_id: int, user_id: int,
                                          is_anonymous: bool, admin_chat_id: int,
                                          group_key: str, state: FSMContext):
        """Обработка медиа-группы с задержкой для сбора всех сообщений"""
        # Ждем 5 секунд для получения всех сообщений группы (увеличено для надежности)
        await asyncio.sleep(5.0)
        
        # Проверяем, что группа еще существует
        if group_key not in self.media_groups:
            logger.warning(f"Медиа-группа {group_key} была удалена до обработки")
            return
        
        # Дополнительная проверка: если после задержки пришли новые сообщения, ждем еще
        initial_count = len(self.media_groups.get(group_key, []))
        await asyncio.sleep(2.0)
        
        if group_key in self.media_groups:
            final_count = len(self.media_groups[group_key])
            if final_count > initial_count:
                logger.info(f"После задержки пришли новые сообщения в группу {group_key}: {initial_count} -> {final_count}")
                # Ждем еще немного
                await asyncio.sleep(2.0)
        
        # Проверяем еще раз, что группа существует
        if group_key not in self.media_groups:
            logger.warning(f"Медиа-группа {group_key} была удалена во время обработки")
            return
        
        logger.info(f"📦 Начинаем обработку медиа-группы {group_key}: собрано {len(self.media_groups[group_key])} сообщений")
        
        # Обрабатываем группу
        await self._handle_media_group(bot, sub_bot_id, user_id, is_anonymous, admin_chat_id, group_key, state)
    
    async def _handle_media_group(self, bot: Bot, sub_bot_id: int, user_id: int, 
                                  is_anonymous: bool, admin_chat_id: int, 
                                  group_key: str, state: FSMContext):
        """Обработка медиа-группы (несколько фото одним сообщением)"""
        try:
            # Получаем все сообщения группы
            group_messages = self.media_groups.get(group_key, [])
            
            if not group_messages:
                logger.warning(f"Медиа-группа {group_key} пуста")
                return
            
            # Сортируем по времени получения (message_id)
            group_messages.sort(key=lambda m: m.message_id)
            
            logger.info(f"📦 Обработка медиа-группы {group_key}: собрано {len(group_messages)} сообщений")
            
            # Логируем типы медиа в группе
            media_types = {}
            for msg in group_messages:
                if msg.photo:
                    media_types['photo'] = media_types.get('photo', 0) + 1
                elif msg.video:
                    media_types['video'] = media_types.get('video', 0) + 1
                elif msg.document:
                    media_types['document'] = media_types.get('document', 0) + 1
                elif msg.audio:
                    media_types['audio'] = media_types.get('audio', 0) + 1
            
            logger.info(f"📦 Типы медиа в группе: {media_types}")
            
            # Получаем caption из всех сообщений группы (caption может быть только у одного элемента)
            # Проверяем все сообщения, так как caption может быть не у первого
            first_message = group_messages[0]
            caption = ""
            caption_entities = None
            
            # Ищем caption во всех сообщениях группы
            for msg in group_messages:
                if msg.caption:
                    caption = msg.caption
                    caption_entities = msg.caption_entities
                    logger.info(f"Найден caption в сообщении {msg.message_id}: {caption[:50]}")
                    break  # Caption может быть только у одного элемента в медиа-группе
            
            logger.info(f"Итоговый caption для медиа-группы: {caption[:100] if caption else '(пусто)'}")
            
            # Получаем данные под-бота
            sub_bot_data = await self.db.get_sub_bot_by_token(bot.token)
            
            # ========== AI MODERATION ==========
            ai_verdict = None
            is_auto_published = False
            
            if sub_bot_data and sub_bot_data.get('moderation_mode') == 'gemini' and sub_bot_data.get('gemini_api_key'):
                logger.info(f"Запуск AI-модерации для медиа-группы {group_key}")
                
                # Every media item in an album must be checked. If one item is
                # missing, unsupported, or the album exceeds the AI limit, keep it
                # in the human review queue instead of approving a partial view.
                media_files = []
                unsupported_media = False
                check_text = caption or ""
                
                # Count the entire album before downloading any content for AI.
                total_media_count = sum(
                    bool(msg.photo or msg.video or msg.document or msg.audio or msg.voice
                         or msg.video_note or msg.animation or msg.sticker)
                    for msg in group_messages
                )
                
                logger.info(f"Медиа-группа содержит {total_media_count} файлов (из {len(group_messages)} сообщений)")
                
                if total_media_count > 5:
                    unsupported_media = True
                for msg in group_messages:
                    if unsupported_media:
                        break
                    media = None
                    if msg.photo:
                        media = (msg.photo[-1].file_id, "image/jpeg")
                    elif msg.video:
                        media = (msg.video.file_id, msg.video.mime_type or "video/mp4")
                    elif msg.document:
                        media = (msg.document.file_id, msg.document.mime_type)
                    elif msg.audio:
                        media = (msg.audio.file_id, msg.audio.mime_type or "audio/mpeg")
                    elif msg.voice:
                        media = (msg.voice.file_id, msg.voice.mime_type or "audio/ogg")
                    if media:
                        try:
                            used_bytes = sum(len(item[0]) for item in media_files)
                            downloaded = await self._download_ai_media(
                                bot,
                                *media,
                                max_bytes=MAX_MEDIA_BYTES - used_bytes,
                            )
                            if downloaded is None:
                                unsupported_media = True
                                break
                            else:
                                media_files.append(downloaded)
                        except Exception:
                            logger.exception("AI media download failed for album %s", group_key)
                            unsupported_media = True
                            break
                    elif msg.video_note or msg.animation or msg.sticker:
                        unsupported_media = True
                        break

                if media_files or check_text or total_media_count:
                    try:
                        is_auto_published = await self.check_with_gemini(
                            api_key=sub_bot_data['gemini_api_key'],
                            prompt=sub_bot_data.get('gemini_prompt', 'You are a moderator. PASS or REJECT.'),
                            text=check_text,
                            media_files=media_files,
                            unsupported_media=unsupported_media,
                        )
                        
                        if is_auto_published:
                            logger.info("✅ AI одобрил медиа-группу. Будет авто-опубликовано.")
                        else:
                            logger.info("❌ AI отклонил медиа-группу.")
                            ai_verdict = "🤖 AI: ❌ Отклонено"
                    except Exception as e:
                        logger.error(f"AI check exception for media group: {e}")
                        ai_verdict = "⚠️ AI: Ошибка проверки"

            # Подготовка частей сообщения
            header_content = "Новое предложение" + (" (анонимно)" if is_anonymous else "")
            header_text = "📬 " + header_content
            
            # Offset: 📬 (2 units) + space (1 unit) = 3
            bold_offset = self._get_utf16_length("📬 ")
            bold_length = self._get_utf16_length(header_content)
            header_entities = [types.MessageEntity(type="bold", offset=bold_offset, length=bold_length)]
            
            if ai_verdict:
                header_text += f" | {ai_verdict}"
            
            header_text += "\n\n"
            header_part = (header_text, header_entities)
            
            # Информация об отправителе
            sender_text = ""
            sender_entities = []
            
            if is_anonymous:
                sender_text = "\n\n📬 Анонимно"
                # Offset: \n\n (2) + 📬 (2) + space (1) = 5
                anon_offset = self._get_utf16_length("\n\n📬 ")
                sender_entities = [types.MessageEntity(type="bold", offset=anon_offset, length=8)]
            else:
                full_name = first_message.from_user.full_name or first_message.from_user.first_name or 'Неизвестно'
                sender_text = f"\n\n👤 От: {full_name}\n"
                
                # ID
                id_prefix = "🆔 ID: "
                sender_text += id_prefix
                id_str = str(user_id)
                sender_text += id_str
                
                # Вычисляем смещение
                text_before_id = f"\n\n👤 От: {full_name}\n🆔 ID: "
                offset = self._get_utf16_length(text_before_id)
                sender_entities.append(types.MessageEntity(type="code", offset=offset, length=len(id_str)))
                
                if first_message.from_user.username:
                    sender_text += f"\n📱 Username: @{first_message.from_user.username}"
            
            sender_part = (sender_text, sender_entities)
            
            # Подготавливаем медиа для отправки
            media_group = []
            media_file_ids = []
            
            # User Caption part
            user_caption = caption or ""
            user_entities = caption_entities or []
            user_part = (user_caption, user_entities)
            
            # Combine everything
            full_caption, full_entities = self._combine_parts([header_part, user_part, sender_part])
            
            # Check length
            use_combined = True
            if len(full_caption) > 1024:
                use_combined = False
                # Fallback: Header+Sender separate, then content
                header_sender_text, header_sender_entities = self._combine_parts([header_part, sender_part])
                await bot.send_message(chat_id=admin_chat_id, text=header_sender_text, entities=header_sender_entities)
            
            for index, msg in enumerate(group_messages):
                is_first = (index == 0)
                current_caption = None
                current_entities = None
                
                if is_first:
                    if use_combined:
                        current_caption = full_caption
                        current_entities = full_entities
                    else:
                        current_caption = user_caption
                        current_entities = user_entities
                
                if msg.photo:
                    file_id = msg.photo[-1].file_id
                    media_file_ids.append(f"photo:{file_id}")
                    media_group.append(InputMediaPhoto(
                        media=file_id,
                        caption=current_caption,
                        caption_entities=current_entities
                    ))
                elif msg.video:
                    file_id = msg.video.file_id
                    media_file_ids.append(f"video:{file_id}")
                    media_group.append(InputMediaVideo(
                        media=file_id,
                        caption=current_caption,
                        caption_entities=current_entities
                    ))
                elif msg.document:
                    file_id = msg.document.file_id
                    media_file_ids.append(f"document:{file_id}")
                    media_group.append(InputMediaDocument(
                        media=file_id,
                        caption=current_caption,
                        caption_entities=current_entities
                    ))
                elif msg.audio:
                    file_id = msg.audio.file_id
                    media_file_ids.append(f"audio:{file_id}")
                    media_group.append(InputMediaAudio(
                        media=file_id,
                        caption=current_caption,
                        caption_entities=current_entities
                    ))
            
            if not media_group:
                logger.warning(f"Медиа-группа {group_key} не содержит поддерживаемых медиа")
                return
            
            # Отправляем медиа-группу в админ-чат
            sent_messages = await bot.send_media_group(
                chat_id=admin_chat_id,
                media=media_group
            )
            
            # Первое сообщение - основное
            admin_message = sent_messages[0]
            

            # Сохраняем в базу данных
            original_text = caption if caption else ""
            # Определяем content_type на основе первого сообщения
            first_msg = group_messages[0]
            if first_msg.photo:
                content_type = "photo"
            elif first_msg.video:
                content_type = "video"
            elif first_msg.audio:
                content_type = "audio"
            elif first_msg.document:
                content_type = "document"
            else:
                content_type = "media_group"
            
            # Сохраняем все message_id из группы (через запятую)
            media_group_ids = ",".join([str(msg.message_id) for msg in group_messages])
            # Также сохраняем file_id для публикации (через запятую)
            media_file_ids_str = ",".join(media_file_ids) if media_file_ids else None
            
            status = 'published' if is_auto_published else 'pending'
            
            # Сериализуем entities для caption
            original_entities_json = entities_to_json(caption_entities) if caption_entities else None
            
            message_db_id = await self.db.add_message(
                sub_bot_id=sub_bot_id,
                user_id=user_id,
                is_anonymous=is_anonymous,
                message_id=first_message.message_id,
                admin_message_id=admin_message.message_id,
                content_type=content_type,
                original_text=original_text,
                original_entities=original_entities_json,
                is_media_group=True,
                media_group_message_ids=media_file_ids_str,  # Сохраняем file_id вместо message_id
                status=status
            )
            
            if is_auto_published:
                # Авто-публикация
                await bot.send_message(
                    chat_id=admin_chat_id,
                    text="✅ <b>Одобрено AI и опубликовано</b>",
                    reply_to_message_id=admin_message.message_id,
                    parse_mode="HTML"
                )
                
                # Публикация в канал
                channel_id = sub_bot_data['channel_id']
                post_footer = sub_bot_data.get('post_footer')
                post_header = sub_bot_data.get('post_header')
                header_mode = sub_bot_data.get('header_mode', 'newline')
                rich_post_html = compose_post_html(
                    original_text or "",
                    caption_entities or [],
                    header=post_header,
                    footer=post_footer,
                    header_mode=header_mode,
                )
                rich_caption_separate = requires_rich_message(rich_post_html)
                caption_html = self._build_html_caption(
                    header=post_header,
                    header_mode=header_mode,
                    user_caption=original_text or "",
                    footer=post_footer,
                    user_entities=caption_entities or [],
                )
                
                # Подготовка медиа для канала
                channel_media_group = []
                channel_caption_added = False
                
                for file_id_str in media_file_ids:
                    try:
                        media_type, file_id = file_id_str.split(':', 1)
                        
                        final_caption = None if channel_caption_added else ("" if rich_caption_separate else caption_html)
                        if final_caption is not None:
                            channel_caption_added = True
                        has_html_in_caption = bool(final_caption and not rich_caption_separate and self._is_valid_html(final_caption))
                            
                        if media_type == 'photo':
                            channel_media_group.append(InputMediaPhoto(media=file_id, caption=final_caption, parse_mode="HTML" if has_html_in_caption else None))
                        elif media_type == 'video':
                            channel_media_group.append(InputMediaVideo(media=file_id, caption=final_caption, parse_mode="HTML" if has_html_in_caption else None))
                        elif media_type == 'document':
                            channel_media_group.append(InputMediaDocument(media=file_id, caption=final_caption, parse_mode="HTML" if has_html_in_caption else None))
                        elif media_type == 'audio':
                            channel_media_group.append(InputMediaAudio(media=file_id, caption=final_caption, parse_mode="HTML" if has_html_in_caption else None))
                    except Exception as e:
                        logger.error(f"Ошибка подготовки медиа для авто-публикации: {e}")
                
                if channel_media_group:
                    try:
                        sent_msgs = await bot.send_media_group(chat_id=channel_id, media=channel_media_group)
                        # Обновляем статус с ID сообщения в канале (первого)
                        await self.db.update_message_status(message_db_id, 'published', sent_msgs[0].message_id)
                        if rich_caption_separate and rich_post_html.strip():
                            await send_rich_html(bot, chat_id=channel_id, content=rich_post_html)
                        # Уведомление пользователю
                        await bot.send_message(chat_id=user_id, text="✅ Ваше предложение опубликовано в канале!", reply_to_message_id=first_message.message_id)
                    except Exception as e:
                        logger.error(f"Ошибка авто-публикации альбома: {e}")
                        await bot.send_message(chat_id=admin_chat_id, text=f"❌ Ошибка публикации: {e}", reply_to_message_id=admin_message.message_id)
                
                del self.media_groups[group_key]
                return

            # Создаем кнопки модерации
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Опубликовать", callback_data=f"approve_{message_db_id}"),
                    InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{message_db_id}")
                ]
            ])
            
            # Отправляем кнопки модерации
            try:
                await bot.send_message(
                    chat_id=admin_chat_id,
                    text="Выберите действие:",
                    reply_to_message_id=admin_message.message_id,
                    reply_markup=keyboard
                )
                logger.info(f"Кнопки модерации отправлены для медиа-группы, message_db_id={message_db_id}")
            except Exception as e:
                logger.error(f"Ошибка отправки кнопок модерации для медиа-группы: {e}")
                # Пробуем добавить кнопки к первому сообщению группы
                try:
                    await bot.edit_message_reply_markup(
                        chat_id=admin_chat_id,
                        message_id=admin_message.message_id,
                        reply_markup=keyboard
                    )
                except Exception as e2:
                    logger.error(f"Не удалось добавить кнопки к медиа-группе: {e2}")
            
            # Удаляем группу из памяти
            del self.media_groups[group_key]
            
            logger.info(f"Медиа-группа отправлена в админ-чат: {len(group_messages)} сообщений")
            
        except Exception as e:
            logger.error(f"Ошибка обработки медиа-группы {group_key}: {e}")
            # Удаляем группу из памяти даже при ошибке
            if group_key in self.media_groups:
                del self.media_groups[group_key]
    
    def _register_handlers(self, dp: Dispatcher, sub_bot_id: int, bot: Bot):
        """Регистрация обработчиков для под-бота"""
        
        # ========== MIDDLEWARE ДЛЯ БЛОКИРОВКИ АДМИН-ЧАТА ==========
        # Это критически важно - блокируем ВСЕ сообщения из админ-чата ДО обработки
        class AdminChatBlockMiddleware:
            def __init__(self, db, bot_token):
                self.db = db
                self.bot_token = bot_token
            
            async def __call__(self, handler, event: types.Message, data):
                """Middleware для блокировки всех сообщений из админ-чата"""
                # КРИТИЧНО: Пропускаем ТОЛЬКО прямые ответы админов на сообщения пользователей
                # Проверяем, что это ответ И это прямой ответ (не ответ на ответ)
                if event.reply_to_message:
                    # Это ответ на сообщение - проверяем, что это прямой ответ (не ответ на ответ)
                    if not event.reply_to_message.reply_to_message:
                        # Это прямой ответ - пропускаем для обработки (это может быть ответ админа на сообщение пользователя)
                        # Проверяем, что это админ-чат, чтобы не пропускать ответы из других чатов
                        sub_bot_data = await self.db.get_sub_bot_by_token(self.bot_token)
                        if sub_bot_data and sub_bot_data.get('admin_chat_id') == event.chat.id:
                            # Это прямой ответ в админ-чате - пропускаем для обработки
                            return await handler(event, data)
                    # Если это ответ на ответ (reply_to_message.reply_to_message существует) - блокируем
                
                # Проверяем тип чата СРАЗУ
                if event.chat.type != "private":
                    # Это группа/канал - проверяем, не админ-чат ли
                    sub_bot_data = await self.db.get_sub_bot_by_token(self.bot_token)
                    if sub_bot_data:
                        admin_chat_id = sub_bot_data.get('admin_chat_id')
                        channel_id = sub_bot_data.get('channel_id')
                        
                        # Если это админ-чат или канал - БЛОКИРУЕМ ВСЕ (кроме ответов, которые уже обработаны выше)
                        if admin_chat_id and event.chat.id == admin_chat_id:
                            logger.info(f"🚫 MIDDLEWARE: Блокируем сообщение из админ-чата (chat_id={event.chat.id})")
                            return  # Блокируем обработку - НЕ вызываем handler
                        if channel_id and event.chat.id == channel_id:
                            logger.info(f"🚫 MIDDLEWARE: Блокируем сообщение из канала (chat_id={event.chat.id})")
                            return  # Блокируем обработку
                        
                        # Блокируем ВСЕ группы и супергруппы
                        if event.chat.type in ["group", "supergroup"]:
                            logger.info(f"🚫 MIDDLEWARE: Блокируем сообщение из группы/супергруппы (chat_id={event.chat.id}, type={event.chat.type})")
                            return  # Блокируем обработку
                
                # Для private чатов - дополнительная проверка по ID
                if event.chat.type == "private":
                    sub_bot_data = await self.db.get_sub_bot_by_token(self.bot_token)
                    if sub_bot_data:
                        admin_chat_id = sub_bot_data.get('admin_chat_id')
                        channel_id = sub_bot_data.get('channel_id')
                        
                        # Проверяем по ID (на случай edge cases)
                        if admin_chat_id and event.chat.id == admin_chat_id:
                            logger.info(f"🚫 MIDDLEWARE: Блокируем private сообщение из админ-чата по ID (chat_id={event.chat.id})")
                            return
                        if channel_id and event.chat.id == channel_id:
                            logger.info(f"🚫 MIDDLEWARE: Блокируем private сообщение из канала по ID (chat_id={event.chat.id})")
                            return
                
                # Пропускаем дальше для обработки
                return await handler(event, data)
        
        # Регистрируем middleware
        admin_block_middleware = AdminChatBlockMiddleware(self.db, bot.token)
        dp.message.middleware.register(admin_block_middleware)
        
        async def is_admin_or_channel_message(message: types.Message) -> bool:
            """Проверка, является ли сообщение из админ-чата или канала"""
            # КРИТИЧНО: Игнорируем ВСЕ сообщения не из private чата
            if message.chat.type != "private":
                return True
            
            # Дополнительная проверка по ID админ-чата и канала
            sub_bot_data = await self.db.get_sub_bot_by_token(bot.token)
            if sub_bot_data:
                admin_chat_id = sub_bot_data.get('admin_chat_id')
                channel_id = sub_bot_data.get('channel_id')
                
                if admin_chat_id and message.chat.id == admin_chat_id:
                    return True
                if channel_id and message.chat.id == channel_id:
                    return True
                
                # Игнорируем группы и супергруппы
                if message.chat.type in ["group", "supergroup"]:
                    return True
            
            return False
        
        # ========== КОМАНДЫ ==========
        @dp.message(CommandStart())
        async def cmd_start(message: types.Message, state: FSMContext):
            """Обработка /start в под-боте"""
            # КРИТИЧНО: Игнорируем сообщения из админ-чата и канала
            if await is_admin_or_channel_message(message):
                logger.debug(f"ИГНОРИРУЕМ /start из админ-чата/канала (chat_id={message.chat.id}, type={message.chat.type})")
                return
            
            user_id = message.from_user.id
            
            # Добавляем пользователя в базу
            await self.db.add_or_update_user(
                sub_bot_id=sub_bot_id,
                user_id=user_id,
                username=message.from_user.username,
                first_name=message.from_user.first_name
            )
            
            # Получаем информацию о под-боте
            sub_bot_info = await self.db.get_sub_bot_by_token(bot.token)
            
            # Проверяем, является ли пользователь владельцем
            is_owner = user_id == sub_bot_info['owner_id']
            
            if is_owner:
                status_text = "👋 <b>Добро пожаловать, владелец!</b>\n\n"
                
                if not sub_bot_info['admin_chat_id']:
                    status_text += "⚠️ <b>Админ-чат не настроен</b>\n"
                    status_text += "   Добавьте бота в группу как администратора\n\n"
                else:
                    status_text += "✅ Админ-чат настроен\n\n"
                
                if not sub_bot_info['channel_id']:
                    status_text += "⚠️ <b>Канал не настроен</b>\n"
                    status_text += "   Добавьте бота в канал как администратора\n\n"
                else:
                    status_text += "✅ Канал настроен\n\n"
                
                if sub_bot_info['admin_chat_id'] and sub_bot_info['channel_id']:
                    status_text += "🎉 Бот готов к работе!\n\n"
                
                status_text += "Все настройки, Rich HTML оформление, статистика и рассылки находятся в панели конструктора."
                keyboard = None
                if self.manager_username:
                    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(
                            text="⚙️ Открыть панель управления",
                            url=f"https://t.me/{self.manager_username}?start=bot_{sub_bot_id}",
                        )
                    ]])
                await message.answer(status_text, parse_mode="HTML", reply_markup=keyboard)
                return
            
            # Для обычных пользователей: получаем текущее состояние анонимности
            # Сначала пытаемся получить из FSM, если нет - из БД
            data = await state.get_data()
            is_anonymous = data.get('is_anonymous', None)
            
            # Если в FSM нет, загружаем из БД
            if is_anonymous is None:
                is_anonymous = await self.db.get_user_anonymous_mode(sub_bot_id, user_id)
                # Сохраняем в FSM для текущей сессии
                await state.update_data(is_anonymous=is_anonymous)
                logger.info(f"Восстановлен режим анонимности из БД для пользователя {user_id} в боте {sub_bot_id}: {is_anonymous}")
            
            keyboard = ReplyKeyboardMarkup(
                keyboard=[
                    [KeyboardButton(text="📝 Отправить предложение")],
                    [KeyboardButton(text="👤 Анонимно"), KeyboardButton(text="👥 Не анонимно")],
                    [KeyboardButton(text="ℹ️ Информация")]
                ],
                resize_keyboard=True
            )
            
            # Формируем текст с актуальным режимом
            mode_text = "Анонимно" if is_anonymous else "Не анонимно"
            
            # Получаем кастомное приветствие из БД
            custom_welcome = sub_bot_info.get('welcome_message')
            
            # Если есть кастомное приветствие - используем его, иначе дефолтное
            if custom_welcome:
                welcome_text = custom_welcome
                # Добавляем информацию о режиме анонимности
                if "{mode}" in welcome_text:
                    welcome_text = welcome_text.replace("{mode}", mode_text)
                else:
                    welcome_text += f"\n\nТекущий режим: <b>{mode_text}</b>"
            else:
                welcome_text = (
                    "👋 <b>Добро пожаловать!</b>\n\n"
                    "Этот бот принимает предложения от пользователей.\n\n"
                    "Вы можете:\n"
                    "📝 Отправить предложение (анонимно или нет)\n"
                    "✅ После модерации оно будет опубликовано в канале\n\n"
                    f"Текущий режим: <b>{mode_text}</b>"
                )
            
            if custom_welcome:
                await send_rich_html(
                    bot,
                    chat_id=message.chat.id,
                    content=welcome_text,
                    reply_markup=keyboard,
                )
            else:
                await message.answer(welcome_text, reply_markup=keyboard, parse_mode="HTML")
        
        # ========== ПОДТВЕРЖДЕНИЕ ПРИВЯЗКИ ЧАТА ВЛАДЕЛЬЦЕМ ==========
        @dp.my_chat_member()
        async def on_chat_member_updated(update: types.ChatMemberUpdated):
            """Never let a group member silently replace the moderation route."""
            chat = update.chat
            if update.new_chat_member.status != "administrator" or chat.type not in {"group", "supergroup", "channel"}:
                return
            info = await self.db.get_sub_bot_by_id(sub_bot_id)
            if not info:
                return
            kind = "admin" if chat.type in {"group", "supergroup"} else "channel"
            link = None
            if chat.type == "channel":
                try:
                    channel = await bot.get_chat(chat.id)
                    link = f"https://t.me/{channel.username}" if channel.username else f"Канал: {channel.title} (ID: {chat.id})"
                except Exception:
                    link = f"Канал: {chat.title} (ID: {chat.id})"

            # A deliberate action by the configured owner can set the target. If
            # somebody else adds the bot, the owner must approve the exact chat.
            actor_id = update.from_user.id if update.from_user else None
            if actor_id == info["owner_id"]:
                kwargs = {"admin_chat_id": chat.id} if kind == "admin" else {"channel_id": chat.id, "channel_link": link}
                await self.db.update_sub_bot_chats(sub_bot_id=sub_bot_id, **kwargs)
                await bot.send_message(info["owner_id"], f"✅ Чат «{chat.title}» назначен: {kind}.")
                return

            await self.db.stage_chat_binding(sub_bot_id, kind, chat.id)
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Подтвердить привязку", callback_data=f"setup_confirm_{kind}_{chat.id}")],
                [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"setup_cancel_{kind}_{chat.id}")],
            ])
            description = "админ-чат для модерации" if kind == "admin" else "канал для публикаций"
            try:
                await bot.send_message(
                    info["owner_id"],
                    f"Бота @{info['bot_username']} назначили администратором в чате «{chat.title}».\n"
                    f"Проверить и привязать его как {description}? Текущая настройка не меняется до вашего подтверждения.",
                    reply_markup=keyboard,
                )
            except Exception:
                logger.info("Could not notify the owner about a pending chat binding")

        async def confirm_chat_setup(callback: types.CallbackQuery, kind: str, chat_id: int) -> None:
            info = await self.db.get_sub_bot_by_id(sub_bot_id)
            if not info or callback.from_user.id != info["owner_id"]:
                await callback.answer("Только владелец может менять привязку", show_alert=True)
                return
            try:
                chat = await bot.get_chat(chat_id)
                member = await bot.get_chat_member(chat_id, (await bot.get_me()).id)
            except Exception:
                await callback.answer("Не удалось проверить чат и права бота", show_alert=True)
                return
            if chat.type not in ({"group", "supergroup"} if kind == "admin" else {"channel"}):
                await callback.answer("Тип чата не совпадает с выбранной привязкой", show_alert=True)
                return
            if member.status != "administrator":
                await callback.answer("Бот уже не является администратором этого чата", show_alert=True)
                return
            link = f"https://t.me/{chat.username}" if getattr(chat, "username", None) else f"Канал: {chat.title} (ID: {chat.id})"
            committed = await self.db.confirm_chat_binding(sub_bot_id, kind, chat_id, link if kind == "channel" else None)
            if not committed:
                await callback.answer("Эта привязка больше не ожидает подтверждения", show_alert=True)
                return
            await callback.message.edit_text(f"✅ Привязка обновлена: {chat.title}.")
            await callback.answer("Сохранено")

        @dp.callback_query(F.data.startswith("setup_confirm_admin_"))
        async def confirm_admin_chat(callback: types.CallbackQuery):
            try:
                chat_id = int(callback.data.rsplit("_", 1)[1])
            except (ValueError, AttributeError):
                await callback.answer("Некорректная привязка", show_alert=True)
                return
            await confirm_chat_setup(callback, "admin", chat_id)

        @dp.callback_query(F.data.startswith("setup_confirm_channel_"))
        async def confirm_channel_chat(callback: types.CallbackQuery):
            try:
                chat_id = int(callback.data.rsplit("_", 1)[1])
            except (ValueError, AttributeError):
                await callback.answer("Некорректная привязка", show_alert=True)
                return
            await confirm_chat_setup(callback, "channel", chat_id)

        @dp.callback_query(F.data.startswith("setup_cancel_"))
        async def cancel_chat_setup(callback: types.CallbackQuery):
            info = await self.db.get_sub_bot_by_id(sub_bot_id)
            if not info or callback.from_user.id != info["owner_id"]:
                await callback.answer("Только владелец может отклонить привязку", show_alert=True)
                return
            try:
                kind, chat_id = callback.data[len("setup_cancel_"):].rsplit("_", 1)
                await self.db.cancel_chat_binding(sub_bot_id, kind, int(chat_id))
            except (ValueError, AttributeError):
                await callback.answer("Некорректная привязка", show_alert=True)
                return
            await callback.message.edit_text("Привязка отклонена. Текущая настройка сохранена.")
            await callback.answer("Отклонено")
        
        # ========== АНОНИМНОСТЬ ==========
        @dp.message(F.text == "👤 Анонимно")
        async def set_anonymous(message: types.Message, state: FSMContext):
            """Включить анонимный режим"""
            # КРИТИЧНО: Игнорируем сообщения из админ-чата и канала
            if await is_admin_or_channel_message(message):
                return
            
            user_id = message.from_user.id
            sub_bot_data = await self.db.get_sub_bot_by_token(bot.token)
            if not sub_bot_data:
                return
            
            sub_bot_id = sub_bot_data['id']
            
            # Сохраняем в FSM для текущей сессии
            await state.update_data(is_anonymous=True)
            # Сохраняем в БД для сохранения после перезапуска
            await self.db.set_user_anonymous_mode(sub_bot_id, user_id, True)
            logger.info(f"Пользователь {user_id} включил анонимный режим (сохранено в БД)")
            await message.answer("✅ Режим: <b>Анонимно</b>\n\nТеперь отправьте ваше предложение.", parse_mode="HTML")
        
        @dp.message(F.text == "👥 Не анонимно")
        async def set_not_anonymous(message: types.Message, state: FSMContext):
            """Выключить анонимный режим"""
            # КРИТИЧНО: Игнорируем сообщения из админ-чата и канала
            if await is_admin_or_channel_message(message):
                return
            
            user_id = message.from_user.id
            sub_bot_data = await self.db.get_sub_bot_by_token(bot.token)
            if not sub_bot_data:
                return
            
            sub_bot_id = sub_bot_data['id']
            
            # Сохраняем в FSM для текущей сессии
            await state.update_data(is_anonymous=False)
            # Сохраняем в БД для сохранения после перезапуска
            await self.db.set_user_anonymous_mode(sub_bot_id, user_id, False)
            logger.info(f"Пользователь {user_id} выключил анонимный режим (сохранено в БД)")
            await message.answer("✅ Режим: <b>Не анонимно</b>\n\nТеперь отправьте ваше предложение.", parse_mode="HTML")
        
        # ========== ОТПРАВКА ПРЕДЛОЖЕНИЯ ==========
        @dp.message(F.text == "📝 Отправить предложение")
        async def ask_for_suggestion(message: types.Message):
            """Запрос на отправку предложения"""
            await message.answer(
                "📝 Отправьте ваше предложение:\n\n"
                "Вы можете отправить текст, фото, видео или другой контент."
            )
        
        @dp.message(F.text == "ℹ️ Информация")
        async def show_info(message: types.Message, state: FSMContext):
            """Показать информацию"""
            # КРИТИЧНО: Игнорируем сообщения из админ-чата и канала
            if await is_admin_or_channel_message(message):
                return
            
            user_id = message.from_user.id
            sub_bot_data = await self.db.get_sub_bot_by_token(bot.token)
            if not sub_bot_data:
                return
            
            sub_bot_id = sub_bot_data['id']
            
            # Получаем текущий режим анонимности (из FSM или БД)
            data = await state.get_data()
            is_anonymous = data.get('is_anonymous', None)
            
            # Если в FSM нет, загружаем из БД
            if is_anonymous is None:
                is_anonymous = await self.db.get_user_anonymous_mode(sub_bot_id, user_id)
                await state.update_data(is_anonymous=is_anonymous)
            
            mode_text = "Анонимно" if is_anonymous else "Не анонимно"
            
            info_text = (
                "ℹ️ <b>Информация</b>\n\n"
                "Этот бот принимает ваши предложения и после модерации публикует их в канале.\n\n"
                "Вы можете отправлять:\n"
                "- Текст\n"
                "- Фото\n"
                "- Видео\n"
                "- Документы\n\n"
                "Режимы:\n"
                "👤 Анонимно - ваше имя не будет показано модераторам\n"
                "👥 Не анонимно - модераторы увидят ваше имя\n\n"
                f"Текущий режим: <b>{mode_text}</b>\n\n"
                "━━━━━━━━━━━━━━━\n\n"
                "⚠️ <b>Важно:</b> из этого бота нельзя ответить автору предложения",
            )
            await message.answer(info_text, parse_mode="HTML")
        
        # ========== КНОПКИ ВЛАДЕЛЬЦА (ПРИОРИТЕТ) ==========
        @dp.message(F.text == "📢 Рассылка пользователям")
        async def start_broadcast_owner(message: types.Message, state: FSMContext):
            """Начать рассылку (только для владельца)"""
            user_id = message.from_user.id
            sub_bot_data = await self.db.get_sub_bot_by_token(bot.token)
            
            logger.info(f"Кнопка 'Рассылка' нажата пользователем {user_id}, проверка прав...")
            
            if not sub_bot_data or user_id != sub_bot_data['owner_id']:
                logger.warning(f"Пользователь {user_id} не является владельцем бота {sub_bot_id}")
                return
            
            logger.info(f"Владелец {user_id} начал рассылку для бота {sub_bot_id}")
            
            # Получаем количество пользователей
            users = await self.db.get_all_users_of_sub_bot(sub_bot_id)
            active_users = [u for u in users if not u['is_blocked']]
            
            if not active_users:
                await message.answer(
                    "❌ У вашего бота пока нет пользователей для рассылки.",
                    parse_mode="HTML"
                )
                return
            
            await message.answer(
                f"📢 <b>Рассылка</b>\n\n"
                f"Отправьте сообщение, которое хотите разослать.\n\n"
                f"Количество получателей: <b>{len(active_users)}</b>\n\n"
                f"Можете отправить текст, фото, видео или документ.",
                parse_mode="HTML"
            )
            await state.set_state(UserBroadcast.waiting_for_message)
            logger.info(f"FSM состояние установлено: UserBroadcast.waiting_for_message для пользователя {user_id}")
        
        @dp.message(F.text == "📊 Статистика бота")
        async def show_owner_stats_priority(message: types.Message):
            """Показать статистику владельцу"""
            # КРИТИЧНО: Игнорируем сообщения из админ-чата и канала
            if await is_admin_or_channel_message(message):
                logger.debug(f"ИГНОРИРУЕМ статистику из админ-чата/канала (chat_id={message.chat.id}, type={message.chat.type})")
                return
            
            user_id = message.from_user.id
            sub_bot_data = await self.db.get_sub_bot_by_token(bot.token)
            
            logger.info(f"Кнопка 'Статистика' нажата пользователем {user_id}, проверка прав...")
            
            if not sub_bot_data or user_id != sub_bot_data['owner_id']:
                logger.warning(f"Пользователь {user_id} не является владельцем бота {sub_bot_id}")
                return
            
            logger.info(f"Показываем статистику владельцу {user_id} для бота {sub_bot_id}")
            
            stats = await self.db.get_sub_bot_statistics(sub_bot_id)
            
            await message.answer(
                f"📊 <b>Статистика вашего бота</b>\n\n"
                f"👥 Всего пользователей: {stats['users']}\n"
                f"💬 Всего сообщений: {stats['messages']}\n"
                f"✅ Опубликовано: {stats['published']}\n"
                f"❌ Отклонено: {stats['rejected']}\n"
                f"⏳ На модерации: {stats['pending']}",
                parse_mode="HTML"
            )
        
        @dp.message(F.text == "📝 Оформление снизу")
        async def change_footer_start_priority(message: types.Message, state: FSMContext):
            """Начать изменение оформления снизу (footer)"""
            user_id = message.from_user.id
            sub_bot_data = await self.db.get_sub_bot_by_token(bot.token)
            
            logger.info(f"Кнопка 'Изменить оформление' нажата пользователем {user_id}, проверка прав...")
            
            if not sub_bot_data or user_id != sub_bot_data['owner_id']:
                logger.warning(f"Пользователь {user_id} не является владельцем бота {sub_bot_id}")
                return
            
            logger.info(f"Владелец {user_id} начал изменение оформления для бота {sub_bot_id}")
            
            current_footer = sub_bot_data.get('post_footer')
            
            footer_preview = f"<code>{current_footer}</code>" if current_footer else "❌ Не установлено"
            
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🗑 Удалить оформление", callback_data="footer_remove")],
                [InlineKeyboardButton(text="❌ Отмена", callback_data="footer_cancel")]
            ])
            
            await message.answer(
                f"📝 <b>Изменение оформления постов</b>\n\n"
                f"<b>Текущее оформление:</b>\n{footer_preview}\n\n"
                f"Отправьте новый текст оформления.\n\n"
                f"💡 <b>Как работает оформление:</b>\n"
                f"Оформление будет добавляться в самом низу каждого одобренного поста в канале.\n\n"
                f"<b>Пример:</b>\n"
                f"Пост пользователя: \"Привет!\"\n"
                f"В канале будет:\n"
                f"Привет!\n\n"
                f"━━━━━━━━━━━━━━━\n"
                f"💬 Ваш канал\n"
                f"🔗 @your_channel",
                reply_markup=keyboard,
                parse_mode="HTML"
            )
            await state.set_state(ChangeFooter.waiting_for_footer)
            logger.info(f"FSM состояние установлено: ChangeFooter.waiting_for_footer для пользователя {user_id}")
        
        # ========== ОБРАБОТКА FSM СОСТОЯНИЙ (ПРИОРИТЕТ) ==========
        @dp.message(ChangeFooter.waiting_for_footer)
        async def process_footer_fsm(message: types.Message, state: FSMContext):
            """Обработка нового оформления (FSM состояние)"""
            user_id = message.from_user.id
            sub_bot_data = await self.db.get_sub_bot_by_token(bot.token)
            
            if not sub_bot_data or user_id != sub_bot_data['owner_id']:
                logger.warning(f"Попытка изменить оформление не владельцем: {user_id}")
                await state.clear()
                return
            
            # Проверяем что это личное сообщение
            if message.chat.type != "private":
                await state.clear()
                return
            
            if not rich_html_from_message(message).strip():
                await message.answer("Отправьте текст или подпись с оформлением.")
                return
            new_footer = rich_html_from_message(message)
            preview = compose_post_html("Пример предложения", footer=new_footer)
            if rich_message_too_long(preview):
                await message.answer("Фрагмент превышает лимит Rich HTML. Укоротите его и отправьте снова.")
                return
            try:
                await send_rich_html(bot, chat_id=message.chat.id, content=preview)
            except Exception as exc:
                logger.info("Child footer preview rejected (%s)", type(exc).__name__)
                await message.answer("Telegram не принял HTML. Исправьте разметку и отправьте фрагмент ещё раз.")
                return
            await state.update_data(pending_footer=new_footer)
            await state.set_state(ChangeFooter.waiting_for_confirmation)
            await message.answer(
                "Предпросмотр выше. Сохранить оформление?",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✅ Сохранить", callback_data="child_footer_save")],
                    [InlineKeyboardButton(text="✏️ Изменить", callback_data="child_footer_edit")],
                    [InlineKeyboardButton(text="❌ Отмена", callback_data="child_footer_cancel")],
                ]),
            )

        @dp.callback_query(F.data.startswith("child_footer_"))
        async def confirm_footer_fsm(callback: types.CallbackQuery, state: FSMContext):
            sub_bot_data = await self.db.get_sub_bot_by_token(bot.token)
            if not sub_bot_data or callback.from_user.id != sub_bot_data["owner_id"]:
                await callback.answer("Нет доступа", show_alert=True)
                return
            action = callback.data.removeprefix("child_footer_")
            data = await state.get_data()
            if action == "save" and await state.get_state() == ChangeFooter.waiting_for_confirmation.state:
                await self.db.update_post_footer(sub_bot_id, data.get("pending_footer"))
                await callback.message.edit_text("✅ Rich HTML оформление сохранено.")
                await state.clear()
            elif action == "edit":
                await state.set_state(ChangeFooter.waiting_for_footer)
                await callback.message.edit_text("Отправьте изменённый фрагмент Rich HTML.")
            elif action == "cancel":
                await state.clear()
                await callback.message.edit_text("Изменение оформления отменено.")
            await callback.answer()
        
        # ========== ОФОРМЛЕНИЕ СВЕРХУ (HEADER) ==========
        @dp.message(F.text == "📝 Оформление сверху")
        async def change_header_start(message: types.Message, state: FSMContext):
            """Начать изменение оформления сверху (header)"""
            user_id = message.from_user.id
            sub_bot_data = await self.db.get_sub_bot_by_token(bot.token)
            
            if not sub_bot_data or user_id != sub_bot_data['owner_id']:
                return
            
            if message.chat.type != "private":
                return
            
            current_header = sub_bot_data.get('post_header')
            header_mode = sub_bot_data.get('header_mode', 'newline')
            
            header_preview = f"<code>{current_header}</code>" if current_header else "❌ Не установлено"
            mode_text = "в одну строку с постом" if header_mode == 'inline' else "на отдельной строке"
            
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🗑 Удалить оформление сверху", callback_data="header_remove")],
                [InlineKeyboardButton(text="❌ Отмена", callback_data="header_cancel")]
            ])
            
            await message.answer(
                f"📝 <b>Оформление сверху (header)</b>\n\n"
                f"<b>Текущее оформление:</b>\n{header_preview}\n"
                f"<b>Режим:</b> {mode_text}\n\n"
                f"Отправьте новый текст оформления.\n\n"
                f"💡 <b>Как работает:</b>\n"
                f"Оформление будет добавляться В НАЧАЛЕ каждого одобренного поста.\n"
                f"Можно использовать одновременно с оформлением снизу.",
                reply_markup=keyboard,
                parse_mode="HTML"
            )
            await state.set_state(ChangeHeader.waiting_for_header)
        
        @dp.message(ChangeHeader.waiting_for_header)
        async def process_header_fsm(message: types.Message, state: FSMContext):
            """Обработка нового оформления сверху"""
            user_id = message.from_user.id
            sub_bot_data = await self.db.get_sub_bot_by_token(bot.token)
            
            if not sub_bot_data or user_id != sub_bot_data['owner_id']:
                await state.clear()
                return
            
            if message.chat.type != "private":
                await state.clear()
                return
            
            if not rich_html_from_message(message).strip():
                await message.answer("Отправьте текст или подпись с оформлением.")
                return
            new_header = rich_html_from_message(message)
            
            # Спрашиваем про режим
            await state.update_data(new_header=new_header)
            
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📄 На отдельной строке", callback_data="header_mode_newline")],
                [InlineKeyboardButton(text="📝 В одну строку с постом", callback_data="header_mode_inline")]
            ])
            
            await message.answer(
                f"<b>Выберите режим:</b>\n\n"
                f"📄 <b>На отдельной строке:</b>\n"
                f"<code>{new_header}</code>\n\n"
                f"<code>Текст поста...</code>\n\n"
                f"📝 <b>В одну строку:</b>\n"
                f"<code>{new_header}</code> <code>Текст поста...</code>",
                reply_markup=keyboard,
                parse_mode="HTML"
            )
            await state.set_state(ChangeHeader.waiting_for_mode)
        
        @dp.callback_query(F.data.startswith("header_mode_"))
        async def process_header_mode(callback: types.CallbackQuery, state: FSMContext):
            """Обработка выбора режима header"""
            user_id = callback.from_user.id
            sub_bot_data = await self.db.get_sub_bot_by_token(bot.token)
            
            if not sub_bot_data or user_id != sub_bot_data['owner_id']:
                await callback.answer("❌ Нет доступа")
                await state.clear()
                return
            
            mode = callback.data.split("_")[-1]  # newline или inline
            data = await state.get_data()
            new_header = data.get('new_header')
            
            if not new_header:
                await callback.answer("❌ Ошибка")
                await state.clear()
                return
            
            # Сохраняем header и mode
            mode_text = "в одну строку с постом" if mode == 'inline' else "на отдельной строке"
            preview = compose_post_html(
                "Пример предложения",
                header=new_header,
                footer=sub_bot_data.get("post_footer"),
                header_mode=mode,
            )
            if rich_message_too_long(preview):
                await callback.answer("Фрагмент превышает лимит Rich HTML", show_alert=True)
                return
            try:
                await send_rich_html(bot, chat_id=callback.message.chat.id, content=preview)
            except Exception as exc:
                logger.info("Child header preview rejected (%s)", type(exc).__name__)
                await callback.answer("Telegram не принял разметку. Исправьте её и попробуйте снова.", show_alert=True)
                return
            await state.update_data(pending_header=new_header, pending_header_mode=mode)
            await state.set_state(ChangeHeader.waiting_for_confirmation)
            await callback.message.edit_text(
                f"Предпросмотр выше. Сохранить оформление сверху ({mode_text})?",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✅ Сохранить", callback_data="child_header_save")],
                    [InlineKeyboardButton(text="✏️ Изменить", callback_data="child_header_edit")],
                    [InlineKeyboardButton(text="❌ Отмена", callback_data="child_header_cancel")],
                ]),
            )
            await callback.answer()

        @dp.callback_query(F.data.startswith("child_header_"))
        async def confirm_header_fsm(callback: types.CallbackQuery, state: FSMContext):
            sub_bot_data = await self.db.get_sub_bot_by_token(bot.token)
            if not sub_bot_data or callback.from_user.id != sub_bot_data["owner_id"]:
                await callback.answer("Нет доступа", show_alert=True)
                return
            action = callback.data.removeprefix("child_header_")
            data = await state.get_data()
            if action == "save" and await state.get_state() == ChangeHeader.waiting_for_confirmation.state:
                await self.db.update_post_header(
                    sub_bot_id,
                    data.get("pending_header"),
                    data.get("pending_header_mode", "newline"),
                )
                await callback.message.edit_text("✅ Rich HTML оформление сверху сохранено.")
                await state.clear()
            elif action == "edit":
                await state.set_state(ChangeHeader.waiting_for_header)
                await callback.message.edit_text("Отправьте изменённый фрагмент Rich HTML.")
            elif action == "cancel":
                await state.clear()
                await callback.message.edit_text("Изменение оформления отменено.")
            await callback.answer()
        
        @dp.callback_query(F.data == "header_remove")
        async def remove_header(callback: types.CallbackQuery, state: FSMContext):
            """Удалить оформление сверху"""
            user_id = callback.from_user.id
            sub_bot_data = await self.db.get_sub_bot_by_token(bot.token)
            
            if not sub_bot_data or user_id != sub_bot_data['owner_id']:
                await callback.answer("❌ Нет доступа")
                return
            
            await self.db.update_post_header(sub_bot_id, None, 'newline')
            
            await callback.message.edit_text(
                "✅ <b>Оформление сверху удалено!</b>",
                parse_mode="HTML"
            )
            await callback.answer("✅ Удалено")
            await state.clear()
        
        @dp.callback_query(F.data == "header_cancel")
        async def cancel_header(callback: types.CallbackQuery, state: FSMContext):
            """Отмена изменения оформления сверху"""
            await callback.message.edit_text("❌ Изменение оформления отменено.")
            await callback.answer()
            await state.clear()
        
        # ========== КОМАНДЫ В АДМИН-ЧАТЕ (РЕГИСТРИРУЕМ ПЕРЕД ОБЩИМ ОБРАБОТЧИКОМ) ==========
        @dp.message(Command("stats"))
        async def show_stats(message: types.Message):
            """Показать статистику (только в админ-чате)"""
            sub_bot_data = await self.db.get_sub_bot_by_token(bot.token)
            if not sub_bot_data:
                return
            
            # Проверяем, что это админ-чат
            if message.chat.id != sub_bot_data['admin_chat_id']:
                return
            if not await self._is_chat_moderator(bot, sub_bot_data, message.chat.id, message.from_user.id):
                return
            
            stats = await self.db.get_sub_bot_statistics(sub_bot_id)
            
            await message.answer(
                f"📊 <b>Статистика бота</b>\n\n"
                f"👥 Пользователей: {stats['users']}\n"
                f"💬 Сообщений: {stats['messages']}\n"
                f"✅ Опубликовано: {stats['published']}\n",
                parse_mode="HTML"
            )
        
        @dp.message(Command("info"))
        async def show_user_info(message: types.Message):
            """Показать информацию о пользователе (только в админ-чате, нужно ответить на сообщение)"""
            sub_bot_data = await self.db.get_sub_bot_by_token(bot.token)
            if not sub_bot_data:
                return
            
            # Проверяем, что это админ-чат
            if message.chat.id != sub_bot_data['admin_chat_id']:
                return
            if not await self._is_chat_moderator(bot, sub_bot_data, message.chat.id, message.from_user.id):
                await message.answer("❌ Команда доступна только администраторам чата.")
                return
            
            # Проверяем, есть ли ответ на сообщение
            if not message.reply_to_message:
                await message.answer("❌ Ответьте на сообщение, чтобы увидеть информацию о пользователе.")
                return
            
            # Получаем ID сообщения из админ-чата
            admin_message_id = message.reply_to_message.message_id
            
            # Получаем сообщение из базы
            msg_data = await self.db.get_message_by_admin_msg_id(admin_message_id, sub_bot_id=sub_bot_data['id'])
            if not msg_data:
                await message.answer("❌ Информация о сообщении не найдена.")
                return
            
            user_id = msg_data['user_id']
            is_anonymous = msg_data['is_anonymous']
            
            # Формируем информацию о пользователе
            if is_anonymous:
                sender_info = "📬 <b>Анонимно</b>"
            else:
                # Получаем информацию о пользователе из Telegram
                try:
                    user_info = await bot.get_chat(user_id)
                    sender_info = f"👤 <b>От:</b> {user_info.full_name or 'Неизвестно'}\n"
                    sender_info += f"🆔 <b>ID:</b> <code>{user_id}</code>"
                    if user_info.username:
                        sender_info += f"\n📱 <b>Username:</b> @{user_info.username}"
                except Exception as e:
                    logger.error(f"Ошибка получения информации о пользователе {user_id}: {e}")
                    sender_info = f"👤 <b>От:</b> Неизвестно\n🆔 <b>ID:</b> <code>{user_id}</code>"
            
            # Отправляем информацию в ответ на команду
            await message.answer(sender_info, parse_mode="HTML", reply_to_message_id=message.reply_to_message.message_id)
        
        @dp.message(Command("block", "ban"))
        async def block_user_cmd(message: types.Message, state: FSMContext):
            """Заблокировать пользователя (только в админ-чате)"""
            sub_bot_data = await self.db.get_sub_bot_by_token(bot.token)
            if not sub_bot_data:
                return
            
            # Проверяем, что это админ-чат
            if message.chat.id != sub_bot_data['admin_chat_id']:
                return
            if not await self._is_chat_moderator(bot, sub_bot_data, message.chat.id, message.from_user.id):
                await message.answer("❌ Команда доступна только администраторам чата.")
                return
            
            user_id_to_block = None
            sub_bot_id = sub_bot_data['id']
            
            # ПРИОРИТЕТ: Проверяем, есть ли ответ на сообщение
            if message.reply_to_message:
                # Получаем ID сообщения, на которое ответили
                replied_message = message.reply_to_message
                replied_message_id = replied_message.message_id
                
                # Находим оригинальное сообщение пользователя в базе
                # Сначала пытаемся найти по прямому ответу
                msg_data = await self.db.get_message_by_admin_msg_id(replied_message_id, sub_bot_id=sub_bot_id)
                
                # Если не нашли, возможно админ ответил на информационное сообщение (reply к основному)
                if not msg_data and replied_message.reply_to_message:
                    # Пытаемся найти по исходному сообщению
                    original_message_id = replied_message.reply_to_message.message_id
                    msg_data = await self.db.get_message_by_admin_msg_id(original_message_id, sub_bot_id=sub_bot_id)
                
                if msg_data:
                    # КРИТИЧНО: Используем sub_bot_id из сообщения для точности
                    msg_sub_bot_id = msg_data.get('sub_bot_id')
                    if msg_sub_bot_id:
                        # Проверяем, что сообщение относится к этому боту
                        if msg_sub_bot_id != sub_bot_id:
                            await message.answer(
                                f"❌ Это сообщение относится к другому боту (ID: {msg_sub_bot_id}).\n"
                                f"Используйте команду /ban в правильном админ-чате."
                            )
                            logger.warning(f"Попытка забанить пользователя из другого бота: msg_sub_bot_id={msg_sub_bot_id}, current_sub_bot_id={sub_bot_id}")
                            return
                        sub_bot_id = msg_sub_bot_id
                    
                    # КРИТИЧНО: Используем user_id из сообщения в БД - это самый надежный источник
                    user_id_to_block = msg_data.get('user_id')
                    if not user_id_to_block:
                        await message.answer("❌ Не удалось найти user_id в базе данных.")
                        return
                    logger.info(f"Найден user_id {user_id_to_block} из БД (admin_message_id={replied_message_id}, sub_bot_id={sub_bot_id})")
                else:
                    # Если не нашли в БД, пытаемся найти ID в тексте сообщения (в footer с информацией)
                    replied_text = replied_message.text or replied_message.caption or ""
                    # Пробуем разные форматы
                    match = re.search(r'🆔 ID: <code>(\d+)</code>', replied_text)  # HTML формат
                    if not match:
                        match = re.search(r'🆔 ID:\s*(\d+)', replied_text)  # Простой формат
                    if not match:
                        match = re.search(r'ID:\s*(\d+)', replied_text)  # Без эмодзи
                    if match:
                        user_id_to_block = int(match.group(1))
                        logger.info(f"Найден user_id {user_id_to_block} из текста сообщения")
                    else:
                        await message.answer(
                            "❌ Не удалось найти пользователя.\n\n"
                            "Ответьте на сообщение с предложением пользователя командой /ban"
                        )
                        return
            else:
                # Проверяем, есть ли ID в команде
                args = message.text.split()
                if len(args) < 2:
                    await message.answer(
                        "❌ Используйте: /ban USER_ID\n"
                        "Или ответьте на сообщение пользователя командой /ban"
                    )
                    return
                
                try:
                    user_id_to_block = int(args[1])
                except ValueError:
                    await message.answer("❌ Неверный ID пользователя")
                    return
            
            if not user_id_to_block:
                await message.answer("❌ Не удалось определить пользователя для блокировки")
                return
            
            # Блокируем пользователя
            await self.db.block_user(sub_bot_id, user_id_to_block)
            await message.answer(f"🚫 Пользователь {user_id_to_block} заблокирован в боте {sub_bot_id}")
            logger.info(f"Пользователь {user_id_to_block} заблокирован в боте {sub_bot_id}")
        
        @dp.message(Command("unblock", "unban"))
        async def unblock_user_cmd(message: types.Message):
            """Разблокировать пользователя (только в админ-чате)"""
            sub_bot_data = await self.db.get_sub_bot_by_token(bot.token)
            if not sub_bot_data:
                return
            
            # Проверяем, что это админ-чат
            if message.chat.id != sub_bot_data['admin_chat_id']:
                return
            if not await self._is_chat_moderator(bot, sub_bot_data, message.chat.id, message.from_user.id):
                return
            
            user_id_to_unblock = None
            sub_bot_id = sub_bot_data['id']
            
            # ПРИОРИТЕТ: Проверяем, есть ли ответ на сообщение
            if message.reply_to_message:
                # Получаем ID сообщения, на которое ответили
                replied_message = message.reply_to_message
                replied_message_id = replied_message.message_id
                
                # Находим оригинальное сообщение пользователя в базе
                # Сначала пытаемся найти по прямому ответу
                msg_data = await self.db.get_message_by_admin_msg_id(replied_message_id, sub_bot_id=sub_bot_id)
                
                # Если не нашли, возможно админ ответил на информационное сообщение (reply к основному)
                if not msg_data and replied_message.reply_to_message:
                    # Пытаемся найти по исходному сообщению
                    original_message_id = replied_message.reply_to_message.message_id
                    msg_data = await self.db.get_message_by_admin_msg_id(original_message_id, sub_bot_id=sub_bot_id)
                
                if msg_data:
                    # КРИТИЧНО: Используем user_id из сообщения в БД - это самый надежный источник
                    user_id_to_unblock = msg_data.get('user_id')
                    if not user_id_to_unblock:
                        await message.answer("❌ Не удалось найти user_id в базе данных.")
                        return
                    
                    # КРИТИЧНО: Проверяем, относится ли сообщение к текущему боту
                    msg_sub_bot_id = msg_data.get('sub_bot_id')
                    if msg_sub_bot_id and msg_sub_bot_id != sub_bot_id:
                        # Сообщение относится к другому боту, но мы можем разблокировать пользователя в текущем боте
                        # Используем sub_bot_id текущего бота (того, в админ-чате которого мы находимся)
                        logger.info(f"Сообщение относится к другому боту (ID: {msg_sub_bot_id}), но разблокируем пользователя {user_id_to_unblock} в текущем боте {sub_bot_id}")
                        # sub_bot_id уже установлен в текущий бот, продолжаем
                    elif msg_sub_bot_id:
                        # Сообщение относится к правильному боту
                        sub_bot_id = msg_sub_bot_id
                    
                    logger.info(f"Найден user_id {user_id_to_unblock} из БД (admin_message_id={replied_message_id}, sub_bot_id={sub_bot_id})")
                else:
                    # Если не нашли в БД, пытаемся найти ID в тексте сообщения (в footer с информацией)
                    replied_text = replied_message.text or replied_message.caption or ""
                    # Пробуем разные форматы
                    match = re.search(r'🆔 ID: <code>(\d+)</code>', replied_text)  # HTML формат
                    if not match:
                        match = re.search(r'🆔 ID:\s*(\d+)', replied_text)  # Простой формат
                    if not match:
                        match = re.search(r'ID:\s*(\d+)', replied_text)  # Без эмодзи
                    if not match:
                        # Пробуем найти в entities (если есть форматирование)
                        if replied_message.entities:
                            for entity in replied_message.entities:
                                if entity.type == "code" and entity.length <= 15:  # ID обычно короткий
                                    code_text = replied_text[entity.offset:entity.offset + entity.length]
                                    try:
                                        potential_id = int(code_text)
                                        if potential_id > 0:  # Валидный Telegram ID
                                            user_id_to_unblock = potential_id
                                            logger.info(f"Найден user_id {user_id_to_unblock} из code entity")
                                            break
                                    except ValueError:
                                        pass
                    
                    if not user_id_to_unblock and match:
                        user_id_to_unblock = int(match.group(1))
                        logger.info(f"Найден user_id {user_id_to_unblock} из текста сообщения")
                    
                    if not user_id_to_unblock:
                        await message.answer(
                            "❌ Не удалось найти пользователя.\n\n"
                            "Ответьте на сообщение с предложением пользователя командой /unban"
                        )
                        return
            else:
                # Нет ответа на сообщение - используем аргумент команды
                args = message.text.split()
                if len(args) < 2:
                    await message.answer(
                        "❌ Используйте: /unban USER_ID или /unban @username\n"
                        "Или ответьте на сообщение пользователя командой /unban"
                    )
                    return
                
                # Пробуем определить, это ID или username
                input_arg = args[1]
                try:
                    # Пробуем как числовой ID
                    user_id_to_unblock = int(input_arg)
                except ValueError:
                    # Это не число, пробуем как username
                    username = input_arg.lstrip('@')
                    user_id_to_unblock = await self.db.get_user_id_by_username_in_sub_bot(sub_bot_id, username)
                    
                    if not user_id_to_unblock:
                        await message.answer(
                            f"❌ Пользователь с username <b>@{username}</b> не найден в этом под-боте.\n\n"
                            f"Используйте числовой <b>USER_ID</b> или username пользователя, который взаимодействовал с этим под-ботом.",
                            parse_mode="HTML"
                        )
                        return
            
            if not user_id_to_unblock:
                await message.answer("❌ Не удалось определить пользователя для разблокировки")
                return
            
            if not user_id_to_unblock:
                await message.answer("❌ Не удалось определить пользователя для разблокировки")
                return
            
            # КРИТИЧНО: Проверяем, что пользователь действительно заблокирован
            # Сначала убеждаемся, что пользователь существует в БД
            await self.db.add_or_update_user(
                sub_bot_id, 
                user_id_to_unblock,
                username=None,
                first_name=None
            )
            
            # Теперь проверяем статус блокировки
            is_blocked = await self.db.is_user_blocked(sub_bot_id, user_id_to_unblock)
            if not is_blocked:
                # Получаем username для красивого сообщения
                user_info = f"{user_id_to_unblock}"
                try:
                    async with aiosqlite.connect(self.db.db_path) as db:
                        async with db.execute(
                            "SELECT username FROM sub_bot_users WHERE sub_bot_id = ? AND user_id = ?",
                            (sub_bot_id, user_id_to_unblock)
                        ) as cursor:
                            row = await cursor.fetchone()
                            if row and row[0]:
                                user_info = f"@{row[0]} ({user_id_to_unblock})"
                except:
                    pass
                
                await message.answer(f"ℹ️ Пользователь {user_info} не заблокирован в этом боте")
                return
            
            # Разблокируем пользователя
            await self.db.unblock_user(sub_bot_id, user_id_to_unblock)
            
            # Проверяем, что разблокировка прошла успешно
            is_still_blocked = await self.db.is_user_blocked(sub_bot_id, user_id_to_unblock)
            if is_still_blocked:
                await message.answer(f"❌ Ошибка: не удалось разблокировать пользователя {user_id_to_unblock}")
                logger.error(f"Ошибка разблокировки: пользователь {user_id_to_unblock} все еще заблокирован в боте {sub_bot_id}")
                return
            
            # Получаем username для красивого сообщения
            user_info = f"{user_id_to_unblock}"
            try:
                async with aiosqlite.connect(self.db.db_path) as db:
                    async with db.execute(
                        "SELECT username FROM sub_bot_users WHERE sub_bot_id = ? AND user_id = ?",
                        (sub_bot_id, user_id_to_unblock)
                    ) as cursor:
                        row = await cursor.fetchone()
                        if row and row[0]:
                            user_info = f"@{row[0]} ({user_id_to_unblock})"
            except:
                pass
            
            await message.answer(f"✅ Пользователь {user_info} разблокирован в этом боте")
            logger.info(f"Пользователь {user_id_to_unblock} разблокирован в боте {sub_bot_id}")
        
        # ========== ОБРАТНАЯ СВЯЗЬ ОТ АДМИНА К ПОЛЬЗОВАТЕЛЮ (РЕГИСТРИРУЕМ ПЕРЕД ОБЩИМ) ==========
        @dp.message(F.reply_to_message)
        async def handle_admin_reply(message: types.Message):
            """Обработка ответов админа в админ-чате - пересылка пользователю"""
            # КРИТИЧНО: САМАЯ ПЕРВАЯ ПРОВЕРКА - это НЕ должен быть private чат
            # Админы отвечают в группе, а не в личке
            if message.chat.type == "private":
                return
            
            # Проверяем, что это админ-чат
            sub_bot_data = await self.db.get_sub_bot_by_token(bot.token)
            if not sub_bot_data or not sub_bot_data.get('admin_chat_id'):
                return
            
            # КРИТИЧНО: Проверяем, что это админ-чат - если НЕ админ-чат, сразу выходим
            if message.chat.id != sub_bot_data['admin_chat_id']:
                return
            if not await self._is_chat_moderator(bot, sub_bot_data, message.chat.id, message.from_user.id):
                return
            
            # Игнорируем команды
            if message.text and message.text.startswith('/'):
                return
            
            # Получаем сообщение, на которое админ ответил
            replied_message = message.reply_to_message
            if not replied_message:
                return
            
            # КРИТИЧНО: Проверяем, что это ответ на сообщение, отправленное БОТОМ
            # Сообщения от бота имеют from_user.id равным ID бота
            bot_info = await bot.get_me()
            bot_user_id = bot_info.id
            
            # Если сообщение, на которое отвечают, НЕ от бота - игнорируем
            if not replied_message.from_user or replied_message.from_user.id != bot_user_id:
                logger.debug(f"Игнорируем ответ на сообщение не от бота (from_user_id={replied_message.from_user.id if replied_message.from_user else 'None'}, bot_id={bot_user_id})")
                return
            
            # КРИТИЧНО: Проверяем, что это НЕ ответ на ответ (цепочка сообщений)
            # Если replied_message само является ответом на другое сообщение - игнорируем
            if replied_message.reply_to_message:
                # Это ответ на ответ - игнорируем, не пересылаем
                logger.debug(f"Игнорируем ответ на ответ (admin_message_id={replied_message.message_id}, reply_to={replied_message.reply_to_message.message_id})")
                return
            
            # Получаем ID сообщения, на которое админ ответил (прямой ответ на сообщение от бота)
            replied_message_id = replied_message.message_id
            
            # Находим оригинальное сообщение пользователя в базе по admin_message_id
            msg_data = await self.db.get_message_by_admin_msg_id(replied_message_id, sub_bot_id=sub_bot_id)
            
            if not msg_data:
                # Не нашли сообщение - возможно, это ответ на информационное сообщение или что-то другое
                logger.debug(f"Не найдено сообщение в БД для admin_message_id={replied_message_id}")
                return
            
            # КРИТИЧНО: Проверяем, что sub_bot_id совпадает с текущим ботом
            # Это защита от ситуации, когда несколько ботов используют один админ-чат (маловероятно, но на всякий случай)
            current_sub_bot_id = sub_bot_data['id']
            if msg_data['sub_bot_id'] != current_sub_bot_id:
                logger.warning(f"Несоответствие sub_bot_id: сообщение из БД={msg_data['sub_bot_id']}, текущий бот={current_sub_bot_id}")
                return
            
            # Получаем user_id оригинального сообщения
            user_id = msg_data['user_id']
            
            # Получаем sub_bot_id из данных сообщения (для логирования)
            sub_bot_id = msg_data['sub_bot_id']
            
            # Проверяем, не заблокирован ли пользователь
            is_blocked = await self.db.is_user_blocked(sub_bot_id, user_id)
            if is_blocked:
                logger.debug(f"Попытка отправить ответ заблокированному пользователю {user_id}")
                return
            
            # Пересылаем ответ админа пользователю в личку
            try:
                # Сначала отправляем заголовок
                header_msg = await bot.send_message(
                    chat_id=user_id,
                    text="💬 <b>Ответ от администратора:</b>",
                    parse_mode="HTML"
                )
                
                # Затем копируем сообщение с сохранением форматирования (HTML, премиум эмодзи и т.д.)
                await bot.copy_message(
                    chat_id=user_id,
                    from_chat_id=message.chat.id,
                    message_id=message.message_id
                )
                logger.info(f"✅ Ответ админа переслан пользователю {user_id} в под-боте {sub_bot_id} (admin_message_id={replied_message_id})")
            except Exception as e:
                error_str = str(e).lower()
                error_type = type(e).__name__
                
                # Проверяем, заблокирован ли бот пользователем
                # Telegram API возвращает разные ошибки:
                # - "Chat not found" - пользователь заблокировал бота
                # - "Forbidden: bot was blocked by the user" - бот заблокирован
                # - "Forbidden: user is deactivated" - аккаунт удален
                is_blocked_error = (
                    "chat not found" in error_str or 
                    "forbidden" in error_str or 
                    "user is deactivated" in error_str or 
                    "blocked" in error_str or
                    "bot was blocked" in error_str
                )
                
                if is_blocked_error:
                    # Пользователь заблокировал бота или удалил аккаунт
                    try:
                        await bot.send_message(
                            chat_id=message.chat.id,
                            text=f"⚠️ <b>Не удалось отправить ответ</b>\n\n"
                                 f"Пользователь {user_id} заблокировал бота или удалил аккаунт.\n"
                                 f"Ответ не может быть доставлен.",
                            reply_to_message_id=message.message_id,
                            parse_mode="HTML"
                        )
                        logger.info(f"Админу отправлено уведомление о блокировке бота пользователем {user_id} (ошибка: {error_type})")
                    except Exception as notify_error:
                        logger.error(f"Ошибка отправки уведомления админу: {notify_error}")
                else:
                    # Другая ошибка - логируем, но не спамим админу
                    logger.error(f"Ошибка отправки ответа пользователю {user_id} в под-боте {sub_bot_id}: {e} (тип: {error_type})", exc_info=True)
        
        # ========== ОБРАБОТКА КОНТЕНТА ОТ ПОЛЬЗОВАТЕЛЕЙ (В САМОМ КОНЦЕ!) ==========
        # ВАЖНО: Этот обработчик регистрируется ПОСЛЕДНИМ, чтобы не перехватывать команды и ответы
        # КРИТИЧНО: Используем фильтр F.chat.type == "private" + middleware блокирует админ-чат
        @dp.message(F.chat.type == "private")
        async def handle_user_content(message: types.Message, state: FSMContext):
            """Обработка контента от пользователя"""
            # Дополнительная проверка (на случай если middleware не сработал)
            if message.chat.type != "private":
                logger.warning(f"⚠️ Сообщение не из private чата попало в handle_user_content (chat_id={message.chat.id}, type={message.chat.type})")
                return
            
            # Еще одна проверка админ-чата по ID (на всякий случай)
            sub_bot_data_check = await self.db.get_sub_bot_by_token(bot.token)
            if sub_bot_data_check:
                admin_chat_id = sub_bot_data_check.get('admin_chat_id')
                channel_id = sub_bot_data_check.get('channel_id')
                
                if admin_chat_id and message.chat.id == admin_chat_id:
                    logger.warning(f"⚠️ Сообщение из админ-чата попало в handle_user_content (chat_id={message.chat.id})")
                    return
                if channel_id and message.chat.id == channel_id:
                    logger.warning(f"⚠️ Сообщение из канала попало в handle_user_content (chat_id={message.chat.id})")
                    return
            
            user_id = message.from_user.id
            
            # Проверка кнопок меню
            if message.text in ["📝 Отправить предложение", "👤 Анонимно", "👥 Не анонимно", "ℹ️ Информация",
                               "📢 Рассылка пользователям", "📊 Статистика бота", 
                               "📝 Оформление снизу", "📝 Оформление сверху"]:
                logger.debug(f"Игнорируем кнопку меню: {message.text}")
                return
            
            # Проверяем FSM состояния - если пользователь в процессе рассылки/изменения оформления, пропускаем
            current_state = await state.get_state()
            if current_state in [UserBroadcast.waiting_for_message.state, 
                                ChangeFooter.waiting_for_footer.state,
                                ChangeHeader.waiting_for_header.state,
                                ChangeHeader.waiting_for_mode.state]:
                return
            
            # Проверяем, не заблокирован ли пользователь
            is_blocked = await self.db.is_user_blocked(sub_bot_id, user_id)
            if is_blocked:
                await message.answer("❌ Вы заблокированы и не можете отправлять предложения.")
                return
            
            allowed, limit_message = self._pre_check_spam(
                sub_bot_id,
                user_id,
                str(message.media_group_id) if message.media_group_id else None,
            )
            if not allowed:
                await message.answer(limit_message)
                return
            
            # Получаем настройки
            data = await state.get_data()
            is_anonymous = data.get('is_anonymous', None)
            
            # Если в FSM нет, загружаем из БД
            if is_anonymous is None:
                is_anonymous = await self.db.get_user_anonymous_mode(sub_bot_id, user_id)
                await state.update_data(is_anonymous=is_anonymous)
                logger.info(f"Восстановлен режим анонимности из БД для пользователя {user_id} в боте {sub_bot_id}: {is_anonymous}")
            
            # Логируем состояние анонимности для отладки
            logger.info(f"Обработка сообщения от пользователя {user_id}: is_anonymous={is_anonymous}, data={data}")
            
            # Получаем данные под-бота
            sub_bot_data = await self.db.get_sub_bot_by_token(bot.token)
            if not sub_bot_data or not sub_bot_data['admin_chat_id']:
                # Проверяем, является ли владельцем
                if user_id == sub_bot_data['owner_id']:
                    await message.answer(
                        "❌ Бот еще не настроен.\n\n"
                        "Используйте команду /setup для настройки админ-чата и канала."
                    )
                else:
                    await message.answer("❌ Бот не настроен. Обратитесь к администратору.")
                return
            
            admin_chat_id = sub_bot_data['admin_chat_id']
            
            ai_verdict = None
            
            # ========== AI MODERATION ==========
            is_auto_published = False
            ai_checked = False
            
            # Check if Gemini is enabled and configured
            if sub_bot_data.get('moderation_mode') == 'gemini':
                if not sub_bot_data.get('gemini_api_key'):
                    logger.warning(f"AI mode enabled but no API key for bot {sub_bot_id}")
                    ai_verdict = "⚠️ AI: Нет API ключа"
                elif message.media_group_id:
                    logger.info(f"Skipping AI check for media group in bot {sub_bot_id}")
                    ai_verdict = "⚠️ AI: Пропущено (альбом)"
                else:
                    logger.info(f"Запуск AI-модерации для сообщения {message.message_id}")
                    ai_checked = True
                    
                    # Prepare content
                    text_content = message.text or message.caption or ""
                    media_files = []
                    unsupported_media = False
                    file_data = None
                    if message.photo:
                        file_data = (message.photo[-1].file_id, "image/jpeg")
                    elif message.video:
                        file_data = (message.video.file_id, message.video.mime_type or "video/mp4")
                    elif message.document:
                        file_data = (message.document.file_id, message.document.mime_type)
                        unsupported_media = not bool(message.document.mime_type and message.document.mime_type.startswith(("image/", "video/", "audio/")))
                    elif message.audio:
                        file_data = (message.audio.file_id, message.audio.mime_type or "audio/mpeg")
                    elif message.voice:
                        file_data = (message.voice.file_id, message.voice.mime_type or "audio/ogg")
                    elif message.video_note:
                        file_data = (message.video_note.file_id, "video/mp4")
                    elif message.animation:
                        file_data = (message.animation.file_id, "video/mp4")
                    elif message.sticker:
                        if message.sticker.is_video or message.sticker.is_animated:
                            unsupported_media = True
                        else:
                            file_data = (message.sticker.file_id, "image/webp")

                    if file_data and not unsupported_media:
                        try:
                            downloaded = await self._download_ai_media(bot, *file_data)
                            if downloaded is None:
                                unsupported_media = True
                            else:
                                media_files.append(downloaded)
                        except Exception:
                            logger.exception("AI media download failed for message %s", message.message_id)
                            unsupported_media = True
                    elif not text_content:
                        unsupported_media = True
                    
                    # Check with Gemini
                    try:
                        is_auto_published = await self.check_with_gemini(
                            api_key=sub_bot_data['gemini_api_key'],
                            prompt=sub_bot_data.get('gemini_prompt', 'You are a moderator. PASS or REJECT.'),
                            text=text_content,
                            media_files=media_files,
                            unsupported_media=unsupported_media,
                        )
                        
                        if is_auto_published:
                            logger.info("✅ AI одобрил сообщение. Будет авто-опубликовано.")
                        else:
                            logger.info("❌ AI отклонил сообщение. Требуется ручная проверка.")
                            ai_verdict = "🤖 AI: ❌ Отклонено"
                    except Exception as e:
                        logger.error(f"AI check exception: {e}")
                        ai_verdict = "⚠️ AI: Ошибка проверки"
            
            # КРИТИЧНО: Обработка media groups (несколько фото одним сообщением)
            if message.media_group_id:
                # Это часть media group
                group_id = message.media_group_id
                group_key = f"{sub_bot_id}_{group_id}"
                
                # Сохраняем сообщение в группу
                if group_key not in self.media_groups:
                    self.media_groups[group_key] = []
                    # Создаем задачу для обработки группы через небольшой таймаут
                    asyncio.create_task(self._process_media_group_delayed(
                        bot, sub_bot_id, user_id, is_anonymous, admin_chat_id, group_key, state
                    ))
                
                self.media_groups[group_key].append(message)
                logger.info(f"Добавлено сообщение в медиа-группу {group_key}, всего: {len(self.media_groups[group_key])}")
                
                # Не обрабатываем сразу - ждем все сообщения
                return
            
            # Подготовка частей сообщения
            header_content = "Новое предложение" + (" (анонимно)" if is_anonymous else "")
            header_text = "📬 " + header_content
            
            # Offset: 📬 (2 units) + space (1 unit) = 3
            bold_offset = self._get_utf16_length("📬 ")
            bold_length = self._get_utf16_length(header_content)
            header_entities = [types.MessageEntity(type="bold", offset=bold_offset, length=bold_length)]
            
            if ai_verdict:
                header_text += f" | {ai_verdict}"
            
            header_text += "\n\n"
            header_part = (header_text, header_entities)
            
            # Информация об отправителе
            sender_text = ""
            sender_entities = []
            
            if is_anonymous:
                sender_text = "\n\n📬 Анонимно"
                # Offset: \n\n (2) + 📬 (2) + space (1) = 5
                anon_offset = self._get_utf16_length("\n\n📬 ")
                sender_entities = [types.MessageEntity(type="bold", offset=anon_offset, length=8)]
            else:
                full_name = message.from_user.full_name or message.from_user.first_name or 'Неизвестно'
                sender_text = f"\n\n👤 От: {full_name}\n"
                
                # ID
                id_prefix = "🆔 ID: "
                sender_text += id_prefix
                id_str = str(user_id)
                sender_text += id_str
                
                # Вычисляем смещение для ID (Code)
                text_before_id = f"\n\n👤 От: {full_name}\n🆔 ID: "
                offset = self._get_utf16_length(text_before_id)
                sender_entities.append(types.MessageEntity(type="code", offset=offset, length=len(id_str)))
                
                if message.from_user.username:
                    sender_text += f"\n📱 Username: @{message.from_user.username}"
            
            sender_part = (sender_text, sender_entities)
            
            # Отправка в админ-чат
            admin_message = None
            
            try:
                # ТЕКСТ
                if message.text:
                    user_part = (message.text, message.entities)
                    full_text, full_entities = self._combine_parts([header_part, user_part, sender_part])
                    
                    admin_message = await bot.send_message(
                        chat_id=admin_chat_id,
                        text=full_text,
                        entities=full_entities,
                        disable_web_page_preview=True
                    )
                
                # МЕДИА С CAPTION
                elif message.photo or message.video or message.document or message.audio or message.voice or message.animation:
                    user_caption = message.caption or ""
                    user_entities = message.caption_entities or []
                    user_part = (user_caption, user_entities)
                    
                    full_caption, full_entities = self._combine_parts([header_part, user_part, sender_part])
                    
                    if len(full_caption) <= 1024:
                        admin_message = await bot.copy_message(
                            chat_id=admin_chat_id,
                            from_chat_id=message.chat.id,
                            message_id=message.message_id,
                            caption=full_caption,
                            caption_entities=full_entities
                        )
                    else:
                        # Fallback: Header+Sender then Content
                        header_sender_text, header_sender_entities = self._combine_parts([header_part, sender_part])
                        await bot.send_message(chat_id=admin_chat_id, text=header_sender_text, entities=header_sender_entities)
                        admin_message = await bot.copy_message(
                            chat_id=admin_chat_id,
                            from_chat_id=message.chat.id,
                            message_id=message.message_id
                        )

                # ОСТАЛЬНОЕ
                else:
                    # Header+Sender
                    header_sender_text, header_sender_entities = self._combine_parts([header_part, sender_part])
                    await bot.send_message(chat_id=admin_chat_id, text=header_sender_text, entities=header_sender_entities)
                    
                    if message.video_note:
                        admin_message = await bot.send_video_note(chat_id=admin_chat_id, video_note=message.video_note.file_id)
                    elif message.sticker:
                        admin_message = await bot.send_sticker(chat_id=admin_chat_id, sticker=message.sticker.file_id)
                    elif message.location:
                        admin_message = await bot.send_location(chat_id=admin_chat_id, latitude=message.location.latitude, longitude=message.location.longitude)
                    elif message.venue:
                        admin_message = await bot.send_venue(chat_id=admin_chat_id, latitude=message.venue.location.latitude, longitude=message.venue.location.longitude, title=message.venue.title, address=message.venue.address)
                    elif message.contact:
                        admin_message = await bot.send_contact(chat_id=admin_chat_id, phone_number=message.contact.phone_number, first_name=message.contact.first_name)
                    elif message.poll:
                        admin_message = await bot.forward_message(chat_id=admin_chat_id, from_chat_id=message.chat.id, message_id=message.message_id)
                    elif message.dice:
                        admin_message = await bot.send_dice(chat_id=admin_chat_id, emoji=message.dice.emoji)
                    else:
                        admin_message = await bot.copy_message(chat_id=admin_chat_id, from_chat_id=message.chat.id, message_id=message.message_id)

                if not admin_message:
                    await message.answer("❌ Не удалось отправить сообщение. Попробуйте другой тип контента.")
                    return
            
            except Exception as e:
                logger.error(f"Ошибка отправки сообщения: {e}")
                return
            
            # Определяем content_type, original_text и entities ПЕРЕД try блоком
            original_entities = None
            has_spoiler = False
            if message.text:
                content_type = 'text'
                original_text = message.text
                original_entities = message.entities
            elif message.photo:
                content_type = 'photo'
                original_text = message.caption or ""
                original_entities = message.caption_entities
                has_spoiler = getattr(message, 'has_media_spoiler', False) or False
            elif message.video:
                content_type = 'video'
                original_text = message.caption or ""
                original_entities = message.caption_entities
                has_spoiler = getattr(message, 'has_media_spoiler', False) or False
            elif message.document:
                content_type = 'document'
                original_text = message.caption or ""
                original_entities = message.caption_entities
            elif message.audio:
                content_type = 'audio'
                original_text = message.caption or ""
                original_entities = message.caption_entities
            elif message.voice:
                content_type = 'voice'
                original_text = message.caption or ""
                original_entities = message.caption_entities
            elif message.animation:
                content_type = 'animation'
                original_text = message.caption or ""
                original_entities = message.caption_entities
            elif message.video_note:
                content_type = 'video_note'
                original_text = ""
            elif message.sticker:
                content_type = 'sticker'
                original_text = ""
            elif message.poll:
                content_type = 'poll'
                original_text = message.poll.question
            elif message.location:
                content_type = 'location'
                original_text = ""
            elif message.venue:
                content_type = 'venue'
                original_text = ""
            elif message.contact:
                content_type = 'contact'
                original_text = ""
            elif message.dice:
                content_type = 'dice'
                original_text = ""
            else:
                content_type = 'unknown'
                original_text = ""
            
            # Сериализуем entities
            original_entities_json = entities_to_json(original_entities) if original_entities else None
                
            try:
                # ШАГ 3: Сохраняем в базу ПЕРЕД созданием кнопок
                logger.info(f"Сохранение сообщения: content_type={content_type}, original_text={original_text[:50] if original_text else 'None'}, user_id={user_id}")
            
                status = 'published' if is_auto_published else 'pending'
            
                message_db_id = await self.db.add_message(
                    sub_bot_id=sub_bot_id,
                    user_id=user_id,
                    is_anonymous=is_anonymous,
                    message_id=message.message_id,
                    content_type=content_type,
                    admin_message_id=admin_message.message_id,
                    original_text=original_text,
                    original_entities=original_entities_json,
                    status=status,
                    has_spoiler=has_spoiler
                )
            
                # ШАГ 4: Добавляем кнопки модерации или авто-публикация
                if is_auto_published:
                    # Авто-публикация
                    await bot.send_message(
                        chat_id=admin_chat_id,
                        text="✅ <b>Одобрено AI и опубликовано</b>",
                        reply_to_message_id=admin_message.message_id,
                        parse_mode="HTML"
                    )
                
                    # ПУБЛИКАЦИЯ В КАНАЛ
                    channel_id = sub_bot_data['channel_id']
                    post_footer = sub_bot_data.get('post_footer')
                    post_header = sub_bot_data.get('post_header')
                    header_mode = sub_bot_data.get('header_mode', 'newline')
                
                    try:
                        user_text = original_text
                        rich_post_html = compose_post_html(
                            user_text,
                            original_entities or [],
                            header=post_header,
                            footer=post_footer,
                            header_mode=header_mode,
                        )
                        has_rich_only_tags = requires_rich_message(rich_post_html)
                        final_caption_html = self._build_html_caption(
                            header=post_header,
                            header_mode=header_mode,
                            user_caption=user_text,
                            footer=post_footer,
                            user_entities=original_entities or [],
                        )

                        if content_type == 'text':
                            await send_rich_html(bot, chat_id=channel_id, content=rich_post_html)
                        elif content_type in {'photo', 'video', 'document', 'audio', 'animation'}:
                            caption = "" if has_rich_only_tags else final_caption_html
                            copied = await bot.copy_message(
                                chat_id=channel_id,
                                from_chat_id=message.chat.id,
                                message_id=message.message_id,
                                caption=caption,
                                parse_mode="HTML" if not has_rich_only_tags and self._is_valid_html(final_caption_html) else None,
                            )
                            if has_rich_only_tags:
                                await send_rich_html(bot, chat_id=channel_id, content=rich_post_html)
                        else:
                            msg = await bot.copy_message(chat_id=channel_id, from_chat_id=message.chat.id, message_id=message.message_id)
                            if post_header or post_footer or user_text:
                                await send_rich_html(bot, chat_id=channel_id, content=rich_post_html)

                        # Уведомление пользователю
                        await message.answer("✅ Ваше предложение опубликовано в канале!")
                    
                    except Exception as pub_e:
                        logger.error(f"Ошибка авто-публикации: {pub_e}")
                        await bot.send_message(chat_id=admin_chat_id, text=f"❌ Ошибка при публикации в канал: {pub_e}", reply_to_message_id=admin_message.message_id)
                
                    return

                # КРИТИЧНО: Используем message_db_id (ID записи в БД), а не admin_message_id
                keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [
                        InlineKeyboardButton(text="✅ Опубликовать", callback_data=f"approve_{message_db_id}"),
                        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{message_db_id}")
                    ]
                ])
            
                try:
                    await bot.edit_message_reply_markup(
                        chat_id=admin_chat_id,
                        message_id=admin_message.message_id,
                        reply_markup=keyboard
                    )
                    logger.info(f"Кнопки модерации добавлены к сообщению {admin_message.message_id}, message_db_id={message_db_id}")
                except Exception as e:
                    logger.error(f"Ошибка добавления кнопок к сообщению: {e}")
                    # Если не удалось добавить кнопки к сообщению, отправляем отдельным сообщением
                    await bot.send_message(
                        chat_id=admin_chat_id,
                        text="Выберите действие:",
                        reply_to_message_id=admin_message.message_id,
                        reply_markup=keyboard
                    )
            
                await message.answer(
                    "✅ Ваше предложение отправлено на модерацию!\n\n"
                    "Оно будет рассмотрено администратором и при одобрении опубликовано в канале."
                )
            
            except Exception as e:
                logger.error(f"Ошибка отправки в админ-чат: {e}", exc_info=True)
                # Пытаемся отправить сообщение об ошибке пользователю
                try:
                    await message.answer("❌ Ошибка отправки. Попробуйте позже.")
                except Exception as e2:
                    logger.error(f"Не удалось отправить сообщение об ошибке пользователю: {e2}")
                    pass  # Если не удалось отправить даже сообщение об ошибке
    
        # ========== CALLBACK ДЛЯ МОДЕРАЦИИ ==========
        @dp.callback_query(F.data.startswith("approve_"))
        async def approve_message(callback: types.CallbackQuery):
            """Одобрить сообщение"""
            # КРИТИЧНО: callback_data содержит message_db_id (ID записи в БД), а не admin_message_id
            try:
                message_db_id = int(callback.data.split("_")[1])
            except (ValueError, IndexError):
                await callback.answer("❌ Ошибка: неверный формат данных")
                return
            
            sub_bot_data, msg_data, denial = await self._authorize_moderation_action(
                callback, bot, sub_bot_id, message_db_id
            )
            if denial:
                await callback.answer(f"❌ {denial}", show_alert=True)
                return
            if msg_data.get("status") != "pending":
                await callback.answer("⚠️ Заявка уже обработана или обрабатывается", show_alert=True)
                return
            if not sub_bot_data or not sub_bot_data.get("channel_id"):
                await callback.answer("❌ Канал не настроен")
                return

            if not await self.db.claim_message_for_publication(message_db_id, sub_bot_id):
                await callback.answer("⚠️ Заявку уже обрабатывает другой модератор", show_alert=True)
                return

            original_reply_markup = callback.message.reply_markup
            channel_message = None

            async def release_claim_if_unpublished() -> None:
                if channel_message is None:
                    await self.db.release_message_claim(message_db_id, sub_bot_id)
                else:
                    logger.error(
                        "Publication already reached Telegram; keeping message %s claimed to prevent duplicates",
                        message_db_id,
                    )

            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                logger.debug("Could not remove review buttons before publishing")
            
            channel_id = sub_bot_data['channel_id']
            post_footer = sub_bot_data.get('post_footer')  # Оформление снизу
            post_header = sub_bot_data.get('post_header')  # Оформление сверху
            header_mode = sub_bot_data.get('header_mode', 'newline')  # inline или newline
            user_id = msg_data['user_id']
            user_message_id = msg_data['message_id']
            original_text_from_db = msg_data.get('original_text')  # Оригинальный текст/caption из БД
            original_entities_json = msg_data.get('original_entities')  # Сохраненные entities
            original_entities = json_to_entities(original_entities_json) if original_entities_json else None
            content_type = msg_data.get('content_type')
            has_spoiler = bool(msg_data.get('has_spoiler', False))  # Есть ли спойлер у медиа
            
            # КРИТИЧНО: Проверяем, является ли это медиа-группой в самом начале
            is_media_group = bool(msg_data.get('is_media_group')) and bool(msg_data.get('media_group_message_ids'))
            
            logger.info(f"📋 approve_message: message_db_id={message_db_id}, content_type={content_type}, is_media_group={is_media_group}, is_media_group_raw={msg_data.get('is_media_group')}, media_group_message_ids={msg_data.get('media_group_message_ids')[:50] if msg_data.get('media_group_message_ids') else None}")
            
            # ==================== ПУБЛИКАЦИЯ МЕДИА-ГРУППЫ ====================
            if is_media_group:
                logger.info(f"🔍 ПУБЛИКАЦИЯ МЕДИА-ГРУППЫ: message_db_id={message_db_id}")
                try:
                        # Получаем все file_id из группы (формат: "photo:file_id" или "video:file_id")
                        group_file_ids_str = msg_data.get('media_group_message_ids', '')
                        if not group_file_ids_str:
                            # Если file_id не сохранены, пытаемся использовать message_id
                            try:
                                if original_reply_markup:
                                    await callback.message.edit_reply_markup(reply_markup=original_reply_markup)
                            except:
                                pass
                            await callback.answer("❌ Ошибка: не удалось найти медиа-группу")
                            await release_claim_if_unpublished()
                            return
                        
                        # Парсим file_id из строки
                        media_items = []
                        user_caption = original_text_from_db if original_text_from_db else ""
                        
                        # Очистка caption
                        if user_caption:
                            lines = user_caption.split('\n')
                            cleaned_lines = []
                            for line in lines:
                                line_clean = line.strip()
                                if any(marker in line_clean for marker in ['🆔 ID:', '📱 Username:', '👤 От:', '━━━━━━━━━━━━━━━', '📬', 'Анонимно', '✅ ОПУБЛИКОВАНО']):
                                    continue
                                cleaned_lines.append(line)
                            user_caption = '\n'.join(cleaned_lines).strip()
                        
                        # КРИТИЧНО: Удаляем дубликаты header/footer если они уже есть в тексте
                        user_caption = self._remove_duplicate_header_footer(
                            user_caption, 
                            header=post_header, 
                            footer=post_footer, 
                            header_mode=header_mode
                        )
                        
                        # Формируем caption с header и footer
                        caption_parts = []
                        
                        # Header сверху (добавляем только если его еще нет)
                        if post_header:
                            # Проверяем, есть ли header уже в начале
                            if not self._text_contains_at_start(user_caption, post_header, allow_separators=(header_mode == 'newline')):
                                if header_mode == 'inline' and user_caption:
                                    # Inline: header + пробел + текст
                                    caption_parts.append(post_header + " " + user_caption)
                                else:
                                    # Newline: header на отдельной строке
                                    caption_parts.append(post_header)
                                    if user_caption:
                                        caption_parts.append(user_caption)
                            else:
                                # Header уже есть - добавляем только текст
                                if user_caption:
                                    caption_parts.append(user_caption)
                        elif user_caption:
                            caption_parts.append(user_caption)
                        
                        # Footer снизу (добавляем только если его еще нет)
                        if post_footer:
                            # Проверяем, есть ли footer уже в конце
                            current_text = '\n\n'.join(caption_parts) if caption_parts else ""
                            if not self._text_contains_at_end(current_text, post_footer, allow_separators=True):
                                caption_parts.append(post_footer)

                        rich_post_html = compose_post_html(
                            user_caption,
                            original_entities or [],
                            header=post_header,
                            footer=post_footer,
                            header_mode=header_mode,
                        )
                        rich_caption_separate = requires_rich_message(rich_post_html)
                        
                        if caption_parts:
                            caption = "\n\n".join(caption_parts)
                        else:
                            caption = None
                        
                        # КРИТИЧНО: Проверяем валидность HTML в caption перед использованием
                        has_valid_html = caption and self._is_valid_html(caption)
                        
                        # Парсим file_id из строки формата "type:file_id"
                        for file_id_str in group_file_ids_str.split(','):
                            file_id_str = file_id_str.strip()
                            if ':' in file_id_str:
                                try:
                                    media_type, file_id = file_id_str.split(':', 1)
                                    if media_type == 'photo':
                                        media_items.append(InputMediaPhoto(
                                            media=file_id,
                                            caption=("" if rich_caption_separate else caption) if len(media_items) == 0 else None,
                                            parse_mode="HTML" if len(media_items) == 0 and has_valid_html and not rich_caption_separate else None,
                                            has_spoiler=has_spoiler if len(media_items) == 0 else False
                                        ))
                                    elif media_type == 'video':
                                        media_items.append(InputMediaVideo(
                                            media=file_id,
                                            caption=("" if rich_caption_separate else caption) if len(media_items) == 0 else None,
                                            parse_mode="HTML" if len(media_items) == 0 and has_valid_html and not rich_caption_separate else None,
                                            has_spoiler=has_spoiler if len(media_items) == 0 else False
                                        ))
                                    elif media_type == 'document':
                                        media_items.append(InputMediaDocument(
                                            media=file_id,
                                            caption=("" if rich_caption_separate else caption) if len(media_items) == 0 else None,
                                            parse_mode="HTML" if len(media_items) == 0 and has_valid_html and not rich_caption_separate else None
                                        ))
                                    elif media_type == 'audio':
                                        media_items.append(InputMediaAudio(
                                            media=file_id,
                                            caption=("" if rich_caption_separate else caption) if len(media_items) == 0 else None,
                                            parse_mode="HTML" if len(media_items) == 0 and has_valid_html and not rich_caption_separate else None
                                        ))
                                except Exception as e:
                                    logger.warning(f"Ошибка парсинга file_id '{file_id_str}': {e}")
                                    continue
                        
                        if not media_items:
                            logger.error("Не удалось распарсить file_id из медиа-группы")
                            try:
                                if original_reply_markup:
                                    await callback.message.edit_reply_markup(reply_markup=original_reply_markup)
                            except:
                                pass
                            await callback.answer("❌ Ошибка: не удалось обработать медиа-группу")
                            await release_claim_if_unpublished()
                            return
                        
                        logger.info(f"Публикация медиа-группы: {len(media_items)} медиа")
                        
                        # КРИТИЧНО: Отправляем медиа-группу в канал
                        # Все медиа будут отправлены как один альбом
                        sent_messages = await bot.send_media_group(
                            chat_id=channel_id,
                            media=media_items
                        )
                        
                        # Первое сообщение - основное
                        channel_message = sent_messages[0]
                        
                        logger.info(f"✅ Медиа-группа опубликована в канал: {len(sent_messages)} сообщений из {len(media_items)}")
                        
                        # КРИТИЧНО: Сразу обновляем статус в БД
                        await self.db.update_message_status(message_db_id, 'published', channel_message.message_id)
                        if rich_caption_separate and rich_post_html.strip():
                            await send_rich_html(bot, chat_id=channel_id, content=rich_post_html)
                        
                        # Обновляем сообщение в админ-чате (добавляем статус и информацию о том, кто одобрил)
                        approver = callback.from_user
                        approver_name = html.escape(approver.full_name or approver.first_name or "Неизвестно")
                        approver_username = html.escape(f"@{approver.username}" if approver.username else "нет username")
                        approval_text = f"\n\n✅ <b>ОПУБЛИКОВАНО</b>\n👤 Одобрено: {approver_name} ({approver_username})"
                        current_text = callback.message.text or callback.message.caption or ""
                        if "✅ <b>ОПУБЛИКОВАНО</b>" not in current_text:
                            new_text = current_text + approval_text
                            try:
                                if callback.message.text:
                                    await callback.message.edit_text(new_text, parse_mode="HTML")
                                elif callback.message.caption:
                                    await callback.message.edit_caption(caption=new_text, parse_mode="HTML")
                            except:
                                pass
                        
                        try:
                            await callback.message.edit_reply_markup(reply_markup=None)
                        except:
                            pass
                        
                        await callback.answer("✅ Опубликовано")
                        logger.info(f"✅ Медиа-группа полностью обработана, выходим из функции. message_db_id={message_db_id}")
                        return
                except Exception as e:
                    logger.error(f"Ошибка публикации медиа-группы: {e}", exc_info=True)
                    
                    if channel_message is None:
                        try:
                            if original_reply_markup:
                                await callback.message.edit_reply_markup(reply_markup=original_reply_markup)
                        except Exception as restore_error:
                            logger.error(f"Не удалось восстановить кнопки: {restore_error}")
                    
                    # Отправляем детальное сообщение об ошибке в админ-чат
                    try:
                        error_details = html.escape(str(e)[:500])  # Ограничиваем длину и экранируем HTML
                        await bot.send_message(
                            chat_id=callback.message.chat.id,
                            text=f"❌ <b>Ошибка публикации медиа-группы</b>\n\n{error_details}",
                            reply_to_message_id=callback.message.message_id,
                            parse_mode="HTML"
                        )
                    except Exception as send_error:
                        logger.error(f"Не удалось отправить сообщение об ошибке: {send_error}")
                    
                    await callback.answer(f"❌ Ошибка: {str(e)[:100]}")
                    await release_claim_if_unpublished()
                    return
                
                # Этот код НИКОГДА не должен выполняться для медиа-группы
                logger.error(f"КРИТИЧЕСКАЯ ОШИБКА: Код после блока медиа-группы выполнился! message_db_id={message_db_id}")
                await release_claim_if_unpublished()
                return
            
            # ==================== ПУБЛИКАЦИЯ ОБЫЧНЫХ СООБЩЕНИЙ ====================
            try:
                # Копируем оригинальное сообщение пользователя в канал
                # Добавляем footer если он есть
                if content_type == 'text':
                    # Текстовое сообщение - берем ТОЛЬКО оригинальный текст из БД
                    user_text = original_text_from_db if original_text_from_db else ""
                    
                    # КРИТИЧНО: Если original_text пустой - это старое сообщение, пытаемся получить через copy_message
                    if not user_text:
                        logger.warning(f"original_text пустой для message_id={msg_data['id']}, это старое сообщение, используем copy_message")
                        try:
                            # Для старых сообщений используем copy_message
                            channel_message = await bot.copy_message(
                                chat_id=channel_id,
                                from_chat_id=user_id,
                                message_id=user_message_id
                            )
                            if post_footer:
                                await bot.send_message(
                                    chat_id=channel_id,
                                    text=post_footer,
                                    reply_to_message_id=channel_message.message_id
                                )
                        except Exception as e:
                            logger.error(f"Ошибка публикации старого сообщения: {e}")
                            try:
                                if original_reply_markup:
                                    await callback.message.edit_reply_markup(reply_markup=original_reply_markup)
                            except:
                                pass
                            await callback.answer("❌ Ошибка: не удалось опубликовать старое сообщение")
                            await release_claim_if_unpublished()
                            return
                        # Пропускаем дальнейшую обработку для старых сообщений
                        pass
                    else:
                        # КРИТИЧНО: Дополнительная очистка от любой информации об авторе (на случай если что-то попало в БД)
                        if user_text:
                            lines = user_text.split('\n')
                            cleaned_lines = []
                            for line in lines:
                                # Пропускаем строки с информацией об авторе
                                line_clean = line.strip()
                                if any(marker in line_clean for marker in ['🆔 ID:', '📱 Username:', '👤 От:', '━━━━━━━━━━━━━━━', '📬', 'Анонимно', '✅ ОПУБЛИКОВАНО']):
                                    continue
                                cleaned_lines.append(line)
                            user_text = '\n'.join(cleaned_lines).strip()
                        
                        # Убеждаемся что есть текст для публикации
                        if not user_text:
                            logger.error(f"Ошибка: не удалось извлечь текст для message_id={msg_data['id']}")
                            try:
                                if original_reply_markup:
                                    await callback.message.edit_reply_markup(reply_markup=original_reply_markup)
                            except:
                                pass
                            await callback.answer("❌ Ошибка: текст сообщения не найден")
                            await release_claim_if_unpublished()
                            return
                        
                        # КРИТИЧНО: Удаляем дубликаты header/footer если они уже есть в тексте
                        user_text = self._remove_duplicate_header_footer(
                            user_text, 
                            header=post_header, 
                            footer=post_footer, 
                            header_mode=header_mode
                        )
                        
                        # Формируем текст с header и footer
                        text_parts = []
                        
                        # Header сверху (добавляем только если его еще нет)
                        if post_header:
                            if not self._text_contains_at_start(user_text, post_header, allow_separators=(header_mode == 'newline')):
                                if header_mode == 'inline':
                                    # Inline: header + пробел + текст
                                    text_parts.append(post_header + " " + user_text)
                                else:
                                    # Newline: header на отдельной строке
                                    text_parts.append(post_header)
                                    text_parts.append(user_text)
                            else:
                                # Header уже есть - добавляем только текст
                                text_parts.append(user_text)
                        else:
                            text_parts.append(user_text)
                        
                        # Footer снизу (добавляем только если его еще нет)
                        if post_footer:
                            current_text = '\n\n'.join(text_parts) if text_parts else ""
                            if not self._text_contains_at_end(current_text, post_footer, allow_separators=True):
                                text_parts.append(post_footer)
                        
                        final_text = "\n\n".join(text_parts)
                        
                        logger.info("Publishing text submission %s (%s UTF-8 bytes)", message_db_id, len(user_text.encode("utf-8")))
                        if sub_bot_data.get("rich_messages_enabled", 1):
                            rich_html = compose_post_html(
                                user_text,
                                original_entities or [],
                                header=post_header,
                                footer=post_footer,
                                header_mode=header_mode,
                            )
                            channel_message = await send_rich_html(
                                bot,
                                chat_id=channel_id,
                                content=rich_html,
                            )
                        elif (post_header and self._is_valid_html(post_header)) or (post_footer and self._is_valid_html(post_footer)) or original_entities:
                            channel_message = await bot.send_message(
                                chat_id=channel_id,
                                text=self._build_html_caption(
                                    header=post_header,
                                    header_mode=header_mode,
                                    user_caption=user_text,
                                    footer=post_footer,
                                    user_entities=original_entities or [],
                                ),
                                parse_mode="HTML",
                                disable_web_page_preview=True,
                            )
                        else:
                            channel_message = await bot.send_message(chat_id=channel_id, text=final_text)
                elif content_type == 'poll':
                    # Опрос - копируем напрямую из чата пользователя
                    try:
                        channel_message = await bot.copy_message(
                            chat_id=channel_id,
                            from_chat_id=user_id,
                            message_id=user_message_id
                        )
                    except Exception as e:
                        logger.error(f"Ошибка публикации опроса: {e}")
                        try:
                            if original_reply_markup:
                                await callback.message.edit_reply_markup(reply_markup=original_reply_markup)
                        except:
                            pass
                        await callback.answer("❌ Ошибка публикации опроса")
                        await release_claim_if_unpublished()
                        return
                    
                    # Если есть footer - отправляем отдельным сообщением
                    if post_footer:
                        # Проверяем, есть ли валидное HTML форматирование в footer
                        has_html_formatting = self._is_valid_html(post_footer)
                        await bot.send_message(
                            chat_id=channel_id,
                            text=post_footer,
                            reply_to_message_id=channel_message.message_id,
                            parse_mode="HTML" if has_html_formatting else None,
                            disable_web_page_preview=True if has_html_formatting else None
                        )
                else:
                    # КРИТИЧНО: Проверяем, что это НЕ медиа-группа ПЕРЕД обработкой
                    # Если это медиа-группа, мы уже обработали её выше и вышли через return
                    if is_media_group:
                        logger.error(f"КРИТИЧЕСКАЯ ОШИБКА: Медиа-группа попала в блок else! message_db_id={message_db_id}")
                        await callback.answer("❌ Ошибка: медиа-группа уже обработана")
                        await release_claim_if_unpublished()
                        return
                    
                    # Обычное медиа-сообщение (не медиа-группа) - используем специфичные методы для сохранения caption
                    # КРИТИЧНО: Используем ТОЛЬКО original_text из БД, который был сохранен при получении от пользователя
                    user_caption = original_text_from_db if original_text_from_db else ""  # Оригинальный caption из БД
                    
                    # КРИТИЧНО: Дополнительная очистка от любой информации об авторе (на случай если что-то попало)
                    if user_caption:
                        lines = user_caption.split('\n')
                        cleaned_lines = []
                        for line in lines:
                            # Пропускаем строки с информацией об авторе
                            line_clean = line.strip()
                            if any(marker in line_clean for marker in ['🆔 ID:', '📱 Username:', '👤 От:', '━━━━━━━━━━━━━━━', '📬', 'Анонимно', '✅ ОПУБЛИКОВАНО']):
                                continue
                            cleaned_lines.append(line)
                        user_caption = '\n'.join(cleaned_lines).strip()
                    
                    # Определяем, поддерживает ли этот тип контента caption
                    # Стикеры, video_note, dice, poll не поддерживают caption
                    supports_caption = content_type not in ['sticker', 'video_note', 'dice', 'poll']
                    
                    # Инициализируем переменную для отдельного сообщения с футером
                    footer_message = None
                    
                    if supports_caption:
                        # КРИТИЧНО: Удаляем дубликаты header/footer если они уже есть в тексте
                        user_caption = self._remove_duplicate_header_footer(
                            user_caption, 
                            header=post_header, 
                            footer=post_footer, 
                            header_mode=header_mode
                        )
                        
                        # Формируем финальный caption с header и footer
                        caption_parts = []
                        
                        # Header сверху (добавляем только если его еще нет)
                        if post_header:
                            if not self._text_contains_at_start(user_caption, post_header, allow_separators=(header_mode == 'newline')):
                                if header_mode == 'inline' and user_caption:
                                    caption_parts.append(post_header + " " + user_caption)
                                else:
                                    caption_parts.append(post_header)
                                    if user_caption:
                                        caption_parts.append(user_caption)
                            else:
                                # Header уже есть - добавляем только текст
                                if user_caption:
                                    caption_parts.append(user_caption)
                        elif user_caption:
                            caption_parts.append(user_caption)
                        
                        # Footer снизу (добавляем только если его еще нет)
                        if post_footer:
                            current_text = '\n\n'.join(caption_parts) if caption_parts else ""
                            if not self._text_contains_at_end(current_text, post_footer, allow_separators=True):
                                caption_parts.append(post_footer)
                        
                        if caption_parts:
                            caption = "\n\n".join(caption_parts)
                        else:
                            caption = None
                    else:
                        # Для типов без caption (стикеры, video_note, dice, poll)
                        caption = None
                        # Если есть footer или header, отправляем отдельным сообщением после медиа
                        footer_message = None
                        combined_msg = []
                        if post_header:
                            combined_msg.append(post_header)
                        if post_footer:
                            combined_msg.append(post_footer)
                        if combined_msg:
                            footer_message = "\n\n".join(combined_msg)
                    
                    logger.info(f"Публикуем медиа в канал: content_type={content_type}, has_spoiler={has_spoiler}, user_caption={user_caption[:50] if user_caption else 'None'}, footer={post_footer[:30] if post_footer else 'None'}, final_caption={caption[:100] if caption else 'None'}")
                    
                    # Всегда формируем HTML caption (конвертируем entities), чтобы сохранить форматирование и избежать дублей
                    final_caption_html = self._build_html_caption(
                        header=post_header,
                        header_mode=header_mode,
                        user_caption=user_caption,
                        footer=post_footer,
                        user_entities=original_entities if original_entities else []
                    )
                    rich_caption_html = compose_post_html(
                        user_caption,
                        original_entities or [],
                        header=post_header,
                        footer=post_footer,
                        header_mode=header_mode,
                    ) if supports_caption else compose_post_html(
                        "", header=post_header, footer=post_footer, header_mode=header_mode
                    )
                    rich_caption_separate = requires_rich_message(rich_caption_html)
                    
                    try:
                        # КРИТИЧНО: Если есть спойлер, используем send_photo/send_video вместо copy_message
                        # copy_message не поддерживает has_spoiler
                        if has_spoiler and (content_type == 'photo' or content_type == 'video'):
                            # Получаем file_id через временное копирование
                            temp_copy = await bot.copy_message(
                                chat_id=channel_id,
                                from_chat_id=user_id,
                                message_id=user_message_id
                            )
                            channel_message = temp_copy
                            
                            if content_type == 'photo' and temp_copy.photo:
                                file_id = temp_copy.photo[-1].file_id
                                try:
                                    await bot.delete_message(chat_id=channel_id, message_id=temp_copy.message_id)
                                except:
                                    pass
                                
                                channel_message = await bot.send_photo(
                                    chat_id=channel_id,
                                    photo=file_id,
                                    caption=("" if rich_caption_separate else final_caption_html) if supports_caption else None,
                                    parse_mode="HTML" if supports_caption and not rich_caption_separate else None,
                                    has_spoiler=True,
                                    disable_web_page_preview=True if supports_caption else None
                                )
                            elif content_type == 'video' and temp_copy.video:
                                file_id = temp_copy.video.file_id
                                try:
                                    await bot.delete_message(chat_id=channel_id, message_id=temp_copy.message_id)
                                except:
                                    pass
                                
                                channel_message = await bot.send_video(
                                    chat_id=channel_id,
                                    video=file_id,
                                    caption=("" if rich_caption_separate else final_caption_html) if supports_caption else None,
                                    parse_mode="HTML" if supports_caption and not rich_caption_separate else None,
                                    has_spoiler=True,
                                    disable_web_page_preview=True if supports_caption else None
                                )
                            else:
                                # Fallback на обычное копирование, если не удалось получить file_id
                                channel_message = temp_copy
                                logger.warning(f"Не удалось получить file_id для спойлера, используется обычное копирование")
                        else:
                            # Обычное копирование без спойлера
                            channel_message = await bot.copy_message(
                                chat_id=channel_id,
                                from_chat_id=user_id,
                                message_id=user_message_id,
                                caption=("" if rich_caption_separate else final_caption_html) if supports_caption else None,
                                parse_mode="HTML" if supports_caption and not rich_caption_separate else None
                            )
                        logger.info(f"Медиа опубликовано в канал: message_id={channel_message.message_id}, has_spoiler={has_spoiler}")
                    except Exception as e:
                        logger.error(f"Ошибка публикации медиа: {e}", exc_info=True)
                        # Fallback: копируем без caption и отправляем header/footer отдельным сообщением
                        try:
                            channel_message = await bot.copy_message(
                                chat_id=channel_id,
                                from_chat_id=user_id,
                                message_id=user_message_id
                            )
                            if supports_caption and final_caption_html and not rich_caption_separate:
                                await bot.send_message(
                                    chat_id=channel_id,
                                    text=final_caption_html,
                                    reply_to_message_id=channel_message.message_id,
                                    parse_mode="HTML",
                                    disable_web_page_preview=True
                                )
                        except Exception as e2:
                            logger.error(f"Ошибка fallback публикации медиа: {e2}")
                            try:
                                if original_reply_markup:
                                    await callback.message.edit_reply_markup(reply_markup=original_reply_markup)
                            except:
                                pass
                            await callback.answer("❌ Ошибка публикации медиа")
                            await release_claim_if_unpublished()
                            return

                    if rich_caption_separate and rich_caption_html.strip():
                        try:
                            await send_rich_html(bot, chat_id=channel_id, content=rich_caption_html)
                        except Exception as rich_exc:
                            logger.error("Rich HTML media caption failed after media publish (%s)", type(rich_exc).__name__)
                    
                    # Если есть footer для типов без caption - отправляем отдельным сообщением
                    if footer_message and not supports_caption and not rich_caption_separate:
                        has_html_in_footer_msg = self._is_valid_html(footer_message)
                        await bot.send_message(
                            chat_id=channel_id,
                            text=footer_message,
                            reply_to_message_id=channel_message.message_id,
                            parse_mode="HTML" if has_html_in_footer_msg else None,
                            disable_web_page_preview=True if has_html_in_footer_msg else None
                        )
                
                # КРИТИЧНО: Проверяем, что это НЕ медиа-группа перед дальнейшей обработкой
                # Медиа-группы уже обработаны выше и вышли через return
                if msg_data.get('is_media_group') and msg_data.get('media_group_message_ids'):
                    logger.error(f"КРИТИЧЕСКАЯ ОШИБКА: Код для медиа-группы попал в блок для обычных сообщений! message_db_id={message_db_id}")
                    await callback.answer("❌ Ошибка: медиа-группа уже обработана")
                    return
                
                # КРИТИЧНО: Проверяем, что channel_message был успешно создан
                if channel_message is None:
                    logger.error(f"channel_message не был создан для message_db_id={message_db_id}")
                    raise Exception("Не удалось создать сообщение в канале")
                
                # Обновляем статус (только для обычных сообщений, не медиа-групп)
                # Медиа-группы уже обновлены выше и вышли через return
                try:
                    await self.db.update_message_status(
                        message_db_id=msg_data['id'],
                        status='published',
                        channel_message_id=channel_message.message_id
                    )
                except Exception as db_error:
                    logger.error(f"Ошибка обновления статуса в БД: {db_error}", exc_info=True)
                    # Не прерываем выполнение, так как сообщение уже опубликовано
                
                # Обновляем сообщение в админ-чате (добавляем статус и информацию о том, кто одобрил)
                # Получаем информацию о том, кто одобрил
                approver = callback.from_user
                approver_name = html.escape(approver.full_name or approver.first_name or "Неизвестно")
                approver_username = html.escape(f"@{approver.username}" if approver.username else "нет username")
                
                # Формируем текст с информацией об одобрении
                approval_text = f"\n\n✅ <b>ОПУБЛИКОВАНО</b>\n👤 Одобрено: {approver_name} ({approver_username})"
                
                # Получаем текущий текст/caption
                current_text = callback.message.text or callback.message.caption or ""
                
                # Проверяем, не добавлен ли уже статус
                if "✅ <b>ОПУБЛИКОВАНО</b>" not in current_text:
                    new_text = current_text + approval_text
                else:
                    new_text = current_text
                
                try:
                    if callback.message.text:
                        await callback.message.edit_text(new_text, parse_mode="HTML")
                    elif callback.message.caption:
                        await callback.message.edit_caption(caption=new_text, parse_mode="HTML")
                    else:
                        # Если нет текста и caption, пытаемся добавить как отдельное сообщение
                        await bot.send_message(
                            chat_id=callback.message.chat.id,
                            text=approval_text,
                            reply_to_message_id=callback.message.message_id,
                            parse_mode="HTML"
                        )
                except Exception as e:
                    logger.error(f"Ошибка обновления сообщения в админ-чате: {e}")
                    # Пытаемся отправить как отдельное сообщение
                    try:
                        await bot.send_message(
                            chat_id=callback.message.chat.id,
                            text=approval_text,
                            reply_to_message_id=callback.message.message_id,
                            parse_mode="HTML"
                        )
                    except:
                        pass
                
                # Кнопки уже удалены в начале функции
                await callback.answer("✅ Опубликовано!")
                
            except Exception as e:
                logger.error(f"Ошибка публикации: {e}", exc_info=True)
                await release_claim_if_unpublished()
                
                # Восстанавливаем кнопки при ошибке
                if channel_message is None:
                    try:
                        if original_reply_markup:
                            await callback.message.edit_reply_markup(reply_markup=original_reply_markup)
                    except Exception as restore_error:
                        logger.error(f"Не удалось восстановить кнопки: {restore_error}")
                
                # Отправляем детальное сообщение об ошибке в админ-чат
                try:
                    error_details = html.escape(str(e)[:500])  # Ограничиваем длину и экранируем HTML
                    await bot.send_message(
                        chat_id=callback.message.chat.id,
                        text=f"❌ <b>Ошибка публикации</b>\n\n{error_details}",
                        reply_to_message_id=callback.message.message_id,
                        parse_mode="HTML"
                    )
                except Exception as send_error:
                    logger.error(f"Не удалось отправить сообщение об ошибке: {send_error}")
                
                await callback.answer(f"❌ Ошибка: {str(e)[:100]}")
        
        @dp.callback_query(F.data.startswith("reject_"))
        async def reject_message(callback: types.CallbackQuery):
            """Отклонить сообщение"""
            # КРИТИЧНО: callback_data содержит message_db_id (ID записи в БД)
            try:
                message_db_id = int(callback.data.split("_")[1])
            except (ValueError, IndexError):
                await callback.answer("❌ Ошибка: неверный формат данных")
                return
            
            sub_bot_data, msg_data, denial = await self._authorize_moderation_action(
                callback, bot, sub_bot_id, message_db_id
            )
            if denial:
                await callback.answer(f"❌ {denial}", show_alert=True)
                return
            if msg_data.get("status") != "pending":
                await callback.answer("⚠️ Заявка уже обработана или обрабатывается", show_alert=True)
                return
            if not await self.db.reject_pending_message(message_db_id, sub_bot_id):
                await callback.answer("⚠️ Заявка уже обработана или обрабатывается", show_alert=True)
                return
            
            # Обновляем сообщение в админ-чате
            admin_name = html.escape(callback.from_user.full_name)
            admin_username = html.escape(f" (@{callback.from_user.username})") if callback.from_user.username else ""
            
            new_text = callback.message.text or callback.message.caption or ""
            new_text += f"\n\n❌ <b>ОТКЛОНЕНО</b>\n👤 {admin_name}{admin_username}"
            
            try:
                if callback.message.text:
                    await callback.message.edit_text(new_text, parse_mode="HTML")
                else:
                    await callback.message.edit_caption(caption=new_text, parse_mode="HTML")
            except:
                pass
            
            # Удаляем кнопки
            await callback.message.edit_reply_markup(reply_markup=None)
            
            await callback.answer("❌ Отклонено")
        
        # ========== РАССЫЛКА ДЛЯ ВЛАДЕЛЬЦА ==========
        @dp.message(UserBroadcast.waiting_for_message)
        async def process_broadcast(message: types.Message, state: FSMContext):
            """Обработка рассылки владельца"""
            user_id = message.from_user.id
            sub_bot_data = await self.db.get_sub_bot_by_token(bot.token)
            
            if not sub_bot_data or user_id != sub_bot_data['owner_id']:
                await state.clear()
                return
            
            # Проверяем, что это private чат
            if message.chat.type != "private":
                await state.clear()
                return
            
            # Получаем список пользователей
            users = await self.db.get_all_users_of_sub_bot(sub_bot_id)
            active_users = [u for u in users if not u['is_blocked']]
            
            # Подтверждение
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Отправить", callback_data="owner_broadcast_confirm")],
                [InlineKeyboardButton(text="❌ Отмена", callback_data="owner_broadcast_cancel")]
            ])
            
            await message.answer(
                f"📤 Отправить рассылку {len(active_users)} пользователям?",
                reply_markup=keyboard
            )
            
            # Сохраняем ID сообщения и chat_id для рассылки
            await state.update_data(
                broadcast_message_id=message.message_id,
                broadcast_chat_id=message.chat.id
            )
        
        @dp.callback_query(F.data == "owner_broadcast_confirm")
        async def execute_owner_broadcast(callback: types.CallbackQuery, state: FSMContext):
            """Выполнить рассылку владельца"""
            user_id = callback.from_user.id
            sub_bot_data = await self.db.get_sub_bot_by_token(bot.token)
            
            if not sub_bot_data or user_id != sub_bot_data['owner_id']:
                await callback.answer("❌ Нет доступа")
                await state.clear()
                return
            
            data = await state.get_data()
            message_id = data.get('broadcast_message_id')
            chat_id = data.get('broadcast_chat_id')
            
            if not message_id or not chat_id:
                await callback.answer("❌ Ошибка: данные не найдены")
                await state.clear()
                return
            
            await callback.message.edit_text("📤 Отправка рассылки...")
            
            # Получаем пользователей
            users = await self.db.get_sub_bot_users(sub_bot_id)
            active_users = [u for u in users if not u['is_blocked']]
            
            success = 0
            failed = 0
            
            for user in active_users:
                try:
                    await bot.copy_message(
                        chat_id=user['user_id'],
                        from_chat_id=chat_id,
                        message_id=message_id
                    )
                    success += 1
                except Exception as e:
                    logger.error(f"Ошибка рассылки {user['user_id']}: {e}")
                    failed += 1
                await asyncio.sleep(0.05)  # Задержка между сообщениями
            
            await callback.message.edit_text(
                f"✅ <b>Рассылка завершена!</b>\n\n"
                f"✅ Успешно: {success}\n"
                f"❌ Ошибок: {failed}",
                parse_mode="HTML"
            )
            await state.clear()
        
        @dp.callback_query(F.data == "owner_broadcast_cancel")
        async def cancel_owner_broadcast(callback: types.CallbackQuery, state: FSMContext):
            """Отмена рассылки владельца"""
            await callback.message.edit_text("❌ Рассылка отменена")
            await state.clear()
        
        @dp.callback_query(F.data == "footer_remove")
        async def remove_footer(callback: types.CallbackQuery, state: FSMContext):
            """Удалить оформление"""
            user_id = callback.from_user.id
            sub_bot_data = await self.db.get_sub_bot_by_token(bot.token)
            
            if not sub_bot_data or user_id != sub_bot_data['owner_id']:
                await callback.answer("❌ Нет доступа")
                await state.clear()
                return
            
            # Удаляем оформление
            await self.db.update_post_footer(sub_bot_id, None)
            
            await callback.message.edit_text(
                "✅ <b>Оформление удалено!</b>\n\n"
                "Посты будут публиковаться без дополнительного оформления.",
                parse_mode="HTML"
            )
            await state.clear()
            await callback.answer()
        
        @dp.callback_query(F.data == "footer_cancel")
        async def cancel_footer_change(callback: types.CallbackQuery, state: FSMContext):
            """Отменить изменение оформления"""
            await callback.message.edit_text("❌ Изменение оформления отменено")
            await state.clear()
            await callback.answer()
        
        logger.info(f"Обработчики зарегистрированы для под-бота {sub_bot_id}")
