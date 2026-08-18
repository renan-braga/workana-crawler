"""
Telegram bot: notifica novos jobs e processa escolhas do usuário.

Todo o ciclo de vida de um job (escolha de ação, estilo, contexto extra,
geração e envio) é gerenciado editando a mesma mensagem do card — nunca
criando mensagens novas — para manter o chat organizado.
"""
import dataclasses
import json
import logging
from pathlib import Path
from telegram import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from .crawler import WorkanaJob

logger = logging.getLogger(__name__)

_STATE_FILE = Path(__file__).parent.parent / "logs" / "bot_state.json"

# IDs curtos para callback_data (limite Telegram: 64 bytes)
_next_id: int = 1
_jobs: dict[str, dict] = {}             # short_id → estado completo do job (card único, editado in-place)
_awaiting_context: dict[str, str] = {}  # chat_id → short_id aguardando texto extra do usuário


def _new_id() -> str:
    global _next_id
    val = _next_id
    _next_id += 1
    _save_state()
    return str(val)


def _save_state():
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        state = {
            "next_id": _next_id,
            "jobs": {
                k: {**v, "job": dataclasses.asdict(v["job"])}
                for k, v in _jobs.items()
            },
        }
        _STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    except Exception as exc:
        logger.warning("[bot] Falha ao salvar estado: %s", exc)


def _load_state():
    global _next_id
    if not _STATE_FILE.exists():
        return
    try:
        state = json.loads(_STATE_FILE.read_text())
        _next_id = state.get("next_id", 1)
        for k, v in state.get("jobs", {}).items():
            job_data = v.pop("job")
            v["job"] = WorkanaJob(**job_data)
            _jobs[k] = v
        logger.info("[bot] Estado restaurado: %d jobs, próximo id=%d", len(_jobs), _next_id)
    except Exception as exc:
        logger.warning("[bot] Falha ao restaurar estado: %s", exc)


_load_state()


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _save_submission(job: "WorkanaJob", action: str, text: str):
    """Salva proposta/pergunta enviada em arquivo de texto para consulta futura."""
    from datetime import datetime
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    label = "proposta" if action == "proposal" else "pergunta"
    path = _STATE_FILE.parent / f"{ts}_{label}_{job.slug[:60]}.txt"
    try:
        path.write_text(
            f"URL: {job.url}\nTítulo: {job.title}\n\n{text}\n",
            encoding="utf-8",
        )
        logger.info("[bot] %s salva em %s", label.capitalize(), path.name)
    except Exception as exc:
        logger.warning("[bot] Falha ao salvar %s: %s", label, exc)


def _job_header(job: WorkanaJob) -> str:
    lines = [
        f"🆕 <b>{_esc(job.title)}</b>",
        "",
        f"📝 {_esc(job.description[:600])}{'…' if len(job.description) > 600 else ''}",
        "",
    ]

    if job.skills:
        lines.append(f"🛠 <b>Skills:</b> {_esc(', '.join(job.skills[:8]))}")

    if job.budget:
        from .gpt import _budget_note
        lines.append(f"💰 <b>Budget:</b> {_esc(_budget_note(job.budget))}")

    if job.country:
        lines.append(f"🌎 <b>País:</b> {_esc(job.country)}")

    client_parts = []
    if job.projects_posted is not None:
        client_parts.append(f"{job.projects_posted} publicados")
    if job.projects_paid is not None:
        client_parts.append(f"{job.projects_paid} pagos")
    if job.payment_verified:
        client_parts.append("✅ pgto verificado")
    if job.member_since:
        client_parts.append(f"desde {job.member_since}")
    if client_parts:
        lines.append(f"👤 <b>Cliente:</b> {_esc(' · '.join(client_parts))}")

    if job.published:
        lines.append(f"🕐 <b>Publicado:</b> {_esc(job.published)}")

    lines.append(f'\n🔗 <a href="{job.url}">Ver no Workana</a>')
    return "\n".join(lines)


def _keyboard(short_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✍️ Proposta", callback_data=f"p:{short_id}"),
            InlineKeyboardButton("❓ Pergunta", callback_data=f"q:{short_id}"),
        ],
        [
            InlineKeyboardButton("🚫 Ignorar", callback_data=f"i:{short_id}"),
        ],
    ])


def _style_keyboard(short_id: str) -> InlineKeyboardMarkup:
    from .gpt import STYLES
    buttons = [
        InlineKeyboardButton(cfg["label"], callback_data=f"st:{style_id}:{short_id}")
        for style_id, cfg in STYLES.items()
    ]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton("✏️ Valor/Contexto", callback_data=f"ctx:{short_id}")])
    rows.append([InlineKeyboardButton("🔙 Voltar", callback_data=f"back:{short_id}")])
    return InlineKeyboardMarkup(rows)


