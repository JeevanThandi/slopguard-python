"""A tiny, fully-tested todo-list package used as slopguard-python's regression
baseline. Low complexity, ~100% coverage — every method should score well below
the crappy threshold."""

from .filter import active, by_title, completed
from .store import TodoStore
from .todo import Todo

__all__ = ["active", "by_title", "completed", "TodoStore", "Todo"]
