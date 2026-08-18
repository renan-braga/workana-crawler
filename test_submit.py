#!/usr/bin/env python3
"""Testa envio de proposta com todos os campos preenchidos."""
import asyncio
import os
from pathlib import Path
from playwright.async_api import async_playwright

EMAIL = os.getenv("WORKANA_EMAIL")
PASSWORD = os.getenv("WORKANA_PASSWORD")
JOB_URL = os.getenv(
    "WORKANA_TEST_JOB_URL",
    "https://www.workana.com/job/criacao-de-landing-page-profissional-com-design-impactante-e-integracao-whatsapp",
)
PROPOSAL = (
    "A integracao com captura de leads requer atencao a taxa de entrega. "
    "Usar o canal oficial evita bloqueios que abordagens nao-oficiais costumam gerar. "
    "Tenho experiencia com landing pages orientadas a conversao integradas a automacoes e CRM. "
    "Posso detalhar o stack se quiser."
)
BID_AMOUNT = "500"   # valor em reais para o orçamento
LOGS = Path(__file__).parent / "logs"
LOGS.mkdir(exist_ok=True)
BASE = "https://www.workana.com"


def bid_url(job_url):
    slug = job_url.rstrip("/").split("/job/")[-1]
    return f"{BASE}/messages/bid/{slug}"


async def login(ctx):
    page = await ctx.new_page()
    await page.goto(f"{BASE}/login", wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(2000)
    btn = page.locator("button:has-text('Aceitar todos os cookies')")
    if await btn.count() > 0:
        await btn.click()
        await page.wait_for_timeout(600)
    await page.wait_for_selector("input[type='email']", timeout=10000)
    await page.locator("input[type='email']").click()
    await page.keyboard.type(EMAIL, delay=60)
    await page.locator("input[type='password']").click()
    await page.keyboard.type(PASSWORD, delay=60)
    await page.locator("button:has-text('Login'), button[type='submit']").first.click()
    await page.wait_for_timeout(4000)
    assert "/login" not in page.url
    print("✓ Login OK")
    await page.close()


async def send_proposal(ctx):
    page = await ctx.new_page()
    try:
        url = bid_url(JOB_URL)
        print(f"→ Abrindo formulário: {url}")
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2000)

        # Texto da proposta
        await page.locator("textarea[name='bid[content]']").fill(PROPOSAL)
        print(f"  Proposta preenchida ({len(PROPOSAL)} chars)")

        # Prazo (opcional)
        delivery = page.locator("input[name='bid[deliveryTime]']")
        if await delivery.count() > 0:
            await delivery.fill("A combinar")
            print("  Prazo: 'A combinar'")

        # Orçamento (obrigatório) — usa o valor mínimo definido pelo job
        amount = page.locator("input[name='bid[amount]']")
        if await amount.count() > 0:
            min_val = await amount.get_attribute("min") or BID_AMOUNT
            await amount.fill(str(min_val))
            print(f"  Orçamento: R$ {min_val} (min do job)")

        await page.wait_for_timeout(500)
        await page.screenshot(path=str(LOGS / "prop_filled.png"), full_page=True)

        # Submit
        await page.locator("input[value='Enviar orçamento'], input[type='submit']").last.click()
        await page.wait_for_timeout(4000)

        final_url = page.url
        await page.screenshot(path=str(LOGS / "prop_after_submit.png"), full_page=True)
        print(f"  URL após submit: {final_url}")

        if "/inbox/" in final_url:
            print("✓ Proposta enviada! (redirect → /inbox/)")
        elif "/bid/" not in final_url:
            print("✓ Proposta enviada! (saiu do formulário)")
        else:
            print("✗ Ainda no formulário — ver prop_after_submit.png")

    except Exception as exc:
        await page.screenshot(path=str(LOGS / "prop_error.png"))
        print(f"✗ Erro: {exc}")
    finally:
        await page.close()


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
        await login(ctx)
        await send_proposal(ctx)
        await browser.close()


asyncio.run(main())
