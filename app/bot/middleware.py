from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message


class AccessMiddleware(BaseMiddleware):
    def __init__(
        self, allowed_chat_ids, allowed_user_ids=(), *, repository=None, registry=None, admins=()
    ):
        self.allowed = frozenset(allowed_chat_ids)
        self.users = frozenset(allowed_user_ids)
        self.repository, self.registry, self.admins = repository, registry, frozenset(admins)

    async def __call__(self, handler, event, data):
        message = event.message if isinstance(event, CallbackQuery) else event
        if not isinstance(message, Message):
            return None
        if not event.from_user or event.from_user.is_bot:
            return None
        if self.repository:
            members = await self.repository.bot_users()
            self.registry.user_grants = {
                uid: set(value["client_ids"])
                for uid, value in members.items()
                if value["enabled"] and uid not in self.admins
            }
            user = event.from_user.id
            if user not in self.admins and user in members:
                if not members[user]["enabled"]:
                    return None
                if message.chat.type == "private" and message.chat.id == user:
                    return await handler(event, data)
                if message.chat.id in self.allowed:
                    return await handler(event, data)
                return None
        if message.chat.id not in self.allowed:
            return None
        if self.users and event.from_user.id not in self.users:
            return None
        return await handler(event, data)
