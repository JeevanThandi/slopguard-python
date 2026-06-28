"""Filtering helpers over a list of todos."""


def active(todos):
    return [t for t in todos if not t.done]


def completed(todos):
    return [t for t in todos if t.done]


def by_title(todos, query):
    q = query.lower()
    return [t for t in todos if q in t.title.lower()]
