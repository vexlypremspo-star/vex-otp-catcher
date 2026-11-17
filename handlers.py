import re
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes
from config import OWNER_ID, SERVICE_DOMAINS
from database import is_authorized, add_admin_record, remove_admin_record, get_admin_ids
from gmail_service import search_latest_service_email

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

KEYWORDS = r"(?:code|otp|passcode|verification|verify|pin|security)"
CODE = r"(?<![\w#.$])(\d{3}[ -]\d{3}|\d{4,8})(?!\d)(?!\.\d)"

AFTER_KEYWORD = re.compile(rf"{KEYWORDS}\D{{0,60}}?{CODE}", re.IGNORECASE)
BEFORE_KEYWORD = re.compile(rf"{CODE}\D{{0,40}}?{KEYWORDS}", re.IGNORECASE)
STANDALONE = re.compile(CODE)


def authorized(update: Update) -> bool:
    return bool(update.effective_user and is_authorized(update.effective_user.id))


def _clean_texts(result: dict) -> list[str]:
    texts = []
    for key in ("body", "text", "html", "snippet", "subject"):
        text = result.get(key) or ""
        if key == "html":
            text = re.sub(r"(?is)<(style|script).*?>.*?</\1>", " ", text)
            text = re.sub(r"<[^>]+>", " ", text)
            text = text.replace("&nbsp;", " ")
        if text:
            texts.append(text)
    return texts


def _normalize(code: str) -> str:
    return re.sub(r"[ -]", "", code)


def extract_otp(result: dict) -> str | None:
    texts = _clean_texts(result)

    # Pass 1: a number next to a word like "code" or "verification"
    for pattern in (AFTER_KEYWORD, BEFORE_KEYWORD):
        for text in texts:
            m = pattern.search(text)
            if m:
                return _normalize(m.group(1))

    # Pass 2: any standalone 4-8 digit number; skip years, prefer 6 digits
    candidates = []
    for text in texts:
        for m in STANDALONE.finditer(text):
            value = _normalize(m.group(1))
            if len(value) == 4 and value.startswith(("19", "20")):
                continue
            candidates.append(value)
    for value in candidates:
        if len(value) == 6:
            return value
    return candidates[0] if candidates else None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        await update.message.reply_text("Access denied. Ask the bot owner for access.")
        return
    keyboard = [[InlineKeyboardButton(name, callback_data=f"service:{name}")]
                for name in SERVICE_DOMAINS]
    await update.message.reply_text(
        "Choose a service first, then send the recipient email address to find its latest "
        "matching message in the connected Gmail inbox.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def service_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_authorized(query.from_user.id):
        await query.edit_message_text("Access denied.")
        return
    service = query.data.split(":", 1)[1]
    context.user_data["selected_service"] = service
    await query.edit_message_text(
        f"Selected: {service}\n\nNow send the recipient email address to search."
    )


async def email_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        await update.message.reply_text("Access denied.")
        return
    service = context.user_data.get("selected_service")
    if not service:
        await update.message.reply_text("Please choose a service first using /start.")
        return
    address = update.message.text.strip()
    if not EMAIL_RE.match(address):
        await update.message.reply_text("That doesn't look like a valid email address. Please try again.")
        return
    await update.message.reply_text(f"Checking the connected Gmail inbox for a {service} message to {address}…")
    try:
        result = search_latest_service_email(service, address)
        if not result:
            await update.message.reply_text(
                f"No matching {service} email was found for {address} in the connected Gmail inbox."
            )
        else:
            subject = result.get("subject") or "(no subject)"
            otp = extract_otp(result) or "(no code found in this email)"
            message = (
                f"🔐 New verification email\n\n"
                f"Service: {service}\n"
                f"Recipient: {address}\n"
                f"Sender: {result.get('from') or '(unknown sender)'}\n"
                f"Subject: {subject}\n"
                f"Received: {result.get('date') or '(unknown)'}\n"
                f"OTP: {otp}"
            )
            await update.message.reply_text(message[:3900])
    except Exception as exc:
        await update.message.reply_text(f"Could not search Gmail: {exc}")
    finally:
        context.user_data.pop("selected_service", None)


async def add_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("Only the owner can add admins.")
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /addadmin TELEGRAM_USER_ID")
        return
    user_id = int(context.args[0])
    add_admin_record(user_id, OWNER_ID)
    await update.message.reply_text(f"Admin added: {user_id}")


async def remove_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("Only the owner can remove admins.")
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /removeadmin TELEGRAM_USER_ID")
        return
    try:
        remove_admin_record(int(context.args[0]))
        await update.message.reply_text(f"Admin removed: {context.args[0]}")
    except ValueError as exc:
        await update.message.reply_text(str(exc))


async def list_admins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("Only the owner can view the admin list.")
        return
    ids = get_admin_ids()
    await update.message.reply_text("Authorized admins:\n" + ("\n".join(map(str, ids)) if ids else "(none)"))