_TIER_BUTTON_LABELS = {
    "abaixo": "⬇️ Abaixo do projeto",
    "minimo": "🔽 Mínimo",
    "medio": "➡️ Médio",
    "maximo": "🔼 Máximo",
    "acima": "⬆️ Acima do projeto",
}


def _price_tier_keyboard(short_id: str, job: WorkanaJob) -> InlineKeyboardMarkup:
    from .gpt import price_tiers
    tiers = price_tiers(job.budget)
    rows = []
    for key, label in _TIER_BUTTON_LABELS.items():
        val = tiers.get(key)
        text = f"{label} (R$ {val:,})".replace(",", ".") if val else label
        rows.append([InlineKeyboardButton(text, callback_data=f"pt:{key}:{short_id}")])
    rows.append([InlineKeyboardButton("🤖 Deixar a IA decidir", callback_data=f"pt:auto:{short_id}")])
    rows.append([InlineKeyboardButton("✏️ Valor/Contexto", callback_data=f"ctx:{short_id}")])
    rows.append([InlineKeyboardButton("🔙 Voltar", callback_data=f"back:{short_id}")])
    return InlineKeyboardMarkup(rows)


def _confirm_keyboard(short_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Enviar", callback_data=f"send:{short_id}"),
            InlineKeyboardButton("🔄 Regerar", callback_data=f"retry:{short_id}"),
        ],
        [
            InlineKeyboardButton("✏️ Valor/Contexto", callback_data=f"ctx:{short_id}"),
            InlineKeyboardButton("❌ Cancelar", callback_data=f"cancel:{short_id}"),
        ],
    ])


def _manual_draft_keyboard(short_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Já enviei", callback_data=f"done:{short_id}"),
            InlineKeyboardButton("🔄 Regerar", callback_data=f"retry:{short_id}"),
        ],
        [
            InlineKeyboardButton("❌ Cancelar", callback_data=f"cancel:{short_id}"),
        ],
    ])


def _bid_url(job_url: str) -> str:
    slug = job_url.rstrip("/").split("/job/")[-1]
    return f"https://www.workana.com/messages/bid/{slug}"


def _card_state(entry: dict) -> tuple[str, InlineKeyboardMarkup]:
    """Monta o texto e o teclado atuais do card, a partir do estado do job."""
    job = entry["job"]
    text = _job_header(job)

    if entry.get("extra_context"):
        text += f"\n\n📝 <b>Contexto extra:</b> {_esc(entry['extra_context'])}"

    if entry.get("text"):
        from .gpt import STYLES
        label = "proposta" if entry["action"] == "p" else "pergunta"
        style_label = STYLES.get(entry.get("style"), {}).get("label", entry.get("style"))
        tier_key = entry.get("price_tier")
        tier_suffix = f" · {_TIER_BUTTON_LABELS[tier_key]}" if tier_key and tier_key in _TIER_BUTTON_LABELS else ""
        amount_line = f"\n\n💰 <b>Orçamento sugerido:</b> R$ {entry['amount']:,}" if entry.get("amount") else ""
        text += (
            f"\n\n📋 <b>{label.capitalize()} gerada</b> ({_esc(style_label)}{_esc(tier_suffix)}):\n\n"
            f"{_esc(entry['text'])}{amount_line}"
            "\n\n<i>💬 Quer ajustar (valor, foco, tom)? Clique ✏️ Valor/Contexto, digite "
            "e depois clique 🔄 Regerar pra aplicar.</i>"
        )
        return text, _confirm_keyboard(entry["short_id"])

    if entry.get("style"):
        text += "\n\n💰 Escolha a faixa de valor:"
        return text, _price_tier_keyboard(entry["short_id"], job)

    if entry.get("action"):
        text += (
            "\n\n💬 Escolha o tom/estilo abaixo, ou clique ✏️ Valor/Contexto pra dar uma "
            "instrução extra antes (ex: um detalhe do projeto)."
        )
        return text, _style_keyboard(entry["short_id"])

    return text, _keyboard(entry["short_id"])


async def _push_card(bot, entry: dict):
    """Reflete o estado atual do job na mensagem-card (edição in-place)."""
    text, markup = _card_state(entry)
    try:
        await bot.edit_message_text(
            chat_id=entry["chat_id"],
            message_id=entry["message_id"],
            text=text,
            parse_mode="HTML",
            reply_markup=markup,
            disable_web_page_preview=True,
        )
    except Exception as exc:
        logger.warning("[bot] Falha ao atualizar card: %s", exc)


