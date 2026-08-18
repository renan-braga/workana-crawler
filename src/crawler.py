"""
Crawler do Workana usando Playwright.
Acessa a listagem de jobs, extrai cards, e busca detalhes de cada job novo.
"""
import asyncio
import logging
import random
import re
from dataclasses import dataclass, field
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, Browser, BrowserContext

logger = logging.getLogger(__name__)

BASE = "https://www.workana.com"


class WorkanaAccessDenied(Exception):
    """Job fechado, encerrado ou sem permissão — erro permanente, não retentável."""


@dataclass
class WorkanaJob:
    slug: str
    title: str
    description: str
    skills: list[str] = field(default_factory=list)
    budget: str | None = None
    bid_count: int = 0
    published: str = ""
    country: str | None = None
    client_name: str | None = None
    projects_posted: int | None = None
    projects_paid: int | None = None
    payment_verified: bool = False
    member_since: str | None = None
    url: str = ""


def _build_playwright_cookies(cfg_cookies: dict) -> list[dict]:
    result = []
    for name, value in cfg_cookies.items():
        result.append({
            "name": name,
            "value": str(value),
            "domain": "www.workana.com",
            "path": "/",
        })
    return result


def _parse_max_pages(html: str) -> int:
    """Detecta o número total de páginas pela paginação da listagem."""
    soup = BeautifulSoup(html, "lxml")
    pag = soup.find("ul", class_="pagination")
    if not pag:
        return 1
    pages = [int(m.group(1)) for a in pag.find_all("a", href=True)
             if (m := re.search(r"page=(\d+)", a["href"]))]
    return max(pages) if pages else 1


def _parse_listing(html: str) -> list[dict]:
    """Extrai dados básicos de cada card na página de listagem."""
    soup = BeautifulSoup(html, "lxml")
    # Captura tanto project-item quanto project-item-featured (jobs em destaque)
    cards = soup.find_all("div", class_=re.compile(r"^project-item"))
    jobs = []

    for card in cards:
        try:
            a_tag = card.find("a", href=re.compile(r"^/job/"))
            if not a_tag:
                continue

            href = a_tag["href"]
            slug = href.split("/job/")[-1].rstrip("/")
            title_span = a_tag.find("span", title=True)
            title = title_span["title"] if title_span else a_tag.get_text(strip=True)

            # published / bids
            published = ""
            date_el = card.find("span", class_="date")
            if date_el:
                published = date_el.get_text(strip=True).replace("Publicado:", "").strip()

            bid_count = 0
            bids_el = card.find("span", class_="bids")
            if bids_el:
                m = re.search(r"\d+", bids_el.get_text())
                if m:
                    bid_count = int(m.group(0))

            # description snippet
            desc_span = card.find("p", class_="text-expander-content")
            desc = ""
            if desc_span:
                spans = desc_span.find_all("span")
                if spans:
                    desc = spans[0].get_text(strip=True)

            # skills
            skills = [h.get_text(strip=True) for h in card.find_all("h3")]

            # budget
            budget = None
            budget_el = card.find("p", class_="budget")
            if budget_el:
                val = budget_el.find("span", class_="values")
                if val:
                    budget = val.get_text(strip=True)

            # country
            country = None
            country_el = card.find("span", class_="country-name")
            if country_el:
                country = country_el.get_text(strip=True)

            # client name
            client_name = None
            author_el = card.find("span", class_="author-info")
            if author_el:
                client_name = author_el.get_text(strip=True)

            jobs.append({
                "slug": slug,
                "title": title,
                "desc_snippet": desc,
                "skills": skills,
                "budget": budget,
                "bid_count": bid_count,
                "published": published,
                "country": country,
                "client_name": client_name,
                "url": f"{BASE}/job/{slug}",
            })
        except Exception as exc:
            logger.debug("Erro ao parsear card: %s", exc)

    return jobs


