import asyncio
import logging
from aiogram import Bot, Dispatcher
from app.config import settings
from app.bot.handlers import router

async def main():
    logging.basicConfig(level=logging.INFO)
    print(repr(settings.tg_bot_token))
    bot = Bot(token=settings.tg_bot_token)
    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())