async def notify_warning(text: str, app: Application, chat_id: str):
    await app.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    logger.warning("[bot] Aviso: %s", text[:80])


async def notify_new_job(job: WorkanaJob, app: Application, chat_id: str):
    short_id = _new_id()
    msg = await app.bot.send_message(
        chat_id=chat_id,
        text=_job_header(job),
        parse_mode="HTML",
        reply_markup=_keyboard(short_id),
        disable_web_page_preview=True,
    )
    _jobs[short_id] = {
        "short_id": short_id,
        "job": job,
        "chat_id": msg.chat_id,
        "message_id": msg.message_id,
        "action": None,
        "style": None,
        "price_tier": None,
        "extra_context": None,
        "text": None,
        "amount": None,
    }
    _save_state()
    logger.info("[bot] Notificado: %s (id=%s)", job.slug, short_id)


async def _run_generation(context: ContextTypes.DEFAULT_TYPE, entry: dict):
    job = entry["job"]
    gpt_action = "proposal" if entry["action"] == "p" else "question"
    label = "proposta" if gpt_action == "proposal" else "pergunta"

    _awaiting_context.pop(str(entry["chat_id"]), None)

    header = _job_header(job)
    if entry.get("extra_context"):
        header += f"\n\n📝 <b>Contexto extra:</b> {_esc(entry['extra_context'])}"

    await context.bot.edit_message_text(
        chat_id=entry["chat_id"],
        message_id=entry["message_id"],
        text=header + f"\n\n⏳ Gerando {label}…",
        parse_mode="HTML",
        reply_markup=None,
        disable_web_page_preview=True,
    )

    config = context.bot_data.get("config", {})
    from . import gpt
    directives = []
    if entry.get("extra_context"):
        directives.append(entry["extra_context"])
    price_directive = gpt.price_tier_directive(job.budget, entry.get("price_tier"))
    if price_directive:
        directives.append(price_directive)
    combined_extra = "\n".join(directives) or None

    result = await gpt.generate(
        job, gpt_action, config,
        style=entry["style"], extra_context=combined_extra,
    )
    entry["text"] = result["text"]
    entry["amount"] = result.get("amount")
    _save_state()

    _awaiting_context[str(entry["chat_id"])] = entry["short_id"]  # permite ajustar e regerar
    await _push_card(context.bot, entry)


