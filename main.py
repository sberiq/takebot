"""
Главный файл запуска конструктора ботов
"""
import asyncio
import logging
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton

from config import (
    MAIN_BOT_TOKEN,
    ADMIN_ID,
    BOT_MANAGEMENT_ENABLED,
    BOT_MANAGEMENT_DEFAULT_NAME,
    INSTANCE_OWNER_ONLY,
)
from database import Database
from sub_bot_manager import SubBotManager
from services.bot_links import managed_bot_creation_link
from services.access_control import GlobalBanMiddleware, InstanceOwnerMiddleware
from services.log_safety import install_secret_redaction
from services.telegram_rich import entities_to_rich_html

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
install_secret_redaction()
logger = logging.getLogger(__name__)

# Инициализация
db = Database()
main_bot = Bot(token=MAIN_BOT_TOKEN)
main_dp = Dispatcher()
sub_bot_manager = SubBotManager(db)

main_dp.message.outer_middleware(InstanceOwnerMiddleware(ADMIN_ID, enabled=INSTANCE_OWNER_ONLY))
main_dp.callback_query.outer_middleware(InstanceOwnerMiddleware(ADMIN_ID, enabled=INSTANCE_OWNER_ONLY))

main_dp.message.outer_middleware(
    GlobalBanMiddleware(
        db,
        exempt_user_id=ADMIN_ID,
        message="❌ Вы заблокированы и не можете пользоваться конструктором.",
    )
)
main_dp.callback_query.outer_middleware(GlobalBanMiddleware(db, exempt_user_id=ADMIN_ID))


# FSM состояния
class BotRegistration(StatesGroup):
    waiting_for_token = State()
    waiting_for_notifications = State()
    waiting_for_footer_choice = State()
    waiting_for_footer_text = State()
    waiting_for_moderation_choice = State()
    waiting_for_gemini_key = State()
    waiting_for_gemini_prompt = State()


class Broadcast(StatesGroup):
    waiting_for_message = State()
    waiting_for_type = State()  # Реклама или уведомление
    waiting_for_audience = State()  # Пользователи/каналы/оба


class SubBotBroadcast(StatesGroup):
    waiting_for_message = State()
    waiting_for_confirm = State()


class BlockUser(StatesGroup):
    waiting_for_user_id = State()

class GlobalBan(StatesGroup):
    waiting_for_user_id = State()


class SubBotSettings(StatesGroup):
    waiting_for_bot_selection = State()
    waiting_for_broadcast = State()
    waiting_for_footer = State()
    waiting_for_header = State()
    waiting_for_header_mode = State()
    waiting_for_welcome = State()
    waiting_for_gemini_key = State()
    waiting_for_gemini_prompt = State()


# ========== МЕНЮ ==========
def get_main_menu(is_owner: bool = False, is_admin: bool = False):
    """Главное меню"""
    buttons = []
    
    buttons.append([KeyboardButton(text="🤖 Создать бот")])
    
    if is_owner:
        buttons.append([KeyboardButton(text="📋 Мои боты")])
        buttons.append([KeyboardButton(text="📊 Статистика"), KeyboardButton(text="📢 Рассылка")])
    
    if is_admin:
        buttons.append([KeyboardButton(text="⚙️ Админ-панель")])
    
    buttons.append([KeyboardButton(text="ℹ️ Помощь")])
    
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)


def get_settings_menu():
    """Меню настроек под-бота"""
    buttons = [
        [KeyboardButton(text="🚫 Блокировки"), KeyboardButton(text="📢 Рассылка пользователям")],
        [KeyboardButton(text="📊 Статистика бота")],
        [KeyboardButton(text="🔙 Главное меню")]
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)


# ========== КОМАНДЫ ==========
@main_dp.message(CommandStart())
async def cmd_start(message: types.Message):
    """Обработка команды /start"""
    user_id = message.from_user.id
    
    # Проверяем, есть ли у пользователя боты
    bots = await db.get_all_sub_bots_by_owner(user_id)
    is_owner = len(bots) > 0
    is_admin = user_id == ADMIN_ID
    
    welcome_text = (
        "🤖 <b>Добро пожаловать в конструктор ботов для приёма предложений!</b>\n\n"
        "Этот бот поможет вам создать ботов для приема предложений "
        "от пользователей с модерацией и публикацией в канал.\n\n"
    )
    
    if is_owner:
        welcome_text += (
            f"✅ У вас есть {len(bots)} бот(ов)\n\n"
            "Используйте меню для управления.\n"
            "Вы можете создать неограниченное количество ботов!"
        )
    else:
        welcome_text += (
            "✨ <b>Возможности:</b>\n"
            "• Несколько ботов на одного владельца\n"
            "• Автоматическая настройка\n"
            "• Анонимные посты\n"
            "• Модерация через кнопки\n"
            "• Статистика и рассылки\n\n"
            "Нажмите <b>🤖 Создать бот</b> для начала!"
        )

    await message.answer(welcome_text, reply_markup=get_main_menu(is_owner, is_admin), parse_mode="HTML")


@main_dp.message(Command("admin"))
@main_dp.message(F.text == "⚙️ Админ-панель")
async def cmd_admin(message: types.Message):
    """Админ-панель для владельца конструктора"""
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ У вас нет доступа к админ-панели.")
        return
    
    stats = await db.get_statistics()
    
    stats_text = (
        "📊 <b>Статистика конструктора</b>\n\n"
        f"🤖 Под-ботов: {stats['sub_bots']}\n"
        f"👥 Пользователей: {stats['users']}\n"
        f"💬 Сообщений: {stats['messages']}\n"
        f"✅ Опубликовано: {stats['published']}\n"
    )
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="🤖 Список ботов", callback_data="admin_bots_list")],
        [InlineKeyboardButton(text="🚫 Глобальная блокировка", callback_data="admin_global_ban")],
        [InlineKeyboardButton(text="🔄 Перезапустить ботов", callback_data="admin_restart_bots")]
    ])
    
    await message.answer(stats_text, reply_markup=keyboard, parse_mode="HTML")


@main_dp.callback_query(F.data == "admin_bots_list")
async def show_admin_bots_list(callback: types.CallbackQuery):
    """Показать список всех ботов с каналами и чатами (первая страница)"""
    await show_admin_bots_list_page(callback, page=0)


