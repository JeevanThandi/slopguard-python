"""An in-memory store of todo items."""

from .todo import Todo


class TodoStore:
    def __init__(self):
        self._items = {}
        self._next_id = 1

    def add(self, title):
        todo = Todo(self._next_id, title)
        self._items[todo.id] = todo
        self._next_id += 1
        return todo

    def get(self, id):
        return self._items.get(id)

    def remove(self, id):
        if id in self._items:
            del self._items[id]
            return True
        return False

    def complete(self, id):
        todo = self._items.get(id)
        if todo is None:
            return False
        todo.mark_done()
        return True

    def all(self):
        return list(self._items.values())
