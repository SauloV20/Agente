# Agente de prospecção — versão para celular

Um site pequeno (Flask) que você acessa pelo navegador do celular. Ele acha
estabelecimentos sem site, escreve mensagens de apresentação e te deixa
revisar antes de qualquer envio. Roda de graça 24h usando um "pinger" externo
— explico por que isso é necessário logo abaixo.

## Por que não dá para deixar rodando direto no app do celular

Android e iOS matam processos em segundo plano para economizar bateria; um
script Python não sobrevive horas sozinho no fundo do celular sem um app
dedicado para isso. A solução real é hospedar o agente num servidor grátis na
nuvem e usar o celular só para abrir o painel e revisar/aprovar. Ele fica
"sempre ativo" mesmo assim, do jeito que você pediu.

## Passo a passo (grátis, sem cartão de crédito)

1. **Crie uma conta no [Render](https://render.com/)** e um "Web Service"
   novo apontando para este código (suba estes arquivos num repositório do
   GitHub e conecte lá, ou use `render.yaml`/deploy manual).
2. Em **Environment**, copie as variáveis de `.env.example` e preencha:
   - `APP_PASSWORD`: senha para abrir o painel.
   - `TICK_SECRET`: uma chave longa e aleatória (protege o endpoint automático).
   - `GEMINI_API_KEY`: pegue grátis em https://aistudio.google.com/apikey
     (usado para escrever as mensagens, sem custo).
   - `SMTP_USER`/`SMTP_PASS`: para enviar e-mail (no Gmail, crie uma
     "senha de app").
   - `GOOGLE_API_KEY` é opcional — só se quiser complementar o OpenStreetMap
     com o Google Places (esse aqui tem cota grátis limitada e exige cartão).
3. **Deploy.** O Render te dá uma URL tipo `https://seu-agente.onrender.com`.
   Abra pelo celular, faça login com a `APP_PASSWORD` e adicione uma busca em
   **Buscas** (ex.: categoria "barbearia", cidade "Copacabana, Rio de Janeiro").
4. **Mantenha ativo 24h:** crie uma conta grátis em
   [cron-job.org](https://cron-job.org) e cadastre um job que chama, a cada
   10 minutos, o endereço:
   `https://seu-agente.onrender.com/tick?key=SUA_TICK_SECRET`
   Isso acorda o servidor gratuito do Render (que dorme sozinho após 15 min
   sem visitas) e faz o agente rodar um ciclo: buscar, rascunhar e enviar os
   e-mails já aprovados. Na prática o agente trabalha o dia inteiro, em
   ciclos de ~10-15 minutos, mesmo no plano grátis.
5. Pelo celular, entre em **Revisar** de tempos em tempos para aprovar,
   editar ou rejeitar as mensagens antes de saírem. E-mails aprovados saem
   sozinhos no próximo tick; WhatsApp, Instagram, Facebook e LinkedIn não têm
   automação de envio aqui de propósito (ver abaixo).

## O que o agente faz sozinho a cada ciclo

1. Busca no OpenStreetMap (grátis, sem chave) negócios da categoria/cidade
   mais atrasada na fila, filtrando quem já tem site.
2. Se `GOOGLE_API_KEY` estiver configurada, complementa com o Google Places
   até a franquia mensal grátis definida em `GOOGLE_MONTHLY_LIMIT`.
3. Escreve os rascunhos de mensagem (Gemini grátis, ou Claude se preferir).
4. Envia os e-mails que você já aprovou (limite diário em `EMAIL_DAILY_LIMIT`).

## Por que WhatsApp/Instagram/Facebook/LinkedIn não saem sozinhos

Essas plataformas banem contas que mandam mensagem automatizada em massa e
isso violaria os termos delas. Por isso o agente prepara o texto e, na aba
**Leads**, você recebe o link pronto (`wa.me/...` para WhatsApp) para dar
o clique final manualmente pelo celular. Se no futuro você quiser automatizar
o WhatsApp de verdade, o caminho oficial é a API do WhatsApp Business, que
exige número verificado e templates aprovados — posso montar isso depois.

## Custos reais

- **OpenStreetMap:** 100% grátis, sem limite de conta (respeite um intervalo
  razoável entre buscas, o código já faz isso).
- **Gemini (redigir mensagens):** grátis, com limite de uso por minuto no
  plano gratuito — por isso há uma pequena pausa entre rascunhos (`LLM_DELAY`).
- **Google Places (opcional):** cota mensal grátis limitada; exige cartão de
  crédito cadastrado no Google Cloud, mesmo sem cobrar dentro da cota.
- **Render + cron-job.org:** grátis.

## Segurança e LGPD

- O painel pede senha (`APP_PASSWORD`).
- A aba **Bloqueio** guarda contatos que pediram para parar — adicione manual
  ou quando alguém responder pedindo isso, e o agente nunca mais rascunha ou
  envia mensagem para esse contato.
- Todo e-mail sai com uma linha de descadastro no rodapé.