def _parse_detail(html: str, base: dict) -> WorkanaJob:
    """Extrai descrição completa e dados do cliente na página de detalhe."""
    soup = BeautifulSoup(html, "lxml")

    # Full description
    expander = soup.find("div", class_="expander")
    description = expander.get_text(" ", strip=True) if expander else base["desc_snippet"]

    # Skills (detail page is more complete)
    skills_div = soup.find("div", class_="skills")
    skills = base["skills"]
    if skills_div:
        skills = [h.get_text(strip=True) for h in skills_div.find_all(["h3", "a"])]
        skills = [s for s in skills if s]

    # Client info from aside
    aside = soup.find("aside")
    projects_posted = projects_paid = None
    payment_verified = False
    member_since = client_name = None

    if aside:
        text = aside.get_text(" ", strip=True)
        client_name = base.get("client_name")

        m = re.search(r"(\d+)\s*Projetos? publicados?", text, re.I)
        if m:
            projects_posted = int(m.group(1))

        m = re.search(r"(\d+)\s*Projetos? pagos?", text, re.I)
        if m:
            projects_paid = int(m.group(1))

        payment_verified = bool(re.search(r"Pagamento verificado", text, re.I))

        m = re.search(r"Membro desde[:\s]+([^\n]+?)(?=\s+Atividades|\s+Projetos|\s*$)", text)
        if m:
            member_since = m.group(1).strip()

    # Bid count from detail (sometimes more accurate)
    bid_count = base["bid_count"]
    text_full = soup.get_text(" ")
    m = re.search(r"(\d+)\s*Propostas?", text_full, re.I)
    if m:
        bid_count = int(m.group(1))

    return WorkanaJob(
        slug=base["slug"],
        title=base["title"],
        description=description,
        skills=skills,
        budget=base.get("budget"),
        bid_count=bid_count,
        published=base.get("published", ""),
        country=base.get("country"),
        client_name=client_name,
        projects_posted=projects_posted,
        projects_paid=projects_paid,
        payment_verified=payment_verified,
        member_since=member_since,
        url=base["url"],
    )