@main_dp.callback_query(F.data.startswith("admin_bots_list_page_"))
async def show_admin_bots_list_page(callback: types.CallbackQuery, page: int = None):
    """Показать список всех ботов с пагинацией"""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Нет доступа")
        return
    
    # Если page не передан, извлекаем из callback_data
    if page is None:
        try:
            page = int(callback.data.split("_")[-1])
        except (ValueError, IndexError):
            page = 0
    
    bots = await db.get_all_sub_bots()
    
    if not bots:
        await callback.message.edit_text("❌ Пока нет созданных ботов")
        await callback.answer()
        return
    
    # Настройки пагинации
    bots_per_page = 10
    total_bots = len(bots)
    total_pages = (total_bots + bots_per_page - 1) // bots_per_page  # Округление вверх
    
    # Проверка корректности страницы
    if page < 0:
        page = 0
    if page >= total_pages:
        page = total_pages - 1
    
    # Вычисляем индексы для текущей страницы
    start_idx = page * bots_per_page
    end_idx = min(start_idx + bots_per_page, total_bots)
    bots_on_page = bots[start_idx:end_idx]
    
    text = f"🤖 <b>Список всех ботов</b>\n"
    text += f"📄 Страница {page + 1} из {total_pages}\n"
    text += f"📊 Всего ботов: {total_bots}\n\n"
    
    for bot_data in bots_on_page:
        bot_username = bot_data.get('bot_username', 'Неизвестно')
        owner_id = bot_data.get('owner_id')
        admin_chat_id = bot_data.get('admin_chat_id')
        channel_id = bot_data.get('channel_id')
        channel_link = bot_data.get('channel_link')
        
        text += f"━━━━━━━━━━━━━━━\n"
        text += f"🤖 <b>@{bot_username}</b>\n"
        text += f"👤 Владелец: <code>{owner_id}</code>\n\n"
        
        if admin_chat_id:
            text += f"💬 <b>Админ-чат:</b> ID <code>{admin_chat_id}</code>\n"
        else:
            text += f"💬 <b>Админ-чат:</b> ❌ Не настроен\n"
        
        if channel_id:
            if channel_link and channel_link.startswith('http'):
                text += f"📢 <b>Канал:</b> {channel_link}\n"
            else:
                text += f"📢 <b>Канал:</b> ID <code>{channel_id}</code>\n"
                if channel_link:
                    text += f"   {channel_link}\n"
        else:
            text += f"📢 <b>Канал:</b> ❌ Не настроен\n"
        
        text += "\n"
    
    # Формируем клавиатуру с навигацией
    keyboard_buttons = []
    
    # Кнопки навигации
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"admin_bots_list_page_{page - 1}"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton(text="Вперед ▶️", callback_data=f"admin_bots_list_page_{page + 1}"))
    
    if nav_buttons:
        keyboard_buttons.append(nav_buttons)
    
    # Кнопка возврата
    keyboard_buttons.append([InlineKeyboardButton(text="⬅️ В админ-панель", callback_data="admin_back")])
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@main_dp.callback_query(F.data == "admin_back")
async def admin_back(callback: types.CallbackQuery):
    """Вернуться в админ-панель"""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Нет доступа")
        return
    
    stats = await db.get_statistics()
    
    stats_text = (
        "📊 <b>Статистика конструктора</b>\n\n"
        f"🤖 Под-ботов: {stats['sub_bots']}\n"
        f"👥 Пользователей: {stats['users']}\n"
        f"💬 Сообщений: {stats['messages']}\n"
        f"✅ Опубликовано: {stats['published']}\n"
    )
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="🤖 Список ботов", callback_data="admin_bots_list")],
        [InlineKeyboardButton(text="🚫 Глобальная блокировка", callback_data="admin_global_ban")],
        [InlineKeyboardButton(text="🔄 Перезапустить ботов", callback_data="admin_restart_bots")]
    ])
    
    await callback.message.edit_text(stats_text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


# ========== СОЗДАНИЕ БОТА ==========
@main_dp.message(F.text == "🤖 Создать бот")
async def create_bot_start(message: types.Message, state: FSMContext):
    """Начало создания бота"""
    if message.chat.type != "private":
        return
    user_id = message.from_user.id
    
    # КРИТИЧНО: Сохраняем user_id в state для использования при создании
    await state.update_data(user_id=user_id)
    
    buttons = []
    if BOT_MANAGEMENT_ENABLED:
        manager_info = await main_bot.get_me()
        link = managed_bot_creation_link(manager_info.username, name=BOT_MANAGEMENT_DEFAULT_NAME)
        buttons.append([InlineKeyboardButton(text="✨ Создать бота по ссылке Telegram", url=link)])
    buttons.append([InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_bot_creation")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    
    await message.answer(
        "🤖 <b>Создание бота</b>\n\n"
        "Отлично! Для создания бота мне понадобится:\n"
        "1️⃣ Токен вашего бота от @BotFather\n"
        "2️⃣ ID админ-чата (куда будут приходить предложки)\n"
        "3️⃣ ID канала (куда будут публиковаться посты)\n\n"
        "<b>Шаг 1:</b> Отправьте токен бота или создайте его по ссылке выше.\n\n"
        "💡 Чтобы получить токен:\n"
        "- Напишите @BotFather\n"
        "- Отправьте /newbot и создайте бота\n"
        "- Скопируйте токен и отправьте сюда",
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    await state.set_state(BotRegistration.waiting_for_token)


@main_dp.callback_query(F.data == "cancel_bot_creation")
async def cancel_bot_creation(callback: types.CallbackQuery, state: FSMContext):
    """Отмена создания бота"""
    await state.clear()
    await callback.message.edit_text(
        "❌ <b>Создание бота отменено</b>",
        parse_mode="HTML"
    )
    await callback.answer("Создание бота отменено")


@main_dp.message(BotRegistration.waiting_for_token)
async def process_bot_token(message: types.Message, state: FSMContext):
    """Обработка токена бота"""
    if message.chat.type != "private":
        try:
            await message.delete()
        except Exception:
            pass
        return
    # Проверяем, не является ли это командой отмены
    if message.text and message.text.strip().lower() in ["отмена", "cancel", "❌ отменить", "/cancel"]:
        await state.clear()
        await message.answer(
            "❌ <b>Создание бота отменено</b>",
            parse_mode="HTML"
        )
        return
    
    token = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        logger.debug("Could not delete a bot token message")
    
    # Проверяем формат токена
    if ":" not in token:
        await message.answer("❌ Неверный формат токена. Попробуйте еще раз.")
        return
    
    # Проверяем токен
    test_bot = None
    try:
        test_bot = Bot(token=token)
        bot_info = await test_bot.get_me()
    except Exception as e:
        logger.warning("Bot token validation failed (%s)", type(e).__name__)
        await message.answer("❌ Неверный токен или бот недоступен. Попробуйте еще раз.")
        return
    finally:
        if test_bot is not None:
            await test_bot.session.close()
    
    # Проверяем, не зарегистрирован ли уже этот бот
    existing = await db.get_sub_bot_by_token(token)
    if existing:
        await message.answer("❌ Этот бот уже зарегистрирован в конструкторе!")
        return
    
    # Сохраняем данные
    await state.update_data(
        token=token,
        username=bot_info.username
    )
    
    # Уведомления принудительно включены, запрашиваем только согласие на рекламу
    await state.update_data(allow_notifications=True)  # Принудительно включаем уведомления
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да", callback_data="ads_yes")],
        [InlineKeyboardButton(text="❌ Нет", callback_data="ads_no")]
    ])
    
    await message.answer(
        f"✅ Бот @{bot_info.username} успешно проверен!\n\n"
        "📢 <b>Согласие на рассылки</b>\n\n"
        "🔔 Уведомления о работе конструктора: <b>включены</b> (обязательно)\n\n"
        "📢 Получать рекламные сообщения?",
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    await state.set_state(BotRegistration.waiting_for_notifications)


# Удалено - уведомления теперь принудительно включены


@main_dp.callback_query(F.data.in_(["ads_yes", "ads_no"]))
async def process_ads_consent(callback: types.CallbackQuery, state: FSMContext):
    """Обработка согласия на рекламу (уведомления принудительно включены)"""
    current_state = await state.get_state()
    if current_state != BotRegistration.waiting_for_notifications.state:
        await callback.answer()
        return
    
    # Уведомления принудительно включены
    await state.update_data(allow_notifications=True)
    
    allow_ads = callback.data == "ads_yes"
    await state.update_data(allow_ads=allow_ads)
    
    # Спрашиваем про оформление поста
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, настроить", callback_data="footer_yes")],
        [InlineKeyboardButton(text="❌ Нет, без оформления", callback_data="footer_no")]
    ])
    
    await callback.message.edit_text(
        "📝 <b>Оформление постов</b>\n\n"
        "Хотите добавить оформление к постам в канале?\n\n"
        "Это текст, который будет добавляться после каждого сообщения пользователя.\n\n"
        "Например:\n"
        "━━━━━━━━━━━━━━━\n"
        "💬 Ваш канал для предложений\n"
        "🔗 @your_channel\n\n"
        "Настроить оформление?",
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    
    await state.set_state(BotRegistration.waiting_for_footer_choice)
    await callback.answer()


@main_dp.callback_query(F.data.in_(["footer_yes", "footer_no"]))
async def process_footer_choice(callback: types.CallbackQuery, state: FSMContext):
    """Обработка выбора оформления"""
    current_state = await state.get_state()
    if current_state != BotRegistration.waiting_for_footer_choice.state:
        await callback.answer()
        return
    
    if callback.data == "footer_yes":
        await callback.message.edit_text(
            "📝 <b>Настройка оформления</b>\n\n"
            "Отправьте текст оформления, который будет добавляться после каждого поста.\n\n"
            "Можно использовать:\n"
            "• Несколько строк\n"
            "• Эмодзи\n"
            "• <b>Жирный текст</b>\n"
            "• <i>Курсив</i>\n"
            "• <code>Моноширинный</code>\n"
            "• <a href=\"https://example.com\">Ссылки</a> (без предпросмотра)\n\n"
            "Пример:\n"
            "━━━━━━━━━━━━━━━\n"
            "💬 <b>Предложка от подписчика</b>\n"
            "📢 <a href=\"https://t.me/channel\">@your_channel</a>",
            parse_mode="HTML"
        )
        await state.set_state(BotRegistration.waiting_for_footer_text)
    else:
        # Без оформления - создаём бота
        await create_bot_final(callback.message, state, post_footer=None, user_id=callback.from_user.id)
        await state.clear()
    
    await callback.answer()


@main_dp.message(BotRegistration.waiting_for_footer_text)
async def process_footer_text(message: types.Message, state: FSMContext):
    """Обработка текста оформления"""
    # Используем HTML текст для поддержки форматирования
    footer_source = message.text or message.caption or ""
    footer_entities = message.entities or message.caption_entities
    footer_text = (
        entities_to_rich_html(footer_source, footer_entities)
        if footer_entities
        else footer_source
    )
    
    # Сохраняем footer в state
    await state.update_data(post_footer=footer_text)
    
    # Переходим к настройке модерации
    await ask_moderation_mode(message, state)


async def create_bot_final(message: types.Message, state: FSMContext, post_footer: str = None, user_id: int = None):
    """Финальное создание бота (вспомогательная функция)"""
    # Теперь это вызывается после выбора модерации
    data = await state.get_data()
    
    # Если post_footer передан явно, используем его, иначе из data
    if post_footer is None:
        post_footer = data.get('post_footer')
    
    # КРИТИЧНО: Если user_id не передан явно, берем из message
    # Но если message - это callback.message, то from_user может быть None
    if user_id is None:
        if hasattr(message, 'from_user') and message.from_user:
            user_id = message.from_user.id
        elif hasattr(message, 'chat') and message.chat:
            user_id = message.chat.id
        else:
            # Пытаемся получить из state (должен быть сохранен при начале создания)
            user_id = data.get('user_id')
            if not user_id:
                logger.error("Не удалось определить user_id при создании бота!")
                await message.answer("❌ Ошибка: не удалось определить пользователя. Попробуйте создать бота заново.")
                return
    
    logger.info(f"Создание бота для user_id={user_id}, username={data.get('username')}")
    
    try:
        # Создаём бота в базе
        sub_bot_id = await db.add_sub_bot(
            owner_id=user_id,
            bot_token=data['token'],
            bot_username=data['username'],
            allow_notifications=data.get('allow_notifications', True),
            allow_ads=data.get('allow_ads', True)
        )
        
        # Если есть оформление - сохраняем его
        if post_footer:
            await db.update_post_footer(sub_bot_id, post_footer)
            
        # Если выбрана AI модерация - сохраняем настройки
        moderation_mode = data.get('moderation_mode', 'manual')
        if moderation_mode == 'gemini':
            await db.update_gemini_settings(sub_bot_id, data.get('gemini_api_key'), data.get('gemini_prompt'))
            await db.update_moderation_mode(sub_bot_id, 'gemini')
        
        # Запускаем под-бот
        await sub_bot_manager.start_sub_bot(sub_bot_id, data['token'])
        
        footer_status = f"✅ С оформлением" if post_footer else "❌ Без оформления"
        mod_status = "🤖 AI (Gemini)" if moderation_mode == 'gemini' else "👤 Ручная"
        
        await message.answer(
            "🎉 <b>Бот создан!</b>\n\n"
            f"🤖 Ваш бот: @{data['username']}\n"
            f"📝 Оформление: {footer_status}\n"
            f"🛡 Модерация: {mod_status}\n\n"
            "📋 <b>Что дальше:</b>\n\n"
            "1️⃣ Создайте <b>группу</b> в Telegram\n"
            "2️⃣ Добавьте бота @{} в группу\n"
            "3️⃣ Сделайте бота <b>администратором</b> группы\n"
            "   → Бот автоматически определит админ-чат\n\n"
            "4️⃣ Создайте <b>канал</b> в Telegram\n"
            "5️⃣ Добавьте бота @{} в канал\n"
            "6️⃣ Сделайте бота <b>администратором</b> с правом публикации\n"
            "   → Бот автоматически определит канал\n\n"
            "✅ После этого бот готов к работе!\n\n"
            "Пользователи смогут отправлять предложки боту в личку.".format(data['username'], data['username']),
            reply_markup=get_main_menu(True),
            parse_mode="HTML"
        )
        
        logger.info(f"Создан новый под-бот: @{data['username']} (ID: {sub_bot_id})")
        
    except Exception as e:
        logger.error(f"Ошибка создания бота: {e}")
        await message.answer(
            "❌ Произошла ошибка при создании бота. Попробуйте еще раз.",
            reply_markup=get_main_menu(False)
        )


async def ask_moderation_mode(message: types.Message, state: FSMContext):
    """Спросить про режим модерации"""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Ручная проверка", callback_data="mod_manual")],
        [InlineKeyboardButton(text="🤖 AI (Gemini)", callback_data="mod_gemini")]
    ])
    
    await message.answer(
        "🛡 <b>Выберите режим модерации</b>\n\n"
        "<b>👤 Ручная проверка:</b>\n"
        "Все посты приходят в админ-чат, вы сами решаете публиковать или нет.\n\n"
        "<b>🤖 AI (Gemini):</b>\n"
        "Нейросеть проверяет текст и фото. Одобренные посты публикуются автоматически, отклоненные идут к вам на проверку.\n"
        "<i>(Требуется API Key)</i>",
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    await state.set_state(BotRegistration.waiting_for_moderation_choice)


@main_dp.callback_query(BotRegistration.waiting_for_moderation_choice, F.data.in_(["mod_manual", "mod_gemini"]))
async def process_moderation_choice(callback: types.CallbackQuery, state: FSMContext):
    """Обработка выбора модерации"""
    if callback.data == "mod_manual":
        await state.update_data(moderation_mode="manual")
        await create_bot_final(callback.message, state, user_id=callback.from_user.id)
        await state.clear()
    else:
        await state.update_data(moderation_mode="gemini")
        
        text = (
            "🔑 <b>Настройка Gemini API</b>\n\n"
            "Отправьте API Key от Google Gemini.\n\n"
            "<b>Как получить ключ:</b>\n"
            "1. Перейдите в <a href=\"https://aistudio.google.com/app/apikey\">Google AI Studio</a>\n"
            "2. Создайте новый API Key\n"
            "3. Скопируйте его и отправьте сюда.\n\n"
            "⚠️ Ключ будет храниться в зашифрованном виде."
        )
        
        await callback.message.edit_text(text, parse_mode="HTML", disable_web_page_preview=True)
        await state.set_state(BotRegistration.waiting_for_gemini_key)
    
    await callback.answer()


@main_dp.message(BotRegistration.waiting_for_gemini_key)
async def process_registration_gemini_key(message: types.Message, state: FSMContext):
    """Обработка API Key при регистрации"""
    api_key = message.text.strip()
    
    if len(api_key) < 10:
        await message.answer("❌ Похоже, это невалидный ключ. Попробуйте еще раз.")
        return
    
    await state.update_data(gemini_api_key=api_key)
    await state.set_state(BotRegistration.waiting_for_gemini_prompt)
    
    text = (
        "📝 <b>Настройка промпта</b>\n\n"
        "Теперь напишите инструкцию (промпт) для нейросети.\n"
        "Опишите, какие посты нужно пропускать, а какие отклонять.\n\n"
        "<b>Пример:</b>\n"
        "<i>Ты модератор канала. Твоя задача - проверять посты на наличие спама, рекламы, оскорблений или запрещенного контента. "
        "Если пост нормальный и интересный - ответь PASS. Если есть нарушения - ответь REJECT. "
        "Контент может содержать текст и фото. Будь строг к рекламе, но лоялен к шуткам.</i>\n\n"
        "Отправьте ваш промпт:"
    )
    
    await message.answer(text, parse_mode="HTML")


@main_dp.message(BotRegistration.waiting_for_gemini_prompt)
async def process_registration_gemini_prompt(message: types.Message, state: FSMContext):
    """Обработка промпта и завершение регистрации"""
    prompt = message.text
    await state.update_data(gemini_prompt=prompt)
    
    # Завершаем создание бота
    await create_bot_final(message, state, user_id=message.from_user.id)
    await state.clear()




# ========== НАСТРОЙКИ ==========
@main_dp.message(F.text == "⚙️ Настройки моего бота")
async def show_settings(message: types.Message):
    """Показать настройки бота"""
    user_id = message.from_user.id
    sub_bot = await db.get_sub_bot_by_owner(user_id)
    
    if not sub_bot:
        await message.answer("❌ У вас нет активного бота!", reply_markup=get_main_menu(False))
        return
    
    await message.answer(
        f"⚙️ <b>Настройки бота @{sub_bot['bot_username']}</b>\n\n"
        f"Админ-чат: {sub_bot['admin_chat_id']}\n"
        f"Канал: {sub_bot['channel_id']}\n\n"
        "Выберите действие:",
        reply_markup=get_settings_menu(),
        parse_mode="HTML"
    )


@main_dp.message(F.text == "🔙 Главное меню")
async def back_to_main(message: types.Message):
    """Вернуться в главное меню"""
    user_id = message.from_user.id
    sub_bot = await db.get_sub_bot_by_owner(user_id)
    is_owner = sub_bot is not None
    
    await message.answer("Главное меню:", reply_markup=get_main_menu(is_owner))


@main_dp.message(F.text == "📊 Статистика")
async def show_statistics(message: types.Message):
    """Показать статистику под-бота"""
    user_id = message.from_user.id
    sub_bot = await db.get_sub_bot_by_owner(user_id)
    
    if not sub_bot:
        await message.answer("❌ У вас нет активного бота!", reply_markup=get_main_menu(False))
        return
    
    stats = await db.get_sub_bot_statistics(sub_bot['id'])
    
    await message.answer(
        f"📊 <b>Статистика бота @{sub_bot['bot_username']}</b>\n\n"
        f"👥 Пользователей: {stats['users']}\n"
        f"💬 Сообщений: {stats['messages']}\n"
        f"✅ Опубликовано: {stats['published']}\n",
        parse_mode="HTML"
    )


@main_dp.message(F.text == "📊 Статистика бота")
async def show_statistics_alt(message: types.Message):
    """Показать статистику под-бота (альтернативная кнопка)"""
    await show_statistics(message)


# ========== РАССЫЛКА ДЛЯ ВЛАДЕЛЬЦА ПОД-БОТА ==========
@main_dp.message(F.text == "📢 Рассылка")
async def global_broadcast_start(message: types.Message, state: FSMContext):
    """Начало рассылки для владельца конструктора"""
    user_id = message.from_user.id
    
    # Если это владелец конструктора - запускаем глобальную рассылку
    if user_id == ADMIN_ID:
        await message.answer(
            "📢 <b>Рассылка</b>\n\n"
            "Отправьте сообщение для рассылки:",
            parse_mode="HTML"
        )
        await state.set_state(Broadcast.waiting_for_message)
        return
    
    # Для владельцев под-ботов - рассылка своим пользователям
    sub_bot = await db.get_sub_bot_by_owner(user_id)
    
    if not sub_bot:
        await message.answer("❌ У вас нет активного бота!", reply_markup=get_main_menu(False))
        return
    
    await message.answer(
        "📢 <b>Рассылка пользователям вашего бота</b>\n\n"
        "Отправьте сообщение для рассылки:",
        parse_mode="HTML"
    )
    await state.set_state(SubBotBroadcast.waiting_for_message)


@main_dp.message(F.text == "📢 Рассылка пользователям")
async def sub_bot_broadcast_start(message: types.Message, state: FSMContext):
    """Начало рассылки для владельца под-бота"""
    user_id = message.from_user.id
    sub_bot = await db.get_sub_bot_by_owner(user_id)
    
    if not sub_bot:
        await message.answer("❌ У вас нет активного бота!", reply_markup=get_main_menu(False))
        return
    
    await message.answer(
        "📢 <b>Рассылка пользователям вашего бота</b>\n\n"
        "Отправьте сообщение для рассылки:",
        parse_mode="HTML"
    )
    await state.set_state(SubBotBroadcast.waiting_for_message)


@main_dp.message(SubBotBroadcast.waiting_for_message)
async def sub_bot_broadcast_confirm(message: types.Message, state: FSMContext):
    """Получение сообщения для рассылки - показываем подтверждение"""
    user_id = message.from_user.id
    sub_bot = await db.get_sub_bot_by_owner(user_id)
    
    if not sub_bot:
        await message.answer("❌ У вас нет активного бота!")
        await state.clear()
        return
    
    # Получаем всех пользователей
    users = await db.get_all_users_of_sub_bot(sub_bot['id'])
    active_users = [u for u in users if not u['is_blocked']]
    
    if not active_users:
        await message.answer("❌ У вашего бота нет активных пользователей для рассылки.")
        await state.clear()
        return
    
    # Сохраняем данные для рассылки
    await state.update_data(
        broadcast_message_id=message.message_id,
        broadcast_chat_id=message.chat.id,
        sub_bot_id=sub_bot['id']
    )
    
    # Формируем превью сообщения
    preview_text = "📋 <b>Превью сообщения для рассылки:</b>\n\n"
    
    # Показываем сообщение для превью
    try:
        # Копируем сообщение для превью
        preview_msg = await main_bot.copy_message(
            chat_id=message.chat.id,
            from_chat_id=message.chat.id,
            message_id=message.message_id
        )
        preview_text += "⬆️ <i>Сообщение выше</i>\n\n"
    except:
        # Если не удалось скопировать, показываем текст
        if message.text:
            text_preview = message.text[:300]
            if len(message.text) > 300:
                text_preview += "..."
            preview_text += f"<code>{text_preview}</code>\n\n"
        elif message.caption:
            caption_preview = message.caption[:300]
            if len(message.caption) > 300:
                caption_preview += "..."
            preview_text += f"<code>{caption_preview}</code>\n\n"
        else:
            preview_text += f"📎 <i>{message.content_type}</i>\n\n"
    
    preview_text += f"📊 <b>Получателей:</b> {len(active_users)}\n\n"
    preview_text += "❓ <b>Точно разослать это сообщение?</b>"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, разослать", callback_data="subbot_broadcast_confirm_yes"),
            InlineKeyboardButton(text="❌ Нет, отменить", callback_data="subbot_broadcast_confirm_no")
        ]
    ])
    
    await message.answer(preview_text, reply_markup=keyboard, parse_mode="HTML")
    await state.set_state(SubBotBroadcast.waiting_for_confirm)


