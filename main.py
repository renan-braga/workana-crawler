#!/usr/bin/env python3
"""
Bot Workana — monitora novos projetos e notifica via Telegram.

Uso:
  python main.py run          Inicia o bot em modo daemon (coleta a cada 30 min)
  python main.py run --once   Coleta uma vez e encerra
"""
import asyncio
import logging
import sys
from datetime import date, datetime
from pathlib import Path

import click
import yaml
from rich.console import Console

console = Console()
CONFIG_PATH = Path(__file__).parent / "config.yaml"
log = logging.getLogger(__name__)

# Contador de ciclos consecutivos com 0 jobs na listagem (possível sessão morta)
_zero_listing_streak = 0
_ZERO_STREAK_THRESHOLD = 2  # notifica após 2 ciclos seguidos vazios


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        console.print(f"[red]config.yaml não encontrado em {CONFIG_PATH}[/red]")
        sys.exit(1)
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def setup_logging(verbose: bool):
    level = logging.DEBUG if verbose else logging.INFO
    log_dir = Path(__file__).parent / "logs"
    log_dir.mkdir(exist_ok=True)

    handlers = [
        logging.StreamHandler(),
        logging.FileHandler(log_dir / "bot.log", encoding="utf-8"),
    ]
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )
    for lib in ("httpx", "httpcore", "telegram", "apscheduler", "playwright"):
        logging.getLogger(lib).setLevel(logging.WARNING)


def _days_until_expiry(config: dict) -> int | None:
    expires_str = config.get("workana", {}).get("session_expires")
    if not expires_str:
        return None
    try:
        expires = datetime.strptime(str(expires_str), "%Y-%m-%d").date()
        return (expires - date.today()).days
    except ValueError:
        return None


async def _check_session_expiry(config: dict, app, chat_id: str):
    """Envia aviso se a sessão estiver próxima de expirar."""
    from src.bot import notify_warning

    days = _days_until_expiry(config)
    if days is None:
        return

    if days < 0:
        await notify_warning(
            "🔴 <b>Sessão Workana expirada!</b>\n\n"
            "O cookie <code>workana_session</code> já venceu. "
            "Acesse o Workana, copie os novos cookies e atualize o <code>config.yaml</code>.\n\n"
            "O bot está parado até a renovação.",
            app, chat_id,
        )
    elif days <= 5:
        await notify_warning(
            f"⚠️ <b>Sessão Workana expira em {days} dia(s)!</b>\n\n"
            "Acesse o Workana, copie os cookies e atualize <code>config.yaml</code> "
            "antes que o bot pare de funcionar.",
            app, chat_id,
        )


async def run_cycle(config: dict, app=None):
    """Executa um ciclo completo: busca jobs novos e notifica."""
    global _zero_listing_streak

    from src.storage import get_seen_slugs, mark_seen
    from src.crawler import WorkanaCrawler
    from src.bot import notify_new_job, notify_warning

    chat_id = config["telegram"]["chat_id"]

    # Verifica expiração antes de qualquer coisa (só no modo daemon)
    if app:
        await _check_session_expiry(config, app, chat_id)

        days = _days_until_expiry(config)
        if days is not None and days < 0:
            log.error("Sessão expirada — abortando ciclo.")
            return

    crawler = WorkanaCrawler(config)
    try:
        listing = await crawler.fetch_listing()

        # Detecta listagem vazia — possível sessão morta
        if len(listing) == 0:
            _zero_listing_streak += 1
            log.warning("Listagem retornou 0 jobs (streak=%d)", _zero_listing_streak)
            if app and _zero_listing_streak >= _ZERO_STREAK_THRESHOLD:
                await notify_warning(
                    "⚠️ <b>Workana sem resultados por 2 ciclos seguidos.</b>\n\n"
                    "Possíveis causas:\n"
                    "• Sessão expirada (verifique os cookies)\n"
                    "• Site fora do ar\n"
                    "• Nenhum projeto novo nos filtros configurados\n\n"
                    f"Última verificação: {datetime.now().strftime('%d/%m %H:%M')}",
                    app, chat_id,
                )
            return

        _zero_listing_streak = 0  # reset ao encontrar jobs

        seen = await get_seen_slugs()
        jobs = await crawler.fetch_new_jobs_from_listing(listing, seen)

        if not jobs:
            log.info("Nenhum job novo encontrado.")
            return

        for job in jobs:
            await mark_seen(job.slug, job.title)
            if app:
                await notify_new_job(job, app, chat_id)
            else:
                console.print(f"[green]Novo job:[/green] {job.title} — {job.url}")

    finally:
        await crawler.close()


@click.group()
@click.option("--verbose", "-v", is_flag=True, default=False)
@click.pass_context
def cli(ctx, verbose):
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    setup_logging(verbose)


@cli.command()
@click.option("--once", is_flag=True, default=False, help="Executa uma vez e encerra.")
@click.pass_context
def run(ctx, once):
    """Inicia o bot Workana."""
    config = load_config()

    if config.get("openai", {}).get("api_key", "").startswith("COLOQUE"):
        console.print("[yellow]⚠️  Defina sua OpenAI API key em config.yaml antes de rodar.[/yellow]")
        if not once:
            sys.exit(1)

    async def _init_db():
        from src.storage import init_db
        await init_db()

    asyncio.run(_init_db())

    if once:
        console.print("[bold blue]Executando coleta única…[/bold blue]")
        asyncio.run(run_cycle(config, app=None))
        console.print("[green]Concluído.[/green]")
        return

    from src.bot import build_application
    from src.crawler import WorkanaCrawler

    app = build_application(config)
    crawler = WorkanaCrawler(config)
    app.bot_data["crawler"] = crawler
    interval = int(config.get("scheduler", {}).get("interval_minutes", 30))

    async def scheduled_job(ctx_):
        log.info("[scheduler] Iniciando ciclo de coleta…")
        await run_cycle(config, app=app)

    app.job_queue.run_once(scheduled_job, when=3)
    app.job_queue.run_repeating(scheduled_job, interval=interval * 60, first=interval * 60)

    console.print(f"[bold green]Bot iniciado — coleta a cada {interval} min. Ctrl+C para parar.[/bold green]")
    try:
        app.run_polling(allowed_updates=["callback_query"])
    finally:
        asyncio.run(crawler.close())


if __name__ == "__main__":
    cli()
