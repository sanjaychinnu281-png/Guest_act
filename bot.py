# ============================================================
#  GUEST ACTIVATOR - PREMIUM TELEGRAM BOT
#  Requires: act.py (with CHINNUTEAM_pb2 integration)
# ============================================================
import asyncio
import json
import os
import time
import html
import tempfile
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message, FSInputFile, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

import act

# ==================== CONFIG ====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "8950405557:AAHXDOtxKQZjhHGOK2p96ni72GAteroKrM4").strip()
MAX_ACCOUNTS = 2000
PER_ACCOUNT_TIMEOUT = 90.0
PROGRESS_INTERVAL = 2.5  # Fast updates for premium feel

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN env variable is missing.")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# ==================== STATE ====================
_active_users = set()
_cancel_events = {}
_lock = asyncio.Lock()

# ==================== UI COMPONENTS ====================
def get_main_menu():
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📁 How to use", callback_data="help_menu")],
        [InlineKeyboardButton(text="⚡ System Status", callback_data="status_menu")]
    ])
    return kb

def get_cancel_button():
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛑 STOP JOB", callback_data="cancel_job")]
    ])
    return kb

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
        "<b>Supported JSON Formats:</b>\n"
        "<pre>[{\"uid\":\"123\", \"password\":\"pass\"}]</pre>\n"
        "<pre>{\"123\": \"pass\"}</pre>\n"
        "<pre>[{\"token\":\"eyJ...\"}]</pre>\n\n"
        f"<i>Maximum limit: {MAX_ACCOUNTS} accounts per job.</i>"
    )
    await callback.message.answer(text)
    await callback.answer()