@main_dp.callback_query(F.data == "subbot_broadcast_confirm_yes")
async def sub_bot_broadcast_execute(callback: types.CallbackQuery, state: FSMContext):
    """Выполнение рассылки после подтверждения"""
    user_id = callback.from_user.id
    data = await state.get_data()
    sub_bot_id = data.get('sub_bot_id')
    message_id = data.get('broadcast_message_id')
    chat_id = data.get('broadcast_chat_id')
    
    if not sub_bot_id or not message_id or not chat_id:
        await callback.answer("❌ Ошибка: данные не найдены")
        await state.clear()
        return
    
    sub_bot_data = await db.get_sub_bot_by_id(sub_bot_id)
    if not sub_bot_data or sub_bot_data['owner_id'] != user_id:
        await callback.answer("❌ Нет доступа")
        await state.clear()
        return
    
    # Получаем экземпляр под-бота
    sub_bot_instance = sub_bot_manager.bots.get(sub_bot_id)
    temp_bot_created = False
    
    if not sub_bot_instance:
        try:
            from aiogram import Bot
            sub_bot_instance = Bot(token=sub_bot_data['bot_token'])
            temp_bot_created = True
        except Exception as e:
            logger.error(f"Не удалось создать экземпляр бота: {e}")
            await callback.answer("❌ Ошибка доступа к боту")
            return

    # Получаем всех пользователей
    users = await db.get_all_users_of_sub_bot(sub_bot_id)
    
    success = 0
    failed = 0
    
    await callback.message.edit_text("📤 Отправка рассылки...")
    
    try:
        # Получаем message object через forward (чтобы получить file_id и т.д.)
        forwarded = await main_bot.forward_message(
            chat_id=chat_id,
            from_chat_id=chat_id,
            message_id=message_id
        )
        original_message = forwarded
        
        # Удаляем пересланное сообщение
        try:
            await main_bot.delete_message(chat_id=chat_id, message_id=forwarded.message_id)
        except:
            pass
        
        # Подготовка контента
        content_type = None
        text_content = original_message.text or original_message.caption
        media_file = None
        media_filename = "file"
        
        if original_message.text:
            content_type = "text"
        elif original_message.photo:
            content_type = "photo"
            file_id = original_message.photo[-1].file_id
            media_filename = "image.jpg"
        elif original_message.video:
            content_type = "video"
            file_id = original_message.video.file_id
            media_filename = "video.mp4"
        elif original_message.document:
            content_type = "document"
            file_id = original_message.document.file_id
            media_filename = original_message.document.file_name or "document"
        elif original_message.audio:
            content_type = "audio"
            file_id = original_message.audio.file_id
            media_filename = original_message.audio.file_name or "audio.mp3"
        elif original_message.voice:
            content_type = "voice"
            file_id = original_message.voice.file_id
            media_filename = "voice.ogg"
        elif original_message.video_note:
            content_type = "video_note"
            file_id = original_message.video_note.file_id
            media_filename = "video_note.mp4"
        elif original_message.sticker:
            content_type = "sticker"
            file_id = original_message.sticker.file_id
        elif original_message.animation:
            content_type = "animation"
            file_id = original_message.animation.file_id
            media_filename = "animation.gif"
            
        # Скачиваем медиа если нужно
        if content_type and content_type != "text":
            file = await main_bot.get_file(file_id)
            from io import BytesIO
            media_io = BytesIO()
            await main_bot.download_file(file.file_path, media_io)
            media_io.seek(0)
            media_file = media_io.read() # Read as bytes
        
        # Рассылка
        from aiogram.types import BufferedInputFile
        
        for user in users:
            if user.get('is_blocked', False):
                continue
            
            try:
                if content_type == "text":
                    await sub_bot_instance.send_message(
                        chat_id=user['user_id'],
                        text=text_content,
                        entities=original_message.entities,
                        parse_mode=None 
                    )
                else:
                    input_file = BufferedInputFile(media_file, filename=media_filename)
                    
                    if content_type == "photo":
                        await sub_bot_instance.send_photo(chat_id=user['user_id'], photo=input_file, caption=text_content, caption_entities=original_message.caption_entities)
                    elif content_type == "video":
                        await sub_bot_instance.send_video(chat_id=user['user_id'], video=input_file, caption=text_content, caption_entities=original_message.caption_entities)
                    elif content_type == "document":
                        await sub_bot_instance.send_document(chat_id=user['user_id'], document=input_file, caption=text_content, caption_entities=original_message.caption_entities)
                    elif content_type == "audio":
                        await sub_bot_instance.send_audio(chat_id=user['user_id'], audio=input_file, caption=text_content, caption_entities=original_message.caption_entities)
                    elif content_type == "voice":
                        await sub_bot_instance.send_voice(chat_id=user['user_id'], voice=input_file, caption=text_content, caption_entities=original_message.caption_entities)
                    elif content_type == "video_note":
                        await sub_bot_instance.send_video_note(chat_id=user['user_id'], video_note=input_file)
                    elif content_type == "sticker":
                        await sub_bot_instance.send_sticker(chat_id=user['user_id'], sticker=input_file)
                    elif content_type == "animation":
                        await sub_bot_instance.send_animation(chat_id=user['user_id'], animation=input_file, caption=text_content, caption_entities=original_message.caption_entities)
                
                success += 1
            except Exception as e:
                logger.error(f"Ошибка отправки {user['user_id']}: {e}")
                failed += 1
            
            await asyncio.sleep(0.05)

    except Exception as e:
        logger.error(f"Ошибка подготовки рассылки: {e}")
        await callback.message.edit_text(f"❌ Ошибка подготовки: {e}")
    finally:
        if temp_bot_created and sub_bot_instance:
            await sub_bot_instance.session.close()
    
    await callback.message.edit_text(
        f"✅ <b>Рассылка завершена!</b>\n\n"
        f"✅ Успешно: {success}\n"
        f"❌ Ошибок: {failed}",
        parse_mode="HTML"
    )
    await state.clear()


@main_dp.callback_query(F.data == "subbot_broadcast_confirm_no")
async def sub_bot_broadcast_cancel(callback: types.CallbackQuery, state: FSMContext):
    """Отмена рассылки"""
    await callback.message.edit_text("❌ Рассылка отменена")
    await state.clear()
    await callback.answer()


# ========== БЛОКИРОВКА ПОЛЬЗОВАТЕЛЕЙ ==========
@main_dp.message(F.text == "🚫 Блокировки")
async def show_block_menu(message: types.Message):
    """Показать меню блокировок"""
    user_id = message.from_user.id
    sub_bot = await db.get_sub_bot_by_owner(user_id)
    
    if not sub_bot:
        await message.answer("❌ У вас нет активного бота!", reply_markup=get_main_menu(False))
        return
    
    await message.answer(
        "🚫 <b>Блокировка пользователей</b>\n\n"
        "Для блокировки пользователя используйте команду /block USER_ID в админ-чате вашего бота.\n\n"
        "Для разблокировки: /unblock USER_ID\n\n"
        "USER_ID можно найти в сообщениях от пользователей в админ-чате.",
        parse_mode="HTML"
    )


