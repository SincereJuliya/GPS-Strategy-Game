import asyncio
import logging
import subprocess
import re
import os
import sys

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
import uvicorn

import config
import database as db
from handlers import common, opposition, system, admin
from game.scheduler import start_schedulers
import server

logging.basicConfig(level=logging.INFO)

bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

dp.include_router(admin.router)
dp.include_router(common.router)
dp.include_router(opposition.router)
dp.include_router(system.router)


async def start_cloudflared():
    """
    Запускает cloudflared и перехватывает URL из вывода.
    Автоматически обновляет config.SERVER_URL.
    """
    # Ищем cloudflared — рядом или в системе
    cf_path = "./cloudflared"
    if not os.path.exists(cf_path):
        cf_path = "cloudflared"

    print("Запускаю cloudflared...")

    proc = await asyncio.create_subprocess_exec(
        cf_path, "tunnel", "--url", "http://localhost:8001",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    # Ждём URL в выводе (до 30 сек)
    url = None
    deadline = asyncio.get_event_loop().time() + 30

    async def read_output(stream):
        nonlocal url
        while True:
            line = await stream.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="ignore")
            # cloudflared печатает URL в stderr
            match = re.search(r'https://[a-zA-Z0-9\-]+\.trycloudflare\.com', text)
            if match and not url:
                url = match.group(0)
                print(f"\n✅ Cloudflare URL: {url}\n")
                # Обновляем config в памяти
                config.SERVER_URL = url
                # Обновляем common.MAP_URL
                try:
                    import handlers.common as common_handler
                    common_handler.MAP_URL = url + "/map"
                except Exception:
                    pass

    # Читаем stderr где cloudflared печатает URL
    asyncio.create_task(read_output(proc.stderr))
    asyncio.create_task(read_output(proc.stdout))

    # Ждём пока URL появится
    while not url and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.5)

    if not url:
        print("⚠️ Не удалось получить cloudflare URL автоматически.")
        print("Проверь что cloudflared запущен отдельно и SERVER_URL в config.py указан вручную.")

    return proc, url


async def main():
    await db.init_db()
    server.set_bot(bot)
    start_schedulers(bot)

    # Запускаем cloudflared и получаем URL автоматически
    cf_proc, cf_url = await start_cloudflared()

    if cf_url:
        print(f"Добавь в config.py для следующего запуска:\nSERVER_URL = '{cf_url}'")

    # Запускаем FastAPI + Telegram бот вместе
    uvicorn_config = uvicorn.Config(
        app=server.app,
        host="0.0.0.0",
        port=8001,
        log_level="warning"
    )
    uvicorn_server = uvicorn.Server(uvicorn_config)

    print("Бот + сервер запущены на порту 8001")

    await asyncio.gather(
        dp.start_polling(bot),
        uvicorn_server.serve(),
    )

if __name__ == "__main__":
    asyncio.run(main())