class WorkanaCrawler:
    def __init__(self, config: dict):
        self.config = config
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._authenticated: bool = False

    async def _ensure_browser(self):
        # Reinicia se o processo do browser foi encerrado inesperadamente
        if self._browser is not None and not self._browser.is_connected():
            logger.warning("[crawler] Browser desconectado — reiniciando…")
            try:
                await self._pw.stop()
            except Exception:
                pass
            self._browser = None
            self._context = None
            self._authenticated = False

        if self._browser is None:
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            self._context = await self._browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/136.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
            )
            await self._context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            cookies = _build_playwright_cookies(
                self.config.get("workana", {}).get("cookies", {})
            )
            await self._context.add_cookies(cookies)
            logger.info("[crawler] Browser iniciado")

    async def _login(self) -> bool:
        """Faz login via JavaScript (contorna inputs React que ignoram fill/type)."""
        from pathlib import Path

        wk = self.config.get("workana", {})
        email = wk.get("email", "")
        password = wk.get("password", "")
        if not email or not password:
            logger.error("[crawler] Credenciais não configuradas.")
            return False

        # Limpa cookies existentes para evitar conflito com CSRF do formulário de login
        await self._context.clear_cookies()

        page = await self._context.new_page()
        try:
            await page.goto("https://www.workana.com/login", wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2000)

            # Fecha o banner de cookies
            cookie_btn = page.locator("button:has-text('Aceitar todos os cookies')")
            if await cookie_btn.count() > 0:
                await cookie_btn.click()
                await page.wait_for_timeout(800)

            # Aguarda a página React hidratar
            await page.wait_for_selector("input[type='email']", timeout=10000)

            # Email — clica e digita caractere a caractere via teclado
            await page.locator("input[type='email']").click()
            await page.wait_for_timeout(300)
            await page.keyboard.type(email, delay=80)
            await page.wait_for_timeout(300)

            # Senha
            await page.locator("input[type='password']").click()
            await page.wait_for_timeout(300)
            await page.keyboard.type(password, delay=80)
            await page.wait_for_timeout(300)

            # Screenshot antes de submeter para confirmar preenchimento
            path_before = Path(__file__).parent.parent / "logs" / "login_before_submit.png"
            await page.screenshot(path=str(path_before))
            logger.info("[crawler] Screenshot pré-submit: %s", path_before)

            submit = page.locator("button:has-text('Login'), button[type='submit']").first
            await submit.click()
            await page.wait_for_timeout(5000)

            current = page.url
            if "/login" in current or "/signup" in current:
                path = Path(__file__).parent.parent / "logs" / "login_failed.png"
                await page.screenshot(path=str(path))
                logger.error("[crawler] Login falhou — URL: %s | screenshot: %s", current, path)
                return False

            logger.info("[crawler] Login OK: %s", current)
            self._authenticated = True
            return True
        except Exception as exc:
            logger.error("[crawler] Erro no login: %s", exc)
            return False
        finally:
            await page.close()

    async def _verify_session_cookies(self) -> bool:
        """Verifica se os cookies de sessão já estão válidos, sem tentar login."""
        page = await self._context.new_page()
        try:
            await page.goto(
                "https://www.workana.com/dashboard",
                wait_until="domcontentloaded",
                timeout=20000,
            )
            url = page.url
            if "/login" in url or "/signup" in url:
                logger.info("[crawler] Cookies de sessão inválidos ou expirados")
                return False
            logger.info("[crawler] Sessão válida via cookies (URL: %s)", url)
            self._authenticated = True
            return True
        except Exception as exc:
            logger.warning("[crawler] Verificação de sessão falhou: %s", exc)
            return False
        finally:
            await page.close()

    async def _ensure_authenticated(self):
        await self._ensure_browser()
        if self._authenticated:
            return
        # Tenta validar cookies de sessão antes de fazer login por formulário
        if self.config.get("workana", {}).get("cookies"):
            if await self._verify_session_cookies():
                return
        ok = await self._login()
        if not ok:
            raise RuntimeError("Falha ao autenticar no Workana")

    async def _check_and_reauth(self, page) -> bool:
        """Verifica se a sessão ainda é válida pela URL atual. Re-autentica se necessário."""
        if "/login" in page.url or "/signup" in page.url:
            logger.warning("[crawler] Sessão expirada detectada — re-autenticando…")
            self._authenticated = False
            await page.close()
            await self._login()
            return True  # página foi fechada, chamador deve abrir nova
        return False

    async def close(self):
        if self._browser:
            await self._browser.close()
            await self._pw.stop()
            self._browser = None
            self._context = None

    @staticmethod
    def _bid_url(job_url: str, tab: str = "") -> str:
        slug = job_url.rstrip("/").split("/job/")[-1]
        base = f"{BASE}/messages/bid/{slug}"
        return f"{base}/?tab={tab}" if tab else base

    async def _screenshot(self, page, name: str):
        from pathlib import Path
        from datetime import datetime
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = Path(__file__).parent.parent / "logs" / f"{ts}_{name}.png"
        await page.screenshot(path=str(path))
        logger.info("[crawler] Screenshot: %s", path)

    async def _check_access_denied(self, page) -> bool:
        """Retorna True se a página exibe 'Acesso Negado', salvando screenshot."""
        try:
            el = page.locator("text=Acesso Negado")
            if await el.count() > 0:
                await self._screenshot(page, "access_denied")
                return True
        except Exception:
            pass
        return False

    async def _captcha_required(self, page) -> bool:
        """Retorna True quando o formulário exibe CAPTCHA/Turnstile."""
        try:
            if await page.locator(
                "iframe[src*='challenges.cloudflare.com'], "
                "iframe[src*='turnstile'], "
                ".cf-turnstile, "
                "input[name='cf-turnstile-response']"
            ).count() > 0:
                return True

            body_text = await page.locator("body").inner_text(timeout=2000)
            return bool(re.search(r"\b(CAPTCHA|Verify you are human)\b", body_text, re.I))
        except Exception:
            return False

    async def _selected_skill_count(self, skills_input) -> int:
        """Conta apenas skills selecionadas de verdade no componente do Workana."""
        try:
            return await skills_input.evaluate(
                """
                (input) => {
                  const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
                  const visible = (el) => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.visibility !== 'hidden'
                      && style.display !== 'none'
                      && rect.width > 0
                      && rect.height > 0;
                  };
                  const root = (() => {
                    let node = input.parentElement;
                    while (node && node !== document.body) {
                      const content = clean(node.textContent);
                      if (/Destaque suas habilidades|Selecione até 5 habilidades/i.test(content)) {
                        return node;
                      }
                      node = node.parentElement;
                    }
                    return input.closest('section, article, .box, .panel, .card, .module, .well')
                      || input.parentElement
                      || document.body;
                  })();

                  const fieldValues = [...root.querySelectorAll(
                    'input[name="bid[skills][]"], input[name="bid[skills]"], select[name="bid[skills][]"], select[name="bid[skills]"]'
                  )].flatMap((el) => {
                    if (el.tagName === 'SELECT') {
                      return [...el.selectedOptions].map((option) => clean(option.value || option.textContent));
                    }
                    return [clean(el.value)];
                  }).filter(Boolean);

                  const chipValues = [...root.querySelectorAll(
                    '[class*="token"], [class*="tag"], [class*="chip"], [class*="selected"], [class*="choice"], .label'
                  )].filter(visible).map((el) => clean(el.textContent)).filter((value) => {
                    return value
                      && value.length <= 60
                      && !/pesquisar|selecione|habilidade|desculpe|correspond/i.test(value);
                  });

                  return Math.max(new Set(fieldValues).size, new Set(chipValues).size);
                }
                """
            )
        except Exception as exc:
            logger.debug("[crawler] Não consegui contar skills selecionadas: %s", exc)
            return 0

    async def _fill_skills(self, page, skills_input, skills: list[str] | None) -> int:
        """Preenche skills obrigatórias usando o autocomplete, quando não há chip válido."""
        selected = await self._selected_skill_count(skills_input)
        logger.info("[crawler] Skills selecionadas no formulário: %s", selected)
        if selected > 0:
            logger.info("[crawler] Usando skills já selecionadas pelo Workana (%s)", selected)
            return selected

        if not skills:
            logger.warning("[crawler] Nenhuma skill do job disponível para preencher o formulário")
            return 0

        option_selectors = (
            ".ui-autocomplete:visible li, "
            ".select2-results__option, "
            ".tt-menu .tt-suggestion, "
            "ul.typeahead.dropdown-menu:visible li, "
            "[role='listbox'] [role='option'], "
            "[role='option']"
        )
        target = min(len(skills), 2)

        for raw_skill in skills[:5]:
            if selected >= target:
                break
            skill = raw_skill.strip()
            if not skill:
                continue

            before = selected
            try:
                await skills_input.scroll_into_view_if_needed()
                await skills_input.click(click_count=3)
                await page.wait_for_timeout(200)
                await skills_input.fill("")
                await skills_input.type(skill, delay=random.randint(80, 130))
                await page.wait_for_timeout(random.randint(1800, 2600))

                options = page.locator(option_selectors)
                matching = options.filter(has_text=re.compile(re.escape(skill), re.I)).first
                clicked = False
                if await matching.count() > 0 and await matching.is_visible():
                    await matching.click()
                    clicked = True
                else:
                    first_option = options.first
                    if await first_option.count() > 0 and await first_option.is_visible():
                        await first_option.click()
                        clicked = True
                    else:
                        await page.keyboard.press("Enter")

                await page.wait_for_timeout(random.randint(500, 900))
                selected = await self._selected_skill_count(skills_input)
                if selected > before:
                    logger.info("[crawler] Skill adicionada: %s", skill)
                else:
                    logger.warning(
                        "[crawler] Skill não foi aceita pelo autocomplete: %s%s",
                        skill,
                        "" if clicked else " (sem sugestão visível)",
                    )
                    await skills_input.fill("")
                    await page.keyboard.press("Escape")
                    await page.wait_for_timeout(300)
            except Exception as skill_exc:
                logger.warning("[crawler] Skill '%s': %s", skill, skill_exc)

        selected = await self._selected_skill_count(skills_input)
        logger.info("[crawler] Skills selecionadas após preenchimento: %s", selected)
        return selected

    async def _human_type(self, page, locator, text: str):
        """Clica no campo, seleciona tudo e digita com velocidade variável — imita comportamento humano."""
        await locator.click()
        await page.wait_for_timeout(random.randint(300, 600))
        # Seleciona conteúdo existente antes de digitar
        await page.keyboard.press("Control+a")
        await page.wait_for_timeout(random.randint(100, 250))
        # Digita em blocos de palavras com pauses aleatórios — mais rápido que tecla por tecla
        words = text.split(" ")
        for i, word in enumerate(words):
            chunk = word if i == 0 else " " + word
            await locator.type(chunk, delay=random.randint(25, 70))
            # Pausa ocasional simulando pensar/revisar
            if random.random() < 0.15:
                await page.wait_for_timeout(random.randint(300, 800))

    async def _human_scroll(self, page, distance: int = 400):
        """Scroll suave para baixo simulando leitura da página."""
        steps = random.randint(3, 6)
        per_step = distance // steps
        for _ in range(steps):
            await page.mouse.wheel(0, per_step)
            await page.wait_for_timeout(random.randint(80, 200))

    async def submit_proposal(self, job_url: str, text: str, amount: int | None = None, skills: list[str] | None = None) -> bool:
        """Submete uma proposta navegando direto para /messages/bid/{slug}."""
        # Reaproveita cookies de sessão válidos quando possível — só faz login por
        # formulário se a sessão realmente estiver inválida. O CSRF fica fresco de
        # qualquer forma, pois a página do bid é sempre recarregada logo abaixo.
        await self._ensure_authenticated()
        page = await self._context.new_page()

        # Intercepta respostas HTTP >= 400 para diagnóstico
        async def _log_resp(resp):
            if resp.status >= 400:
                logger.warning("[crawler] HTTP %s ← %s", resp.status, resp.url)
        page.on("response", _log_resp)

        try:
            await page.goto(self._bid_url(job_url), wait_until="domcontentloaded", timeout=30000)
            # Simula leitura da página antes de começar a preencher
            await page.wait_for_timeout(random.randint(2500, 4500))
            if await self._check_and_reauth(page):
                page = await self._context.new_page()
                await page.goto(self._bid_url(job_url), wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(random.randint(2500, 4000))

            if await self._check_access_denied(page):
                raise WorkanaAccessDenied(job_url)

            # Scroll suave antes de preencher — simula leitura do formulário
            await self._human_scroll(page, 300)
            await page.wait_for_timeout(random.randint(500, 1000))

            # Texto da proposta — digitado, não colado
            textarea = page.locator("textarea[name='bid[content]']")
            await self._human_type(page, textarea, text)
            await page.wait_for_timeout(random.randint(600, 1200))

            # deliveryTime é numérico — usa o mínimo do campo ou 7 dias como fallback
            delivery = page.locator("input[name='bid[deliveryTime]']")
            if await delivery.count() > 0:
                min_days = await delivery.get_attribute("min")
                await delivery.click(click_count=3)
                await page.wait_for_timeout(random.randint(200, 400))
                await delivery.type(min_days if min_days and min_days.isdigit() else "7", delay=random.randint(50, 100))
                await page.wait_for_timeout(random.randint(300, 600))

            # Orçamento
            amount_field = page.locator("input[name='bid[amount]']")
            if await amount_field.count() > 0:
                min_val = int(await amount_field.get_attribute("min") or 0)
                if amount:
                    bid_val = max(amount, min_val)
                    if bid_val != amount:
                        logger.info("[crawler] Orçamento ajustado: R$ %s → R$ %s (mín do form)", amount, bid_val)
                else:
                    bid_val = max(min_val, 100)
                await amount_field.click(click_count=3)
                await page.wait_for_timeout(random.randint(200, 400))
                await amount_field.type(str(bid_val), delay=random.randint(60, 120))
                logger.info("[crawler] Orçamento: R$ %s", bid_val)
                await page.wait_for_timeout(random.randint(400, 800))

            # Fecha qualquer modal aberto (ex: portfólio) antes de preencher skills
            try:
                if await page.locator("div.modal.fade.in").count() > 0:
                    await page.keyboard.press("Escape")
                    await page.wait_for_timeout(random.randint(700, 1200))
                    logger.info("[crawler] Modal fechado antes de preencher skills")
            except Exception:
                pass

            # Habilidades — o Workana pode pré-selecionar skills; valida pelo valor real do componente
            skills_input = page.locator("input[placeholder*='habilidade'], input[placeholder*='Pesquisar habilidade']").first
            skills_count = await skills_input.count()
            if skills_count > 0:
                await skills_input.scroll_into_view_if_needed()
                await page.wait_for_timeout(random.randint(800, 1500))
                await self._screenshot(page, "skills_section")

                selected_skills = await self._fill_skills(page, skills_input, skills)
                if selected_skills == 0:
                    await self._screenshot(page, "skills_required_missing")
                    logger.warning("[crawler] Nenhuma skill selecionada; envio cancelado antes do submit")
                    return False

            # Fecha banner de cookies caso apareça na página de bid
            try:
                cookie_btn = page.locator("button:has-text('Aceitar todos os cookies')")
                if await cookie_btn.count() > 0 and await cookie_btn.is_visible():
                    await cookie_btn.click()
                    await page.wait_for_timeout(800)
                    logger.info("[crawler] Banner de cookies fechado no bid")
            except Exception:
                pass

            # Pausa final antes de submeter — simula revisão
            await page.wait_for_timeout(random.randint(1500, 3000))
            if await self._captcha_required(page):
                await self._screenshot(page, "captcha_required")
                logger.warning("[crawler] CAPTCHA detectado no formulário; envio cancelado para intervenção manual")
                return False

            submit_btn = page.locator(
                "button:has-text('Enviar orçamento'), "
                "input[value='Enviar orçamento'], "
                "button[type='submit'], input[type='submit']"
            ).last
            await submit_btn.scroll_into_view_if_needed()
            await page.wait_for_timeout(random.randint(500, 800))
            await self._screenshot(page, "pre_submit")
            await submit_btn.click()
            await page.wait_for_timeout(random.randint(4000, 6000))

            ok = "/inbox/" in page.url
            if ok:
                logger.info("[crawler] Proposta enviada — %s", page.url)
                # Aguarda JS de confirmação do Workana executar antes de fechar a página
                # (parâmetros ?added=...&proposal_sent=1 disparam requests de confirmação)
                try:
                    await page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass  # inbox tem requests contínuos — timeout aqui é normal
                await page.wait_for_timeout(random.randint(3000, 5000))
            else:
                await self._screenshot(page, "proposal_submit_failed")
                errors = await page.locator(".error, .alert, .has-error, [class*='error']").all_text_contents()
                errors = [e.strip() for e in errors if e.strip()]
                logger.warning("[crawler] Submit não redirecionou — URL: %s | erros: %s", page.url, errors or "nenhum capturado")
            return ok
        except Exception as exc:
            await self._screenshot(page, "proposal_error")
            logger.error("[crawler] Falha ao enviar proposta para %s: %s", job_url, exc)
            return False
        finally:
            await page.close()

    async def submit_question(self, job_url: str, text: str) -> bool:
        """Envia uma pergunta navegando para /messages/bid/{slug}/?tab=message."""
        await self._ensure_authenticated()
        page = await self._context.new_page()
        try:
            await page.goto(self._bid_url(job_url, tab="message"), wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(random.randint(2500, 4500))
            if await self._check_and_reauth(page):
                page = await self._context.new_page()
                await page.goto(self._bid_url(job_url, tab="message"), wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(random.randint(2500, 4000))

            if await self._check_access_denied(page):
                raise WorkanaAccessDenied(job_url)

            await self._human_scroll(page, 250)
            await page.wait_for_timeout(random.randint(500, 1000))

            textarea = page.locator("textarea[name='message[content]']")
            await self._human_type(page, textarea, text)
            await page.wait_for_timeout(random.randint(1000, 2500))

            await page.locator("input[type='submit'], input.btn-primary, button[type='submit']").last.click()
            await page.wait_for_timeout(random.randint(3000, 5000))

            ok = "/inbox/" in page.url
            if ok:
                logger.info("[crawler] Pergunta enviada — %s", page.url)
                try:
                    await page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass
                await page.wait_for_timeout(random.randint(3000, 5000))
            else:
                await self._screenshot(page, "question_submit_failed")
                logger.warning("[crawler] Pergunta enviada mas URL inesperada: %s", page.url)
            return ok
        except Exception as exc:
            await self._screenshot(page, "question_error")
            logger.error("[crawler] Falha ao enviar pergunta para %s: %s", job_url, exc)
            return False
        finally:
            await page.close()

    async def fetch_listing(self) -> list[dict]:
        await self._ensure_authenticated()
        base_url = self.config["workana"]["url"].split("&page=")[0]
        max_pages = int(self.config.get("workana", {}).get("max_pages", 3))

        seen_slugs: set[str] = set()
        all_jobs: list[dict] = []
        total_pages = 1

        for page_num in range(1, max_pages + 1):
            url = base_url if page_num == 1 else f"{base_url}&page={page_num}"
            page = await self._context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(3000)
                html = await page.content()

                if page_num == 1:
                    from pathlib import Path
                    dump = Path(__file__).parent.parent / "logs" / "listing_debug.html"
                    dump.write_text(html, encoding="utf-8")
                    total_pages = min(_parse_max_pages(html), max_pages)

                for job in _parse_listing(html):
                    if job["slug"] not in seen_slugs:
                        seen_slugs.add(job["slug"])
                        all_jobs.append(job)

                if page_num >= total_pages:
                    break

                await asyncio.sleep(1.5)
            except Exception as exc:
                logger.warning("[crawler] Falha ao buscar página %d: %s", page_num, exc)
                break
            finally:
                await page.close()

        logger.info("[crawler] %d jobs na listagem (%d página(s) varridas)", len(all_jobs), page_num)
        return all_jobs

    async def fetch_detail(self, job_base: dict) -> WorkanaJob:
        await self._ensure_browser()
        page = await self._context.new_page()
        try:
            await page.goto(job_base["url"], wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(2000)
            html = await page.content()
            return _parse_detail(html, job_base)
        except Exception as exc:
            logger.warning("[crawler] Falha ao buscar detalhe de %s: %s", job_base["slug"], exc)
            return WorkanaJob(
                slug=job_base["slug"],
                title=job_base["title"],
                description=job_base.get("desc_snippet", ""),
                skills=job_base.get("skills", []),
                budget=job_base.get("budget"),
                bid_count=job_base.get("bid_count", 0),
                published=job_base.get("published", ""),
                country=job_base.get("country"),
                client_name=job_base.get("client_name"),
                url=job_base["url"],
            )
        finally:
            await page.close()

    async def fetch_new_jobs_from_listing(
        self, listing: list[dict], seen_slugs: set[str]
    ) -> list[WorkanaJob]:
        new_bases = [j for j in listing if j["slug"] not in seen_slugs]
        logger.info("[crawler] %d jobs novos para processar", len(new_bases))

        results = []
        for base in new_bases:
            job = await self.fetch_detail(base)
            results.append(job)
            await asyncio.sleep(1.5)

        return results

    async def fetch_new_jobs(self, seen_slugs: set[str]) -> list[WorkanaJob]:
        listing = await self.fetch_listing()
        return await self.fetch_new_jobs_from_listing(listing, seen_slugs)
