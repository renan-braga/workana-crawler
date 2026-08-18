#!/usr/bin/env python3
"""Testa o login no Workana via Playwright e salva screenshots em cada etapa."""
import asyncio
import os
from pathlib import Path
from playwright.async_api import async_playwright

EMAIL = os.getenv("WORKANA_EMAIL")
PASSWORD = os.getenv("WORKANA_PASSWORD")
LOGS = Path(__file__).parent / "logs"
LOGS.mkdir(exist_ok=True)


async def main():
    if not EMAIL or not PASSWORD:
        raise RuntimeError("Defina WORKANA_EMAIL e WORKANA_PASSWORD antes de rodar este teste.")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            )
        )
        page = await ctx.new_page()

        print("→ Abrindo página de login…")
        await page.goto("https://www.workana.com/login", wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)
        await page.screenshot(path=str(LOGS / "01_login_page.png"))
        print("  screenshot: logs/01_login_page.png")

        # Banner de cookies
        cookie_btn = page.locator("button:has-text('Aceitar todos os cookies')")
        if await cookie_btn.count() > 0:
            print("→ Fechando banner de cookies…")
            await cookie_btn.click()
            await page.wait_for_timeout(800)
            await page.screenshot(path=str(LOGS / "02_after_cookie.png"))
            print("  screenshot: logs/02_after_cookie.png")

        # Aguarda input de email
        print("→ Aguardando campo email…")
        await page.wait_for_selector("input[type='email']", timeout=10000)

        # Lista todos os inputs visíveis para diagnóstico
        inputs = await page.evaluate("""() => {
            return Array.from(document.querySelectorAll('input')).map(el => ({
                type: el.type, name: el.name, id: el.id,
                placeholder: el.placeholder, visible: el.offsetParent !== null
            }));
        }""")
        print(f"  Inputs encontrados: {inputs}")

        # Preenche email
        print("→ Preenchendo email…")
        email_field = page.locator("input[type='email']").first
        await email_field.click()
        await page.wait_for_timeout(300)
        await page.keyboard.type(EMAIL, delay=80)
        await page.wait_for_timeout(300)
        await page.screenshot(path=str(LOGS / "03_after_email.png"))
        print("  screenshot: logs/03_after_email.png")

        # Preenche senha
        print("→ Preenchendo senha…")
        pwd_field = page.locator("input[type='password']").first
        await pwd_field.click()
        await page.wait_for_timeout(300)
        await page.keyboard.type(PASSWORD, delay=80)
        await page.wait_for_timeout(300)
        await page.screenshot(path=str(LOGS / "04_after_password.png"))
        print("  screenshot: logs/04_after_password.png")

        # Verifica valores preenchidos
        email_val = await email_field.input_value()
        pwd_val   = await pwd_field.input_value()
        print(f"  Email no campo: '{email_val}'")
        print(f"  Senha no campo: '{pwd_val[:3]}{'*' * (len(pwd_val)-3) if len(pwd_val) > 3 else '***'}'")

        # Clica em Login
        print("→ Clicando em Login…")
        submit = page.locator("button:has-text('Login'), button[type='submit']").first
        await submit.click()
        await page.wait_for_timeout(5000)
        await page.screenshot(path=str(LOGS / "05_after_submit.png"))
        print(f"  URL final: {page.url}")
        print("  screenshot: logs/05_after_submit.png")

        if "/login" in page.url:
            print("✗ Login FALHOU")
        else:
            print("✓ Login OK!")

        await browser.close()


asyncio.run(main())
