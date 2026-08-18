"""
Geração de proposta ou pergunta via OpenAI.
"""
import json
import logging
import re
from openai import AsyncOpenAI
from .crawler import WorkanaJob

logger = logging.getLogger(__name__)

PROFILE = """Engenheiro Fullstack Sênior especializado em backend (Java, Scala, Spring Boot, Python), \
frontend (React, Angular, TypeScript), automação/bots, integrações de API e IA aplicada. \
Atua em empresa americana de saúde desenvolvendo plataforma de IA clínica com Kafka, Cassandra, \
PostgreSQL e AWS. Experiência com web scraping, sistemas de automação, APIs RESTful/GraphQL, \
Docker e Kubernetes. Formação em Ciência da Computação."""

# Perfis de estilo — escolhidos manualmente pelo usuário no Telegram antes da geração.
STYLES = {
    "t": {
        "label": "🔧 Técnico Direto",
        "proposal_structure": """Estrutura obrigatória da proposta (tom direto, sem enrolação):
1. ABERTURA — 1 frase mostrando que você identificou a causa raiz do problema técnico do projeto (sem saudação genérica, vá direto ao ponto)
2. ABORDAGEM — 2-3 frases descrevendo a solução técnica concreta que você aplicaria, com decisões específicas (arquitetura, stack, abordagem)
3. INVESTIMENTO — 1 frase com o valor e o que está incluso
4. CTA — 1 frase objetiva ("Posso começar assim que aprovado.")

Regras de formato:
- Entre 100 e 150 palavras no total
- Sem bullets, sem saudação, sem seções de "diferenciais" — texto corrido
- Tom: confiante, técnico, sem jargão de vendas""",
        "question_structure": """Estrutura obrigatória da pergunta (2-3 frases, direto ao ponto):
1. Sem saudação — comece identificando uma decisão técnica ambígua ou não especificada no projeto
2. Faça a pergunta que resolve essa ambiguidade, demonstrando que você já pensou em pelo menos uma abordagem possível

Regras de formato:
- Máximo 3 frases
- Tom direto, técnico, sem introdução""",
    },
    "s": {
        "label": "📈 Prova Social",
        "proposal_structure": """Estrutura obrigatória da proposta:
1. CASE — abra citando um resultado real e comparável que você já entregou (tipo de projeto similar + métrica de impacto: tempo economizado, bugs reduzidos, performance) — 1-2 frases
2. CONEXÃO — 1 frase conectando esse case ao problema específico deste projeto
3. ABORDAGEM — 1-2 frases sobre como aplicaria a mesma experiência aqui
4. INVESTIMENTO — 1 frase com o valor e o que inclui
5. CTA — convite pra falar mais sobre o case anterior se fizer sentido

Regras de formato:
- Entre 130 e 180 palavras
- O case deve soar específico e verossímil (nunca invente números impossíveis de checar; mantenha na faixa plausível pra um freelancer sênior)
- Tom: confiante por evidência, não por promessa""",
        "question_structure": """Estrutura obrigatória da pergunta (3-4 frases):
1. Referência breve a uma experiência prévia relevante ao tipo de problema do projeto
2. Pergunta específica que só alguém com essa experiência faria (evite perguntas óbvias)

Regras de formato:
- Tom: mentor experiente que já viu esse problema antes""",
    },
    "d": {
        "label": "🔍 Descoberta",
        "proposal_structure": """Estrutura obrigatória da proposta:
1. ABERTURA — identifique uma ambiguidade real de escopo ou requisito no projeto (1 frase)
2. PERGUNTA — faça a pergunta que resolveria essa ambiguidade (1 frase)
3. PROPOSTA CONDICIONAL — explique que o valor abaixo é uma estimativa inicial e pode ajustar após a resposta; dê uma faixa de valor em vez de número fechado
4. CTA — convide para uma conversa rápida antes de fechar escopo

Regras de formato:
- Entre 90 e 140 palavras
- Nunca comprometa com escopo fechado — a faixa de valor deve refletir a incerteza
- Tom: consultivo, curioso, parceiro — não vendedor""",
        "question_structure": """Estrutura obrigatória da pergunta (3-5 frases):
1. Identifique a ambiguidade central de escopo/requisito do projeto
2. Faça 1-2 perguntas que abririam uma conversa mais profunda sobre a real necessidade do cliente (não só detalhe técnico)
3. Sinalize disponibilidade para uma call rápida se ajudar

Regras de formato:
- Tom: genuinamente curioso, focado em entender o problema de negócio por trás do pedido técnico""",
    },
    "u": {
        "label": "⚡ Execução Rápida",
        "proposal_structure": """Estrutura obrigatória da proposta:
1. ABERTURA — 1 frase confirmando que você pode começar imediatamente
2. PRAZO — 1 frase com um prazo concreto e curto pra primeira entrega/protótipo (seja realista pro escopo do projeto)
3. ABORDAGEM — 1 frase sobre como entregaria rápido sem cortar qualidade
4. INVESTIMENTO — 1 frase com o valor
5. CTA — "Posso começar hoje/amanhã."

Regras de formato:
- Entre 80 e 120 palavras — quanto mais curto, melhor (reforça a mensagem de velocidade)
- Não mencione metodologia, processo ou "diferenciais" — o foco é 100% tempo
- Tom: enérgico, direto, sem enrolação""",
        "question_structure": """Estrutura obrigatória da pergunta (2-3 frases):
1. Confirme disponibilidade imediata
2. Faça a única pergunta que falta pra você começar agora (bloqueador prático, não filosófico)

Regras de formato:
- Máximo 3 frases, tom de urgência positiva""",
    },
    "n": {
        "label": "💼 Tradução p/ Negócio",
        "proposal_structure": """Estrutura obrigatória da proposta (zero jargão técnico):
1. ABERTURA — 1 frase traduzindo o problema do cliente em termos de negócio (tempo perdido, dinheiro, risco, oportunidade)
2. RESULTADO — 1-2 frases descrevendo o resultado prático que o cliente vai ter (não a tecnologia usada) — tecnologia só é citada de passagem, sem explicar
3. INVESTIMENTO — valor e o retorno esperado ("se paga em X" quando fizer sentido)
4. CTA — convite simples e amigável

Regras de formato:
- Entre 100 e 150 palavras
- Proibido: nomes de frameworks, linguagens específicas, termos como "API", "backend", "arquitetura" — fale como se explicasse pra um dono de loja
- Tom: acessível, consultivo, confiável""",
        "question_structure": """Estrutura obrigatória da pergunta (3-4 frases, zero jargão técnico):
1. Traduza uma decisão técnica pendente em termos de impacto no negócio do cliente
2. Faça a pergunta de forma que um leigo entenda facilmente o que está sendo perguntado e por que importa pro resultado dele

Regras de formato:
- Proibido jargão técnico
- Tom: acessível e didático""",
    },
    "r": {
        "label": "🛡️ Risco Zero",
        "proposal_structure": """Estrutura obrigatória da proposta:
1. ABERTURA — 1 frase reconhecendo que escolher um freelancer novo é um risco pro cliente
2. GARANTIA — proponha uma forma concreta de reduzir esse risco: uma primeira entrega pequena e verificável (protótipo, primeira função, primeiro módulo — específico ao escopo do projeto) antes do restante do valor ser cobrado
3. ABORDAGEM — 1 frase sobre o restante do plano após essa primeira entrega ser aprovada
4. INVESTIMENTO — valor total, deixando claro que a primeira parte é menor/condicional
5. CTA — convite direto pra começar por essa primeira entrega

Regras de formato:
- Entre 110 e 160 palavras
- A garantia deve ser específica ao projeto, nunca genérica ("garantia de satisfação")
- Tom: confiante mas humilde — reconhece a incerteza do cliente sem soar inseguro""",
        "question_structure": """Estrutura obrigatória da pergunta (3-4 frases):
1. Reconheça brevemente que o cliente está avaliando risco ao escolher um freelancer
2. Pergunte o que faria o cliente se sentir seguro pra aprovar uma primeira entrega pequena antes do resto (ex: qual seria o critério de aceite de um primeiro passo)

Regras de formato:
- Tom: transparente, focado em alinhar expectativa de risco/confiança antes de começar""",
    },
    "e": {
        "label": "💡 Insight Grátis",
        "proposal_structure": """Estrutura obrigatória da proposta:
1. INSIGHT — abra ensinando algo específico e não óbvio sobre o problema do projeto (um trade-off, um risco técnico, uma armadilha comum que o cliente provavelmente não percebeu) — 2 frases
2. SOLUÇÃO — 1 frase conectando esse insight à abordagem que você aplicaria
3. INVESTIMENTO — valor
4. CTA — convite objetivo

Regras de formato:
- Entre 120 e 170 palavras
- O insight precisa ser genuinamente específico ao projeto (nunca uma dica genérica tipo "testes são importantes")
- Tom: professor competente — gera valor antes de pedir algo em troca""",
        "question_structure": """Estrutura obrigatória da pergunta (3-4 frases):
1. Ensine algo específico e não óbvio sobre um risco ou trade-off do projeto que o cliente provavelmente não considerou
2. A partir desse insight, faça a pergunta que decide o caminho técnico a seguir

Regras de formato:
- Tom: professor competente — entrega valor real antes de pedir algo""",
    },
    "x": {
        "label": "⏳ Escassez",
        "proposal_structure": """Estrutura obrigatória da proposta:
1. ABERTURA — 1 frase sinalizando que você é seletivo com os projetos que aceita no momento (ex: está avaliando poucos projetos essa semana) — sem soar arrogante
2. MOTIVO — 1 frase explicando por que esse projeto específico chamou sua atenção (algo concreto da descrição)
3. ABORDAGEM — 1-2 frases sobre como você resolveria
4. INVESTIMENTO — valor
5. CTA — convite com leve senso de janela limitada (ex: "consigo encaixar essa semana")

Regras de formato:
- Entre 100 e 150 palavras
- A seletividade deve soar genuína e específica ao projeto, nunca genérica ou como pressão de vendas
- Tom: confiante, nunca arrogante — a seletividade é consequência natural de ser bom, não uma ameaça""",
        "question_structure": """Estrutura obrigatória da pergunta (3 frases):
1. Sinalize brevemente que está avaliando poucos projetos essa semana e esse chamou atenção por um motivo específico do escopo
2. Faça a pergunta que ajudaria a decidir se topa encaixar esse projeto na agenda

Regras de formato:
- Tom: seletivo mas nunca arrogante — a pergunta ainda precisa ser genuinamente útil pro projeto""",
    },
    "c": {
        "label": "🎭 Antes/Depois",
        "proposal_structure": """Estrutura obrigatória da proposta:
1. ANTES — 1-2 frases descrevendo vividamente a dor/situação atual do cliente (o que está lento, quebrado, caro ou manual hoje), com base na descrição do projeto
2. DEPOIS — 1-2 frases descrevendo o estado após a solução, em termos concretos e tangíveis (não abstratos)
3. PONTE — 1 frase breve sobre como você chega do antes ao depois
4. INVESTIMENTO — valor
5. CTA — convite simples

Regras de formato:
- Entre 120 e 170 palavras
- O contraste antes/depois deve ser específico ao projeto, nunca genérico
- Tom: vívido, direto, aposta em impacto emocional mais que em detalhe técnico""",
        "question_structure": """Estrutura obrigatória da pergunta (3-4 frases):
1. Descreva brevemente o contraste entre a situação atual (dor) e o resultado desejado, conforme sugerido na descrição do projeto
2. Faça a pergunta que ajuda a confirmar exatamente qual é o "depois" que o cliente tem em mente, já que isso muda a abordagem técnica

Regras de formato:
- Tom: vívido, focado em alinhar expectativa de resultado final""",
    },
}

