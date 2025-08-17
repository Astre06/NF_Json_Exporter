# bot.py
import os
import uuid
import subprocess
from telegram import Update, InputFile
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, ContextTypes, filters

# Replace with your actual bot token from @BotFather
BOT_TOKEN = "8495284623:AAHiz1sJeaufKkO6mz5fvqJwykTglMVQhPU"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Send me a `.txt` cookie file and I’ll send back the exported one.")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    document = update.message.document

    if not document or not document.file_name.endswith(".txt"):
        await update.message.reply_text("❌ Please send a `.txt` file only.")
        return

    filename = f"cookie_{uuid.uuid4().hex[:8]}.txt"
    await document.get_file().download_to_drive(filename)

    await update.message.reply_text("🔄 Processing your cookie...")

    # Run the processing script
    result = subprocess.run(["python3", "sampleplayrigt.py", filename], capture_output=True, text=True)

    # Find the latest exported file
    exported_files = sorted([f for f in os.listdir() if f.startswith("exported_") and f.endswith(".txt")],
                            key=os.path.getmtime, reverse=True)

    if exported_files:
        await update.message.reply_document(InputFile(exported_files[0]))
    else:
        await update.message.reply_text("❌ Cookie invalid or processing failed.")

    # Clean up
    os.remove(filename)

app = ApplicationBuilder().token(BOT_TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

print("🤖 Bot is running...")
app.run_polling()
