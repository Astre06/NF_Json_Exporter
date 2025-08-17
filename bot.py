import os
import uuid
import json
import asyncio
import re
import shutil
import logging
from pyunpack import Archive
from telegram import Update, InputFile
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler,
    ContextTypes, filters
)
from playwright.async_api import async_playwright

# ========== Logging ==========
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ========== Config ==========
BOT_TOKEN = "8495284623:AAEyQ5XqAD9muGHwtCS05j2znIH5JzglfdQ"
TARGET_URL = "https://www.netflix.com/account"

# ========== Helpers ==========

def normalize_cookie(c):
    out = {
        "name": c["name"],
        "value": c["value"],
        "domain": c.get("domain"),
        "path": c.get("path", "/"),
        "httpOnly": c.get("httpOnly", False),
        "secure": c.get("secure", False),
    }
    if "expires" in c and isinstance(c["expires"], (int, float)):
        out["expires"] = c["expires"]

    ss = c.get("sameSite", "").lower()
    mapping = {
        "lax": "Lax",
        "strict": "Strict",
        "none": "None",
        "no_restriction": "None",
        "unspecified": "Lax",
        "": "Lax"
    }
    out["sameSite"] = mapping.get(ss, "Lax")
    return out

def next_export_filename(base="working", ext=".txt"):
    files = [f for f in os.listdir() if f.startswith(base) and f.endswith(ext)]
    nums = [int(re.search(rf"{base}(\d+){ext}", f).group(1)) for f in files if re.search(rf"{base}(\d+){ext}", f)]
    next_num = max(nums, default=0) + 1
    return f"{base}{next_num}{ext}"

async def process_cookie_file(input_path):
    print(f"▶️ Processing file: {input_path}")
    try:
        with open(input_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"❌ Failed to load JSON: {e}")
        return None

    all_cookies = data if isinstance(data, list) else [data]
    playwright_cookies = []

    for c in all_cookies:
        try:
            playwright_cookies.append(normalize_cookie(c))
        except Exception as e:
            print(f"⚠️ Skipping malformed cookie: {e}")

    if not playwright_cookies:
        print("❌ No valid cookies to process.")
        return None

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()

        try:
            await context.add_cookies(playwright_cookies)
        except Exception as e:
            print(f"⚠️ Cookie inject failed: {e}")
            await browser.close()
            return None

        page = await context.new_page()
        print(f"🌐 Navigating to {TARGET_URL}...")
        await page.goto(TARGET_URL, wait_until="load")
        await page.wait_for_load_state("networkidle")

        if page.url.startswith(TARGET_URL):
            print("✅ Valid session — account page loaded")
            new_cookies = await context.cookies()

            if not new_cookies:
                print("❌ No cookies returned — not exporting.")
                await browser.close()
                return None

            for cookie in new_cookies:
                if "sameSite" in cookie and isinstance(cookie["sameSite"], str):
                    s = cookie["sameSite"].lower()
                    mapping = {"lax": "lax", "strict": "strict", "none": "no_restriction"}
                    cookie["sameSite"] = mapping.get(s, "lax")

            export_path = next_export_filename()
            with open(export_path, "w", encoding="utf-8") as f:
                json.dump(new_cookies, f, separators=(",", ":"))
                print(f"✅ Exported cookies to {export_path}")

            await browser.close()
            return export_path
        else:
            print("❌ Invalid session — redirected to login or another page")
            await browser.close()
            return None

async def send_result(update, exported_path):
    if exported_path and os.path.isfile(exported_path):
        file_size = os.path.getsize(exported_path)
        if file_size > 10:
            with open(exported_path, "rb") as f:
                await update.message.reply_document(
                    document=InputFile(f, filename=os.path.basename(exported_path))
                )
        else:
            await update.message.reply_text("❌ Exported file is too small or empty.")
        os.remove(exported_path)
    else:
        await update.message.reply_text("❌ Cookie invalid or processing failed.")

# ========== Telegram Bot Logic ==========

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Send me a `.txt`, `.zip`, or `.rar` cookie file and I’ll process each for you.")

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong 🏓")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    document = update.message.document
    if not document:
        await update.message.reply_text("❌ Please send a valid file.")
        return

    file_ext = os.path.splitext(document.file_name)[-1].lower()

    if file_ext not in [".txt", ".zip", ".rar"]:
        await update.message.reply_text("❌ Please send a `.txt`, `.zip`, or `.rar` file only.")
        return

    unique_id = uuid.uuid4().hex[:8]
    downloaded_name = f"upload_{unique_id}{file_ext}"
    telegram_file = await document.get_file()
    await telegram_file.download_to_drive(downloaded_name)

    if file_ext == ".txt":
        await update.message.reply_text("🔄 Processing your cookie...")
        exported_path = await process_cookie_file(downloaded_name)
        await send_result(update, exported_path)
        os.remove(downloaded_name)
    else:
        extract_dir = f"extracted_{unique_id}"
        os.makedirs(extract_dir, exist_ok=True)
        try:
            Archive(downloaded_name).extractall(extract_dir)
        except Exception as e:
            await update.message.reply_text(f"❌ Failed to extract archive: {e}")
            shutil.rmtree(extract_dir)
            os.remove(downloaded_name)
            return

        processed = 0
        for root, dirs, files in os.walk(extract_dir):
            for filename in files:
                if filename.endswith(".txt"):
                    full_path = os.path.join(root, filename)
                    await update.message.reply_text(f"🔄 Processing `{filename}`...")
                    exported_path = await process_cookie_file(full_path)
                    if exported_path:
                        await send_result(update, exported_path)
                        processed += 1

        if processed == 0:
            await update.message.reply_text("❌ No valid `.txt` cookie files found in the archive.")

        shutil.rmtree(extract_dir)
        os.remove(downloaded_name)

# ========== Init & Run ==========

async def post_init(app):
    await app.bot.delete_webhook(drop_pending_updates=True)
    me = await app.bot.get_me()
    logger.info("✅ Logged in as @%s (%s)", me.username, me.id)

app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("ping", ping))
app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

print("🤖 Bot is running...")
app.run_polling(drop_pending_updates=True)
