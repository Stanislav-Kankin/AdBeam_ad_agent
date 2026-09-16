"""Render the supported model Markdown as text and explicit Telegram entities."""

import re

from aiogram.types import MessageEntity

from app.reporting.formatter import split_message


def markdown_parts(value, limit=4000):
    value = re.sub(r"(?m)^#{1,6}\s+(.+)$", r"**\1**", value)
    value = re.sub(r"(?m)^\s*[-*] ", "• ", value)
    pattern = re.compile(r"```(?:\w+\n)?([\s\S]*?)```|\*\*(.+?)\*\*|__(.+?)__|`([^`\n]+)`")
    pieces, spans, position, offset = [], [], 0, 0
    for match in pattern.finditer(value):
        prefix = value[position : match.start()]
        pieces.append(prefix)
        offset += len(prefix.encode("utf-16-le")) // 2
        content = next(g for g in match.groups() if g is not None)
        length = len(content.encode("utf-16-le")) // 2
        kind = (
            "pre"
            if match.group(1) is not None
            else "code"
            if match.group(4) is not None
            else "bold"
        )
        if length:
            spans.append((offset, offset + length, kind))
        pieces.append(content)
        offset += length
        position = match.end()
    pieces.append(value[position:])
    offset = 0
    for part in split_message("".join(pieces), limit):
        end = offset + len(part.encode("utf-16-le")) // 2
        entities = [
            MessageEntity(
                type=kind,
                offset=max(start, offset) - offset,
                length=min(stop, end) - max(start, offset),
            )
            for start, stop, kind in spans
            if start < end and stop > offset
        ]
        yield part, entities
        offset = end
