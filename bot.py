"""
Telegram Bot Interface — Founder's Office Internship Finder.

Multi-source search across 7+ platforms with AI-powered Founder Proximity Score (FPS),
semantic query understanding, caching, and paginated results.
"""

import asyncio
import html
import logging
import os
from typing import Dict, Any, List

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler

from founders_hunt import run_search
from supabase_client import (
    supabase,
    store_jobs,
    get_cached_jobs,
    get_user_location,
    set_user_location,
    normalize_query,
    store_user_page_data,
    get_user_page_data,
)

load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
PORT = int(os.environ.get("PORT", 8080))
WEBHOOK_URL = os.environ.get("RENDER_EXTERNAL_URL")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

RESULTS_PER_PAGE = 7

SOURCE_BADGES: Dict[str, str] = {
    "Internshala": "📋",
    "LinkedIn": "💼",
    "Cutshort": "🎯",
    "Wellfound": "🚀",
    "Twitter/X": "🐦",
    "Hashjob": "#️⃣",
    "DuckDuckGo": "🔍",
}


def get_source_badge(source: str) -> str:
    """Return an emoji badge for the given source name."""
    for key, badge in SOURCE_BADGES.items():
        if key.lower() in source.lower():
            return badge
    return "🔗"


def format_job(job: dict, index: int) -> str:
    """Format a single job listing as an HTML string suitable for Telegram."""
    title = html.escape(str(job.get("title", "Untitled")))
    company = html.escape(str(job.get("company", "Unknown")))
    location = html.escape(str(job.get("location", "Remote")))
    fps = float(job.get("fps", 0))
    reasoning = html.escape(str(job.get("fps_reasoning", "")))
    confidence = float(job.get("confidence", 1.0))
    summary = html.escape(str(job.get("role_summary", "No description available.")))
    url = job.get("url", "")
    stipend = html.escape(str(job.get("stipend", "")))
    source = html.escape(str(job.get("source", "Unknown")))
    red_flags: List[str] = job.get("red_flags", []) or []
    skills: List[str] = job.get("skills_needed", []) or []

    # FPS emoji
    if fps >= 7:
        fps_emoji = "🔥"
    elif fps >= 5:
        fps_emoji = "✨"
    else:
        fps_emoji = "📌"

    lines: List[str] = []
    lines.append(f"{index}. <b>{title}</b> @ {company}")
    lines.append(f"📍 {location}")
    lines.append(f"{fps_emoji} FPS: {fps:.1f}/10 — {reasoning}")

    # Confidence badge for low-confidence results
    if confidence < 0.5:
        lines.append("❓ <i>Low confidence — verify listing details</i>")

    lines.append(f"📋 {summary}")

    if url:
        lines.append(f"🔗 <a href='{url}'>Apply here</a>")

    if stipend:
        lines.append(f"💰 {stipend}")

    if red_flags:
        flags_text = html.escape(", ".join(red_flags[:2]))
        lines.append(f"⚠️ Red flags: {flags_text}")

    if skills:
        skills_text = html.escape(", ".join(skills[:3]))
        lines.append(f"🛠 Skills: {skills_text}")

    source_badge = get_source_badge(source)
    lines.append(f"{source_badge} Source: {source}")

    return "\n".join(lines)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /start command — welcome the user."""
    text = (
        "👋 <b>Welcome to the Founder's Office Finder!</b>\n\n"
        "I help you discover internships and roles that give you <b>direct access to founders</b> — "
        "not generic HR-filtered listings.\n\n"
        "🔎 <b>Multi-source search</b> — I scan <b>7+ platforms</b> simultaneously "
        "(Internshala, LinkedIn, Cutshort, Wellfound, Twitter/X, Hashjob &amp; more).\n\n"
        "🧠 <b>AI-powered Founder Proximity Score (FPS)</b> — every listing is scored 0-10 "
        "based on how close you'll actually work with the founder.\n\n"
        "💡 <b>Semantic understanding</b> — I understand what you <i>mean</i>, not just what you type. "
        "Try natural queries like <code>work with CEO in Bangalore</code>.\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "📌 <b>Commands</b>\n"
        "/search &lt;query&gt; — Find founder-access roles\n"
        "/set_location &lt;city&gt; — Set your preferred city\n"
        "/help — Detailed usage guide\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "🚀 Get started: just type <code>/search</code>"
    )
    await update.message.reply_text(text, parse_mode="HTML", disable_web_page_preview=True)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /help command — show detailed usage guide."""
    text = (
        "📖 <b>How to use Founder's Office Finder</b>\n\n"
        "<b>Commands:</b>\n"
        "• <code>/search &lt;query&gt;</code> — Find founder-access roles\n"
        "• <code>/search</code> — Uses default: <i>founder's office intern</i>\n"
        "• <code>/search work with CEO Bangalore</code> — Semantic search works!\n"
        "• <code>/set_location &lt;city&gt;</code> — Set your preferred city\n"
        "• <code>/help</code> — This guide\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "📊 <b>Founder Proximity Score (FPS)</b>\n\n"
        "Every listing gets an AI-generated score from 0-10:\n"
        "🔥 <b>7-10</b> — True founder access (direct reports, shadow roles)\n"
        "✨ <b>5-6</b> — Good proximity (small team, founder-led)\n"
        "📌 <b>0-4</b> — Limited founder interaction\n\n"
        "💡 <b>Pro tip:</b> FPS &gt; 7 = true founder access. "
        "These are the roles where you'll learn the most.\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🔎 <b>Search tips</b>\n"
        "• Use natural language — I understand intent\n"
        "• Mention specific cities for better results\n"
        "• Results are cached for 3 hours for speed\n"
        "• I search 7+ platforms in parallel"
    )
    await update.message.reply_text(text, parse_mode="HTML", disable_web_page_preview=True)


