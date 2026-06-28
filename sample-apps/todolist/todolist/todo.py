"""A single todo item."""


class Todo:
    def __init__(self, id, title, done=False):
        self.id = id
        self.title = title
        self.done = done

    def mark_done(self):
        self.done = True

    def rename(self, title):
        self.title = title
