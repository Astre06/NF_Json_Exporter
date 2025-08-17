# bot.py
import os
import uuid
import json
import asyncio
import re
import subprocess
from telegram import Update, InputFile
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, ContextTypes, filters
from playwright.async_api import async_playwright

BOT_TOKEN = "8495284623:AAE7LvbTXLWsEnkezCvkgqfG8m7Wmvo4N7c"  # Replace with your actual token
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

# ========== Telegram Bot ==========

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Send me a `.txt` cookie file and I’ll send back the exported one.")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    document = update.message.document

    if not document or not document.file_name.endswith(".txt"):
        await update.message.reply_text("❌ Please send a `.txt` file only.")
        return

    filename = f"cookie_{uuid.uuid4().hex[:8]}.txt"
    telegram_file = await document.get_file()
    await telegram_file.download_to_drive(filename)

    await update.message.reply_text("🔄 Processing your cookie...")

    exported_path = await process_cookie_file(filename)

    if exported_path and os.path.isfile(exported_path):
        file_size = os.path.getsize(exported_path)
        if file_size > 10:
            await update.message.reply_document(
                document=InputFile(exported_path),
                filename=exported_path
            )
        else:
            await update.message.reply_text("❌ Exported file is too small or empty.")
    else:
        await update.message.reply_text("❌ Cookie invalid or processing failed.")

    os.remove(filename)

# ========== Run Bot ==========

app = ApplicationBuilder().token(BOT_TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

print("🤖 Bot is running...")
app.run_polling()
