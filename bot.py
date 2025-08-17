# bot.py
import os
import uuid
import subprocess
from telegram import Update, InputFile
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, ContextTypes, filters

# Replace with your actual bot token from @BotFather
BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Send me a `.txt` cookie file and I’ll send back the exported one.")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    document = update.message.document

    if not document or not document.file_name.endswith(".txt"):
        await update.message.reply_text("❌ Please send a `.txt` file only.")
        return

    filename = f"cookie_{uuid.uuid4().hex[:8]}.txt"
    
    # 🔧 Correct async file download
    telegram_file = await document.get_file()
    await telegram_file.download_to_drive(filename)

    await update.message.reply_text("🔄 Processing your cookie...")

    # ▶️ Run the processor
    result = subprocess.run(["python3", "sampleplayrigt.py", filename], capture_output=True, text=True)

    # 🔍 Find latest exported file
    exported_files = sorted(
        [f for f in os.listdir() if f.startswith("exported_") and f.endswith(".txt")],
        key=os.path.getmtime,
        reverse=True
    )

    if exported_files:
        await update.message.reply_document(InputFile(exported_files[0]))
    else:
        await update.message.reply_text("❌ Cookie invalid or processing failed.")

    # 🧹 Clean up input file (optional)
    os.remove(filename)

# 🔧 Set up the bot application
app = ApplicationBuilder().token(BOT_TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

print("🤖 Bot is running...")
app.run_polling()
