from __future__ import annotations

import runtime
import runtime.lifecycle
import runtime.resources
import runtime.supervisor
import web.lifecycle


def test_runtime_package_exports_defining_types():
    assert runtime.ApplicationLifecycle is runtime.lifecycle.ApplicationLifecycle
    assert runtime.LifecycleSnapshot is runtime.lifecycle.LifecycleSnapshot
    assert runtime.LifecycleState is runtime.lifecycle.LifecycleState
    assert runtime.RequestLease is runtime.lifecycle.RequestLease
    assert runtime.ResourceRegistry is runtime.resources.ResourceRegistry
    assert runtime.ResourceCloseError is runtime.resources.ResourceCloseError
    assert runtime.AsyncCloseRequiredError is (
        runtime.resources.AsyncCloseRequiredError
    )
    assert runtime.SupervisedTaskSnapshot is (
        runtime.supervisor.SupervisedTaskSnapshot
    )
    assert runtime.TaskAdmissionClosedError is (
        runtime.supervisor.TaskAdmissionClosedError
    )
    assert runtime.TaskSupervisor is runtime.supervisor.TaskSupervisor


def test_web_lifecycle_keeps_legacy_runtime_exports():
    assert web.lifecycle.ApplicationLifecycle is runtime.ApplicationLifecycle
    assert web.lifecycle.LifecycleSnapshot is runtime.LifecycleSnapshot
    assert web.lifecycle.LifecycleState is runtime.LifecycleState
    assert web.lifecycle.RequestLease is runtime.RequestLease
    assert web.lifecycle.SupervisedTaskSnapshot is runtime.SupervisedTaskSnapshot
    assert web.lifecycle.TaskAdmissionClosedError is (
        runtime.TaskAdmissionClosedError
    )
    assert web.lifecycle.TaskSupervisor is runtime.TaskSupervisor
