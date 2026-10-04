import os
from dotenv import load_dotenv

load_dotenv()

# Токен основного бота этого экземпляра. Каждый оператор указывает свой токен.
MAIN_BOT_TOKEN = os.getenv("MAIN_BOT_TOKEN")

# Telegram ID владельца этого экземпляра.
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

# Путь к локальной базе этого экземпляра.
DATABASE_PATH = os.getenv("DATABASE_PATH", "bot_constructor.db")

INSTANCE_OWNER_ONLY = os.getenv("INSTANCE_OWNER_ONLY", "true").strip().lower() in {"1", "true", "yes", "on"}

# Poll for managed-bot updates by default. The create link appears only after
# Telegram confirms Bot Management Mode for this bot in @BotFather.
BOT_MANAGEMENT_ENABLED = os.getenv("BOT_MANAGEMENT_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
BOT_MANAGEMENT_DEFAULT_NAME = os.getenv("BOT_MANAGEMENT_DEFAULT_NAME", "Suggestions Bot")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
