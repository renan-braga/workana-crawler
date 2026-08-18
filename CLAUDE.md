# Instruções para o Claude neste projeto

- **Nunca execute, reinicie ou mate o processo `main.py run` (bot do Telegram).** O usuário sempre roda manualmente no terminal dele. Rodar uma cópia extra (mesmo em background) causa conflito no Telegram (`Conflict: terminated by other getUpdates request`), já que só uma conexão de polling é permitida por bot.
- Para testar lógica do bot/crawler/gpt, use scripts Python diretos chamando as funções (`gpt.generate()`, `bot._card_state()`, `crawler._ensure_authenticated()`, etc.) em vez de subir o daemon.
- Se precisar validar algo no fluxo real do Telegram, peça pro usuário rodar e mandar o log/print.
