import asyncio
import logging
import os
from typing import Dict, Any, List

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from founders_hunt import run_search
from supabase_client import supabase, store_jobs, get_cached_jobs, get_user_location, set_user_location

load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
PORT = int(os.environ.get("PORT", 8080))
WEBHOOK_URL = os.environ.get("RENDER_EXTERNAL_URL")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Helper to format job listing with HTML (safer than Markdown)
def format_job(job: Dict[str, Any]) -> str:
    fps_emoji = "🔥" if job["fps"] >= 7 else "✨" if job["fps"] >= 5 else "📌"
    red_flags_text = ""
    if job.get("red_flags"):
        red_flags_text = f"\n⚠️ Red flags: {', '.join(job['red_flags'][:2])}"
    skills_text = ""
    if job.get("skills_needed"):
        skills_text = f"\n🛠 Skills: {', '.join(job['skills_needed'][:3])}"
    stipend_text = f"\n💰 {job['stipend']}" if job.get("stipend") else ""
    
    # Escape HTML special characters in user-supplied text
    import html
    title = html.escape(job['title'])
    company = html.escape(job['company'])
    location = html.escape(job['location'])
    reasoning = html.escape(job.get('fps_reasoning', ''))
    summary = html.escape(job.get('role_summary', 'No summary available'))
    url = job['url']  # URL doesn't need escaping
    source = html.escape(job['source'])
    
    return (
        f"<b>{title}</b> @ {company}\n"
        f"📍 {location}\n"
        f"{fps_emoji} FPS: {job['fps']:.1f}/10 — {reasoning}\n"
        f"📋 {summary}\n"
        f"🔗 <a href='{url}'>Apply here</a>{stipend_text}{red_flags_text}{skills_text}\n"
        f"📎 Source: {source}\n"
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Plain text fallback if HTML fails, but HTML is safe here
    await update.message.reply_text(
        "👋 Welcome to the Founder's Office Hunt bot!\n\n"
        "I find internships where you work directly with the founder – the highest-leverage roles for aspiring entrepreneurs.\n\n"
        "Commands:\n"
        "/search <query> – Find founder's office roles (e.g. /search chief of staff)\n"
        "/set_location <city> – Change your default location (current: India)\n"
        "/help – Show this message\n\n"
        "Built with Founder Proximity Score (FPS) – a unique metric that predicts how close you'll be to the CEO."
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔍 How to use\n"
        "• /search founder's office intern – default search\n"
        "• /search growth intern report to founder – more specific\n"
        "• /set_location Bangalore – change location\n\n"
        "The bot caches results for 1 hour to save API costs.\n"
        "Each result includes Founder Proximity Score (FPS).\n\n"
        "💡 Pro tip: Look for roles with FPS > 7 – those are true 'shadow the CEO' opportunities."
    )

async def set_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text("Please provide a location, e.g., /set_location Bangalore")
        return
    location = " ".join(context.args)
    set_user_location(user_id, location)
    await update.message.reply_text(f"✅ Location set to {location} for future searches.")

async def search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    query = " ".join(context.args) if context.args else "founder's office intern"
    location = get_user_location(user_id)
    
    msg = await update.message.reply_text(
        f"🔎 Searching for {query} in {location}...\n"
        f"⏳ This may take 20-30 seconds (scraping + AI scoring)."
    )
    
    cached = get_cached_jobs(query, hours=1)
    if cached:
        jobs = cached
        await msg.edit_text(f"📦 Using cached results for '{query}' (last hour). Found {len(jobs)} unique listings.\n\nTop picks:")
    else:
        jobs = await asyncio.to_thread(
            run_search,
            query=query,
            location=location,
            ai_provider="auto",
            max_results=50,
            skip_ai=False
        )
        if supabase:
            store_jobs(jobs, query)
    
    if not jobs:
        await msg.edit_text(f"😕 No 'Founder's Office' roles found for '{query}'. Try a different keyword.")
        return
    
    genuine = [j for j in jobs if j.get("is_genuine_fo_role", False)]
    if not genuine:
        genuine = jobs
    top_jobs = sorted(genuine, key=lambda x: x.get("fps", 0), reverse=True)[:7]
    
    response = f"🎯 <b>Top {len(top_jobs)} results for '{query}' in {location}</b>\n\n"
    for i, job in enumerate(top_jobs, 1):
        response += f"{i}. {format_job(job)}\n\n"
    
    # Split if too long
    if len(response) > 4000:
        parts = [response[i:i+4000] for i in range(0, len(response), 4000)]
        for part in parts:
            await update.message.reply_text(part, parse_mode="HTML", disable_web_page_preview=True)
    else:
        await msg.edit_text(response, parse_mode="HTML", disable_web_page_preview=True)

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error {context.error}")
    if update and update.effective_message:
        # Try plain text fallback
        await update.effective_message.reply_text("⚠️ An error occurred. Please try again later.")

def main():
    if not TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN not set")
    
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("set_location", set_location))
    app.add_handler(CommandHandler("search", search))
    app.add_error_handler(error_handler)
    
    if WEBHOOK_URL:
        webhook_path = f"/webhook/{TOKEN}"
        webhook_url = f"{WEBHOOK_URL}{webhook_path}"
        print(f"Starting webhook on port {PORT} at {webhook_url}")
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=webhook_path,
            webhook_url=webhook_url,
        )
    else:
        print("Polling mode: bot is listening for messages...")
        app.run_polling()

if __name__ == "__main__":
    print("🚀 Starting bot...")
    print(f"Token present: {'Yes' if TOKEN else 'No'}")
    print(f"WEBHOOK_URL: {WEBHOOK_URL or 'Not set, using polling'}")
    main()