_DEFAULT_STYLE = "t"

_PROPOSAL_COMMON_RULES = """Regras gerais:
- Os rótulos da "Estrutura obrigatória" acima (ABERTURA, INSIGHT, SOLUÇÃO, INVESTIMENTO, CTA, CASE, etc.) são guia interno de conteúdo, NUNCA texto literal — o texto final não deve conter nenhum desses rótulos, números ou marcadores de seção. Escreva em parágrafos corridos, como uma mensagem real para o cliente
- Escreva no idioma do projeto (detecte pelo título e descrição: PT ou EN)
- NUNCA inclua nome completo, e-mail, telefone, LinkedIn, GitHub
- Sem emojis

Regras do orçamento (campo "amount"):
- Se as instruções adicionais do freelancer especificarem um valor exato (ex: "cobrar R$ 3.000", "valor fixo: R$ 5000", "quero pedir 2500"), use EXATAMENTE esse valor como amount e ignore toda a avaliação de complexidade e cálculo abaixo — a instrução do freelancer sempre tem prioridade
- O amount DEVE ser SEMPRE em BRL (reais brasileiros) — independente da moeda do budget
- Se o budget estiver em USD (campo contém "[USD → BRL aprox.: ...]"), use o valor BRL convertido como referência

Avaliação de complexidade (faça isso ANTES de definir o amount):
- Classifique o projeto em: SIMPLES / MÉDIO / COMPLEXO / MUITO COMPLEXO
- SIMPLES: ajustes pontuais, landing page básica, correção de bug → R$ 500 - 3.000
- MÉDIO: sistema com CRUD, integração de API única, site completo → R$ 3.000 - 9.000
- COMPLEXO: múltiplas integrações, IA, automações avançadas, app mobile/web completo → R$ 9.000 - 20.000
- MUITO COMPLEXO: plataforma SaaS, sistema distribuído, IA customizada, múltiplos módulos → R$ 20.000 - 50.000

Definição do amount com base no range do budget e complexidade:
- Se o budget for um range: use a média indicada no campo "[média: R$ X]" como ponto de partida; ajuste para cima se a complexidade for condizente ou maior, para baixo se o projeto for mais simples que o range sugere
- Se o budget for muito baixo para a complexidade real do projeto: sugira o teto do range mesmo assim (é o máximo possível dentro do que o cliente propôs)
- Se o budget for por hora: sugira o valor/hora no topo do range
- Se o budget não for informado: use as faixas de complexidade acima como referência
- NUNCA sugira menos de 60% do topo do range
- Valor inteiro, sem centavos. Mínimo: 150

Retorne APENAS um JSON válido, sem markdown, sem explicações:
{"text": "texto da proposta aqui", "amount": 3500}"""

