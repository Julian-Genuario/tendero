"""Drives the UI end-to-end with a headless browser and saves screenshots.

    python scripts/ui_smoke.py http://localhost:8000 out_dir
"""
import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright


async def main(base: str, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        b = await p.chromium.launch()
        page = await b.new_page(viewport={"width": 1280, "height": 720})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        await page.goto(base)
        await page.wait_for_selector("#llmBadge.demo, #llmBadge.live")
        await page.screenshot(path=out / "01_empty.png")
        await page.fill("#custInput", "Hi! Do you ship to Madrid? I'd like 2 mate gourds and a bombilla")
        await page.press("#custInput", "Enter")
        await page.wait_for_selector("text=Shall I send you")
        await page.fill("#custInput", "Yes, send me the link")
        await page.press("#custInput", "Enter")
        await page.wait_for_selector(".paybtn")
        await page.screenshot(path=out / "02_link.png")
        await page.click(".paybtn")
        frame = page.frame_locator("#checkoutFrame")
        await frame.locator("#approve").wait_for()
        await page.screenshot(path=out / "03_checkout.png")
        await frame.locator("#approve").click()
        await page.wait_for_selector(".paidcard", timeout=15000)
        await page.wait_for_timeout(1500)
        await page.screenshot(path=out / "04_paid.png")
        await page.click("#forgeBtn")
        await page.wait_for_timeout(1500)
        await page.fill("#mInput", "How did we do today? Any issues?")
        await page.press("#mInput", "Enter")
        await page.wait_for_selector("#mThread .msg.agent")
        await page.fill("#mInput", "Refund the last order - the gourd arrived cracked")
        await page.press("#mInput", "Enter")
        await page.wait_for_selector("text=refund of 60.00 USD", timeout=15000)
        await page.wait_for_timeout(1200)
        await page.screenshot(path=out / "05_refund.png")
        await b.close()
        if errors:
            print("JS errors:", errors)
            sys.exit(1)
        print("UI smoke OK ->", out)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000",
                     Path(sys.argv[2] if len(sys.argv) > 2 else "data/shots")))
