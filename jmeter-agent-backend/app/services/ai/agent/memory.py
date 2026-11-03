"""ConversationMemory — what one agent run has said and done, in order.

Each request makes its own agent, so this is that request's transcript. A retried step rebuilds its
conversation from it, and the adapter trims what does not fit the model's context.
"""

from app.models.schemas import AIChatMessage


class ConversationMemory:
    def __init__(self):
        self._messages: list[AIChatMessage] = []

    @property
    def messages(self) -> list[AIChatMessage]:
        """The transcript without tool results whose request is missing."""
        from app.services.ai.adapters.base import message_groups
        return [message for group in message_groups(self._messages) for message in group]

    def add(self, message: AIChatMessage) -> None:
        self._messages.append(message)

    def add_user(self, content: str) -> None:
        self.add(AIChatMessage(role="user", content=content))

    def add_assistant(self, content: str) -> None:
        self.add(AIChatMessage(role="assistant", content=content))

    def clear(self) -> None:
        self._messages.clear()

    def __len__(self) -> int:
        return len(self._messages)
