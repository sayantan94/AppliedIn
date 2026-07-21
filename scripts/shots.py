"""Capture dashboard screenshots (demo mode) for the README.

Runs the committed demo config (sample data), so the board looks populated. Saves
PNGs to docs/screenshots/. Usage:  .venv/bin/python scripts/shots.py
"""

import asyncio
import pathlib

from playwright.async_api import async_playwright

OUT = pathlib.Path("docs/screenshots")
OUT.mkdir(parents=True, exist_ok=True)
URL = "http://127.0.0.1:8899/index.html"

TABS = [("apps", "applications"), ("needs", "needs-you"),
        ("stuck", "unable"), ("logs", "logs")]


async def shot(page, name):
    await page.wait_for_timeout(800)
    await page.screenshot(path=str(OUT / f"{name}.png"))
    print("saved", name)


async def main():
    errors = []
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": 1460, "height": 940},
                                      device_scale_factor=2)
        page.on("console", lambda m: m.type == "error" and errors.append(m.text))
        await page.goto(URL, wait_until="networkidle")
        await page.wait_for_timeout(1800)

        # 1) hero — control deck + Pipeline (default tab)
        await shot(page, "01-pipeline")

        # 2) each of the other tabs
        for tab, name in TABS:
            await page.click(f'.tab[data-tab="{tab}"]')
            await shot(page, f"tab-{name}")

        # 3) discover company picker open (back to pipeline)
        await page.click('.tab[data-tab="pipeline"]')
        await page.wait_for_timeout(300)
        await page.click('#btn-companies')
        await page.wait_for_timeout(600)
        await shot(page, "discover-picker")
        await page.keyboard.press("Escape")

        # 4) detail drawer — click the first Applications row
        await page.click('.tab[data-tab="apps"]')
        await page.wait_for_timeout(700)
        row = page.locator('tr[data-pk]').first
        if await row.count():
            await row.click()
            await page.wait_for_timeout(900)
            await shot(page, "drawer")

        # 5) light theme hero (toggle via settings menu)
        await page.keyboard.press("Escape")
        await page.click('.tab[data-tab="pipeline"]')
        await page.click('#menu-btn')
        await page.wait_for_timeout(300)
        await page.click('#m-theme')
        await page.wait_for_timeout(500)
        # close menu
        await page.mouse.click(730, 500)
        await page.wait_for_timeout(500)
        await shot(page, "02-pipeline-light")

        await browser.close()
    print("CONSOLE ERRORS:", errors or "none")


asyncio.run(main())
