KEYWORDS = r"(?:code|otp|passcode|verification|verify|pin|security)"
CODE = r"(?<![\w#.$])(\d{3}[ -]\d{3}|\d{4,8})(?!\d)(?!\.\d)"

AFTER_KEYWORD = re.compile(rf"{KEYWORDS}\D{{0,60}}?{CODE}", re.IGNORECASE)
BEFORE_KEYWORD = re.compile(rf"{CODE}\D{{0,40}}?{KEYWORDS}", re.IGNORECASE)
STANDALONE = re.compile(CODE)


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