async def _send_to_workana(context: ContextTypes.DEFAULT_TYPE, entry: dict):
    job = entry["job"]
    gpt_action = "proposal" if entry["action"] == "p" else "question"
    label = "proposta" if gpt_action == "proposal" else "pergunta"
    text = entry["text"]
    short_id = entry["short_id"]
    config = context.bot_data.get("config", {})
    manual_submit = bool(config.get("workana", {}).get("manual_submit", False))
    manual_submit_on_challenge = bool(config.get("workana", {}).get("manual_submit_on_challenge", True))

    _awaiting_context.pop(str(entry["chat_id"]), None)

    await context.bot.edit_message_text(
        chat_id=entry["chat_id"],
        message_id=entry["message_id"],
        text=_job_header(job) + f"\n\n⏳ {'Preparando' if manual_submit and gpt_action == 'proposal' else 'Enviando'} {label} no Workana…",
        parse_mode="HTML",
        reply_markup=None,
        disable_web_page_preview=True,
    )

    crawler = context.bot_data.get("crawler")
    if crawler is None:
        await context.bot.edit_message_text(
            chat_id=entry["chat_id"],
            message_id=entry["message_id"],
            text=_job_header(job) + "\n\n⚠️ Crawler não disponível. Reinicie o bot.",
            parse_mode="HTML",
            reply_markup=_confirm_keyboard(short_id),
            disable_web_page_preview=True,
        )
        return

    from .crawler import WorkanaAccessDenied, WorkanaChallengeRequired

    async def _send_manual_draft_after_challenge():
        amount_line = f"\n\n💰 <b>Orçamento sugerido:</b> R$ {entry['amount']:,}" if entry.get("amount") else ""
        await context.bot.edit_message_text(
            chat_id=entry["chat_id"],
            message_id=entry["message_id"],
            text=(
                _job_header(job)
                + "\n\n🛑 <b>Workana pediu Cloudflare/CAPTCHA.</b>\n\n"
                "Para proteger a conta, parei a automação neste envio. Abra o orçamento no seu navegador normal, "
                "resolva a verificação se aparecer e cole o rascunho abaixo."
                + f'\n\n🔗 <a href="{_bid_url(job.url)}">Abrir orçamento no Workana</a>'
                + amount_line
                + f"\n\n📋 <b>Rascunho:</b>\n\n<pre>{_esc(text)}</pre>"
            ),
            parse_mode="HTML",
            reply_markup=_manual_draft_keyboard(short_id),
            disable_web_page_preview=True,
        )

    ok = False
    denied = False
    challenge_required = False
    try:
        if gpt_action == "proposal" and manual_submit:
            prepared = await crawler.prepare_proposal_for_manual_submit(
                job.url,
                text,
                amount=entry.get("amount"),
                skills=job.skills,
            )
            if prepared.get("ready"):
                hold_minutes = prepared.get("hold_minutes", 15)
                screenshot = prepared.get("screenshot")
                await context.bot.edit_message_text(
                    chat_id=entry["chat_id"],
                    message_id=entry["message_id"],
                    text=(
                        _job_header(job)
                        + f"\n\n🟣 <b>Proposta preparada para envio manual.</b>\n\n"
                        f"A janela do Workana está aberta por {hold_minutes} min com a proposta preenchida. "
                        "Revise e clique em <b>Enviar orçamento</b> manualmente."
                        + (f"\n\nScreenshot: <code>{_esc(str(screenshot))}</code>" if screenshot else "")
                    ),
                    parse_mode="HTML",
                    reply_markup=None,
                    disable_web_page_preview=True,
                )
                _jobs.pop(short_id, None)
                _save_state()
                return
            ok = False
        elif gpt_action == "proposal":
            ok = await crawler.submit_proposal(job.url, text, amount=entry.get("amount"), skills=job.skills)
        else:
            ok = await crawler.submit_question(job.url, text)
    except WorkanaAccessDenied:
        denied = True
    except WorkanaChallengeRequired:
        if gpt_action == "proposal" and manual_submit_on_challenge:
            await _send_manual_draft_after_challenge()
            return
        else:
            challenge_required = True
    except Exception as exc:
        logger.error("[bot] Erro ao submeter: %s", exc)
        ok = False

    if denied:
        await context.bot.edit_message_text(
            chat_id=entry["chat_id"],
            message_id=entry["message_id"],
            text=_job_header(job) + f"\n\n🚫 Job fechado ou sem permissão de acesso — {label} não enviada.",
            parse_mode="HTML",
            reply_markup=None,
            disable_web_page_preview=True,
        )
        _jobs.pop(short_id, None)
        _save_state()
        return

    if challenge_required:
        await context.bot.edit_message_text(
            chat_id=entry["chat_id"],
            message_id=entry["message_id"],
            text=(
                _job_header(job)
                + f"\n\n🛑 Workana pediu verificação Cloudflare/CAPTCHA — {label} não enviada. "
                "Abra o Workana manualmente nessa sessão, resolva a verificação se aparecer e aguarde alguns minutos antes de tentar de novo."
            ),
            parse_mode="HTML",
            reply_markup=None,
            disable_web_page_preview=True,
        )
        return

    if ok:
        _save_submission(job, gpt_action, text)
        status = "✅ Proposta enviada" if gpt_action == "proposal" else "✅ Pergunta enviada"
        await context.bot.edit_message_text(
            chat_id=entry["chat_id"],
            message_id=entry["message_id"],
            text=_job_header(job) + f"\n\n<b>{status}</b>",
            parse_mode="HTML",
            reply_markup=None,
            disable_web_page_preview=True,
        )
        _jobs.pop(short_id, None)
        _save_state()
    else:
        await context.bot.edit_message_text(
            chat_id=entry["chat_id"],
            message_id=entry["message_id"],
            text=_job_header(job) + f"\n\n❌ Falha ao enviar {label}. Tente novamente.",
            parse_mode="HTML",
            reply_markup=_confirm_keyboard(short_id),
            disable_web_page_preview=True,
        )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass  # callback query expirou (>10 min) — continua processando normalmente

    data = query.data or ""
    if ":" not in data:
        return

    parts = data.split(":")
    action = parts[0]

    if action == "i":
        short_id = parts[1]
        entry = _jobs.pop(short_id, None)
        _awaiting_context.pop(str(query.message.chat_id), None)
        _save_state()
        if entry:
            await query.edit_message_text(
                _job_header(entry["job"]) + "\n\n🚫 <b>Ignorado</b>",
                parse_mode="HTML",
                reply_markup=None,
                disable_web_page_preview=True,
            )
        return

    if action in ("p", "q"):
        short_id = parts[1]
        entry = _jobs.get(short_id)
        if not entry:
            return
        entry["action"] = action
        _awaiting_context[str(query.message.chat_id)] = short_id
        _save_state()
        await _push_card(context.bot, entry)
        return

    if action == "back":
        short_id = parts[1]
        entry = _jobs.get(short_id)
        if not entry:
            return
        if entry.get("style"):
            entry["style"] = None
            entry["price_tier"] = None
        else:
            entry.update(action=None, extra_context=None)
            _awaiting_context.pop(str(query.message.chat_id), None)
        _save_state()
        await _push_card(context.bot, entry)
        return

    if action == "st":
        style_id, short_id = parts[1], parts[2]
        entry = _jobs.get(short_id)
        if not entry or not entry.get("action"):
            return
        entry["style"] = style_id
        _save_state()
        await _push_card(context.bot, entry)
        return

    if action == "pt":
        tier_key, short_id = parts[1], parts[2]
        entry = _jobs.get(short_id)
        if not entry or not entry.get("style"):
            return
        entry["price_tier"] = tier_key
        await _run_generation(context, entry)
        return

    if action == "retry":
        short_id = parts[1]
        entry = _jobs.get(short_id)
        if not entry or not entry.get("style"):
            return
        await _run_generation(context, entry)
        return

    if action == "cancel":
        short_id = parts[1]
        entry = _jobs.get(short_id)
        if not entry:
            return
        entry.update(action=None, style=None, price_tier=None, text=None, amount=None, extra_context=None)
        _awaiting_context.pop(str(query.message.chat_id), None)
        _save_state()
        await _push_card(context.bot, entry)
        return

    if action == "done":
        short_id = parts[1]
        entry = _jobs.pop(short_id, None)
        _awaiting_context.pop(str(query.message.chat_id), None)
        _save_state()
        if entry:
            job = entry["job"]
            if entry.get("text"):
                _save_submission(job, "proposal" if entry.get("action") == "p" else "question", entry["text"])
            await query.edit_message_text(
                _job_header(job) + "\n\n✅ <b>Marcado como enviado manualmente</b>",
                parse_mode="HTML",
                reply_markup=None,
                disable_web_page_preview=True,
            )
        return

    if action == "send":
        short_id = parts[1]
        entry = _jobs.get(short_id)
        if not entry or not entry.get("text"):
            return
        await _send_to_workana(context, entry)
        return

    if action == "ctx":
        short_id = parts[1]
        entry = _jobs.get(short_id)
        if not entry:
            return
        _awaiting_context[str(query.message.chat_id)] = short_id
        prompt = await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="✏️ Digite o valor ou instrução extra pra essa proposta/pergunta:",
            reply_markup=ForceReply(
                selective=True,
                input_field_placeholder='ex: "cobrar R$ 800 fixo" ou um detalhe do projeto',
            ),
        )
        entry["prompt_msg_id"] = prompt.message_id
        _save_state()
        return


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    short_id = _awaiting_context.get(chat_id)
    if not short_id or short_id not in _jobs:
        return  # nenhuma proposta/pergunta aguardando contexto extra no momento

    text = (update.message.text or "").strip()
    if not text:
        return

    entry = _jobs[short_id]
    entry["extra_context"] = text
    _save_state()

    prompt_msg_id = entry.pop("prompt_msg_id", None)
    if prompt_msg_id:
        try:
            await context.bot.delete_message(chat_id=int(chat_id), message_id=prompt_msg_id)
        except Exception:
            pass  # prompt já apagado ou expirado — não é crítico

    try:
        await update.message.delete()
    except Exception:
        pass  # sem permissão pra apagar — não é crítico

    await _push_card(context.bot, entry)


async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    err = context.error
    # Erros transitórios de rede/callback expirado — apenas log, sem stacktrace
    from telegram.error import NetworkError, TimedOut, BadRequest
    if isinstance(err, (NetworkError, TimedOut)):
        logger.warning("[bot] Erro de rede Telegram (transitório): %s", err)
    elif isinstance(err, BadRequest):
        logger.warning("[bot] BadRequest Telegram (ignorado): %s", err)
    else:
        logger.error("[bot] Erro inesperado no Telegram", exc_info=err)


def build_application(config: dict) -> Application:
    token = config["telegram"]["bot_token"]
    app = Application.builder().token(token).build()
    app.bot_data["config"] = config
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(_error_handler)
    return app
