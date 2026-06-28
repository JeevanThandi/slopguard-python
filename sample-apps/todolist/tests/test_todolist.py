import unittest

from todolist import Todo, TodoStore, active, by_title, completed


class TodoTests(unittest.TestCase):
    def test_mark_done(self):
        t = Todo(1, "buy milk")
        self.assertFalse(t.done)
        t.mark_done()
        self.assertTrue(t.done)

    def test_rename(self):
        t = Todo(1, "old")
        t.rename("new")
        self.assertEqual(t.title, "new")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.store = TodoStore()

    def test_add_and_get(self):
        t = self.store.add("write tests")
        self.assertEqual(self.store.get(t.id).title, "write tests")

    def test_remove(self):
        t = self.store.add("temp")
        self.assertTrue(self.store.remove(t.id))
        self.assertFalse(self.store.remove(t.id))
        self.assertIsNone(self.store.get(t.id))

    def test_complete(self):
        t = self.store.add("ship it")
        self.assertTrue(self.store.complete(t.id))
        self.assertTrue(self.store.get(t.id).done)
        self.assertFalse(self.store.complete(999))

    def test_all(self):
        self.store.add("a")
        self.store.add("b")
        self.assertEqual(len(self.store.all()), 2)


class FilterTests(unittest.TestCase):
    def setUp(self):
        self.store = TodoStore()
        self.a = self.store.add("Alpha task")
        self.b = self.store.add("Beta task")
        self.store.complete(self.b.id)

    def test_active(self):
        self.assertEqual([t.id for t in active(self.store.all())], [self.a.id])

    def test_completed(self):
        self.assertEqual([t.id for t in completed(self.store.all())], [self.b.id])

    def test_by_title(self):
        self.assertEqual([t.id for t in by_title(self.store.all(), "alpha")], [self.a.id])


if __name__ == "__main__":
    unittest.main()
