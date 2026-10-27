import asyncio
import json
import os
import time
import html
import tempfile
from datetime import datetime
from collections import Counter

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message, FSInputFile, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

import act

# ==================== CONFIG ====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN env variable is missing.")

WEBHOOK_HOST = os.getenv("WEBHOOK_HOST") or os.getenv("RENDER_EXTERNAL_URL") or ""
WEBHOOK_HOST = WEBHOOK_HOST.rstrip("/")
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}" if WEBHOOK_HOST else ""

PORT = int(os.getenv("PORT", "8080"))

MAX_ACCOUNTS = 2000
PER_ACCOUNT_TIMEOUT = 90.0
PROGRESS_INTERVAL = 2.5

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# ==================== STATE ====================
_active_users = set()
_cancel_events = {}
_lock = asyncio.Lock()

# GLOBAL STATS FOR DASHBOARD
GLOBAL_STATS = {
    "total_processed": 0,
    "active": 0,
    "failed": 0,
    "regions": Counter(),
    "last_job_time": "No jobs yet"
}

# ==================== UI COMPONENTS ====================
def get_main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📁 How to use", callback_data="help_menu")],
        [InlineKeyboardButton(text="⚡ System Status", callback_data="status_menu")],
    ])

def get_cancel_button():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛑 STOP JOB", callback_data="cancel_job")]
    ])

def esc(s):
    return html.escape(str(s) if s is not None else "")

def generate_progress_bar(percent):
    bar_length = 15
    filled = int(bar_length * percent / 100)
    return "█" * filled + "░" * (bar_length - filled)

# ==================== CALLBACKS ====================
@dp.callback_query(F.data == "help_menu")
async def cb_help(callback: CallbackQuery):
    text = (
        "📖 <b>How to use Guest Activator</b>\n\n"
        "1️⃣ Prepare a <code>.json</code> file with your accounts.\n"
        "2️⃣ Send it to me as a document.\n"
        "3️⃣ I will activate them and send you the results.\n\n"
        f"<i>Maximum limit: {MAX_ACCOUNTS} accounts per job.</i>"
    )
    await callback.message.answer(text)
    await callback.answer()

@dp.callback_query(F.data == "status_menu")
async def cb_status(callback: CallbackQuery):
    text = (
        "⚙️ <b>System Status</b>\n\n"
        "🟢 <b>Engine:</b> Online (Webhook Mode)\n"
        f"⚡ <b>Batch Size:</b> {act.BATCH_SIZE} accounts\n"
        f"🚀 <b>Concurrency:</b> {act.LOGIN_CONCURRENCY} threads\n"
    )
    await callback.message.answer(text)
    await callback.answer()

@dp.callback_query(F.data == "cancel_job")
async def cb_cancel(callback: CallbackQuery):
    uid = callback.from_user.id
    async with _lock:
        ev = _cancel_events.get(uid)
    if ev and not ev.is_set():
        ev.set()
        await callback.message.edit_text("🛑 <b>Stopping...</b>")
    else:
        await callback.answer("No active job to cancel.", show_alert=True)
    await callback.answer()

# ==================== COMMANDS ====================
@dp.message(Command("start"))
async def cmd_start(msg: Message):
    text = (
        "⚡ <b>GUEST ACTIVATOR PRO</b> ⚡\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Welcome! Please upload your <code>.json</code> file containing the accounts to begin."
    )
    await msg.answer(text, reply_markup=get_main_menu())

