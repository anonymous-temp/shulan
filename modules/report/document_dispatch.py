"""Exact admitted document manifests, isolated per asynchronous Report task."""

import json
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy


def _key(manifest):
    if not isinstance(manifest, dict) or not isinstance(manifest.get('consumerRevision'), str):
        raise ValueError('an exact Report document manifest is required')
    return json.dumps(manifest, sort_keys=True, ensure_ascii=False, allow_nan=False)


class ReportDocumentFactoryDispatcher:
    def __init__(self, *, default, supported):
        self._factories = {}
        for factory in (default, *supported):
            key = _key(factory.document_input_manifest())
            previous = self._factories.setdefault(key, factory)
            if previous is not factory:
                raise ValueError('one document manifest cannot select different factories')
        self._default = _key(default.document_input_manifest())
        self._selected = ContextVar('report_document_factory', default=None)

    def document_input_manifest(self):
        return json.loads(self._default)

    def supported_document_manifests(self):
        return [json.loads(key) for key in self._factories]

    @contextmanager
    def bind_document_manifest(self, manifest):
        key = _key(deepcopy(manifest))
        factory = self._factories.get(key)
        if factory is None or _key(factory.document_input_manifest()) != key:
            raise ValueError('the admitted Report document implementation is unavailable')
        token = self._selected.set(key)
        try:
            yield
        finally:
            self._selected.reset(token)

    @property
    def supports_dedicated_evaluation_pdf(self):
        factory = self._factories.get(self._selected.get())
        return getattr(factory, 'supports_dedicated_evaluation_pdf', False) is True

    async def recover_document_submissions(self):
        recovered = 0
        for factory in self._factories.values():
            recover = getattr(factory, 'recover_document_submissions', None)
            if callable(recover):
                recovered += await recover()
        return recovered

    def create(self, **kwargs):
        key = self._selected.get()
        if key is None:
            raise ValueError('Report must bind its admitted document manifest before execution')
        factory = self._factories[key]
        if _key(factory.document_input_manifest()) != key:
            raise ValueError('the admitted Report document implementation changed')
        return factory.create(**kwargs)