_QUESTION_COMMON_RULES = """Regras gerais:
- Os rótulos da "Estrutura obrigatória" acima (CONTEXTO, ABORDAGEM, PERGUNTA PRÁTICA, etc.) são guia interno de conteúdo, NUNCA texto literal — o texto final não deve conter nenhum desses rótulos, números ou marcadores de seção. Escreva em parágrafos corridos, como uma mensagem real para o cliente
- Escreva no idioma do projeto (detecte pelo título e descrição: PT ou EN)
- NUNCA inclua nome completo, e-mail, telefone, LinkedIn, GitHub
- Sem emojis
- Evite perguntas genéricas: "Qual o prazo?", "Qual o budget?", "Já tem design?", "Há documentação?"

Retorne apenas o texto da pergunta, sem explicações extras."""


def _proposal_system(style: str) -> str:
    cfg = STYLES.get(style, STYLES[_DEFAULT_STYLE])
    return f"""Você é um especialista em propostas de freelancing de alto impacto no Workana.

Perfil do freelancer:
{PROFILE}

{cfg['proposal_structure']}

{_PROPOSAL_COMMON_RULES}"""


def _question_system(style: str) -> str:
    cfg = STYLES.get(style, STYLES[_DEFAULT_STYLE])
    return f"""Você é um especialista em freelancing que usa comunicação estratégica para se destacar no Workana.

Perfil do freelancer:
{PROFILE}

{cfg['question_structure']}

{_QUESTION_COMMON_RULES}"""


