import os
import json
import logging
import requests
import google.generativeai as genai
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from supabase import create_client

logging.basicConfig(level=logging.INFO)

# ── CONFIG ────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
SUPABASE_URL   = os.environ["SUPABASE_URL"]
SUPABASE_KEY   = os.environ["SUPABASE_KEY"]

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
genai.configure(api_key=GEMINI_API_KEY)

SYSTEM_PROMPT = """Você é um especialista em concursos públicos brasileiros.
Analise a questão enviada (texto ou imagem) e responda SOMENTE com JSON puro, sem markdown.
Formato exato:
{
  "disciplina": "nome da disciplina",
  "assunto": "subtópico específico",
  "nucleo": "o que exatamente a questão cobra em 1-2 frases diretas",
  "resumo": "explicação didática do raciocínio correto em até 4 linhas",
  "mnemonico": "mnemônico criativo, sigla ou história curta e memorável para fixar o conteúdo",
  "armadilha": "o erro mais comum que candidatos cometem neste tipo de questão"
}"""

# ── ANÁLISE COM GEMINI ────────────────────────────────────────
def analisar_com_gemini(texto=None, imagem_bytes=None, imagem_mime=None):
    model = genai.GenerativeModel("gemini-pro")
    parts = [SYSTEM_PROMPT]
    if imagem_bytes:
        parts.append({"mime_type": imagem_mime, "data": imagem_bytes})
    if texto:
        parts.append(texto)
    response = model.generate_content(parts)
    raw = response.text.strip()
    clean = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(clean)

# ── SALVAR NO SUPABASE ────────────────────────────────────────
def salvar_questao(user_id, dados, tipo, ia):
    supabase.table("questoes").insert({
        "telegram_user_id": str(user_id),
        "disciplina": dados["disciplina"],
        "assunto": dados["assunto"],
        "nucleo": dados["nucleo"],
        "resumo": dados["resumo"],
        "mnemonico": dados["mnemonico"],
        "armadilha": dados["armadilha"],
        "tipo": tipo,
        "ia_usada": ia
    }).execute()

# ── FORMATAR RESPOSTA ─────────────────────────────────────────
def formatar_resposta(dados, ia="Gemini"):
    return f"""📚 *{dados['disciplina']}* — {dados['assunto']}

🎯 *Núcleo da cobrança*
{dados['nucleo']}

📖 *Raciocínio*
{dados['resumo']}

🧠 *Mnemônico*
`{dados['mnemonico']}`

⚠️ *Armadilha*
{dados['armadilha']}

_Analisado por {ia}_"""

# ── HANDLERS ─────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Olá! Sou seu assistente de revisão para concursos.\n\n"
        "📸 Mande um *print* da questão ou *cole o texto* e eu analiso na hora.\n\n"
        "Comandos:\n"
        "/errei — salvar última questão como ❌ Errei\n"
        "/favorita — salvar última questão como ⭐ Favorita\n"
        "/stats — ver suas estatísticas\n"
        "/revisao — ver questões salvas\n"
        "/revisao Direito Constitucional — filtrar por disciplina",
        parse_mode="Markdown"
    )

async def analisar_imagem(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Analisando a questão...")
    try:
        photo = update.message.photo[-1]
        file = await ctx.bot.get_file(photo.file_id)
        img_bytes = bytes(await file.download_as_bytearray())
        dados = analisar_com_gemini(imagem_bytes=img_bytes, imagem_mime="image/jpeg")
        ctx.user_data["ultima"] = dados
        await update.message.reply_text(
            formatar_resposta(dados) + "\n\n_Use /errei ou /favorita para salvar._",
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Erro ao analisar: {str(e)}")

async def analisar_texto(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text
    if texto.startswith("/"): return
    await update.message.reply_text("🔍 Analisando a questão...")
    try:
        dados = analisar_com_gemini(texto=texto)
        ctx.user_data["ultima"] = dados
        await update.message.reply_text(
            formatar_resposta(dados) + "\n\n_Use /errei ou /favorita para salvar._",
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Erro ao analisar: {str(e)}")

async def salvar_errei(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    dados = ctx.user_data.get("ultima")
    if not dados:
        await update.message.reply_text("Nenhuma questão para salvar. Mande um print ou texto primeiro.")
        return
    salvar_questao(update.effective_user.id, dados, "errei", "Gemini")
    await update.message.reply_text("✅ Salva como ❌ Errei!")

async def salvar_favorita(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    dados = ctx.user_data.get("ultima")
    if not dados:
        await update.message.reply_text("Nenhuma questão para salvar. Mande um print ou texto primeiro.")
        return
    salvar_questao(update.effective_user.id, dados, "favorita", "Gemini")
    await update.message.reply_text("✅ Salva como ⭐ Favorita!")

async def stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    qs = supabase.table("questoes").select("*").eq("telegram_user_id", uid).execute().data
    if not qs:
        await update.message.reply_text("Nenhuma questão salva ainda. Mande um print para começar!")
        return
    total = len(qs)
    erros = sum(1 for q in qs if q["tipo"] == "errei")
    favs  = sum(1 for q in qs if q["tipo"] == "favorita")
    disc_map = {}
    for q in qs:
        disc_map[q["disciplina"]] = disc_map.get(q["disciplina"], 0) + 1
    disc_txt = "\n".join(f"  • {d}: {c}q" for d, c in sorted(disc_map.items(), key=lambda x: -x[1]))
    await update.message.reply_text(
        f"📊 *Suas estatísticas*\n\n"
        f"📚 Total salvas: {total}\n"
        f"❌ Errei: {erros}\n"
        f"⭐ Favoritas: {favs}\n\n"
        f"*Por disciplina:*\n{disc_txt}",
        parse_mode="Markdown"
    )

async def revisao(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    args = ctx.args
    query = supabase.table("questoes").select("*").eq("telegram_user_id", uid)
    if args:
        disciplina = " ".join(args)
        query = query.ilike("disciplina", f"%{disciplina}%")
    qs = query.order("created_at", desc=True).limit(5).execute().data
    if not qs:
        await update.message.reply_text("Nenhuma questão encontrada.")
        return
    for q in qs:
        await update.message.reply_text(
            f"{'❌' if q['tipo']=='errei' else '⭐'} *{q['disciplina']}* — {q['assunto']}\n\n"
            f"🎯 {q['nucleo']}\n\n"
            f"🧠 `{q['mnemonico']}`\n\n"
            f"⚠️ {q['armadilha']}\n\n"
            f"_{q['created_at'][:10]}_",
            parse_mode="Markdown"
        )

# ── MAIN ──────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("errei", salvar_errei))
    app.add_handler(CommandHandler("favorita", salvar_favorita))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("revisao", revisao))
    app.add_handler(MessageHandler(filters.PHOTO, analisar_imagem))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, analisar_texto))
    print("Bot rodando...")
    app.run_polling()

if __name__ == "__main__":
    main()
