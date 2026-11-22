"""One-shot run for GitHub Actions: handle Telegram commands, manage positions, scan, save state."""
import asyncio
from datetime import datetime, timezone
from telegram.ext import Application
import bot


async def run():
    bot.load_state()
    app = Application.builder().token(bot.TG_TOKEN).build()
    bot.register(app)
    await app.initialize()
    try:
        # 1. Telegram commands sent since the last run
        updates = await app.bot.get_updates(offset=bot.state.get("tg_offset"), timeout=0)
        for u in updates:
            await app.process_update(u)
            bot.state["tg_offset"] = u.update_id + 1
        # 2. exits, then new entries
        await bot.manage_positions(app)
        await bot.scan(app)
        # 3. daily report
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        if now.hour >= bot.REPORT_HOUR_UTC and bot.state.get("last_report_day") != today:
            await app.bot.send_message(bot.ADMIN_ID, await bot.build_report(app))
            bot.state["last_report_day"] = today
    finally:
        bot.save_state()
        await app.shutdown()


if __name__ == "__main__":
    asyncio.run(run())