_USD_RATE = 5.5  # taxa de conversão padrão USD → BRL


def _extract_brl_values(text: str) -> list[int]:
    """Extrai valores numéricos de uma string de budget em BRL."""
    return [int(n.replace(".", "").replace(",", "")) for n in re.findall(r"[\d][0-9.,]*", text)]


def _budget_note(budget: str | None) -> str:
    """Retorna a string de budget com média e, se USD, conversão para BRL."""
    if not budget:
        return "A combinar"
    is_usd = ("$" in budget and "R$" not in budget) or budget.upper().startswith("USD")
    if is_usd:
        nums = [int(n.replace(",", "").replace(".", "")) for n in re.findall(r"[\d][0-9,\.]*", budget)]
        if nums:
            brl_vals = [int(n * _USD_RATE) for n in nums]
            converted = " - ".join(f"R$ {v:,}".replace(",", ".") for v in brl_vals)
            avg = int(sum(brl_vals) / len(brl_vals))
            return f"{budget} [USD → BRL aprox.: {converted} | média BRL: R$ {avg:,}]".replace(",", ".")
        return f"{budget} [USD — use taxa ~R$ {_USD_RATE}/USD para converter]"

    # BRL: tenta calcular média se for um range
    nums = _extract_brl_values(budget)
    if len(nums) >= 2:
        avg = int(sum(nums) / len(nums))
        return f"{budget} [média: R$ {avg:,}]".replace(",", ".")
    return budget


