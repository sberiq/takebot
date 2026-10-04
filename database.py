import aiosqlite
from typing import Optional, List, Dict
import logging
import hashlib
import os
from contextlib import asynccontextmanager
from pathlib import Path

from config import DATABASE_PATH

logger = logging.getLogger(__name__)


class Database:
    def __init__(self, db_path: str = DATABASE_PATH):
        self.db_path = db_path
        db_file = Path(db_path)
        if db_file.exists():
            try:
                os.chmod(db_file, 0o600)
            except OSError:
                logger.warning("Could not restrict database file permissions")
    @asynccontextmanager
    async def _connect(self):
        async with aiosqlite.connect(self.db_path, timeout=30) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("PRAGMA busy_timeout = 30000")
            yield db

    def _decode_bot_row(self, row: Dict | None) -> Dict | None:
        """Return a row as a plain mapping; credentials are stored as-is."""
        return dict(row) if row else None

    async def _schema_migration(self) -> None:
        """Add routing fields without transforming stored values."""
        async with self._connect() as db:
            async with db.execute("PRAGMA table_info(sub_bots)") as cursor:
                bot_columns = {row[1] for row in await cursor.fetchall()}
            additions = {
                "bot_token_hash": "TEXT",
                "managed_bot_id": "INTEGER",
                "pending_admin_chat_id": "INTEGER",
                "pending_channel_id": "INTEGER",
                "rich_messages_enabled": "INTEGER NOT NULL DEFAULT 1",
            }
            for name, declaration in additions.items():
                if name not in bot_columns:
                    await db.execute(f"ALTER TABLE sub_bots ADD COLUMN {name} {declaration}")

            async with db.execute("PRAGMA table_info(messages)") as cursor:
                message_columns = {row[1] for row in await cursor.fetchall()}
            if "moderation_message_id" not in message_columns:
                await db.execute("ALTER TABLE messages ADD COLUMN moderation_message_id INTEGER")

            async with db.execute(
                "SELECT id, bot_token, bot_token_hash FROM sub_bots WHERE bot_token_hash IS NULL"
            ) as cursor:
                rows_without_hash = await cursor.fetchall()
            for bot_id, token, _ in rows_without_hash:
                if token:
                    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
                    await db.execute(
                        "UPDATE sub_bots SET bot_token_hash = ? WHERE id = ?",
                        (token_hash, bot_id),
                    )

            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_subbot_status ON messages(sub_bot_id, status)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_subbot_admin_message ON messages(sub_bot_id, admin_message_id)"
            )
            await db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_sub_bots_token_hash ON sub_bots(bot_token_hash)"
            )
            await db.commit()
        try:
            os.chmod(self.db_path, 0o600)
        except OSError:
            logger.warning("Could not restrict database file permissions")

    async def init_db(self):
        """Инициализация базы данных"""
        async with self._connect() as db:
            # Таблица под-ботов
            await db.execute("""
                CREATE TABLE IF NOT EXISTS sub_bots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_id INTEGER NOT NULL,
                    bot_token TEXT NOT NULL UNIQUE,
                    bot_username TEXT,
                    admin_chat_id INTEGER,
                    channel_id INTEGER,
                    admin_chat_link TEXT,
                    channel_link TEXT,
                    moderation_mode TEXT DEFAULT 'manual',
                    allow_notifications INTEGER DEFAULT 1,
                    allow_ads INTEGER DEFAULT 1,
                    post_footer TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Добавляем колонку post_footer если её нет (для существующих баз)
            try:
                await db.execute("ALTER TABLE sub_bots ADD COLUMN post_footer TEXT")
                await db.commit()
            except:
                pass  # Колонка уже существует
            
            # Добавляем колонку post_header если её нет
            try:
                await db.execute("ALTER TABLE sub_bots ADD COLUMN post_header TEXT")
                await db.commit()
            except:
                pass  # Колонка уже существует
            
            # Добавляем колонку header_mode (inline/newline) если её нет
            try:
                await db.execute("ALTER TABLE sub_bots ADD COLUMN header_mode TEXT DEFAULT 'newline'")
                await db.commit()
            except:
                pass  # Колонка уже существует
            
            # Добавляем колонки для ссылок если их нет
            try:
                await db.execute("ALTER TABLE sub_bots ADD COLUMN admin_chat_link TEXT")
                await db.commit()
            except:
                pass  # Колонка уже существует
            
            try:
                await db.execute("ALTER TABLE sub_bots ADD COLUMN channel_link TEXT")
                await db.commit()
            except:
                pass  # Колонка уже существует
            
            # Добавляем колонку welcome_message если её нет
            try:
                await db.execute("ALTER TABLE sub_bots ADD COLUMN welcome_message TEXT")
                await db.commit()
            except:
                pass  # Колонка уже существует
            
            # Добавляем колонки для Gemini если их нет
            try:
                await db.execute("ALTER TABLE sub_bots ADD COLUMN gemini_api_key TEXT")
                await db.commit()
            except:
                pass

            try:
                await db.execute("ALTER TABLE sub_bots ADD COLUMN gemini_prompt TEXT")
                await db.commit()
            except:
                pass

            # Таблица пользователей под-ботов
            await db.execute("""
                CREATE TABLE IF NOT EXISTS sub_bot_users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sub_bot_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    username TEXT,
                    first_name TEXT,
                    is_blocked INTEGER DEFAULT 0,
                    preferred_anonymous_mode INTEGER DEFAULT 0,
                    joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (sub_bot_id) REFERENCES sub_bots (id),
                    UNIQUE(sub_bot_id, user_id)
                )
            """)
            
            # Добавляем колонку preferred_anonymous_mode если её нет
            try:
                await db.execute("ALTER TABLE sub_bot_users ADD COLUMN preferred_anonymous_mode INTEGER DEFAULT 0")
                await db.commit()
            except:
                pass  # Колонка уже существует

            # Таблица сообщений (предложек)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sub_bot_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    is_anonymous INTEGER DEFAULT 0,
                    message_id INTEGER,
                    admin_message_id INTEGER,
                    moderation_message_id INTEGER,
                    channel_message_id INTEGER,
                    status TEXT DEFAULT 'pending',
                    content_type TEXT,
                    original_text TEXT,
                    original_entities TEXT,
                    is_media_group INTEGER DEFAULT 0,
                    media_group_message_ids TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (sub_bot_id) REFERENCES sub_bots (id)
                )
            """)
            
            # Таблица глобальных блокировок (для конструктора)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS global_bans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL UNIQUE,
                    banned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    banned_by INTEGER
                )
            """)
            
            # Добавляем колонку original_text если её нет
            try:
                await db.execute("ALTER TABLE messages ADD COLUMN original_text TEXT")
                await db.commit()
            except:
                pass  # Колонка уже существует
            
            # Добавляем колонки для медиа-групп если их нет
            try:
                await db.execute("ALTER TABLE messages ADD COLUMN is_media_group INTEGER DEFAULT 0")
                await db.commit()
            except:
                pass  # Колонка уже существует
            
            try:
                await db.execute("ALTER TABLE messages ADD COLUMN media_group_message_ids TEXT")
                await db.commit()
            except:
                pass  # Колонка уже существует
            
            # Добавляем колонку has_spoiler если её нет
            try:
                await db.execute("ALTER TABLE messages ADD COLUMN has_spoiler INTEGER DEFAULT 0")
                await db.commit()
            except:
                pass  # Колонка уже существует
            
            # Добавляем колонку original_entities если её нет
            try:
                await db.execute("ALTER TABLE messages ADD COLUMN original_entities TEXT")
                await db.commit()
            except:
                pass  # Колонка уже существует

            await db.commit()
        await self._schema_migration()
        try:
            os.chmod(self.db_path, 0o600)
        except OSError:
            logger.warning("Could not restrict database file permissions")

    # ========== SUB BOTS ==========
    async def add_sub_bot(
        self,
        owner_id: int,
        bot_token: str,
        bot_username: str = None,
        allow_notifications: bool = True,
        allow_ads: bool = True,
        managed_bot_id: int | None = None,
    ) -> int:
        """Добавить под-бот"""
        async with self._connect() as db:
            token_hash = hashlib.sha256(bot_token.encode("utf-8")).hexdigest()
            cursor = await db.execute(
                """INSERT INTO sub_bots (owner_id, bot_token, bot_token_hash, bot_username, allow_notifications, allow_ads, managed_bot_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (owner_id, bot_token, token_hash, bot_username, int(allow_notifications), int(allow_ads), managed_bot_id)
            )
            sub_bot_id = cursor.lastrowid
            await db.commit()
            logger.info("Created sub-bot id=%s owner_id=%s username=%s", sub_bot_id, owner_id, bot_username)
            return sub_bot_id

    async def get_sub_bot_by_managed_id(self, managed_bot_id: int) -> Optional[Dict]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM sub_bots WHERE managed_bot_id = ?", (managed_bot_id,)) as cursor:
                row = await cursor.fetchone()
                return self._decode_bot_row(row) if row else None

    async def update_managed_sub_bot(
        self, sub_bot_id: int, managed_bot_id: int, owner_id: int, bot_token: str, bot_username: str
    ) -> None:
        token_hash = hashlib.sha256(bot_token.encode("utf-8")).hexdigest()
        async with self._connect() as db:
            await db.execute(
                "UPDATE sub_bots SET managed_bot_id = ?, owner_id = ?, bot_token = ?, bot_token_hash = ?, bot_username = ? WHERE id = ?",
                (managed_bot_id, owner_id, bot_token, token_hash, bot_username, sub_bot_id),
            )
            await db.commit()

    async def get_sub_bot_by_token(self, bot_token: str) -> Optional[Dict]:
        """Получить под-бот по токену"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            token_hash = hashlib.sha256(bot_token.encode("utf-8")).hexdigest()
            async with db.execute(
                "SELECT * FROM sub_bots WHERE bot_token_hash = ?", (token_hash,)
            ) as cursor:
                row = await cursor.fetchone()
                return self._decode_bot_row(row) if row else None
    
    async def get_sub_bot_by_id(self, sub_bot_id: int) -> Optional[Dict]:
        """Получить под-бот по ID"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM sub_bots WHERE id = ?", (sub_bot_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return self._decode_bot_row(row) if row else None

    async def get_sub_bot_by_owner(self, owner_id: int) -> Optional[Dict]:
        """Получить под-бот по владельцу (первый)"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM sub_bots WHERE owner_id = ? LIMIT 1", (owner_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return self._decode_bot_row(row) if row else None
    
    async def get_all_sub_bots_by_owner(self, owner_id: int) -> List[Dict]:
        """Получить все под-боты владельца"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM sub_bots WHERE owner_id = ? ORDER BY created_at DESC", (owner_id,)
            ) as cursor:
                rows = await cursor.fetchall()
                bots = [self._decode_bot_row(row) for row in rows]
                logger.info(f"Найдено {len(bots)} ботов для владельца {owner_id}")
                return bots

    async def get_all_sub_bots(self) -> List[Dict]:
        """Получить все под-боты"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM sub_bots") as cursor:
                rows = await cursor.fetchall()
                return [self._decode_bot_row(row) for row in rows]

    async def update_sub_bot_chats(self, sub_bot_id: int, admin_chat_id: int = None,
                                   channel_id: int = None, admin_chat_link: str = None,
                                   channel_link: str = None):
        """Обновить ID и ссылки чатов под-бота"""
        async with self._connect() as db:
            updates = []
            params = []
            if admin_chat_id is not None:
                updates.append("admin_chat_id = ?")
                params.append(admin_chat_id)
            if channel_id is not None:
                updates.append("channel_id = ?")
                params.append(channel_id)
            if admin_chat_link is not None:
                updates.append("admin_chat_link = ?")
                params.append(admin_chat_link)
            if channel_link is not None:
                updates.append("channel_link = ?")
                params.append(channel_link)
            
            if updates:
                params.append(sub_bot_id)
                await db.execute(
                    f"UPDATE sub_bots SET {', '.join(updates)} WHERE id = ?",
                    params
                )
                await db.commit()

    async def stage_chat_binding(self, sub_bot_id: int, kind: str, chat_id: int) -> bool:
        """Stage a chat/channel change until the bot owner confirms it."""
        column = {"admin": "pending_admin_chat_id", "channel": "pending_channel_id"}.get(kind)
        if not column:
            raise ValueError("Unsupported chat binding type")
        async with self._connect() as db:
            cursor = await db.execute(
                f"UPDATE sub_bots SET {column} = ? WHERE id = ?",
                (chat_id, sub_bot_id),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def confirm_chat_binding(
        self, sub_bot_id: int, kind: str, chat_id: int, link: str | None = None
    ) -> bool:
        """Commit a pending routing change only when its stored target matches."""
        if kind == "admin":
            target, pending, link_column = "admin_chat_id", "pending_admin_chat_id", "admin_chat_link"
        elif kind == "channel":
            target, pending, link_column = "channel_id", "pending_channel_id", "channel_link"
        else:
            raise ValueError("Unsupported chat binding type")
        async with self._connect() as db:
            cursor = await db.execute(
                f"UPDATE sub_bots SET {target} = ?, {link_column} = COALESCE(?, {link_column}), {pending} = NULL WHERE id = ? AND {pending} = ?",
                (chat_id, link, sub_bot_id, chat_id),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def cancel_chat_binding(self, sub_bot_id: int, kind: str, chat_id: int) -> bool:
        column = {"admin": "pending_admin_chat_id", "channel": "pending_channel_id"}.get(kind)
        if not column:
            raise ValueError("Unsupported chat binding type")
        async with self._connect() as db:
            cursor = await db.execute(
                f"UPDATE sub_bots SET {column} = NULL WHERE id = ? AND {column} = ?",
                (sub_bot_id, chat_id),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def update_moderation_mode(self, sub_bot_id: int, mode: str):
        """Обновить режим модерации"""
        async with self._connect() as db:
            await db.execute(
                "UPDATE sub_bots SET moderation_mode = ? WHERE id = ?",
                (mode, sub_bot_id)
            )
            await db.commit()

    async def update_gemini_settings(self, sub_bot_id: int, api_key: str, prompt: str):
        """Обновить настройки Gemini"""
        async with self._connect() as db:
            await db.execute(
                "UPDATE sub_bots SET gemini_api_key = ?, gemini_prompt = ? WHERE id = ?",
                (api_key, prompt, sub_bot_id)
            )
            await db.commit()

    async def claim_message_for_publication(self, message_id: int, sub_bot_id: int) -> bool:
        """Atomically reserve a pending submission; a second approval click loses."""
        async with self._connect() as db:
            cursor = await db.execute(
                "UPDATE messages SET status = 'publishing' WHERE id = ? AND sub_bot_id = ? AND status = 'pending'",
                (message_id, sub_bot_id),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def set_moderation_message_id(
        self, message_id: int, sub_bot_id: int, moderation_message_id: int
    ) -> bool:
        """Store the separate action-card message used for album moderation."""
        async with self._connect() as db:
            cursor = await db.execute(
                "UPDATE messages SET moderation_message_id = ? WHERE id = ? AND sub_bot_id = ? AND status = 'pending'",
                (moderation_message_id, message_id, sub_bot_id),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def release_message_claim(self, message_id: int, sub_bot_id: int) -> None:
        async with self._connect() as db:
            await db.execute(
                "UPDATE messages SET status = 'pending' WHERE id = ? AND sub_bot_id = ? AND status = 'publishing'",
                (message_id, sub_bot_id),
            )
            await db.commit()

    async def reject_pending_message(self, message_id: int, sub_bot_id: int) -> bool:
        async with self._connect() as db:
            cursor = await db.execute(
                "UPDATE messages SET status = 'rejected' WHERE id = ? AND sub_bot_id = ? AND status = 'pending'",
                (message_id, sub_bot_id),
            )
            await db.commit()
            return cursor.rowcount == 1
    
    async def update_post_footer(self, sub_bot_id: int, footer: str = None):
        """Обновить оформление поста (footer)"""
        async with self._connect() as db:
            await db.execute(
                "UPDATE sub_bots SET post_footer = ? WHERE id = ?",
                (footer, sub_bot_id)
            )
            await db.commit()
    
    async def update_post_header(self, sub_bot_id: int, header: str = None, header_mode: str = 'newline'):
        """Обновить оформление сверху (header)"""
        async with self._connect() as db:
            await db.execute(
                "UPDATE sub_bots SET post_header = ?, header_mode = ? WHERE id = ?",
                (header, header_mode, sub_bot_id)
            )
            await db.commit()
    
    async def update_welcome_message(self, sub_bot_id: int, welcome_message: str = None):
        """Обновить приветственное сообщение под-бота"""
        async with self._connect() as db:
            await db.execute(
                "UPDATE sub_bots SET welcome_message = ? WHERE id = ?",
                (welcome_message, sub_bot_id)
            )
            await db.commit()

    # ========== USERS ==========
    async def add_or_update_user(self, sub_bot_id: int, user_id: int,
                                username: str = None, first_name: str = None):
        """Добавить или обновить пользователя"""
        async with self._connect() as db:
            await db.execute(
                """INSERT INTO sub_bot_users (sub_bot_id, user_id, username, first_name)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(sub_bot_id, user_id) DO UPDATE SET
                   username = excluded.username,
                   first_name = excluded.first_name""",
                (sub_bot_id, user_id, username, first_name)
            )
            await db.commit()
    
    async def set_user_anonymous_mode(self, sub_bot_id: int, user_id: int, is_anonymous: bool):
        """Установить предпочтительный режим анонимности для пользователя"""
        async with self._connect() as db:
            # Сначала убеждаемся, что пользователь существует
            await self.add_or_update_user(sub_bot_id, user_id, None, None)
            
            # Обновляем режим анонимности
            await db.execute(
                "UPDATE sub_bot_users SET preferred_anonymous_mode = ? WHERE sub_bot_id = ? AND user_id = ?",
                (int(is_anonymous), sub_bot_id, user_id)
            )
            await db.commit()
            logger.info(f"Установлен режим анонимности для пользователя {user_id} в боте {sub_bot_id}: {is_anonymous}")
    
    async def get_user_anonymous_mode(self, sub_bot_id: int, user_id: int) -> bool:
        """Получить предпочтительный режим анонимности для пользователя"""
        async with self._connect() as db:
            async with db.execute(
                "SELECT preferred_anonymous_mode FROM sub_bot_users WHERE sub_bot_id = ? AND user_id = ?",
                (sub_bot_id, user_id)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    return bool(row[0])
                # Если пользователя нет в БД, возвращаем False (по умолчанию)
                return False

    async def block_user(self, sub_bot_id: int, user_id: int):
        """Заблокировать пользователя"""
        async with self._connect() as db:
            # Сначала проверяем, существует ли запись
            async with db.execute(
                "SELECT id, is_blocked FROM sub_bot_users WHERE sub_bot_id = ? AND user_id = ?",
                (sub_bot_id, user_id)
            ) as cursor:
                row = await cursor.fetchone()
                
                if row:
                    # Запись существует - обновляем is_blocked
                    await db.execute(
                        "UPDATE sub_bot_users SET is_blocked = 1 WHERE sub_bot_id = ? AND user_id = ?",
                        (sub_bot_id, user_id)
                    )
                    logger.info(f"Обновлена запись для пользователя {user_id} в боте {sub_bot_id}: is_blocked = 1")
                else:
                    # Записи нет - создаем новую с is_blocked = 1
                    await db.execute(
                        """INSERT INTO sub_bot_users (sub_bot_id, user_id, is_blocked, joined_at)
                           VALUES (?, ?, 1, datetime('now'))""",
                        (sub_bot_id, user_id)
                    )
                    logger.info(f"Создана новая запись для пользователя {user_id} в боте {sub_bot_id} с is_blocked = 1")
            
            await db.commit()
    
    async def block_user_globally(self, user_id: int, banned_by: int = None):
        """Глобальная блокировка пользователя во всех под-ботах и конструкторе"""
        async with self._connect() as db:
            # Добавляем в таблицу глобальных блокировок (для конструктора)
            try:
                await db.execute(
                    """INSERT INTO global_bans (user_id, banned_by)
                       VALUES (?, ?)
                       ON CONFLICT(user_id) DO NOTHING""",
                    (user_id, banned_by)
                )
            except Exception as e:
                logger.warning(f"Ошибка при добавлении в global_bans: {e}")
            
            # Получаем все под-боты
            async with db.execute("SELECT id FROM sub_bots") as cursor:
                sub_bots = await cursor.fetchall()
            
            blocked_count = 0
            for (sub_bot_id,) in sub_bots:
                # Проверяем, существует ли пользователь в этом боте
                async with db.execute(
                    "SELECT id FROM sub_bot_users WHERE sub_bot_id = ? AND user_id = ?",
                    (sub_bot_id, user_id)
                ) as check_cursor:
                    exists = await check_cursor.fetchone()
                    if exists:
                        # Обновляем существующую запись
                        await db.execute(
                            "UPDATE sub_bot_users SET is_blocked = 1 WHERE sub_bot_id = ? AND user_id = ?",
                            (sub_bot_id, user_id)
                        )
                        blocked_count += 1
                    else:
                        # Создаем новую запись с блокировкой
                        await db.execute(
                            """INSERT INTO sub_bot_users (sub_bot_id, user_id, is_blocked, joined_at)
                               VALUES (?, ?, 1, datetime('now'))""",
                            (sub_bot_id, user_id)
                        )
                        blocked_count += 1
            
            await db.commit()
            logger.info(f"Пользователь {user_id} заблокирован глобально в {blocked_count} под-ботах и конструкторе")
            return blocked_count
    
    async def is_user_globally_banned(self, user_id: int) -> bool:
        """Проверить, заблокирован ли пользователь глобально (в конструкторе)"""
        async with self._connect() as db:
            async with db.execute(
                "SELECT id FROM global_bans WHERE user_id = ?",
                (user_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return row is not None
    
    async def get_user_id_by_username(self, username: str) -> Optional[int]:
        """Получить user_id по username из базы данных"""
        # Убираем @ если есть
        username_clean = username.lstrip('@').lower()
        
        async with self._connect() as db:
            # Ищем в таблице sub_bot_users по username
            async with db.execute(
                "SELECT DISTINCT user_id FROM sub_bot_users WHERE LOWER(username) = ? LIMIT 1",
                (username_clean,)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    return row[0]
        return None
    
    async def get_user_id_by_username_in_sub_bot(self, sub_bot_id: int, username: str) -> Optional[int]:
        """Получить user_id по username в конкретном под-боте"""
        # Убираем @ если есть
        username_clean = username.lstrip('@').lower()
        
        async with self._connect() as db:
            # Ищем в таблице sub_bot_users по username в конкретном под-боте
            async with db.execute(
                "SELECT user_id FROM sub_bot_users WHERE sub_bot_id = ? AND LOWER(username) = ? LIMIT 1",
                (sub_bot_id, username_clean)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    return row[0]
        return None

    async def unblock_user(self, sub_bot_id: int, user_id: int):
        """Разблокировать пользователя"""
        async with self._connect() as db:
            # Сначала проверяем текущее состояние
            async with db.execute(
                "SELECT id, is_blocked FROM sub_bot_users WHERE sub_bot_id = ? AND user_id = ?",
                (sub_bot_id, user_id)
            ) as cursor:
                row = await cursor.fetchone()
                if not row:
                    # Если пользователя нет, создаем запись с is_blocked = 0
                    await db.execute(
                        """INSERT INTO sub_bot_users (sub_bot_id, user_id, is_blocked, joined_at)
                           VALUES (?, ?, 0, datetime('now'))""",
                        (sub_bot_id, user_id)
                    )
                    logger.info(f"Создана запись для пользователя {user_id} в боте {sub_bot_id} при разблокировке")
                else:
                    current_blocked = row[1]
                    logger.info(f"Текущее состояние is_blocked для пользователя {user_id} в боте {sub_bot_id}: {current_blocked}")
                    
                    # Если пользователь существует, обновляем is_blocked = 0
                    cursor_update = await db.execute(
                        "UPDATE sub_bot_users SET is_blocked = 0 WHERE sub_bot_id = ? AND user_id = ?",
                        (sub_bot_id, user_id)
                    )
                    rows_affected = cursor_update.rowcount
                    logger.info(f"UPDATE выполнен для пользователя {user_id} в боте {sub_bot_id}, затронуто строк: {rows_affected}")
                    
                    if rows_affected == 0:
                        logger.error(f"ОШИБКА: UPDATE не затронул ни одной строки для пользователя {user_id} в боте {sub_bot_id}")
                    else:
                        # Проверяем, что значение действительно изменилось
                        async with db.execute(
                            "SELECT is_blocked FROM sub_bot_users WHERE sub_bot_id = ? AND user_id = ?",
                            (sub_bot_id, user_id)
                        ) as cursor_check:
                            row_check = await cursor_check.fetchone()
                            if row_check:
                                new_blocked = row_check[0]
                                logger.info(f"Проверка после UPDATE: is_blocked = {new_blocked} для пользователя {user_id} в боте {sub_bot_id}")
                                if new_blocked != 0:
                                    logger.error(f"ОШИБКА: is_blocked не изменился на 0, текущее значение: {new_blocked}")
            await db.commit()
            logger.info(f"Коммит выполнен для разблокировки пользователя {user_id} в боте {sub_bot_id}")

    async def is_user_blocked(self, sub_bot_id: int, user_id: int) -> bool:
        """Проверить, заблокирован ли пользователь"""
        async with self._connect() as db:
            async with db.execute(
                "SELECT is_blocked FROM sub_bot_users WHERE sub_bot_id = ? AND user_id = ?",
                (sub_bot_id, user_id)
            ) as cursor:
                row = await cursor.fetchone()
                return bool(row[0]) if row else False

    async def get_all_users_of_sub_bot(self, sub_bot_id: int) -> List[Dict]:
        """Получить всех пользователей под-бота"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM sub_bot_users WHERE sub_bot_id = ?", (sub_bot_id,)
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def get_all_users(self) -> List[Dict]:
        """Получить всех пользователей всех под-ботов"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM sub_bot_users") as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    # ========== MESSAGES ==========
    async def add_message(self, sub_bot_id: int, user_id: int, is_anonymous: bool,
                         message_id: int, content_type: str, admin_message_id: int = None,
                         original_text: str = None, original_entities: str = None,
                         is_media_group: bool = False,
                         media_group_message_ids: str = None, status: str = 'pending',
                         has_spoiler: bool = False) -> int:
        """Добавить сообщение"""
        async with self._connect() as db:
            cursor = await db.execute(
                """INSERT INTO messages (sub_bot_id, user_id, is_anonymous, message_id, 
                   admin_message_id, content_type, original_text, original_entities, is_media_group, media_group_message_ids, status, has_spoiler)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (sub_bot_id, user_id, int(is_anonymous), message_id, admin_message_id, 
                 content_type, original_text, original_entities, int(is_media_group), media_group_message_ids, status, int(has_spoiler))
            )
            await db.commit()
            return cursor.lastrowid

    async def update_message_status(self, message_db_id: int, status: str,
                                   channel_message_id: int = None):
        """Обновить статус сообщения"""
        async with self._connect() as db:
            if channel_message_id:
                await db.execute(
                    "UPDATE messages SET status = ?, channel_message_id = ? WHERE id = ?",
                    (status, channel_message_id, message_db_id)
                )
            else:
                await db.execute(
                    "UPDATE messages SET status = ? WHERE id = ?",
                    (status, message_db_id)
                )
            await db.commit()

    async def get_message_by_id(
        self, message_id: int, sub_bot_id: int | None = None
    ) -> Optional[Dict]:
        """Получить сообщение по ID записи в базе данных"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            if sub_bot_id is None:
                query, parameters = "SELECT * FROM messages WHERE id = ?", (message_id,)
            else:
                query, parameters = "SELECT * FROM messages WHERE id = ? AND sub_bot_id = ?", (message_id, sub_bot_id)
            async with db.execute(query, parameters) as cursor:
                row = await cursor.fetchone()
                return self._decode_bot_row(row) if row else None
    
    async def get_message_by_admin_msg_id(
        self, admin_message_id: int, sub_bot_id: int | None = None
    ) -> Optional[Dict]:
        """Look up an admin message only inside the bot that owns that chat."""
        if sub_bot_id is None:
            return None
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM messages WHERE (admin_message_id = ? OR moderation_message_id = ?) AND sub_bot_id = ? ORDER BY id DESC LIMIT 1",
                (admin_message_id, admin_message_id, sub_bot_id),
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def delete_sub_bot(self, sub_bot_id: int):
        """Удалить под-бота и все связанные данные"""
        async with self._connect() as db:
            # Удаляем сообщения
            await db.execute("DELETE FROM messages WHERE sub_bot_id = ?", (sub_bot_id,))
            # Удаляем пользователей
            await db.execute("DELETE FROM sub_bot_users WHERE sub_bot_id = ?", (sub_bot_id,))
            # Удаляем бота
            await db.execute("DELETE FROM sub_bots WHERE id = ?", (sub_bot_id,))
            await db.commit()

    # ========== STATISTICS ==========
    async def get_statistics(self) -> Dict:
        """Получить общую статистику"""
        async with self._connect() as db:
            # Количество под-ботов
            async with db.execute("SELECT COUNT(*) FROM sub_bots") as cursor:
                sub_bots_count = (await cursor.fetchone())[0]

            # Количество пользователей
            async with db.execute("SELECT COUNT(*) FROM sub_bot_users") as cursor:
                users_count = (await cursor.fetchone())[0]

            # Количество сообщений
            async with db.execute("SELECT COUNT(*) FROM messages") as cursor:
                messages_count = (await cursor.fetchone())[0]

            # Опубликованных сообщений
            async with db.execute(
                "SELECT COUNT(*) FROM messages WHERE status = 'published'"
            ) as cursor:
                published_count = (await cursor.fetchone())[0]

            return {
                "sub_bots": sub_bots_count,
                "users": users_count,
                "messages": messages_count,
                "published": published_count
            }

    async def get_sub_bot_statistics(self, sub_bot_id: int) -> Dict:
        """Получить статистику конкретного под-бота"""
        async with self._connect() as db:
            # Количество пользователей
            async with db.execute(
                "SELECT COUNT(*) FROM sub_bot_users WHERE sub_bot_id = ?", (sub_bot_id,)
            ) as cursor:
                users_count = (await cursor.fetchone())[0]

            # Количество сообщений
            async with db.execute(
                "SELECT COUNT(*) FROM messages WHERE sub_bot_id = ?", (sub_bot_id,)
            ) as cursor:
                messages_count = (await cursor.fetchone())[0]

            # Опубликованных сообщений
            async with db.execute(
                "SELECT COUNT(*) FROM messages WHERE sub_bot_id = ? AND status = 'published'",
                (sub_bot_id,)
            ) as cursor:
                published_count = (await cursor.fetchone())[0]
            
            # Отклоненных сообщений
            async with db.execute(
                "SELECT COUNT(*) FROM messages WHERE sub_bot_id = ? AND status = 'rejected'",
                (sub_bot_id,)
            ) as cursor:
                rejected_count = (await cursor.fetchone())[0]
            
            # На модерации
            async with db.execute(
                "SELECT COUNT(*) FROM messages WHERE sub_bot_id = ? AND status = 'pending'",
                (sub_bot_id,)
            ) as cursor:
                pending_count = (await cursor.fetchone())[0]

            return {
                "users": users_count,
                "messages": messages_count,
                "published": published_count,
                "rejected": rejected_count,
                "pending": pending_count
            }
