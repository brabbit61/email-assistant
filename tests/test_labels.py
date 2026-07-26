"""Label reconcile: idempotent create, drift-only patch."""

from assistant import labels


class _ListExec:
    def __init__(self, items):
        self._items = items

    def execute(self):
        return {"labels": self._items}


class _LabelExec:
    def __init__(self, label):
        self._label = label

    def execute(self):
        return self._label


class FakeLabelsResource:
    """Stand-in for svc.users().labels(), avoiding any network."""

    def __init__(self, store):
        self._store = store
        self._next_id = 1

    def list(self, userId):
        return _ListExec(list(self._store.values()))

    def create(self, userId, body):
        label = {"id": f"Label_{self._next_id}", **body}
        self._next_id += 1
        self._store[label["name"]] = label
        return _LabelExec(label)

    def patch(self, userId, id, body):
        for name, existing in list(self._store.items()):
            if existing["id"] == id:
                del self._store[name]
        label = {"id": id, **body}
        self._store[label["name"]] = label
        return _LabelExec(label)


class FakeService:
    def __init__(self):
        self.store: dict[str, dict] = {}

    def users(self):
        return self  # only .labels() is needed off "users"

    def labels(self):
        return FakeLabelsResource(self.store)


def test_first_reconcile_creates_all_labels():
    svc = FakeService()
    result = labels.reconcile(svc)
    assert len(result.created) == len(labels.LABELS)
    assert result.updated == []
    assert result.unchanged == []
    assert len(result.ids) == len(labels.LABELS)


def test_second_reconcile_is_idempotent():
    svc = FakeService()
    labels.reconcile(svc)
    result = labels.reconcile(svc)
    assert result.created == []
    assert result.updated == []
    assert len(result.unchanged) == len(labels.LABELS)


def test_drifted_color_gets_patched_only_for_that_label():
    svc = FakeService()
    labels.reconcile(svc)
    drifted_name = labels.LABELS[0].full_name
    svc.store[drifted_name]["color"]["backgroundColor"] = "#000000"

    result = labels.reconcile(svc)

    assert result.updated == [drifted_name]
    assert result.created == []
    assert len(result.unchanged) == len(labels.LABELS) - 1
    assert svc.store[drifted_name]["color"]["backgroundColor"] == labels.LABELS[0].bg