# ========== ПОМОЩЬ ==========
@main_dp.message(F.text == "📋 Мои боты")
async def show_my_bots(message: types.Message):
    """Показать список ботов пользователя с кнопками управления"""
    await show_my_bots_page(message, page=0)


async def show_my_bots_page(message_or_callback, page: int = 0, is_edit: bool = False):
    """Показать страницу списка ботов с пагинацией"""
    if hasattr(message_or_callback, 'from_user'):
        user_id = message_or_callback.from_user.id
    else:
        user_id = message_or_callback.message.from_user.id
    
    bots = await db.get_all_sub_bots_by_owner(user_id)
    
    logger.info(f"show_my_bots_page: user_id={user_id}, total_bots={len(bots)}")
    
    if not bots:
        text = "У вас пока нет ботов.\n\nНажмите \"🤖 Создать бот\" чтобы создать первого бота!"
        if is_edit:
            await message_or_callback.message.edit_text(text)
        else:
            await message_or_callback.answer(text, reply_markup=get_main_menu(False))
        return
    
    # Пагинация: 5 ботов на страницу
    bots_per_page = 5
    total_bots = len(bots)
    total_pages = (total_bots + bots_per_page - 1) // bots_per_page
    
    if page < 0:
        page = 0
    if page >= total_pages:
        page = total_pages - 1
    
    start_idx = page * bots_per_page
    end_idx = min(start_idx + bots_per_page, total_bots)
    bots_on_page = bots[start_idx:end_idx]
    
    logger.info(f"show_my_bots_page: page={page}, showing bots {start_idx}-{end_idx} of {total_bots}")
    
    text = f"📋 <b>Ваши боты</b> ({page + 1}/{total_pages}, всего: {total_bots}):\n\n"
    buttons = []
    
    for bot_data in bots_on_page:
        bot_username = bot_data.get('bot_username') or f"bot_{bot_data['id']}"
        status_admin = "✅" if bot_data.get('admin_chat_id') else "❌"
        status_channel = "✅" if bot_data.get('channel_id') else "❌"
        status = "✅" if (bot_data.get('admin_chat_id') and bot_data.get('channel_id')) else "⚠️"
        
        text += f"🤖 <b>@{bot_username}</b> {status}\n"
        text += f"   Админ: {status_admin} | Канал: {status_channel}\n\n"
        
        buttons.append([
            InlineKeyboardButton(
                text=f"⚙️ @{bot_username}",
                callback_data=f"subbot_settings_{bot_data['id']}"
            )
        ])
    
    # Кнопки пагинации
    if total_pages > 1:
        nav_buttons = []
        if page > 0:
            nav_buttons.append(InlineKeyboardButton(text="◀️", callback_data=f"my_bots_page_{page - 1}"))
        nav_buttons.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop"))
        if page < total_pages - 1:
            nav_buttons.append(InlineKeyboardButton(text="▶️", callback_data=f"my_bots_page_{page + 1}"))
        buttons.append(nav_buttons)
    
    text += "💡 Выберите бота для управления настройками."
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    
    if is_edit:
        await message_or_callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    else:
        await message_or_callback.answer(text, parse_mode="HTML", reply_markup=keyboard)


@main_dp.callback_query(F.data.startswith("my_bots_page_"))
async def handle_my_bots_page(callback: types.CallbackQuery):
    """Обработка пагинации списка ботов"""
    try:
        page = int(callback.data.split("_")[-1])
    except (ValueError, IndexError):
        page = 0
    
    await show_my_bots_page(callback, page=page, is_edit=True)
    await callback.answer()


@main_dp.message(F.text == "ℹ️ Помощь")
async def show_help(message: types.Message):
    """Показать помощь"""
    help_text = (
        "ℹ️ <b>Помощь</b>\n\n"
        "Этот конструктор позволяет создать бота для приема предложений от пользователей.\n\n"
        "<b>Как это работает:</b>\n"
        "1. Создаете бота через @BotFather\n"
        "2. Регистрируете его в конструкторе (только токен)\n"
        "3. Добавляете бота в группу как администратора\n"
        "4. Добавляете бота в канал как администратора\n"
        "5. Готово! Бот автоматически настроится\n\n"
        "<b>Возможности:</b>\n"
        "- Несколько ботов на одного владельца\n"
        "- Анонимные и неанонимные посты\n"
        "- Ручная модерация через кнопки\n"
        "- Блокировка пользователей\n"
        "- Рассылка среди пользователей\n"
        "- Статистика"
    )
    await message.answer(help_text, parse_mode="HTML")


# ========== АДМИНСКИЕ ФУНКЦИИ ==========
@main_dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_start(callback: types.CallbackQuery, state: FSMContext):
    """Начало рассылки от админа"""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Нет доступа")
        return
    
    await callback.message.answer(
        "📢 <b>Рассылка</b>\n\n"
        "Отправьте сообщение для рассылки:",
        parse_mode="HTML"
    )
    await state.set_state(Broadcast.waiting_for_message)
    await callback.answer()