@dp.callback_query(F.data == "status_menu")
async def cb_status(callback: CallbackQuery):
    text = (
        "⚙️ <b>System Status</b>\n\n"
        "🟢 <b>Engine:</b> Online\n"
        f"⚡ <b>Batch Size:</b> {act.BATCH_SIZE} accounts\n"
        f"🚀 <b>Concurrency:</b> {act.LOGIN_CONCURRENCY} threads\n"
        f"⏱ <b>Timeout:</b> {PER_ACCOUNT_TIMEOUT}s per account"
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
        await callback.message.edit_text("🛑 <b>Stopping...</b>\nFinishing current batch, please wait.")
    else:
        await callback.answer("No active job to cancel.", show_alert=True)
    
    await callback.answer()

# ==================== COMMANDS ====================
@dp.message(Command("start"))
async def cmd_start(msg: Message):
    text = (
        "⚡ <b>GUEST ACTIVATOR PRO</b> ⚡\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Welcome to the most advanced account activation system.\n\n"
        "🔹 <b>High-Speed Engine</b>\n"
        "🔹 <b>Live Progress Tracking</b>\n"
        "🔹 <b>Instant JSON Reporting</b>\n\n"
        "Please upload your <code>.json</code> file containing the accounts to begin."
    )
    await msg.answer(text, reply_markup=get_main_menu())

@dp.message(Command("help"))
async def cmd_help(msg: Message):
    await cb_help_logic(msg)

async def cb_help_logic(msg):
    text = (
        "📖 <b>Help Menu</b>\n\n"
        "Just send me a <code>.json</code> file and I will process it automatically.\n"
        "Use the <b>STOP JOB</b> button if you need to cancel."
    )
    await msg.answer(text, reply_markup=get_main_menu())

# ==================== FILE HANDLER ====================
@dp.message(F.document)
async def handle_doc(msg: Message):
    doc = msg.document
    uid = msg.from_user.id

    if not doc.file_name or not doc.file_name.lower().endswith(".json"):
        await msg.answer("❌ <b>Invalid file type.</b>\nPlease upload a valid <code>.json</code> file.")
        return

    if doc.file_size and doc.file_size > 20 * 1024 * 1024:
        await msg.answer("❌ <b>File too large.</b>\nMaximum size is 20 MB.")
        return

    async with _lock:
        if uid in _active_users:
            await msg.answer("⏳ <b>Job in progress.</b>\nPlease wait or cancel the current job.")
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
            await status.edit_text("❌ <b>Empty or Invalid JSON.</b>\nNo accounts found.")
            return

        if len(accounts) > MAX_ACCOUNTS:
            await status.edit_text(f"❌ <b>Limit Exceeded.</b>\nMax allowed is {MAX_ACCOUNTS} accounts.")
            return

        results = {"active": [], "failed": []}
        rlock = asyncio.Lock()
        total = len(accounts)
        processed = 0
        started = time.time()
        last_edit = 0.0

        # Initial Progress UI
        pct = 0
        bar = generate_progress_bar(pct)
        prog_text = (
            f"⚡ <b>ACTIVATING ACCOUNTS</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"<code>[{bar}]</code> {pct:.1f}%\n\n"
            f"📦 <b>Total:</b> {total}\n"
            f"✅ <b>Active:</b> 0\n"
            f"❌ <b>Failed:</b> 0\n"
            f"⏱ <b>Speed:</b> 0.0 acc/s\n\n"
            f"<i>Processing...</i>"
        )
        prog_msg = await status.edit_text(prog_text, reply_markup=get_cancel_button())

        async def update_progress(force=False):
            nonlocal last_edit, processed
            now = time.time()
            if not force and now - last_edit < PROGRESS_INTERVAL:
                return
            last_edit = now
            
            active_count = len(results["active"])
            failed_count = len(results["failed"])
            pct = (processed / total) * 100
            bar = generate_progress_bar(pct)
            elapsed = now - started
            rate = (processed / elapsed) if elapsed > 0 else 0

            text = (
                f"⚡ <b>ACTIVATING ACCOUNTS</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"<code>[{bar}]</code> {pct:.1f}%\n\n"
                f"📦 <b>Total:</b> {total}\n"
                f"✅ <b>Active:</b> {active_count}\n"
                f"❌ <b>Failed:</b> {failed_count}\n"
                f"⏱ <b>Speed:</b> {rate:.1f} acc/s\n"
                f"🕒 <b>Time:</b> {elapsed:.1f}s\n\n"
                f"<i>Processing batch {processed}/{total}...</i>"
            )
            try:
                await prog_msg.edit_text(text, reply_markup=get_cancel_button())
            except Exception:
                pass

        # Batching loop
        bs = act.BATCH_SIZE
        for i in range(0, total, bs):
            if cancel_ev.is_set():
                break
                
            batch = accounts[i:i + bs]
            
            # Wrap act.activate_single_account with a timeout
            async def safe_activate(acc, idx):
                try:
                    return await asyncio.wait_for(
                        act.activate_single_account(acc, idx, total, results, rlock),
                        timeout=PER_ACCOUNT_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    uid_str = str(acc.get("uid") or acc.get("token", "")[:16] or "unknown")
                    async with rlock:
                        results["failed"].append({"uid": uid_str, "reason": "timeout"})
                    return False

            tasks = [safe_activate(acc, i + j + 1) for j, acc in enumerate(batch)]
            await asyncio.gather(*tasks, return_exceptions=True)
            
            processed += len(batch)
            await update_progress()
            
            if i + bs < total and not cancel_ev.is_set():
                await asyncio.sleep(act.BATCH_INTERVAL)

        cancelled = cancel_ev.is_set()
        await update_progress(force=True)
        
        # Finalizing UI
        try:
            final_pct = (processed / total) * 100
            final_bar = generate_progress_bar(final_pct)
            status_word = "CANCELLED" if cancelled else "COMPLETED"
            
            final_text = (
                f"{'🛑' if cancelled else '✅'} <b>JOB {status_word}</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"<code>[{final_bar}]</code> 100%\n\n"
                f"📦 <b>Processed:</b> {processed}/{total}\n"
                f"✅ <b>Active:</b> {len(results['active'])}\n"
                f"❌ <b>Failed:</b> {len(results['failed'])}\n"
                f"🕒 <b>Total Time:</b> {time.time() - started:.1f}s"
            )
            await prog_msg.edit_text(final_text, reply_markup=None)
        except Exception:
            pass

        # ==================== SEND RESULTS ====================
        active = results["active"]
        failed = results["failed"]

        if active:
            res_text = "✅ <b>ACTIVE ACCOUNTS</b>\n━━━━━━━━━━━━━━━━━━━━━━\n\n"
            for i, a in enumerate(active[:30], 1):
                res_text += f"{i}. <code>{esc(a['uid'])}</code> | Lvl {esc(a['level'])} | {esc(a['nickname'])} | {esc(a['region'])}\n"
            
            if len(active) > 30:
                res_text += f"\n<i>...and {len(active) - 30} more (see attached file).</i>"
                
            await msg.answer(res_text)

        # Prepare JSON file
        out = {
            "summary": {
                "total_active": len(active),
                "total_failed": len(failed),
                "cancelled": cancelled,
                "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            },
            "active_accounts": active,
            "failed_accounts": failed,
        }
        out_path = os.path.join(tempfile.gettempdir(), f"result_{uid}_{int(time.time())}.json")
        try:
            with open(out_path, "w", encoding="utf-8") as fp:
                json.dump(out, fp, indent=4, ensure_ascii=False)
            
            await msg.answer_document(
                FSInputFile(out_path, filename=f"GUEST_RESULTS_{doc.file_name}"),
                caption="📁 <b>Full JSON Report</b>\nDownload to see all active and failed accounts.",
            )
        finally:
            if os.path.exists(out_path):
                try: os.remove(out_path)
                except Exception: pass

    except Exception as e:
        try:
            await msg.answer(f"❌ <b>Error:</b> <code>{esc(str(e)[:200])}</code>")
        except Exception:
            pass
    finally:
        async with _lock:
            _active_users.discard(uid)
            _cancel_events.pop(uid, None)
        if tmp_path and os.path.exists(tmp_path):
            try: os.remove(tmp_path)
            except Exception: pass

# ==================== MAIN ====================
async def main():
    print("🤖 GUEST ACTIVATOR PRO is starting...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Stopped.")