import re
# ... your other imports stay the same

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
OTP_RE = re.compile(r"(?<!\d)\d{4,8}(?!\d)")


def extract_otp(result: dict) -> str | None:
    """Search subject first, then snippet/body, and return the first 4-8 digit code."""
    for key in ("subject", "snippet", "body", "text", "html"):
        text = result.get(key) or ""
        if key == "html":
            text = re.sub(r"<[^>]+>", " ", text)  # strip tags so CSS/HTML numbers aren't matched
        match = OTP_RE.search(text)
        if match:
            return match.group(0)
    return None


async def email_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        await update.message.reply_text("Access denied.")
        return
    service = context.user_data.get("selected_service")
    if not service:
        await update.message.reply_text("Please choose Disney+, Netflix, or ChatGPT first using /start.")
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
                f"OTP: {otp}"
            )
            await update.message.reply_text(message[:3900])
    except Exception as exc:
        await update.message.reply_text(f"Could not search Gmail: {exc}")
    finally:
        context.user_data.pop("selected_service", None)