@main_dp.message(Broadcast.waiting_for_message)
async def admin_broadcast_message(message: types.Message, state: FSMContext):
    """Получение сообщения для рассылки"""
    if message.from_user.id != ADMIN_ID:
        return
    
    # Сохраняем chat_id и message_id для рассылки
    await state.update_data(
        broadcast_message_id=message.message_id,
        broadcast_chat_id=message.chat.id
    )
    logger.info(f"Сохранены данные сообщения: message_id={message.message_id}, chat_id={message.chat.id}")
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Рекламная рассылка", callback_data="broadcast_type_ads")],
        [InlineKeyboardButton(text="🔔 Рассылка уведомлений", callback_data="broadcast_type_notifications")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="broadcast_cancel")]
    ])
    
    await message.answer(
        "Выберите тип рассылки:\n\n"
        "📢 <b>Рекламная рассылка</b> - только в каналы, согласившиеся на рекламу\n"
        "🔔 <b>Рассылка уведомлений</b> - только в каналы, согласившиеся на уведомления",
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    await state.set_state(Broadcast.waiting_for_type)


@main_dp.callback_query(F.data.in_(["broadcast_type_ads", "broadcast_type_notifications"]))
async def admin_broadcast_select_type(callback: types.CallbackQuery, state: FSMContext):
    """Выбор типа рассылки (реклама/уведомление)"""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Нет доступа")
        return
    
    if callback.data == "broadcast_cancel":
        await callback.message.edit_text("❌ Рассылка отменена")
        await state.clear()
        return
    
    # Сохраняем тип рассылки
    broadcast_type = "ads" if callback.data == "broadcast_type_ads" else "notifications"
    # Сохраняем тип рассылки и убеждаемся, что предыдущие данные не потеряны
    current_data = await state.get_data()
    current_data['broadcast_type'] = broadcast_type
    await state.set_data(current_data)
    logger.info(f"Сохранен тип рассылки: {broadcast_type}, данные: {current_data}")
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Только пользователи", callback_data="broadcast_audience_users")],
        [InlineKeyboardButton(text="📢 Только каналы", callback_data="broadcast_audience_channels")],
        [InlineKeyboardButton(text="🌐 Пользователи и каналы", callback_data="broadcast_audience_both")],
        [InlineKeyboardButton(text="🤖 Всем пользователям под-ботов", callback_data="broadcast_audience_sub_bot_users")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="broadcast_cancel")]
    ])
    
    type_text = "рекламную" if broadcast_type == "ads" else "уведомлений"
    await callback.message.edit_text(
        f"Вы выбрали <b>{type_text}</b> рассылку.\n\n"
        "Выберите аудиторию:",
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    await callback.answer()
    await state.set_state(Broadcast.waiting_for_audience)


@main_dp.callback_query(F.data.in_(["broadcast_audience_users", "broadcast_audience_channels", "broadcast_audience_both", "broadcast_audience_sub_bot_users", "broadcast_cancel"]))
async def admin_broadcast_execute(callback: types.CallbackQuery, state: FSMContext):
    """Выполнение рассылки"""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Нет доступа")
        return
    
    if callback.data == "broadcast_cancel":
        await callback.message.edit_text("❌ Рассылка отменена")
        await state.clear()
        return
    
    data = await state.get_data()
    message_id = data.get('broadcast_message_id')
    chat_id = data.get('broadcast_chat_id')
    broadcast_type = data.get('broadcast_type')  # "ads" или "notifications"
    
    # Логируем данные для отладки
    logger.info(f"Данные рассылки: message_id={message_id}, chat_id={chat_id}, broadcast_type={broadcast_type}")
    logger.info(f"Все данные state: {data}")
    
    if not message_id or not chat_id or not broadcast_type:
        error_msg = f"❌ Ошибка: данные не найдены. message_id={message_id}, chat_id={chat_id}, broadcast_type={broadcast_type}"
        logger.error(error_msg)
        await callback.answer(error_msg, show_alert=True)
        await state.clear()
        return
    
    success = 0
    failed = 0
    
    await callback.message.edit_text("📤 Отправка рассылки...")
    
    # Определяем подпись в зависимости от типа рассылки
    footer_text = "\n\n<i>#реклама</i>" if broadcast_type == "ads" else "\n\n<i>#уведомление</i>"
    
    # Получаем оригинальное сообщение для извлечения контента
    original_message = None
    try:
        # Пробуем получить сообщение через forward в админ-чат
        forwarded = await main_bot.forward_message(
            chat_id=ADMIN_ID,
            from_chat_id=chat_id,
            message_id=message_id
        )
        original_message = forwarded
        # Удаляем пересланное сообщение
        try:
            await main_bot.delete_message(chat_id=ADMIN_ID, message_id=forwarded.message_id)
        except:
            pass
    except Exception as e:
        logger.error(f"Ошибка получения оригинального сообщения: {e}")
        original_message = None
    
    # Функция для отправки сообщения с подписью БЕЗ использования админ-чата
    async def send_with_footer(target_chat_id, target_bot=None, admin_chat_id=None):
        """Отправляет сообщение с подписью в одном сообщении через под-бота БЕЗ админ-чата"""
        bot_to_use = target_bot or main_bot
        
        try:
            # Если это текстовое сообщение - просто отправляем текст с footer
            if original_message and original_message.text:
                full_text = original_message.text + footer_text
                sent_msg = await bot_to_use.send_message(
                    chat_id=target_chat_id,
                    text=full_text,
                    parse_mode="HTML",
                    entities=original_message.entities
                )
                return True
            
            # Если это медиа - скачиваем и загружаем заново через под-бота
            elif original_message:
                file_to_download = None
                media_type = None
                
                # Определяем тип медиа и получаем file_id
                if original_message.photo:
                    file_to_download = original_message.photo[-1].file_id  # Самое большое фото
                    media_type = "photo"
                elif original_message.video:
                    file_to_download = original_message.video.file_id
                    media_type = "video"
                elif original_message.document:
                    file_to_download = original_message.document.file_id
                    media_type = "document"
                elif original_message.audio:
                    file_to_download = original_message.audio.file_id
                    media_type = "audio"
                elif original_message.animation:
                    file_to_download = original_message.animation.file_id
                    media_type = "animation"
                elif original_message.voice:
                    file_to_download = original_message.voice.file_id
                    media_type = "voice"
                elif original_message.video_note:
                    file_to_download = original_message.video_note.file_id
                    media_type = "video_note"
                elif original_message.sticker:
                    file_to_download = original_message.sticker.file_id
                    media_type = "sticker"
                
                if file_to_download and media_type:
                    logger.info(f"Скачиваем медиа типа {media_type} для отправки через под-бота")
                    
                    # Скачиваем файл через главного бота
                    file = await main_bot.get_file(file_to_download)
                    file_path = file.file_path
                    
                    # Скачиваем в память
                    from io import BytesIO
                    file_content = BytesIO()
                    await main_bot.download_file(file_path, file_content)
                    file_content.seek(0)
                    
                    # Формируем caption с footer
                    caption = (original_message.caption or "") + footer_text if media_type not in ['sticker', 'video_note'] else None
                    
                    # Отправляем через под-бота
                    from aiogram.types import BufferedInputFile
                    input_file = BufferedInputFile(file_content.read(), filename=f"file.{media_type}")
                    
                    if media_type == "photo":
                        sent_msg = await bot_to_use.send_photo(
                            chat_id=target_chat_id,
                            photo=input_file,
                            caption=caption,
                            parse_mode="HTML" if caption else None,
                            caption_entities=original_message.caption_entities if caption else None
                        )
                    elif media_type == "video":
                        sent_msg = await bot_to_use.send_video(
                            chat_id=target_chat_id,
                            video=input_file,
                            caption=caption,
                            parse_mode="HTML" if caption else None,
                            caption_entities=original_message.caption_entities if caption else None
                        )
                    elif media_type == "document":
                        sent_msg = await bot_to_use.send_document(
                            chat_id=target_chat_id,
                            document=input_file,
                            caption=caption,
                            parse_mode="HTML" if caption else None,
                            caption_entities=original_message.caption_entities if caption else None
                        )
                    elif media_type == "audio":
                        sent_msg = await bot_to_use.send_audio(
                            chat_id=target_chat_id,
                            audio=input_file,
                            caption=caption,
                            parse_mode="HTML" if caption else None,
                            caption_entities=original_message.caption_entities if caption else None
                        )
                    elif media_type == "animation":
                        sent_msg = await bot_to_use.send_animation(
                            chat_id=target_chat_id,
                            animation=input_file,
                            caption=caption,
                            parse_mode="HTML" if caption else None,
                            caption_entities=original_message.caption_entities if caption else None
                        )
                    elif media_type == "voice":
                        sent_msg = await bot_to_use.send_voice(
                            chat_id=target_chat_id,
                            voice=input_file
                        )
                        # Для voice отправляем footer отдельно
                        if footer_text:
                            await bot_to_use.send_message(
                                chat_id=target_chat_id,
                                text=footer_text,
                                parse_mode="HTML",
                                reply_to_message_id=sent_msg.message_id
                            )
                    elif media_type == "video_note":
                        sent_msg = await bot_to_use.send_video_note(
                            chat_id=target_chat_id,
                            video_note=input_file
                        )
                        # Для video_note отправляем footer отдельно
                        if footer_text:
                            await bot_to_use.send_message(
                                chat_id=target_chat_id,
                                text=footer_text,
                                parse_mode="HTML",
                                reply_to_message_id=sent_msg.message_id
                            )
                    elif media_type == "sticker":
                        sent_msg = await bot_to_use.send_sticker(
                            chat_id=target_chat_id,
                            sticker=input_file
                        )
                        # Для sticker отправляем footer отдельно
                        if footer_text:
                            await bot_to_use.send_message(
                                chat_id=target_chat_id,
                                text=footer_text,
                                parse_mode="HTML",
                                reply_to_message_id=sent_msg.message_id
                            )
                    
                    logger.info(f"✅ Медиа успешно отправлено через под-бота в {target_chat_id}")
                    return True
                else:
                    logger.warning(f"Неизвестный тип сообщения для рассылки")
                    return False
            
            else:
                logger.warning(f"Нет данных для отправки")
                return False
                
        except Exception as e:
            logger.error(f"Ошибка отправки в {target_chat_id}: {e}", exc_info=True)
            return False
    
    audience = callback.data.split("_")[-1]  # "users", "channels", "both"
    
    # Рассылка пользователям
    if audience in ["users", "both"]:
        # Получаем всех пользователей (кто хоть раз запускал под-бот)
        all_users = await db.get_all_users()
        # Получаем всех владельцев под-ботов
        sub_bots = await db.get_all_sub_bots()
        owner_ids = set(sub_bot['owner_id'] for sub_bot in sub_bots)
        
        # Собираем уникальный список пользователей (обычные пользователи + владельцы)
        user_ids_to_send = set()
        for user in all_users:
            if not user.get('is_blocked', False):
                user_ids_to_send.add(user['user_id'])
        # Добавляем владельцев под-ботов
        for owner_id in owner_ids:
            user_ids_to_send.add(owner_id)
        
        # Рассылаем всем
        for user_id in user_ids_to_send:
            if await send_with_footer(user_id):
                success += 1
            else:
                failed += 1
            await asyncio.sleep(0.05)
    
    # Рассылка в каналы - через под-ботов
    if audience in ["channels", "both"]:
        # Получаем все под-боты
        all_sub_bots = await db.get_all_sub_bots()
        logger.info(f"Всего под-ботов в базе: {len(all_sub_bots)}")
        
        # Фильтруем под-боты по типу рассылки и наличию канала
        filtered_sub_bots = []
        for sub_bot in all_sub_bots:
            sub_bot_id = sub_bot.get('id')
            logger.info(f"Проверяем под-бот {sub_bot_id}: channel_id={sub_bot.get('channel_id')}, allow_ads={sub_bot.get('allow_ads')}, allow_notifications={sub_bot.get('allow_notifications')}")
            
            # Проверяем наличие канала
            if not sub_bot.get('channel_id'):
                logger.warning(f"Под-бот {sub_bot_id} пропущен: нет channel_id")
                continue
            
            # Проверяем тип рассылки
            if broadcast_type == "ads":
                # Для рекламы проверяем allow_ads (по умолчанию True если не установлено)
                allow_ads = sub_bot.get('allow_ads')
                if allow_ads is None:
                    allow_ads = True  # По умолчанию разрешаем
                if not allow_ads:
                    logger.warning(f"Под-бот {sub_bot_id} пропущен: allow_ads=False")
                    continue
            elif broadcast_type == "notifications":
                # Для уведомлений проверяем allow_notifications (по умолчанию True если не установлено)
                allow_notifications = sub_bot.get('allow_notifications')
                if allow_notifications is None:
                    allow_notifications = True  # По умолчанию разрешаем
                if not allow_notifications:
                    logger.warning(f"Под-бот {sub_bot_id} пропущен: allow_notifications=False")
                    continue
            
            filtered_sub_bots.append(sub_bot)
            logger.info(f"✅ Под-бот {sub_bot_id} добавлен в список для рассылки")
        
        logger.info(f"Найдено каналов для рассылки: {len(filtered_sub_bots)} из {len(all_sub_bots)}")
        
        # Рассылаем в каждый канал через под-ботов
        for sub_bot in filtered_sub_bots:
            channel_id = sub_bot['channel_id']
            sub_bot_id = sub_bot['id']
            admin_chat_id = sub_bot.get('admin_chat_id')
            
            logger.info(f"Начинаем рассылку в канал {channel_id} через под-бота {sub_bot_id}, admin_chat_id={admin_chat_id}")
            
            try:
                # Получаем запущенный экземпляр под-бота
                sub_bot_instance = sub_bot_manager.bots.get(sub_bot_id)
                
                if not sub_bot_instance:
                    # Если под-бот не запущен - создаем временный экземпляр
                    from aiogram import Bot
                    sub_bot_instance = Bot(token=sub_bot['bot_token'])
                    logger.info(f"Создан временный экземпляр бота для под-бота {sub_bot_id}")
                    temp_bot_created = True
                else:
                    logger.info(f"Используем запущенный под-бот {sub_bot_id}")
                    temp_bot_created = False
                
                # Отправляем через функцию send_with_footer
                # Теперь она работает БЕЗ админ-чата - скачивает и загружает контент заново
                try:
                    if await send_with_footer(channel_id, sub_bot_instance, admin_chat_id):
                        success += 1
                        logger.info(f"✅ Успешно отправлено в канал {channel_id} через под-бота {sub_bot_id}")
                    else:
                        failed += 1
                        logger.error(f"❌ send_with_footer вернул False для канала {channel_id}")
                except Exception as send_error:
                    failed += 1
                    logger.error(f"❌ Ошибка отправки в канал {channel_id}: {send_error}", exc_info=True)
                
                # Закрываем временный бот если был создан
                if temp_bot_created:
                    try:
                        await sub_bot_instance.session.close()
                        logger.info(f"Закрыт временный бот для под-бота {sub_bot_id}")
                    except Exception as close_error:
                        logger.warning(f"Ошибка закрытия временного бота: {close_error}")
                        
            except Exception as e:
                failed += 1
                logger.error(f"❌ Критическая ошибка рассылки в канал {channel_id}: {e}", exc_info=True)
            
            # Небольшая задержка между каналами
            await asyncio.sleep(0.15)
    
    # Рассылка всем пользователям под-ботов
    if audience == "sub_bot_users":
        # Получаем все под-боты
        all_sub_bots = await db.get_all_sub_bots()
        logger.info(f"Начинаем рассылку всем пользователям под-ботов. Всего под-ботов: {len(all_sub_bots)}")
        
        for sub_bot_data in all_sub_bots:
            sub_bot_id = sub_bot_data['id']
            bot_token = sub_bot_data['bot_token']
            admin_chat_id = sub_bot_data.get('admin_chat_id')
            
            # Получаем всех пользователей этого под-бота
            users = await db.get_all_users_of_sub_bot(sub_bot_id)
            active_users = [u for u in users if not u.get('is_blocked', False)]
            
            if not active_users:
                logger.info(f"У под-бота {sub_bot_id} нет активных пользователей, пропускаем")
                continue
            
            logger.info(f"Под-бот {sub_bot_id}: найдено {len(active_users)} активных пользователей")
            
            # Создаем экземпляр под-бота для рассылки
            sub_bot_instance = None
            temp_bot_created = False
            try:
                from aiogram import Bot
                sub_bot_instance = Bot(token=bot_token)
                temp_bot_created = True
                
                # Отправляем каждому пользователю через под-бота
                for user in active_users:
                    user_id = user['user_id']
                    try:
                        if await send_with_footer(user_id, sub_bot_instance, admin_chat_id):
                            success += 1
                        else:
                            failed += 1
                        await asyncio.sleep(0.05)
                    except Exception as e:
                        failed += 1
                        logger.error(f"Ошибка отправки пользователю {user_id} под-бота {sub_bot_id}: {e}")
                        await asyncio.sleep(0.05)
                
                # Закрываем временный бот
                if temp_bot_created:
                    try:
                        await sub_bot_instance.session.close()
                    except Exception as close_error:
                        logger.warning(f"Ошибка закрытия временного бота: {close_error}")
                        
            except Exception as e:
                failed += len(active_users)
                logger.error(f"❌ Критическая ошибка рассылки пользователям под-бота {sub_bot_id}: {e}", exc_info=True)
                if temp_bot_created and sub_bot_instance:
                    try:
                        await sub_bot_instance.session.close()
                    except:
                        pass
            
            # Небольшая задержка между под-ботами
            await asyncio.sleep(0.1)
    
    await callback.message.edit_text(
        f"✅ Рассылка завершена!\n\n"
        f"✅ Успешно: {success}\n"
        f"❌ Ошибок: {failed}"
    )
    await state.clear()


@main_dp.callback_query(F.data == "admin_global_ban")
async def admin_global_ban_start(callback: types.CallbackQuery, state: FSMContext):
    """Начало глобальной блокировки пользователя"""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Нет доступа")
        return
    
    await callback.message.edit_text(
        "🚫 <b>Глобальная блокировка пользователя</b>\n\n"
        "Эта функция заблокирует пользователя во <b>всех</b> под-ботах и в конструкторе.\n\n"
        "Отправьте <b>USER_ID</b> (число) или <b>username</b> (например: @username) пользователя для блокировки:",
        parse_mode="HTML"
    )
    await state.set_state(GlobalBan.waiting_for_user_id)
    await callback.answer()


@main_dp.message(GlobalBan.waiting_for_user_id)
async def admin_global_ban_execute(message: types.Message, state: FSMContext):
    """Выполнение глобальной блокировки"""
    if message.from_user.id != ADMIN_ID:
        return
    
    input_text = message.text.strip()
    user_id_to_ban = None
    user_info = ""
    
    # Пробуем определить, это ID или username
    try:
        # Пробуем как числовой ID
        user_id_to_ban = int(input_text)
        user_info = f"ID: {user_id_to_ban}"
    except ValueError:
        # Это не число, пробуем как username
        username = input_text.lstrip('@')
        user_id_to_ban = await db.get_user_id_by_username(username)
        
        if user_id_to_ban:
            user_info = f"@{username} (ID: {user_id_to_ban})"
        else:
            await message.answer(
                f"❌ Пользователь с username <b>@{username}</b> не найден в базе данных.\n\n"
                f"Отправьте числовой <b>USER_ID</b> или username пользователя, который взаимодействовал с под-ботами.",
                parse_mode="HTML"
            )
            return
    
    if not user_id_to_ban:
        await message.answer("❌ Не удалось определить пользователя. Отправьте числовой USER_ID или username (например: @username).")
        return
    
    if user_id_to_ban == ADMIN_ID:
        await message.answer("❌ Нельзя заблокировать администратора конструктора.")
        await state.clear()
        return
    
    await message.answer("🔄 Выполняется глобальная блокировка...")
    
    # Блокируем во всех под-ботах и конструкторе
    blocked_count = await db.block_user_globally(user_id_to_ban, banned_by=message.from_user.id)
    
    await message.answer(
        f"✅ <b>Пользователь {user_info} заблокирован глобально</b>\n\n"
        f"🚫 Заблокирован в {blocked_count} под-ботах\n"
        f"🚫 Заблокирован в конструкторе\n\n"
        f"Пользователь не сможет использовать ни один под-бот и конструктор.",
        parse_mode="HTML"
    )
    
    logger.info(f"Админ {message.from_user.id} выполнил глобальную блокировку пользователя {user_id_to_ban} ({user_info})")
    await state.clear()


@main_dp.callback_query(F.data == "admin_restart_bots")
async def admin_restart_bots(callback: types.CallbackQuery):
    """Перезапуск всех под-ботов"""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("❌ Нет доступа")
        return
    
    await callback.message.edit_text("🔄 Перезапуск ботов...")
    
    # Получаем все боты
    sub_bots = await db.get_all_sub_bots()
    
    failed = 0
    for sub_bot in sub_bots:
        try:
            await sub_bot_manager.restart_sub_bot(sub_bot['id'], sub_bot['bot_token'])
        except Exception as exc:
            failed += 1
            logger.error("Could not restart sub-bot %s (%s)", sub_bot['id'], type(exc).__name__)
    
    await callback.message.edit_text(
        f"✅ Перезапущено: {len(sub_bots) - failed} из {len(sub_bots)}. Ошибок: {failed}."
    )
    await callback.answer()


# ========== УПРАВЛЕНИЕ ПОД-БОТАМИ ==========
@main_dp.callback_query(F.data.startswith("subbot_settings_"))
async def show_sub_bot_settings(callback: types.CallbackQuery, state: FSMContext):
    """Показать меню настроек под-бота"""
    user_id = callback.from_user.id
    sub_bot_id = int(callback.data.split("_")[2])
    
    # Проверяем права доступа
    sub_bot_data = await db.get_sub_bot_by_id(sub_bot_id)
    if not sub_bot_data or sub_bot_data['owner_id'] != user_id:
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Рассылка пользователям", callback_data=f"subbot_broadcast_{sub_bot_id}")],
        [InlineKeyboardButton(text="📊 Статистика бота", callback_data=f"subbot_stats_{sub_bot_id}")],
        [InlineKeyboardButton(text="📝 Оформление сверху", callback_data=f"subbot_header_{sub_bot_id}")],
        [InlineKeyboardButton(text="📝 Оформление снизу", callback_data=f"subbot_footer_{sub_bot_id}")],
        [InlineKeyboardButton(text="💬 Изменить приветствие", callback_data=f"subbot_welcome_{sub_bot_id}")],
        [InlineKeyboardButton(text="🤖 Настройка AI-модерации", callback_data=f"subbot_moderation_{sub_bot_id}")],
        [InlineKeyboardButton(text="🗑 Удалить бота", callback_data=f"subbot_delete_{sub_bot_id}")],
        [InlineKeyboardButton(text="🔙 Назад к списку", callback_data="subbot_back_list")]
    ])
    
    status_admin = "✅" if sub_bot_data['admin_chat_id'] else "❌"
    status_channel = "✅" if sub_bot_data['channel_id'] else "❌"
    
    text = (
        f"⚙️ <b>Настройки бота @{sub_bot_data['bot_username']}</b>\n\n"
        f"Статус:\n"
        f"Админ-чат: {status_admin}\n"
        f"Канал: {status_channel}\n\n"
        f"Выберите действие:"
    )
    
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()


