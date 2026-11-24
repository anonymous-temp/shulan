from __future__ import annotations

import unittest
from types import SimpleNamespace

from runtime.application import ApplicationBundle
from runtime.resources import AsyncCloseRequiredError
from runtime.resources import ResourceCloseError
from runtime.resources import ResourceRegistry
from runtime.resources import ResourceRegistryClosedError


class _SyncResource:
    def __init__(self, events: list[str], name: str, error=None) -> None:
        self._events = events
        self._name = name
        self._error = error

    def close(self) -> None:
        self._events.append(f'close:{self._name}')
        if self._error is not None:
            raise self._error


class _AsyncResource:
    def __init__(self, events: list[str], name: str, error=None) -> None:
        self._events = events
        self._name = name
        self._error = error

    async def aclose(self) -> None:
        self._events.append(f'aclose:{self._name}')
        if self._error is not None:
            raise self._error


class ResourceRegistryTest(unittest.IsolatedAsyncioTestCase):
    def test_sync_resources_close_in_reverse_order_once(self):
        events: list[str] = []
        registry = ResourceRegistry()
        first = _SyncResource(events, 'first')
        second = _SyncResource(events, 'second')

        self.assertIs(registry.register_sync(first, name='first'), first)
        self.assertIs(registry.register_sync(second, name='second'), second)
        registry.close()
        registry.close()

        self.assertEqual(events, ['close:second', 'close:first'])
        self.assertTrue(registry.closed)
        self.assertEqual(registry.resource_count, 0)
        with self.assertRaises(ResourceRegistryClosedError):
            registry.register_sync(_SyncResource(events, 'late'))

    def test_close_aggregates_failures_and_continues(self):
        events: list[str] = []
        registry = ResourceRegistry()
        registry.register_sync(
            _SyncResource(events, 'first', ValueError('first failed')),
            name='first',
        )
        registry.register_sync(_SyncResource(events, 'middle'), name='middle')
        registry.register_sync(
            _SyncResource(events, 'last', RuntimeError('last failed')),
            name='last',
        )

        with self.assertRaises(ResourceCloseError) as raised:
            registry.close()

        self.assertEqual(
            events,
            ['close:last', 'close:middle', 'close:first'],
        )
        self.assertEqual(
            [failure.resource_name for failure in raised.exception.failures],
            ['last', 'first'],
        )
        self.assertIsInstance(
            raised.exception.failures[0].error,
            RuntimeError,
        )
        self.assertTrue(registry.closed)
        registry.close()

    async def test_aclose_handles_both_kinds_in_reverse_order(self):
        events: list[str] = []
        registry = ResourceRegistry()
        registry.register_sync(_SyncResource(events, 'sync'), name='sync')
        registry.register_async(_AsyncResource(events, 'async'), name='async')

        await registry.aclose()
        await registry.aclose()

        self.assertEqual(events, ['aclose:async', 'close:sync'])
        self.assertTrue(registry.closed)

    async def test_aclose_aggregates_sync_and_async_failures(self):
        events: list[str] = []
        registry = ResourceRegistry()
        registry.register_async(
            _AsyncResource(events, 'async', ValueError('async failed')),
            name='async',
        )
        registry.register_sync(
            _SyncResource(events, 'sync', RuntimeError('sync failed')),
            name='sync',
        )

        with self.assertRaises(ResourceCloseError) as raised:
            await registry.aclose()

        self.assertEqual(events, ['close:sync', 'aclose:async'])
        self.assertEqual(
            [failure.resource_name for failure in raised.exception.failures],
            ['sync', 'async'],
        )
        self.assertTrue(registry.closed)

    async def test_sync_close_requires_aclose_without_partial_shutdown(self):
        events: list[str] = []
        registry = ResourceRegistry()
        registry.register_sync(_SyncResource(events, 'sync'), name='sync')
        registry.register_async(_AsyncResource(events, 'async'), name='async')

        with self.assertRaises(AsyncCloseRequiredError) as raised:
            registry.close()

        self.assertEqual(raised.exception.resource_names, ('async',))
        self.assertEqual(events, [])
        self.assertFalse(registry.closed)

        await registry.aclose()
        self.assertEqual(events, ['aclose:async', 'close:sync'])


class ApplicationBundleResourceTest(unittest.IsolatedAsyncioTestCase):
    def test_close_preserves_legacy_agent_runtime_ownership(self):
        events: list[str] = []
        agent_runtime = _SyncResource(events, 'agent-runtime')
        bundle = ApplicationBundle(
            web_application=object(),
            container=SimpleNamespace(agent_runtime=agent_runtime),
            owns_agent_runtime=True,
        )

        bundle.close()
        bundle.close()

        self.assertEqual(events, ['close:agent-runtime'])

    def test_owned_agent_runtime_is_not_registered_twice(self):
        events: list[str] = []
        agent_runtime = _SyncResource(events, 'agent-runtime')
        resources = ResourceRegistry()
        resources.register_sync(agent_runtime, name='agent-runtime')
        bundle = ApplicationBundle(
            web_application=object(),
            container=SimpleNamespace(agent_runtime=agent_runtime),
            owns_agent_runtime=True,
            resources=resources,
        )

        bundle.close()

        self.assertEqual(events, ['close:agent-runtime'])

    async def test_aclose_closes_explicit_registry_resources(self):
        events: list[str] = []
        resources = ResourceRegistry()
        resources.register_sync(_SyncResource(events, 'database'))
        resources.register_async(_AsyncResource(events, 'http-client'))
        bundle = ApplicationBundle(
            web_application=object(),
            container=SimpleNamespace(agent_runtime=object()),
            resources=resources,
        )

        await bundle.aclose()
        await bundle.aclose()

        self.assertEqual(events, ['aclose:http-client', 'close:database'])

    def test_bundle_adopts_container_resource_registry(self):
        events: list[str] = []
        resources = ResourceRegistry()
        resources.register_sync(_SyncResource(events, 'container-resource'))
        bundle = ApplicationBundle(
            web_application=object(),
            container=SimpleNamespace(
                agent_runtime=object(),
                resources=resources,
            ),
        )

        self.assertIs(bundle.resources, resources)
        bundle.close()
        self.assertEqual(events, ['close:container-resource'])


if __name__ == '__main__':
    unittest.main()
