# Function.py
import os
import json
import asyncio
import re
from playwright.async_api import async_playwright

TARGET_URL = "https://www.netflix.com/account"

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

def next_export_filename(base="exported_", ext=".txt"):
    files = [f for f in os.listdir() if f.startswith(base) and f.endswith(ext)]
    nums = [int(re.search(rf"{base}(\d+){ext}", f).group(1)) for f in files if re.search(rf"{base}(\d+){ext}", f)]
    next_num = max(nums, default=0) + 1
    return f"{base}{next_num}{ext}"

async def main(input_path):
    print(f"▶️ Running Function.py with file: {input_path}")
    print("📁 Working dir:", os.getcwd())

    try:
        with open(input_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"❌ Failed to load JSON: {e}")
        return

    all_cookies = data if isinstance(data, list) else [data]
    playwright_cookies = []

    for c in all_cookies:
        try:
            playwright_cookies.append(normalize_cookie(c))
        except Exception as e:
            print(f"⚠️ Skipping malformed cookie: {e}")

    if not playwright_cookies:
        print("❌ No valid cookies to process.")
        return

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()

        try:
            await context.add_cookies(playwright_cookies)
        except Exception as e:
            print(f"⚠️ Cookie inject failed: {e}")
            await browser.close()
            return

        page = await context.new_page()
        print(f"🌐 Navigating to {TARGET_URL}...")
        await page.goto(TARGET_URL, wait_until="load")
        await page.wait_for_load_state("networkidle")

        if page.url.startswith(TARGET_URL):
            print("✅ Valid session — account page loaded")
            new_cookies = await context.cookies()
            for cookie in new_cookies:
                if "sameSite" in cookie and isinstance(cookie["sameSite"], str):
                    s = cookie["sameSite"].lower()
                    mapping = {"lax": "lax", "strict": "strict", "none": "no_restriction"}
                    cookie["sameSite"] = mapping.get(s, "lax")

            export_path = next_export_filename()
            with open(export_path, "w", encoding="utf-8") as f:
                json.dump(new_cookies, f, separators=(",", ":"))
                print(f"✅ Exported cookies to {export_path}")
        else:
            print("❌ Invalid session — redirected to login or another page")

        await browser.close()

if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        print("Usage: python Function.py <cookie_file.txt>")
    else:
        asyncio.run(main(sys.argv[1]))