@main_dp.callback_query(F.data == "subbot_back_list")
async def back_to_bots_list(callback: types.CallbackQuery):
    """Вернуться к списку ботов"""
    await show_my_bots_page(callback, page=0, is_edit=True)
    await callback.answer()


@main_dp.callback_query(F.data.startswith("subbot_stats_"))
async def show_sub_bot_stats(callback: types.CallbackQuery):
    """Показать статистику под-бота"""
    # Отвечаем сразу, чтобы убрать загрузку
    await callback.answer()
    
    try:
        user_id = callback.from_user.id
        sub_bot_id = int(callback.data.split("_")[2])
        
        sub_bot_data = await db.get_sub_bot_by_id(sub_bot_id)
        if not sub_bot_data or sub_bot_data['owner_id'] != user_id:
            await callback.message.edit_text("❌ Нет доступа")
            return
        
        stats = await db.get_sub_bot_statistics(sub_bot_id)
        
        text = (
            f"📊 <b>Статистика бота @{sub_bot_data['bot_username']}</b>\n\n"
            f"👥 Всего пользователей: {stats['users']}\n"
            f"💬 Всего сообщений: {stats['messages']}\n"
            f"✅ Опубликовано: {stats['published']}\n"
            f"❌ Отклонено: {stats['rejected']}\n"
            f"⏳ На модерации: {stats['pending']}"
        )
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Назад", callback_data=f"subbot_settings_{sub_bot_id}")]
        ])
        
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    except Exception as e:
        logger.error(f"Ошибка при показе статистики: {e}", exc_info=True)
        try:
            await callback.message.edit_text("❌ Ошибка при получении статистики")
        except:
            pass


@main_dp.callback_query(F.data.startswith("subbot_broadcast_"))
async def start_sub_bot_broadcast(callback: types.CallbackQuery, state: FSMContext):
    """Начать рассылку для под-бота"""
    user_id = callback.from_user.id
    sub_bot_id = int(callback.data.split("_")[2])
    
    sub_bot_data = await db.get_sub_bot_by_id(sub_bot_id)
    if not sub_bot_data or sub_bot_data['owner_id'] != user_id:
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    
    users = await db.get_all_users_of_sub_bot(sub_bot_id)
    active_users = [u for u in users if not u['is_blocked']]
    
    if not active_users:
        await callback.answer("❌ Нет пользователей для рассылки", show_alert=True)
        return
    
    await state.update_data(sub_bot_id=sub_bot_id)
    await state.set_state(SubBotSettings.waiting_for_broadcast)
    
    text = (
        f"📢 <b>Рассылка для @{sub_bot_data['bot_username']}</b>\n\n"
        f"Отправьте сообщение, которое хотите разослать.\n\n"
        f"Количество получателей: <b>{len(active_users)}</b>\n\n"
        f"Можете отправить текст, фото, видео или документ."
    )
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"subbot_settings_{sub_bot_id}")]
    ])
    
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()


@main_dp.message(SubBotSettings.waiting_for_broadcast)
async def process_sub_bot_broadcast(message: types.Message, state: FSMContext):
    """Обработка рассылки под-бота - показываем подтверждение"""
    user_id = message.from_user.id
    data = await state.get_data()
    sub_bot_id = data.get('sub_bot_id')
    
    if not sub_bot_id:
        await state.clear()
        return
    
    sub_bot_data = await db.get_sub_bot_by_id(sub_bot_id)
    if not sub_bot_data or sub_bot_data['owner_id'] != user_id:
        await state.clear()
        return
    
    # Получаем всех пользователей
    users = await db.get_all_users_of_sub_bot(sub_bot_id)
    active_users = [u for u in users if not u.get('is_blocked', False)]
    
    if not active_users:
        await message.answer("❌ У вашего бота нет активных пользователей для рассылки.")
        await state.clear()
        return
    
    # Сохраняем данные для рассылки
    await state.update_data(
        broadcast_message_id=message.message_id,
        broadcast_chat_id=message.chat.id
    )
    
    # Формируем превью сообщения
    preview_text = "📋 <b>Превью сообщения для рассылки:</b>\n\n"
    
    # Показываем сообщение для превью
    try:
        # Копируем сообщение для превью
        preview_msg = await main_bot.copy_message(
            chat_id=message.chat.id,
            from_chat_id=message.chat.id,
            message_id=message.message_id
        )
        preview_text += "⬆️ <i>Сообщение выше</i>\n\n"
    except:
        # Если не удалось скопировать, показываем текст
        if message.text:
            text_preview = message.text[:300]
            if len(message.text) > 300:
                text_preview += "..."
            preview_text += f"<code>{text_preview}</code>\n\n"
        elif message.caption:
            caption_preview = message.caption[:300]
            if len(message.caption) > 300:
                caption_preview += "..."
            preview_text += f"<code>{caption_preview}</code>\n\n"
        else:
            preview_text += f"📎 <i>{message.content_type}</i>\n\n"
    
    preview_text += f"📊 <b>Получателей:</b> {len(active_users)}\n\n"
    preview_text += "❓ <b>Точно разослать это сообщение?</b>"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, разослать", callback_data="subbot_broadcast_confirm_yes"),
            InlineKeyboardButton(text="❌ Нет, отменить", callback_data="subbot_broadcast_confirm_no")
        ]
    ])
    
    await message.answer(preview_text, reply_markup=keyboard, parse_mode="HTML")
    await state.set_state(SubBotBroadcast.waiting_for_confirm)


@main_dp.callback_query(F.data.startswith("subbot_footer_"))
async def start_sub_bot_footer_change(callback: types.CallbackQuery, state: FSMContext):
    """Начать изменение оформления под-бота"""
    user_id = callback.from_user.id
    sub_bot_id = int(callback.data.split("_")[2])
    
    sub_bot_data = await db.get_sub_bot_by_id(sub_bot_id)
    if not sub_bot_data or sub_bot_data['owner_id'] != user_id:
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    
    current_footer = sub_bot_data.get('post_footer')
    footer_preview = f"<code>{current_footer}</code>" if current_footer else "❌ Не установлено"
    
    await state.update_data(sub_bot_id=sub_bot_id)
    await state.set_state(SubBotSettings.waiting_for_footer)
    
    text = (
        f"📝 <b>Изменение оформления для @{sub_bot_data['bot_username']}</b>\n\n"
        f"<b>Текущее оформление:</b>\n{footer_preview}\n\n"
        f"Отправьте новый текст оформления.\n\n"
        f"💡 Можно использовать HTML форматирование:\n"
        f"• <b>Жирный</b>, <i>курсив</i>, <code>моноширинный</code>\n"
        f"• <a href=\"https://example.com\">Ссылки</a> (без предпросмотра)\n\n"
        f"Оформление будет добавляться в самом низу каждого одобренного поста в канале."
    )
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Удалить оформление", callback_data=f"subbot_footer_remove_{sub_bot_id}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"subbot_settings_{sub_bot_id}")]
    ])
    
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()


@main_dp.message(SubBotSettings.waiting_for_footer)
async def process_sub_bot_footer(message: types.Message, state: FSMContext):
    """Обработка нового оформления под-бота"""
    user_id = message.from_user.id
    data = await state.get_data()
    sub_bot_id = data.get('sub_bot_id')
    
    if not sub_bot_id:
        await state.clear()
        return
    
    sub_bot_data = await db.get_sub_bot_by_id(sub_bot_id)
    if not sub_bot_data or sub_bot_data['owner_id'] != user_id:
        await state.clear()
        return
    
    # Используем HTML текст для поддержки форматирования
    footer_source = message.text or message.caption or ""
    footer_entities = message.entities or message.caption_entities
    new_footer = (
        entities_to_rich_html(footer_source, footer_entities)
        if footer_entities
        else footer_source
    )
    await db.update_post_footer(sub_bot_id, new_footer)
    
    text = (
        f"✅ <b>Оформление обновлено!</b>\n\n"
        f"<b>Новое оформление:</b>\n<code>{new_footer}</code>\n\n"
        f"Оно будет добавляться в самом низу каждого одобренного поста в канале."
    )
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data=f"subbot_settings_{sub_bot_id}")]
    ])
    
    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)
    await state.clear()


@main_dp.callback_query(F.data.startswith("subbot_header_change_mode_"))
async def change_header_mode(callback: types.CallbackQuery, state: FSMContext):
    """Изменить режим оформления сверху"""
    # КРИТИЧНО: Отвечаем сразу, чтобы убрать загрузку
    await callback.answer()
    
    try:
        user_id = callback.from_user.id
        # Парсим sub_bot_id из callback_data: subbot_header_change_mode_{id}
        parts = callback.data.split("_")
        if len(parts) < 5:
            logger.error(f"Неверный формат callback_data: {callback.data}")
            await callback.message.edit_text("❌ Ошибка: неверный формат данных")
            return
        
        sub_bot_id = int(parts[4])
        
        sub_bot_data = await db.get_sub_bot_by_id(sub_bot_id)
        if not sub_bot_data or sub_bot_data['owner_id'] != user_id:
            await callback.message.edit_text("❌ Нет доступа")
            return
        
        current_header = sub_bot_data.get('post_header')
        if not current_header:
            await callback.message.edit_text("❌ Оформление сверху не установлено")
            return
        
        current_mode = sub_bot_data.get('header_mode', 'newline')
        
        # Определяем противоположный режим
        new_mode = 'inline' if current_mode == 'newline' else 'newline'
        mode_text = "в одну строку с постом" if new_mode == 'inline' else "на отдельной строке"
        
        # Обновляем режим
        await db.update_post_header(sub_bot_id, current_header, new_mode)
        
        text = (
            f"✅ <b>Режим изменен!</b>\n\n"
            f"<b>Оформление:</b>\n<code>{current_header}</code>\n"
            f"<b>Новый режим:</b> {mode_text}\n\n"
        )
        
        if new_mode == 'inline':
            text += f"<b>Пример:</b>\n<code>{current_header}</code> <code>Текст поста...</code>"
        else:
            text += f"<b>Пример:</b>\n<code>{current_header}</code>\n\n<code>Текст поста...</code>"
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Изменить режим", callback_data=f"subbot_header_change_mode_{sub_bot_id}")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data=f"subbot_settings_{sub_bot_id}")]
        ])
        
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    except Exception as e:
        logger.error(f"Ошибка при изменении режима header: {e}", exc_info=True)
        try:
            await callback.message.edit_text(f"❌ Ошибка: {str(e)}")
        except:
            pass