async def set_location_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /set_location command — set the user's preferred location."""
    user_id = update.effective_user.id

    if not context.args:
        await update.message.reply_text(
            "📍 <b>Set your location</b>\n\n"
            "Usage: <code>/set_location &lt;city&gt;</code>\n"
            "Example: <code>/set_location Bangalore</code>\n\n"
            "This will be used as the default location for your searches.",
            parse_mode="HTML",
        )
        return

    location = " ".join(context.args).strip()
    set_user_location(user_id, location)
    escaped_location = html.escape(location)
    await update.message.reply_text(
        f"✅ Location set to <b>{escaped_location}</b>\n\n"
        f"All your future searches will default to {escaped_location}. "
        f"You can always override this by mentioning a city in your search query.",
        parse_mode="HTML",
    )


async def search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /search command — main search handler."""
    user_id = update.effective_user.id
    query = " ".join(context.args).strip() if context.args else "founder's office intern"
    location = get_user_location(user_id) or "India"
    escaped_query = html.escape(query)
    escaped_location = html.escape(location)

    # Send initial searching message
    status_msg = await update.message.reply_text(
        f"🔍 Searching for <b>{escaped_query}</b> in <b>{escaped_location}</b>...\n"
        f"⏳ Hang tight!",
        parse_mode="HTML",
    )

    jobs: List[Dict[str, Any]] = []

    try:
        # Check cache first
        cached = get_cached_jobs(query, location, hours=3)

        if cached and len(cached) > 0:
            jobs = cached
            await status_msg.edit_text(
                f"⚡ <b>Using cached results</b> for <b>{escaped_query}</b> in <b>{escaped_location}</b>\n"
                f"📦 {len(jobs)} listings loaded from cache",
                parse_mode="HTML",
            )
        else:
            await status_msg.edit_text(
                f"🔎 Scanning <b>7 platforms</b> for <b>{escaped_query}</b>...\n"
                f"⏱ This takes 10-15 seconds — worth the wait!",
                parse_mode="HTML",
            )
            jobs = await run_search(query=query, location=location)

            if supabase and jobs:
                try:
                    store_jobs(jobs, query, normalize_query(query, location))
                except Exception as store_err:
                    logger.warning("Failed to store jobs in cache: %s", store_err)

        if not jobs:
            await status_msg.edit_text(
                f"😔 <b>No results found</b> for <b>{escaped_query}</b> in <b>{escaped_location}</b>.\n\n"
                f"💡 <b>Try:</b>\n"
                f"• A broader query like <code>/search founder's office</code>\n"
                f"• A different city with <code>/set_location</code>\n"
                f"• Checking back later — new listings appear daily!",
                parse_mode="HTML",
            )
            return

        # Filter to genuine founder's office roles first
        genuine_jobs = [j for j in jobs if j.get("is_genuine_fo_role", False)]
        sorted_jobs = sorted(
            genuine_jobs if genuine_jobs else jobs,
            key=lambda j: float(j.get("fps", 0)),
            reverse=True,
        )

        # Store full results for pagination
        store_user_page_data(user_id, query, sorted_jobs)

        # Build first page
        total = len(sorted_jobs)
        page_jobs = sorted_jobs[:RESULTS_PER_PAGE]
        unique_sources = len({j.get("source", "Unknown") for j in sorted_jobs})

        header = (
            f"🎯 <b>Top results for '{escaped_query}' in {escaped_location}</b>\n"
            f"📊 {total} listings from {unique_sources} source{'s' if unique_sources != 1 else ''}\n\n"
        )

        job_blocks: List[str] = []
        for i, job in enumerate(page_jobs, start=1):
            job_blocks.append(format_job(job, i))

        body = "\n\n".join(job_blocks)
        full_text = header + body

        # Pagination keyboard
        keyboard = None
        if total > RESULTS_PER_PAGE:
            remaining = total - RESULTS_PER_PAGE
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    f"Show more ▶ ({remaining} remaining)",
                    callback_data=f"page_2_{user_id}",
                )]
            ])

        # Delete the status message
        try:
            await status_msg.delete()
        except Exception:
            pass

        # Send results, splitting if too long
        await _send_long_message(
            update.message.chat_id,
            context,
            full_text,
            reply_markup=keyboard,
        )

    except Exception as exc:
        logger.error("Search failed for user %s, query '%s': %s", user_id, query, exc, exc_info=True)
        try:
            await status_msg.edit_text(
                f"❌ <b>Search failed</b>\n\n"
                f"Something went wrong while searching. Please try again in a moment.\n"
                f"If the issue persists, try a simpler query.",
                parse_mode="HTML",
            )
        except Exception:
            await status_msg.edit_text(
                "❌ Search failed. Please try again in a moment."
            )


async def _send_long_message(
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    reply_markup=None,
) -> None:
    """Send a message, splitting into multiple messages if it exceeds Telegram's limit."""
    max_len = 4000

    if len(text) <= max_len:
        await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=reply_markup,
        )
        return

    # Split on double newlines (between job blocks) to keep formatting intact
    chunks: List[str] = []
    current_chunk = ""
    sections = text.split("\n\n")

    for section in sections:
        candidate = current_chunk + ("\n\n" if current_chunk else "") + section
        if len(candidate) > max_len and current_chunk:
            chunks.append(current_chunk)
            current_chunk = section
        else:
            current_chunk = candidate

    if current_chunk:
        chunks.append(current_chunk)

    # Send all chunks; only the last one gets the reply markup
    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1
        await context.bot.send_message(
            chat_id=chat_id,
            text=chunk,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=reply_markup if is_last else None,
        )


