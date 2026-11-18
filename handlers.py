import re
from datetime import datetime, timezone
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes
from config import OWNER_ID, SERVICE_DOMAINS
from database import is_authorized, add_admin_record, remove_admin_record, get_admin_ids
from gmail_service import search_latest_service_email

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

OTP_EXPIRY_MINUTES = 15  # older than this = expired

# A 4-8 digit code (or "123 456" / "123-456"), not part of a longer number, link, date or price.
CODE_RE = re.compile(
    r"(?<![\w#.$+@/=&?-])(\d{3}[ -]\d{3}|\d{4,8})(?![\w@/-]|[ -]\d|[.,]\d)"
)
KEYWORDS = re.compile(
    r"(passcode|pass code|otp|one[- ]time|verification|verify|security code|"
    r"login code|sign[- ]?in code|access code|\bcode\b|\bpin\b)",
    re.IGNORECASE,
)
BAD_AFTER = re.compile(
    r"\s*(?:minutes?|mins?|hours?|hrs?|days?|seconds?|%|usd|php|pesos?)\b", re.IGNORECASE
)
BAD_BEFORE = re.compile(r"(©|copyright|\$|₱|tel|phone|call|order|ref|invoice|#)", re.IGNORECASE)


def authorized(update: Update) -> bool:
    return bool(update.effective_user and is_authorized(update.effective_user.id))


def _normalize(code: str) -> str:
    return re.sub(r"[ -]", "", code)


def _score(text: str, m: re.Match) -> int:
    value = _normalize(m.group(1))
    start, end = m.span(1)
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    if line_end == -1:
        line_end = len(text)
    before = text[max(line_start, start - 80):start]
    after = text[end:min(line_end, end + 40)]
    line = text[line_start:line_end].strip()

    score = 0
    if len(value) == 6:
        score += 2
    if KEYWORDS.search(before):
        score += 4
    elif KEYWORDS.search(after):
        score += 2
    if line == m.group(1).strip():  # code alone on its own line
        score += 5
    if len(value) == 4 and value[:2] in ("19", "20"):  # looks like a year
        score -= 6
    if BAD_AFTER.match(after):  # "15 minutes", "100%", ...
        score -= 6
    if BAD_BEFORE.search(before[-15:]):  # price, phone, order number, copyright
        score -= 6
    return score


def extract_otp(result: dict) -> str | None:
    best_value, best_score = None, 0  # needs a score of 1 or more
    for key in ("subject", "body"):
        text = result.get(key) or ""
        for m in CODE_RE.finditer(text):
            score = _score(text, m)
            if score > best_score:
                best_value, best_score = _normalize(m.group(1)), score
    return best_value


def _age_minutes(result: dict) -> float | None:
    try:
        received = datetime.strptime(result.get("date"), "%Y-%m-%d %H:%M:%S UTC")
    except (TypeError, ValueError):
        return None
    received = received.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - received).total_seconds() / 60


def _format_age(minutes: float) -> str:
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{int(minutes)} min ago"
    if minutes < 1440:
        return f"{int(minutes // 60)} hr ago"
    return f"{int(minutes // 1440)} day(s) ago"


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
            age = _age_minutes(result)
            header = (
                f"🔐 Verification email\n\n"
                f"Service: {service}\n"
                f"Recipient: {address}\n"
                f"Sender: {result.get('from') or '(unknown sender)'}\n"
                f"Subject: {subject}\n"
                f"Received: {_format_age(age) if age is not None else (result.get('date') or '(unknown)')}\n"
            )
            if age is not None and age > OTP_EXPIRY_MINUTES:
                message = header + (
                    f"\n⛔ EXPIRED: the latest code arrived {_format_age(age)}, "
                    f"and codes only last {OTP_EXPIRY_MINUTES} minutes.\n"
                    f"Ask {service} to send a new code, then check again."
                )
            else:
                otp = extract_otp(result)
                if otp:
                    note = "✅ Still valid" if age is not None else "(could not check expiry time)"
                    message = header + f"\nOTP: {otp}\n{note}"
                else:
                    message = header + "\nOTP: (no code found in this email)"
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