@main_dp.callback_query(F.data.startswith("subbot_header_mode_"))
async def process_sub_bot_header_mode(callback: types.CallbackQuery, state: FSMContext):
    """Обработка выбора режима header"""
    # КРИТИЧНО: Отвечаем сразу
    await callback.answer()
    
    try:
        user_id = callback.from_user.id
        # Формат: subbot_header_mode_{mode}_{sub_bot_id}
        # Пример: subbot_header_mode_inline_123
        parts = callback.data.split("_")
        if len(parts) < 5:
            logger.error(f"Неверный формат callback_data: {callback.data}")
            await callback.message.edit_text("❌ Ошибка: неверный формат данных")
            return
        
        mode = parts[3]  # newline или inline (4-й элемент, индекс 3)
        sub_bot_id = int(parts[4])  # ID бота (5-й элемент, индекс 4)
        
        sub_bot_data = await db.get_sub_bot_by_id(sub_bot_id)
        if not sub_bot_data or sub_bot_data['owner_id'] != user_id:
            await callback.message.edit_text("❌ Нет доступа")
            return
        
        data = await state.get_data()
        new_header = data.get('new_header')
        
        if not new_header:
            await callback.message.edit_text("❌ Ошибка: текст оформления не найден")
            await state.clear()
            return
        
        # Сохраняем header и mode
        await db.update_post_header(sub_bot_id, new_header, mode)
        
        mode_text = "в одну строку с постом" if mode == 'inline' else "на отдельной строке"
        
        text = (
            f"✅ <b>Оформление сверху обновлено!</b>\n\n"
            f"<b>Оформление:</b>\n<code>{new_header}</code>\n"
            f"<b>Режим:</b> {mode_text}\n\n"
        )
        
        if mode == 'inline':
            text += f"<b>Пример:</b>\n<code>{new_header}</code> <code>Текст поста...</code>"
        else:
            text += f"<b>Пример:</b>\n<code>{new_header}</code>\n\n<code>Текст поста...</code>"
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Изменить режим", callback_data=f"subbot_header_change_mode_{sub_bot_id}")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data=f"subbot_settings_{sub_bot_id}")]
        ])
        
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
        await state.clear()
    except Exception as e:
        logger.error(f"Ошибка при сохранении режима header: {e}", exc_info=True)
        try:
            await callback.message.edit_text(f"❌ Ошибка: {str(e)}")
        except:
            pass


@main_dp.callback_query(F.data.startswith("subbot_header_remove_"))
async def remove_sub_bot_header(callback: types.CallbackQuery, state: FSMContext):
    """Удалить оформление сверху под-бота"""
    user_id = callback.from_user.id
    sub_bot_id = int(callback.data.split("_")[3])
    
    sub_bot_data = await db.get_sub_bot_by_id(sub_bot_id)
    if not sub_bot_data or sub_bot_data['owner_id'] != user_id:
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    
    await db.update_post_header(sub_bot_id, None, 'newline')
    
    text = "✅ <b>Оформление сверху удалено!</b>"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data=f"subbot_settings_{sub_bot_id}")]
    ])
    
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer("✅ Удалено")


@main_dp.callback_query(F.data.startswith("subbot_header_"))
async def start_sub_bot_header_change(callback: types.CallbackQuery, state: FSMContext):
    """Начать изменение оформления сверху под-бота"""
    # КРИТИЧНО: Пропускаем если это уже обработано другими обработчиками
    # Проверяем ВСЕ возможные варианты
    if (callback.data.startswith("subbot_header_change_mode_") or 
        callback.data.startswith("subbot_header_remove_") or 
        callback.data.startswith("subbot_header_mode_")):
        return
    
    user_id = callback.from_user.id
    sub_bot_id = int(callback.data.split("_")[2])
    
    sub_bot_data = await db.get_sub_bot_by_id(sub_bot_id)
    if not sub_bot_data or sub_bot_data['owner_id'] != user_id:
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    
    current_header = sub_bot_data.get('post_header')
    header_mode = sub_bot_data.get('header_mode', 'newline')
    header_preview = f"<code>{current_header}</code>" if current_header else "❌ Не установлено"
    mode_text = "в одну строку с постом" if header_mode == 'inline' else "на отдельной строке"
    
    await state.update_data(sub_bot_id=sub_bot_id)
    await state.set_state(SubBotSettings.waiting_for_header)
    
    text = (
        f"📝 <b>Оформление сверху для @{sub_bot_data['bot_username']}</b>\n\n"
        f"<b>Текущее оформление:</b>\n{header_preview}\n"
        f"<b>Режим:</b> {mode_text}\n\n"
        f"Отправьте новый текст оформления.\n\n"
        f"💡 Можно использовать HTML форматирование:\n"
        f"• <b>Жирный</b>, <i>курсив</i>, <code>моноширинный</code>\n"
        f"• <a href=\"https://example.com\">Ссылки</a> (без предпросмотра)\n\n"
        f"Оформление будет добавляться В НАЧАЛЕ каждого одобренного поста.\n"
        f"Можно использовать одновременно с оформлением снизу."
    )
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Изменить режим", callback_data=f"subbot_header_change_mode_{sub_bot_id}")],
        [InlineKeyboardButton(text="🗑 Удалить оформление сверху", callback_data=f"subbot_header_remove_{sub_bot_id}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"subbot_settings_{sub_bot_id}")]
    ])
    
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()


@main_dp.message(SubBotSettings.waiting_for_header)
async def process_sub_bot_header(message: types.Message, state: FSMContext):
    """Обработка нового оформления сверху под-бота"""
    user_id = message.from_user.id
    data = await state.get_data()
    sub_bot_id = data.get('sub_bot_id')
    
    if not sub_bot_id:
        await state.clear()
        return
    
    sub_bot_data = await db.get_sub_bot_by_id(sub_bot_id)
    if not sub_bot_data or sub_bot_data['owner_id'] != user_id:
        await state.clear()
        return
    
    # Используем HTML текст для поддержки форматирования
    header_source = message.text or message.caption or ""
    header_entities = message.entities or message.caption_entities
    new_header = (
        entities_to_rich_html(header_source, header_entities)
        if header_entities
        else header_source
    )
    await state.update_data(new_header=new_header)
    await state.set_state(SubBotSettings.waiting_for_header_mode)
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📄 На отдельной строке", callback_data=f"subbot_header_mode_newline_{sub_bot_id}")],
        [InlineKeyboardButton(text="📝 В одну строку с постом", callback_data=f"subbot_header_mode_inline_{sub_bot_id}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"subbot_settings_{sub_bot_id}")]
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


@main_dp.callback_query(F.data.startswith("subbot_header_remove_"))
async def remove_sub_bot_header(callback: types.CallbackQuery, state: FSMContext):
    """Удалить оформление сверху под-бота"""
    user_id = callback.from_user.id
    sub_bot_id = int(callback.data.split("_")[3])
    
    sub_bot_data = await db.get_sub_bot_by_id(sub_bot_id)
    if not sub_bot_data or sub_bot_data['owner_id'] != user_id:
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    
    await db.update_post_header(sub_bot_id, None, 'newline')
    
    text = "✅ <b>Оформление сверху удалено!</b>"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data=f"subbot_settings_{sub_bot_id}")]
    ])
    
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer("✅ Удалено")


@main_dp.callback_query(F.data.startswith("subbot_welcome_"))
async def start_sub_bot_welcome_change(callback: types.CallbackQuery, state: FSMContext):
    """Начать изменение приветствия под-бота"""
    user_id = callback.from_user.id
    sub_bot_id = int(callback.data.split("_")[2])
    
    sub_bot_data = await db.get_sub_bot_by_id(sub_bot_id)
    if not sub_bot_data or sub_bot_data['owner_id'] != user_id:
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    
    current_welcome = sub_bot_data.get('welcome_message')
    welcome_preview = f"<code>{current_welcome}</code>" if current_welcome else "❌ Не установлено (используется стандартное)"
    
    await state.update_data(sub_bot_id=sub_bot_id)
    await state.set_state(SubBotSettings.waiting_for_welcome)
    
    text = (
        f"💬 <b>Изменение приветствия для @{sub_bot_data['bot_username']}</b>\n\n"
        f"<b>Текущее приветствие:</b>\n{welcome_preview}\n\n"
        f"Отправьте новый текст приветствия.\n\n"
        f"💡 Приветствие будет показываться при команде /start.\n"
        f"💡 Можно использовать HTML-разметку: <b>жирный</b>, <i>курсив</i>, <a href=\"url\">ссылка</a>\n"
        f"💡 Используйте {{mode}} для автоматической вставки текущего режима анонимности\n"
        f"💡 Внизу автоматически будет добавлена информация о конструкторе"
    )
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Удалить приветствие", callback_data=f"subbot_welcome_remove_{sub_bot_id}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"subbot_settings_{sub_bot_id}")]
    ])
    
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()


@main_dp.message(SubBotSettings.waiting_for_welcome)
async def process_sub_bot_welcome(message: types.Message, state: FSMContext):
    """Обработка нового приветствия под-бота"""
    user_id = message.from_user.id
    data = await state.get_data()
    sub_bot_id = data.get('sub_bot_id')
    
    if not sub_bot_id:
        await state.clear()
        return
    
    sub_bot_data = await db.get_sub_bot_by_id(sub_bot_id)
    if not sub_bot_data or sub_bot_data['owner_id'] != user_id:
        await state.clear()
        return
    
    new_welcome = message.text
    await db.update_welcome_message(sub_bot_id, new_welcome)
    
    text = (
        f"✅ <b>Приветствие обновлено!</b>\n\n"
        f"<b>Новое приветствие:</b>\n<code>{new_welcome}</code>\n\n"
        f"Оно будет показываться при команде /start."
    )
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data=f"subbot_settings_{sub_bot_id}")]
    ])
    
    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)
    await state.clear()


@main_dp.callback_query(F.data.startswith("subbot_welcome_remove_"))
async def remove_sub_bot_welcome(callback: types.CallbackQuery, state: FSMContext):
    """Удалить приветствие под-бота"""
    user_id = callback.from_user.id
    sub_bot_id = int(callback.data.split("_")[3])
    
    sub_bot_data = await db.get_sub_bot_by_id(sub_bot_id)
    if not sub_bot_data or sub_bot_data['owner_id'] != user_id:
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    
    await db.update_welcome_message(sub_bot_id, None)
    
    text = (
        f"✅ <b>Приветствие удалено!</b>\n\n"
        f"Теперь будет использоваться стандартное приветствие."
    )
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data=f"subbot_settings_{sub_bot_id}")]
    ])
    
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()


# ========== AI MODERATION ==========
@main_dp.callback_query(F.data.startswith("subbot_moderation_"))
async def start_sub_bot_moderation_change(callback: types.CallbackQuery, state: FSMContext):
    """Начать настройку модерации"""
    user_id = callback.from_user.id
    sub_bot_id = int(callback.data.split("_")[2])
    
    sub_bot_data = await db.get_sub_bot_by_id(sub_bot_id)
    if not sub_bot_data or sub_bot_data['owner_id'] != user_id:
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    
    current_mode = sub_bot_data.get('moderation_mode', 'manual')
    mode_text = "🤖 AI (Gemini)" if current_mode == 'gemini' else "👤 Ручная"
    
    text = (
        f"🤖 <b>Настройка модерации для @{sub_bot_data['bot_username']}</b>\n\n"
        f"<b>Текущий режим:</b> {mode_text}\n\n"
        f"В режиме <b>AI (Gemini)</b> нейросеть будет автоматически проверять предложенные посты.\n"
        f"✅ Если пост проходит проверку - он автоматически публикуется в канал.\n"
        f"❌ Если пост не проходит - он отправляется в админ-чат для ручной проверки.\n\n"
        f"Для работы требуется API Key от Google Gemini."
    )
    
    # Динамическая клавиатура в зависимости от текущего режима
    buttons = []
    if current_mode == 'manual':
        buttons.append([InlineKeyboardButton(text="🤖 Включить AI-модерацию", callback_data=f"subbot_mod_gemini_{sub_bot_id}")])
    else:
        buttons.append([InlineKeyboardButton(text="👤 Включить ручную модерацию", callback_data=f"subbot_mod_manual_{sub_bot_id}")])
        
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"subbot_settings_{sub_bot_id}")])
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()