_TIER_LABELS = {
    "abaixo": "abaixo da faixa de orçamento do projeto",
    "minimo": "no valor mínimo da faixa de orçamento do projeto",
    "medio": "no valor médio da faixa de orçamento do projeto",
    "maximo": "no valor máximo da faixa de orçamento do projeto",
    "acima": "acima da faixa de orçamento do projeto",
}


def _budget_bounds(budget: str | None) -> tuple[int, int] | None:
    """Retorna (min, max) em BRL a partir do budget do job, ou None se não der pra calcular."""
    if not budget:
        return None
    is_usd = ("$" in budget and "R$" not in budget) or budget.upper().startswith("USD")
    nums = _extract_brl_values(budget)
    if not nums:
        return None
    if is_usd:
        nums = [int(n * _USD_RATE) for n in nums]
    lo, hi = min(nums), max(nums)
    if lo == hi:
        lo, hi = int(lo * 0.85), int(lo * 1.15)
    return lo, hi


def price_tiers(budget: str | None) -> dict[str, int | None]:
    """Calcula os 5 valores de referência (BRL) a partir do budget do job."""
    bounds = _budget_bounds(budget)
    if not bounds:
        return {"abaixo": None, "minimo": None, "medio": None, "maximo": None, "acima": None}
    lo, hi = bounds
    return {
        "abaixo": max(150, int(lo * 0.7)),
        "minimo": lo,
        "medio": int((lo + hi) / 2),
        "maximo": hi,
        "acima": int(hi * 1.3),
    }


def price_tier_directive(budget: str | None, tier_key: str | None) -> str | None:
    """Instrução de valor fixo a injetar no contexto da LLM, a partir do tier escolhido."""
    if not tier_key or tier_key == "auto":
        return None
    val = price_tiers(budget).get(tier_key)
    label = _TIER_LABELS.get(tier_key, tier_key)
    if val is None:
        return f"Cobre um valor {label}."
    return f"Cobre exatamente R$ {val:,} ({label}).".replace(",", ".")


# Apps de mensagem e plataformas de contato que o Workana bloqueia
_CONTACT_APPS = re.compile(
    r'\b(whatsapp|telegram|signal|discord|skype|zoom|meet|teams|slack|viber|line|wechat|messenger|linkedin|github|calendly)\b',
    re.IGNORECASE,
)
# Padrões de telefone (ex: +55 11 99999-9999, (11) 9999-9999)
_PHONE_PATTERN = re.compile(r'(\+?\d[\d\s\-\(\)]{7,}\d)')
# Email
_EMAIL_PATTERN = re.compile(r'\b[\w.+-]+@[\w-]+\.[a-z]{2,}\b', re.IGNORECASE)


_URL_PATTERN = re.compile(r'https?://\S+|www\.\S+', re.IGNORECASE)


