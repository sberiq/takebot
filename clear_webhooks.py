import asyncio
import logging
from aiogram import Bot
from config import MAIN_BOT_TOKEN
from database import Database
from services.log_safety import install_secret_redaction

# Настройка логирования
logging.basicConfig(level=logging.INFO)
install_secret_redaction()
logger = logging.getLogger(__name__)

async def clear_webhooks():
    db = Database()
    await db.init_db()
    
    # 1. Очистка вебхука главного бота
    logger.info("Очистка вебхука главного бота...")
    main_bot = None
    try:
        main_bot = Bot(token=MAIN_BOT_TOKEN)
        await main_bot.delete_webhook()
        logger.info("✅ Вебхук главного бота удален")
    except Exception as e:
        logger.error("Could not clear the main bot webhook (%s)", type(e).__name__)
    finally:
        if main_bot is not None:
            await main_bot.session.close()

    # 2. Очистка вебхуков под-ботов
    sub_bots = await db.get_all_sub_bots()
    logger.info(f"Найдено {len(sub_bots)} под-ботов. Очистка...")
    
    for sub_bot in sub_bots:
        bot = None
        try:
            bot = Bot(token=sub_bot['bot_token'])
            await bot.delete_webhook()
            logger.info(f"✅ Вебхук бота {sub_bot['id']} (@{sub_bot['bot_username']}) удален")
        except Exception as e:
            logger.error("Could not clear webhook for sub-bot %s (%s)", sub_bot['id'], type(e).__name__)
        finally:
            if bot is not None:
                await bot.session.close()
            
    logger.info("🏁 Очистка завершена")

if __name__ == "__main__":
    asyncio.run(clear_webhooks())