@main_dp.callback_query(F.data.startswith("subbot_mod_manual_"))
async def set_manual_moderation(callback: types.CallbackQuery):
    """Включить ручную модерацию"""
    user_id = callback.from_user.id
    sub_bot_id = int(callback.data.split("_")[3])
    
    sub_bot_data = await db.get_sub_bot_by_id(sub_bot_id)
    if not sub_bot_data or sub_bot_data['owner_id'] != user_id:
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    
    await db.update_moderation_mode(sub_bot_id, 'manual')
    
    await callback.answer("✅ Включен режим ручной модерации", show_alert=True)
    
    # Возвращаемся в меню настроек
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data=f"subbot_settings_{sub_bot_id}")]
    ])
    await callback.message.edit_text(
        "👤 <b>Режим ручной модерации включен</b>\n\n"
        "Все предложенные посты будут отправляться в админ-чат для проверки.",
        reply_markup=keyboard,
        parse_mode="HTML"
    )


@main_dp.callback_query(F.data.startswith("subbot_mod_gemini_"))
async def start_gemini_setup(callback: types.CallbackQuery, state: FSMContext):
    """Начать настройку Gemini"""
    user_id = callback.from_user.id
    sub_bot_id = int(callback.data.split("_")[3])
    
    sub_bot_data = await db.get_sub_bot_by_id(sub_bot_id)
    if not sub_bot_data or sub_bot_data['owner_id'] != user_id:
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    
    await state.update_data(sub_bot_id=sub_bot_id)
    await state.set_state(SubBotSettings.waiting_for_gemini_key)
    
    text = (
        "🔑 <b>Настройка Gemini API</b>\n\n"
        "Для работы AI-модерации нужен API Key от Google Gemini.\n\n"
        "<b>Как получить ключ:</b>\n"
        "1. Перейдите в <a href=\"https://aistudio.google.com/app/apikey\">Google AI Studio</a>\n"
        "2. Создайте новый API Key\n"
        "3. Скопируйте его и отправьте мне в следующем сообщении.\n\n"
        "⚠️ Ключ будет храниться в зашифрованном виде."
    )
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"subbot_settings_{sub_bot_id}")]
    ])
    
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard, disable_web_page_preview=True)
    await callback.answer()


@main_dp.message(SubBotSettings.waiting_for_gemini_key)
async def process_gemini_key(message: types.Message, state: FSMContext):
    """Обработка API Key"""
    if message.chat.type != "private":
        try:
            await message.delete()
        except Exception:
            pass
        return
    api_key = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        logger.debug("Could not delete a Gemini API key message")
    
    if len(api_key) < 10:
        await message.answer("❌ Похоже, это невалидный ключ. Попробуйте еще раз.")
        return
    
    await state.update_data(gemini_api_key=api_key)
    await state.set_state(SubBotSettings.waiting_for_gemini_prompt)
    
    text = (
        "📝 <b>Настройка промпта</b>\n\n"
        "Теперь напишите инструкцию (промпт) для нейросети.\n"
        "Опишите, какие посты нужно пропускать, а какие отклонять.\n\n"
        "<b>Пример:</b>\n"
        "<i>Ты модератор канала. Твоя задача - проверять посты на наличие спама, рекламы, оскорблений или запрещенного контента. "
        "Если пост нормальный и интересный - ответь PASS. Если есть нарушения - ответь REJECT. "
        "Контент может содержать текст и фото. Будь строг к рекламе, но лоялен к шуткам.</i>\n\n"
        "Отправьте ваш промпт:"
    )
    
    await message.answer(text, parse_mode="HTML")


@main_dp.message(SubBotSettings.waiting_for_gemini_prompt)
async def process_gemini_prompt(message: types.Message, state: FSMContext):
    """Обработка промпта и сохранение настроек"""
    if message.chat.type != "private":
        return
    prompt = message.text or ""
    if len(prompt) > 20_000:
        await message.answer("❌ Инструкция слишком длинная. Максимум — 20 000 символов.")
        return
    data = await state.get_data()
    sub_bot_id = data.get('sub_bot_id')
    api_key = data.get('gemini_api_key')
    
    if not sub_bot_id or not api_key:
        await message.answer("❌ Ошибка данных. Начните настройку заново.")
        await state.clear()
        return
    
    # Сохраняем настройки
    await db.update_gemini_settings(sub_bot_id, api_key, prompt)
    await db.update_moderation_mode(sub_bot_id, 'gemini')
    
    await state.clear()
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 К настройкам", callback_data=f"subbot_settings_{sub_bot_id}")]
    ])
    
    await message.answer(
        "✅ <b>AI-модерация успешно настроена!</b>\n\n"
        "Теперь нейросеть будет проверять новые предложенные посты.\n"
        "Те посты, которые пройдут проверку, будут публиковаться автоматически.\n"
        "Остальные будут попадать к вам на ручную проверку.",
        reply_markup=keyboard,
        parse_mode="HTML"
    )


@main_dp.callback_query(F.data.startswith("subbot_footer_remove_"))
async def remove_sub_bot_footer(callback: types.CallbackQuery, state: FSMContext):
    """Удалить оформление под-бота"""
    user_id = callback.from_user.id
    sub_bot_id = int(callback.data.split("_")[3])
    
    sub_bot_data = await db.get_sub_bot_by_id(sub_bot_id)
    if not sub_bot_data or sub_bot_data['owner_id'] != user_id:
        await callback.answer("❌ Нет доступа", show_alert=True)
        return
    
    await db.update_post_footer(sub_bot_id, None)
    await state.clear()
    
    text = (
        f"✅ <b>Оформление удалено!</b>\n\n"
        f"Посты будут публиковаться без дополнительного оформления."
    )
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data=f"subbot_settings_{sub_bot_id}")]
    ])
    
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()


async def main():
    """Запуск системы"""
    # Инициализация базы данных
    await db.init_db()
    logger.info("База данных инициализирована")

    @main_dp.managed_bot()
    async def on_managed_bot(update):
        """Register managed bots created through Telegram's newbot deep link."""
        if not BOT_MANAGEMENT_ENABLED:
            return
        creator_id = update.user.id
        if creator_id != ADMIN_ID and await db.is_user_globally_banned(creator_id):
            await main_bot.send_message(creator_id, "❌ Вы заблокированы и не можете пользоваться конструктором.")
            return
        managed_bot_id = update.bot.id
        try:
            token = await main_bot.get_managed_bot_token(user_id=managed_bot_id)
        except Exception as exc:
            logger.warning("Could not retrieve managed bot token (%s)", type(exc).__name__)
            await main_bot.send_message(creator_id, "Не удалось получить созданного бота. Обратитесь к владельцу конструктора.")
            return

        record = await db.get_sub_bot_by_managed_id(managed_bot_id)
        if record and record["owner_id"] != creator_id:
            logger.error("Managed bot owner changed; refusing silent account reassignment")
            await main_bot.send_message(
                creator_id,
                "Этот бот уже зарегистрирован на другого владельца. Передача требует ручной проверки.",
            )
            return
        if not record:
            record = await db.get_sub_bot_by_token(token)
            if record and record["owner_id"] != creator_id:
                logger.error("Managed bot token is already registered to another owner; refusing reassignment")
                await main_bot.send_message(creator_id, "Этот бот уже подключён к другому владельцу конструктора.")
                return

        try:
            if record:
                await db.update_managed_sub_bot(
                    record["id"], managed_bot_id, creator_id, token, update.bot.username
                )
                await sub_bot_manager.restart_sub_bot(record["id"], token)
                sub_bot_id = record["id"]
            else:
                sub_bot_id = await db.add_sub_bot(
                    owner_id=creator_id,
                    bot_token=token,
                    bot_username=update.bot.username,
                    managed_bot_id=managed_bot_id,
                )
                await sub_bot_manager.start_sub_bot(sub_bot_id, token)
        except Exception as exc:
            logger.error("Could not start managed bot %s (%s)", managed_bot_id, type(exc).__name__)
            await main_bot.send_message(
                creator_id,
                "Бот зарегистрирован, но пока не запустился. Администратор может перезапустить его из панели.",
            )
            return

        context = FSMContext(
            storage=main_dp.storage,
            key=StorageKey(bot_id=main_bot.id, chat_id=creator_id, user_id=creator_id),
        )
        await context.clear()
        await main_bot.send_message(
            creator_id,
            f"✅ Бот @{update.bot.username} подключён. Теперь добавьте его администратором в чат модерации и канал. "
            "Каждую новую привязку нужно подтвердить кнопкой, которую пришлёт сам бот.",
        )
    
    # ========== УДАЛЕНИЕ ПОД-БОТОВ ==========
    @main_dp.callback_query(F.data.startswith("subbot_delete_"))
    async def confirm_sub_bot_delete(callback: types.CallbackQuery, state: FSMContext):
        """Подтверждение удаления под-бота"""
        user_id = callback.from_user.id
        sub_bot_id = int(callback.data.split("_")[2])
        
        sub_bot_data = await db.get_sub_bot_by_id(sub_bot_id)
        if not sub_bot_data or sub_bot_data['owner_id'] != user_id:
            await callback.answer("❌ Нет доступа", show_alert=True)
            return
        
        text = (
            f"🗑 <b>Удаление бота @{sub_bot_data['bot_username']}</b>\n\n"
            "⚠️ Вы уверены, что хотите удалить этого бота?\n\n"
            "<b>Это действие нельзя отменить!</b>\n"
            "• Бот перестанет работать\n"
            "• Все настройки и история будут удалены\n"
            "• Пользователи не смогут отправлять сообщения"
        )
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Да, удалить навсегда", callback_data=f"subbot_delete_confirm_{sub_bot_id}")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data=f"subbot_settings_{sub_bot_id}")]
        ])
        
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
        await callback.answer()

    @main_dp.callback_query(F.data.startswith("subbot_delete_confirm_"))
    async def execute_sub_bot_delete(callback: types.CallbackQuery, state: FSMContext):
        """Выполнение удаления под-бота"""
        user_id = callback.from_user.id
        sub_bot_id = int(callback.data.split("_")[3])
        
        sub_bot_data = await db.get_sub_bot_by_id(sub_bot_id)
        if not sub_bot_data or sub_bot_data['owner_id'] != user_id:
            await callback.answer("❌ Нет доступа", show_alert=True)
            return
        
        try:
            # Останавливаем бота
            await sub_bot_manager.stop_sub_bot(sub_bot_id)
            
            # Удаляем из БД
            await db.delete_sub_bot(sub_bot_id)
            
            logger.info(f"Бот {sub_bot_id} (@{sub_bot_data['bot_username']}) удален владельцем {user_id}")
            
            await callback.message.edit_text(
                f"✅ Бот @{sub_bot_data['bot_username']} успешно удален.",
                parse_mode="HTML"
            )
            
            # Кнопка возврата к списку
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📋 Мои боты", callback_data="subbot_back_list")]
            ])
            await callback.message.edit_reply_markup(reply_markup=keyboard)
            
        except Exception as e:
            logger.error(f"Ошибка удаления бота {sub_bot_id}: {e}")
            await callback.answer("❌ Ошибка при удалении бота", show_alert=True)
    # Загружаем и запускаем все существующие под-боты
    sub_bots = await db.get_all_sub_bots()
    logger.info(f"Найдено под-ботов: {len(sub_bots)}")
    
    for sub_bot in sub_bots:
        try:
            await sub_bot_manager.start_sub_bot(sub_bot['id'], sub_bot['bot_token'])
            logger.info(f"Запущен под-бот @{sub_bot['bot_username']}")
        except Exception as e:
            logger.error(f"Ошибка запуска под-бота {sub_bot['id']}: {e}")
    
    # Запуск главного бота
    logger.info("Запуск главного бота...")
    await main_bot.delete_webhook()
    allowed_updates = ["message", "callback_query", "my_chat_member", "chat_member"]
    if BOT_MANAGEMENT_ENABLED:
        allowed_updates.append("managed_bot")
    try:
        await main_dp.start_polling(
            main_bot,
            allowed_updates=allowed_updates,
        )
    finally:
        await sub_bot_manager.stop_all()
        await main_bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен")