# ==================== FILE HANDLER ====================
@dp.message(F.document)
async def handle_doc(msg: Message):
    global GLOBAL_STATS
    doc = msg.document
    uid = msg.from_user.id

    if not doc.file_name or not doc.file_name.lower().endswith(".json"):
        await msg.answer("❌ <b>Invalid file type.</b>\nPlease upload a valid <code>.json</code> file.")
        return

    async with _lock:
        if uid in _active_users:
            await msg.answer("⏳ <b>Job in progress.</b>")
            return
        _active_users.add(uid)
        cancel_ev = asyncio.Event()
        _cancel_events[uid] = cancel_ev

    tmp_path = None
    try:
        status = await msg.answer("📥 <i>Downloading file...</i>")
        f = await bot.get_file(doc.file_id)
        tmp_path = os.path.join(tempfile.gettempdir(), f"ff_{uid}_{int(time.time())}.json")
        await bot.download_file(f.file_path, tmp_path)

        accounts = act.load_accounts_from_json(tmp_path)
        if not accounts:
            await status.edit_text("❌ <b>Empty or Invalid JSON.</b>")
            return

        results = {"active": [], "failed": []}
        rlock = asyncio.Lock()
        total = len(accounts)
        processed = 0
        started = time.time()

        prog_text = f"⚡ <b>ACTIVATING ACCOUNTS</b>\n\n<code>[{generate_progress_bar(0)}]</code> 0%\n\n<i>Processing...</i>"
        prog_msg = await status.edit_text(prog_text, reply_markup=get_cancel_button())

        async def update_progress(force=False):
            nonlocal processed
            pct = (processed / total) * 100 if total else 0
            text = (
                f"⚡ <b>ACTIVATING ACCOUNTS</b>\n\n"
                f"<code>[{generate_progress_bar(pct)}]</code> {pct:.1f}%\n\n"
                f"📦 <b>Total:</b> {total}\n"
                f"✅ <b>Active:</b> {len(results['active'])}\n"
                f"❌ <b>Failed:</b> {len(results['failed'])}\n"
                f"🕒 <b>Time:</b> {time.time() - started:.1f}s"
            )
            try:
                await prog_msg.edit_text(text, reply_markup=get_cancel_button())
            except Exception:
                pass

        bs = act.BATCH_SIZE
        for i in range(0, total, bs):
            if cancel_ev.is_set():
                break
            batch = accounts[i:i + bs]

            async def safe_activate(acc, idx):
                try:
                    return await asyncio.wait_for(
                        act.activate_single_account(acc, idx, total, results, rlock),
                        timeout=PER_ACCOUNT_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    async with rlock:
                        results["failed"].append({"uid": "unknown", "reason": "timeout"})
                    return False

            tasks = [safe_activate(acc, i + j + 1) for j, acc in enumerate(batch)]
            await asyncio.gather(*tasks, return_exceptions=True)
            processed += len(batch)
            await update_progress()
            
            # --- UPDATE GLOBAL STATS FOR HTML DASHBOARD ---
            GLOBAL_STATS["total_processed"] += len(batch)
            GLOBAL_STATS["active"] = len(results["active"])
            GLOBAL_STATS["failed"] = len(results["failed"])
            for a in results["active"]:
                GLOBAL_STATS["regions"][a["region"]] += 1
            GLOBAL_STATS["last_job_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            if i + bs < total and not cancel_ev.is_set():
                await asyncio.sleep(act.BATCH_INTERVAL)

        await prog_msg.edit_text(f"✅ <b>JOB COMPLETED</b>\nActive: {len(results['active'])} | Failed: {len(results['failed'])}", reply_markup=None)

        # Send JSON file
        out_path = os.path.join(tempfile.gettempdir(), f"result_{uid}.json")
        with open(out_path, "w", encoding="utf-8") as fp:
            json.dump(results, fp, indent=4)
        await msg.answer_document(FSInputFile(out_path, filename=f"RESULT_{doc.file_name}"), caption="📁 <b>Full JSON Report</b>")
        os.remove(out_path)

    except Exception as e:
        await msg.answer(f"❌ <b>Error:</b> <code>{esc(str(e))}</code>")
    finally:
        async with _lock:
            _active_users.discard(uid)
            _cancel_events.pop(uid, None)
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)


# ==================== HTML DASHBOARD ROUTE ====================
async def dashboard(request):
    # Get region data for chart
    regions = dict(GLOBAL_STATS["regions"])
    region_html = "".join([f"<li>{k}: <b>{v}</b> accounts</li>" for k, v in regions.items()])
    if not region_html:
        region_html = "<li>No regions yet</li>"

    html_content = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Guest Activator Dashboard</title>
        <style>
            body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: #0f172a; color: #fff; margin: 0; padding: 20px; }}
            .container {{ max-width: 800px; margin: auto; }}
            .header {{ text-align: center; margin-bottom: 30px; }}
            .card {{ background: #1e293b; border-radius: 12px; padding: 20px; margin-bottom: 20px; box-shadow: 0 4px 6px rgba(0,0,0,0.3); }}
            .grid {{ display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 15px; text-align: center; }}
            .stat-value {{ font-size: 32px; font-weight: bold; margin: 10px 0; }}
            .active {{ color: #10b981; }}
            .failed {{ color: #ef4444; }}
            .total {{ color: #3b82f6; }}
            ul {{ list-style-type: none; padding: 0; }}
            li {{ background: #334155; padding: 10px; margin-bottom: 5px; border-radius: 6px; display: flex; justify-content: space-between; }}
            .time {{ font-size: 12px; color: #94a3b8; text-align: right; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>⚡ GUEST ACTIVATOR PRO</h1>
                <p>Live Monitoring Dashboard</p>
            </div>
            
            <div class="card grid">
                <div>
                    <div class="stat-value total">{GLOBAL_STATS['total_processed']}</div>
                    <div>Total Processed</div>
                </div>
                <div>
                    <div class="stat-value active">{GLOBAL_STATS['active']}</div>
                    <div>Active Accounts</div>
                </div>
                <div>
                    <div class="stat-value failed">{GLOBAL_STATS['failed']}</div>
                    <div>Failed Accounts</div>
                </div>
            </div>

            <div class="card">
                <h3>🌍 Active Accounts by Region</h3>
                <ul>
                    {region_html}
                </ul>
            </div>

            <div class="time">
                Last Update: {GLOBAL_STATS['last_job_time']}
            </div>
        </div>
    </body>
    </html>
    """
    return web.Response(text=html_content, content_type='text/html')


# ==================== WEBHOOK / HEALTH ====================
async def on_startup(bot: Bot):
    if WEBHOOK_URL:
        await bot.set_webhook(WEBHOOK_URL, drop_pending_updates=True, allowed_updates=dp.resolve_used_update_types())
        print(f"[OK] Webhook set -> {WEBHOOK_URL}")

async def on_shutdown(bot: Bot):
    await bot.delete_webhook()

async def main():
    print("🤖 GUEST ACTIVATOR PRO starting...")
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    app = web.Application()
    
    # HTML DASHBOARD LINK
    app.router.add_get("/", dashboard) 
    
    # Webhook Router
    webhook_handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
    webhook_handler.register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"[OK] Server listening on port {PORT}")

    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
