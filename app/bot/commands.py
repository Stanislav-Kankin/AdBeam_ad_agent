import shlex
from dataclasses import dataclass

from app.analytics.periods import make_period

HELP = """AdBeam Performance Analyst
/clients — доступные клиенты
/check_all [7d|14d|yesterday] — проверка всех
/check <клиент> [период] — подробная проверка
/summary_all [период] — краткая сводка
/summary <клиент> [период] — основные показатели
/schedule — расписание (администраторы)
/cancel — отменить выбор и очистить контекст диалога

Пример: /check "West Экспорт" 14d
Без клиента бот предложит кнопки. Период — до 90 завершённых дней, по умолчанию 7d.
Можно задать вопрос обычным текстом. Бот работает только на чтение."""


@dataclass
class ParsedCommand:
    name: str
    client_query: str
    period: str


def parse_command(text):
    try:
        parts = shlex.split(text)
    except ValueError:
        raise ValueError('Не закрыты кавычки. Пример: /check "West Экспорт" 14d') from None
    name = parts.pop(0).split("@", 1)[0].removeprefix("/")
    period = "7d"
    if parts and (
        parts[-1] in ("yesterday", "вчера") or parts[-1].endswith("d") and parts[-1][:-1].isdigit()
    ):
        period = parts.pop()
    make_period(period)
    if name.endswith("_all") and parts:
        raise ValueError("Укажите период, например /check_all 7d.")
    return ParsedCommand(name, " ".join(parts), period)
