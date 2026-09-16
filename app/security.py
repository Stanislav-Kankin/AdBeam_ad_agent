import logging
import os
import re


def redact(text: str) -> str:
    for name, value in os.environ.items():
        if (
            any(part in name.upper() for part in ("TOKEN", "SECRET", "API_KEY", "PASSWORD"))
            and len(value) >= 6
        ):
            text = text.replace(value, "[скрыто]")
    text = re.sub(r"(?i)(bearer|oauth)\s+[\w.-]+", r"\1 [скрыто]", text)
    text = re.sub(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b", "[токен скрыт]", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[ключ скрыт]", text)
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}", "[email скрыт]", text)
    text = re.sub(
        r"(?<![\w.])(?:\+7|8)[ (.-]*\d{3}[ ).-]*\d{3}[ -]*\d{2}[ -]*\d{2}(?![\w.])",
        "[номер скрыт]", text,
    )
    text = re.sub(r"(?<![\w.])\+\d{10,15}(?![\w.])", "[номер скрыт]", text)
    return text


class RedactingFormatter(logging.Formatter):
    def format(self, record):
        return redact(super().format(record))


def configure_logging(level: str):
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.basicConfig(level=level, handlers=[handler], force=True)
    for name in ("httpx", "httpcore", "openai", "aiogram.event"):
        logging.getLogger(name).setLevel(logging.WARNING)