def _strip_contact_data(text: str) -> str:
    """Remove URLs, apps de contato, telefones e e-mails que o Workana bloqueia."""
    text = _URL_PATTERN.sub('', text)
    text = _CONTACT_APPS.sub('', text)
    text = _PHONE_PATTERN.sub('', text)
    text = _EMAIL_PATTERN.sub('', text)
    text = re.sub(r' {2,}', ' ', text).strip()
    return text


def _strip_external_domains(text: str) -> str:
    """Remove extensões de domínio de nomes de serviços para evitar bloqueio do Workana."""
    return re.sub(r'\b(\w[\w-]*)\.(ai|io|com|net|org|co)\b', r'\1', text)


def _soften_external_service_names(text: str) -> str:
    """Troca nomes que costumam disparar alerta de link por descrições neutras."""
    replacements = [
        (r'\bn8n\b', 'orquestrador de automações'),
        (r'\bwa\.me\b', ''),
    ]
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return re.sub(r' {2,}', ' ', text).strip()


def _build_context(job: WorkanaJob) -> str:
    parts = [
        f"Título: {job.title}",
        f"Descrição: {job.description}",
        f"Skills exigidas: {', '.join(job.skills) if job.skills else '—'}",
        f"Budget: {_budget_note(job.budget)}",
        f"País do cliente: {job.country or '—'}",
    ]
    if job.client_name:
        first_name = job.client_name.strip().split()[0]
        parts.append(f"Nome do cliente: {first_name}")
    if job.projects_posted is not None:
        parts.append(f"Projetos publicados pelo cliente: {job.projects_posted}")
    if job.projects_paid is not None:
        parts.append(f"Projetos pagos: {job.projects_paid}")
    if job.payment_verified:
        parts.append("Pagamento verificado: Sim")
    if job.member_since:
        parts.append(f"Membro desde: {job.member_since}")
    parts.append(f"Propostas recebidas: {job.bid_count}")
    return "\n".join(parts)


async def generate(
    job: WorkanaJob,
    action: str,
    config: dict,
    style: str = _DEFAULT_STYLE,
    extra_context: str | None = None,
) -> dict:
    """Retorna dict com 'text' (str) e, para propostas, 'amount' (int | None)."""
    oai_cfg = config.get("openai", {})
    api_key = oai_cfg.get("api_key", "")
    model = oai_cfg.get("model", "gpt-5.5")

    client = AsyncOpenAI(api_key=api_key)
    system = _proposal_system(style) if action == "proposal" else _question_system(style)
    context = _build_context(job)
    user_msg = f"Contexto do projeto:\n{context}"
    if extra_context:
        user_msg += f"\n\nInstruções adicionais do freelancer para esta mensagem específica:\n{extra_context}"

    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ],
            max_completion_tokens=1500,
        )
        raw = (resp.choices[0].message.content or "").strip()
        if not raw:
            finish = resp.choices[0].finish_reason
            logger.error("[gpt] Resposta vazia do modelo (finish_reason=%s)", finish)
            return {"text": "", "amount": None} if action == "proposal" else {"text": ""}
        # Remove bloco markdown se o modelo retornar ```json ... ```
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-z]*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw)
            raw = raw.strip()

        def _clean(t: str) -> str:
            return _strip_contact_data(_soften_external_service_names(_strip_external_domains(t)))

        if action == "proposal":
            try:
                # Tenta parse direto; se falhar, extrai o primeiro objeto JSON da resposta
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    m = re.search(r'\{.*\}', raw, re.DOTALL)
                    if not m:
                        raise
                    data = json.loads(m.group())
                text = _clean(str(data.get("text", "")).strip())
                return {
                    "text": text,
                    "amount": int(data["amount"]) if data.get("amount") else None,
                }
            except (json.JSONDecodeError, KeyError, ValueError) as e:
                logger.warning("[gpt] JSON inválido na proposta (%s). Raw: %.300s", e, raw)
                return {"text": _clean(raw), "amount": None}

        return {"text": _clean(raw)}

    except Exception as exc:
        logger.error("[gpt] Falha ao gerar %s: %s", action, exc)
        return {"text": f"Erro ao gerar texto: {exc}"}
