"""Tests for ConversationMemory: one run's transcript."""

from app.models.schemas import AIChatMessage
from app.services.ai.agent.memory import ConversationMemory


def test_memory_keeps_order_and_drops_orphan_tool_results():
    mem = ConversationMemory()
    mem.add_user('task')
    mem.add(AIChatMessage(role='tool', content='orphan', tool_call_id='gone'))
    mem.add(AIChatMessage(role='assistant', content='', tool_calls=[
        {'id': 'c1', 'type': 'function', 'function': {'name': 'read', 'arguments': '{}'}}]))
    mem.add(AIChatMessage(role='tool', content='result', tool_call_id='c1'))
    mem.add_assistant('done')
    assert [m.content for m in mem.messages] == ['task', '', 'result', 'done']
    assert len(mem) == 5
