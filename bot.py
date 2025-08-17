# bot.py
import os
import uuid
import subprocess
from telegram import Update, InputFile
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, ContextTypes, filters

BOT_TOKEN = "8495284623:AAFl6NMBY9gf2gEJJcHW180eYouUHAy9sDw"  # Replace with your real token

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

    # Run the processor
    result = subprocess.run(["python3", "Function.py", filename], capture_output=True, text=True)

    print("📁 Working dir:", os.getcwd())
    print("▶️ STDOUT:\n", result.stdout)
    print("⚠️ STDERR:\n", result.stderr)

    if result.returncode != 0:
        await update.message.reply_text(f"❌ Script crashed:\n{result.stderr.strip()[:1000]}")
        os.remove(filename)
        return

    exported_files = sorted(
        [f for f in os.listdir() if f.startswith("working") and f.endswith(".txt")],
        key=os.path.getmtime,
        reverse=True
    )

    if exported_files:
        exported_path = exported_files[0]
        file_size = os.path.getsize(exported_path)

        if file_size > 10:
            await update.message.reply_document(
                document=InputFile(exported_path),
                filename=exported_path
            )
        else:
            await update.message.reply_text("❌ Exported file is empty or too small.")
    else:
        await update.message.reply_text("❌ Cookie invalid or no export file was generated.")

    os.remove(filename)

# Start the bot
app = ApplicationBuilder().token(BOT_TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

print("🤖 Bot is running...")
app.run_polling()
