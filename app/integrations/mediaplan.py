"""Reads a media plan sheet shared by link and lets the model turn its free layout
into monthly numbers. Every sheet is drawn differently (reward steps at Grand Line,
a channel plan with facts at PhosAgro), so a fixed parser would not survive the next
client; the result is shown to a manager before it is saved."""

import asyncio
import hashlib
import json
import logging
import re
from datetime import date
from urllib.parse import urljoin, urlsplit

from pydantic import ValidationError

from app.domain.mediaplan import MediaPlan, sheet_location

logger = logging.getLogger(__name__)

MAX_SHEET_BYTES = 3_000_000
MAX_PROMPT_CHARS = 40_000
ALLOWED_HOSTS = ("docs.google.com",)
ALLOWED_SUFFIXES = (".googleusercontent.com",)

PROMPT = """Ты читаешь медиаплан рекламного агентства, выгруженный из Google Таблицы в CSV.
Сегодня {today}. Верни только JSON без пояснений и без Markdown:
{{
  "months": [{{"month": "YYYY-MM", "budget": число, "leads": число, "clicks": число или null,
              "rewards": {{"50": число, "100": число}}}}],
  "budget_vat": "without" | "with" | "unknown",
  "lead_definition": "что считается лидом, если написано, иначе пустая строка",
  "goal_ids": ["ID целей Метрики, если перечислены"],
  "leads_scale_with_budget": true | false,
  "notes": ["до трёх важных условий плана"]
}}
Правила:
- Нужен только план Яндекс Директа (контекстная реклама). Если в таблице несколько
  каналов, бери строку или блок Директа. Если канал не подписан и план один, бери его.
- Каждый месяц отдельно. Год бери из заголовков (например «ПЛАН 2026», «Факт 2026»),
  иначе текущий. Если месяцы идут не с января (например, февраль … январь), январь в
  конце списка относится к следующему году. Строки ИТОГО, кварталы, столбцы факта и
  баланса пропускай.
- budget: рекламный бюджет месяца. Если есть бюджет без НДС и с НДС, бери без НДС и
  поставь budget_vat "without". Услуги и вознаграждение агентства в бюджет не включай.
- leads: плановые лиды, обращения или конверсии месяца при выполнении плана на 100%.
- rewards: только если есть шкала вознаграждения по проценту выполнения KPI: ключ —
  процент («до 90%» → "90"), значение — сумма за месяц. Иначе пустой объект.
- goal_ids: числовые ID целей Метрики, если они указаны (обычно в скобках после названия).
- leads_scale_with_budget: true, если написано, что при превышении бюджета план по
  лидам растёт пропорционально.
- Числа пиши без пробелов, валют и процентов, дробную часть через точку.
- Если месячного бюджета или лидов в таблице нет, верни {{"error": "что не найдено"}}.

CSV:
{csv}"""


class MediaPlanError(Exception):
    """Shown to the manager as is."""


def number(value):
    if isinstance(value, str):
        cleaned = re.sub(r"[^\d.,-]", "", value.replace(" ", ""))
        if cleaned.count(",") == 1 and "." not in cleaned:
            cleaned = cleaned.replace(",", ".")
        return cleaned.replace(",", "") or None
    return value


def clean(payload):
    for month in payload.get("months") or []:
        if isinstance(month, dict):
            for key in ("budget", "leads", "clicks"):
                if key in month:
                    month[key] = number(month[key])
            if isinstance(month.get("rewards"), dict):
                month["rewards"] = {
                    re.sub(r"\D", "", str(step)): number(value)
                    for step, value in month["rewards"].items()
                    if value not in (None, "")
                }
    payload["goal_ids"] = [
        str(value) for value in payload.get("goal_ids") or [] if str(value).isdigit()
    ]
    return payload


def parse_reply(text):
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise MediaPlanError("Модель не вернула таблицу плана. Попробуйте ещё раз.")
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise MediaPlanError("Модель вернула нечитаемый ответ. Попробуйте ещё раз.") from exc
    if payload.get("error"):
        raise MediaPlanError(f"В таблице не нашлось плана: {str(payload['error'])[:200]}")
    try:
        return MediaPlan.model_validate(clean(payload))
    except ValidationError as exc:
        logger.warning("Media plan reply did not validate: %s", exc.errors()[:3])
        raise MediaPlanError(
            "Не удалось разобрать помесячный план. Проверьте, что ссылка ведёт на лист "
            "с бюджетом и лидами по месяцам."
        ) from exc


class MediaPlanReader:
    def __init__(self, http, llm):
        self.http, self.llm = http, llm

    async def fetch(self, url):
        """CSV text of the linked sheet; the sheet must be open to anyone with the link."""
        sheet_id, gid = sheet_location(url)
        target = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"
        if gid:
            target += f"&gid={gid}"
        for _ in range(5):
            response = await self.http.get(target)
            if response.status_code in (301, 302, 303, 307, 308):
                target = urljoin(target, response.headers.get("location", ""))
                host = urlsplit(target).hostname or ""
                # Follow Google's export redirect only, never an arbitrary location.
                if urlsplit(target).scheme != "https" or not (
                    host in ALLOWED_HOSTS or host.endswith(ALLOWED_SUFFIXES)
                ):
                    break
                continue
            kind = response.headers.get("content-type", "")
            if response.status_code == 200 and "text/csv" in kind:
                if len(response.content) > MAX_SHEET_BYTES:
                    raise MediaPlanError("Лист слишком большой. Дайте ссылку на лист с планом.")
                return response.content.decode("utf-8", errors="replace")
            if response.status_code == 404:
                raise MediaPlanError("Таблица или лист не найдены. Проверьте ссылку.")
            break
        # A closed sheet answers with Google's login page.
        raise MediaPlanError(
            "Нет доступа к таблице. Откройте доступ «Все, у кого есть ссылка» (просмотр) "
            "или пришлите ссылку на другой лист."
        )

    async def extract(self, csv_text, today=None):
        if self.llm is None:
            raise MediaPlanError("Модель не подключена: медиаплан прочитать нечем.")
        rows = [line for line in csv_text.splitlines() if line.strip(", \t")]
        prompt = PROMPT.format(today=today or date.today(), csv="\n".join(rows)[:MAX_PROMPT_CHARS])
        async with asyncio.timeout(90):
            reply = await self.llm.complete([{"role": "user", "content": prompt}], [])
        return parse_reply(reply.content or "")

    async def read(self, url):
        """The plan and a hash of the sheet text, to skip the model on unchanged sheets."""
        text = await self.fetch(url)
        return await self.extract(text), source_hash(text)


def source_hash(text):
    return hashlib.sha256(text.encode()).hexdigest()