async def pagination_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle pagination button presses."""
    query_obj = update.callback_query
    await query_obj.answer()

    data = query_obj.data or ""
    # Format: page_{page_num}_{user_id}
    parts = data.split("_")
    if len(parts) < 3 or parts[0] != "page":
        return

    try:
        page_num = int(parts[1])
        user_id = int("_".join(parts[2:]))
    except (ValueError, IndexError):
        return

    page_data = get_user_page_data(user_id)
    if not page_data:
        await query_obj.message.reply_text(
            "⚠️ Session expired. Please run a new <code>/search</code>.",
            parse_mode="HTML",
        )
        return

    stored_query = page_data.get("query", "")
    all_jobs: List[Dict[str, Any]] = page_data.get("jobs", [])
    total = len(all_jobs)

    start_idx = (page_num - 1) * RESULTS_PER_PAGE
    end_idx = start_idx + RESULTS_PER_PAGE
    page_jobs = all_jobs[start_idx:end_idx]

    if not page_jobs:
        await query_obj.message.reply_text(
            "📭 No more results to show.",
            parse_mode="HTML",
        )
        return

    escaped_query = html.escape(stored_query)
    header = (
        f"📄 <b>Page {page_num} — '{escaped_query}'</b>\n"
        f"Showing {start_idx + 1}-{min(end_idx, total)} of {total}\n\n"
    )

    job_blocks: List[str] = []
    for i, job in enumerate(page_jobs, start=start_idx + 1):
        job_blocks.append(format_job(job, i))

    body = "\n\n".join(job_blocks)
    full_text = header + body

    # Build pagination buttons
    buttons: List[InlineKeyboardButton] = []
    if page_num > 1:
        buttons.append(InlineKeyboardButton(
            "◀ Back",
            callback_data=f"page_{page_num - 1}_{user_id}",
        ))
    if end_idx < total:
        remaining = total - end_idx
        buttons.append(InlineKeyboardButton(
            f"Show more ▶ ({remaining} remaining)",
            callback_data=f"page_{page_num + 1}_{user_id}",
        ))

    keyboard = InlineKeyboardMarkup([buttons]) if buttons else None

    await _send_long_message(
        query_obj.message.chat_id,
        context,
        full_text,
        reply_markup=keyboard,
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log errors and send a user-friendly message."""
    logger.error("Unhandled exception: %s", context.error, exc_info=context.error)

    if not isinstance(update, Update) or not update.effective_message:
        return

    try:
        await update.effective_message.reply_text(
            "⚠️ <b>Oops!</b> Something went wrong.\n\n"
            "Please try again. If the problem continues, try a different search query.",
            parse_mode="HTML",
        )
    except Exception:
        try:
            await update.effective_message.reply_text(
                "⚠️ Something went wrong. Please try again."
            )
        except Exception:
            logger.error("Failed to send error message to user.")


def main() -> None:
    """Build and run the Telegram bot application."""
    if not TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is not set. Exiting.")
        return

    application = Application.builder().token(TOKEN).build()

    # Register command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("set_location", set_location_command))
    application.add_handler(CommandHandler("search", search))

    # Register callback query handler for pagination
    application.add_handler(CallbackQueryHandler(pagination_callback, pattern=r"^page_"))

    # Register error handler
    application.add_error_handler(error_handler)

    logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    logger.info("🚀 Founder's Office Finder Bot starting")
    logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    if WEBHOOK_URL:
        webhook_path = f"/webhook/{TOKEN}"
        full_webhook_url = f"{WEBHOOK_URL}{webhook_path}"
        logger.info("Running with webhook: %s", WEBHOOK_URL)
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=webhook_path,
            webhook_url=full_webhook_url,
        )
    else:
        logger.info("Running with polling (no RENDER_EXTERNAL_URL set)")
        application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()