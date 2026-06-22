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
    Starts cloudflared and captures the URL from the output.
    Automatically updates config.SERVER_URL.
    """
    # Look for cloudflared — either locally or in the system
    cf_path = "./cloudflared"
    if not os.path.exists(cf_path):
        cf_path = "cloudflared"

    print("Starting cloudflared...")

    proc = await asyncio.create_subprocess_exec(
        cf_path, "tunnel", "--url", "http://localhost:8001",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    # Wait for URL in the output (up to 30 seconds)
    url = None
    deadline = asyncio.get_event_loop().time() + 30

    async def read_output(stream):
        nonlocal url
        while True:
            line = await stream.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="ignore")
            # cloudflared prints the URL to stderr
            match = re.search(r'https://[a-zA-Z0-9\-]+\.trycloudflare\.com', text)
            if match and not url:
                url = match.group(0)
                print(f"\n✅ Cloudflare URL: {url}\n")
                # Update config in memory
                config.SERVER_URL = url
                # Update common.MAP_URL
                try:
                    import handlers.common as common_handler
                    common_handler.MAP_URL = url + "/map"
                except Exception:
                    pass

    # Read stderr where cloudflared prints the URL
    asyncio.create_task(read_output(proc.stderr))
    asyncio.create_task(read_output(proc.stdout))

    # Wait until the URL appears
    while not url and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.5)

    if not url:
        print("⚠️ Failed to obtain cloudflare URL automatically.")
        print("Make sure cloudflared is running separately and SERVER_URL is specified manually in config.py.")

    return proc, url


async def main():
    await db.init_db()
    server.set_bot(bot)
    start_schedulers(bot)

    # Start cloudflared and get the URL automatically
    cf_proc, cf_url = await start_cloudflared()

    if cf_url:
        print(f"Add this to config.py for the next launch:\nSERVER_URL = '{cf_url}'")

    # Launch FastAPI + Telegram bot together
    uvicorn_config = uvicorn.Config(
        app=server.app,
        host="0.0.0.0",
        port=8001,
        log_level="warning"
    )
    uvicorn_server = uvicorn.Server(uvicorn_config)

    print("Bot + server started on port 8001")

    await asyncio.gather(
        dp.start_polling(bot),
        uvicorn_server.serve(),
    )

if __name__ == "__main__":
    asyncio.run(main())