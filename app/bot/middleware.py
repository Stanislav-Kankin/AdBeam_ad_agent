from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message


class AccessMiddleware(BaseMiddleware):
    def __init__(self, allowed_chat_ids):
        self.allowed = frozenset(allowed_chat_ids)

    async def __call__(self, handler, event, data):
        message = event.message if isinstance(event, CallbackQuery) else event
        if not isinstance(message, Message) or message.chat.id not in self.allowed:
            return None
        if not event.from_user or event.from_user.is_bot:
            return None
        return await handler(event, data)
