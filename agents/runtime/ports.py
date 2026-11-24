"""Exact public domain capability versions, including non-Graph workflows."""

from ..contracts import AgentSelection
from ..errors import AgentCapabilityAlreadyRegisteredError
from ..errors import AgentCapabilityNotFoundError
from ..ports import AgentPort


class AgentPortRegistry:
    def __init__(self):
        self._ports = {}

    def register(self, selection: AgentSelection, port: AgentPort):
        if not isinstance(selection, AgentSelection) or not isinstance(port, AgentPort):
            raise TypeError('Registration requires a complete selection and AgentPort')
        if getattr(port, 'selection', selection) != selection:
            raise ValueError('Port selection does not match registration')
        if selection in self._ports:
            raise AgentCapabilityAlreadyRegisteredError(f'Agent port already registered: {selection}')
        self._ports[selection] = port
        return port

    def get(self, selection: AgentSelection) -> AgentPort:
        try:
            return self._ports[selection]
        except KeyError as error:
            raise AgentCapabilityNotFoundError(f'Unknown Agent port version: {selection}') from error

    def selections(self):
        return tuple(
            sorted(self._ports, key=lambda item: (item.capability, item.contract_version, item.implementation_revision))
        )

    def merged(self, staged):
        result = AgentPortRegistry()
        result._ports = dict(self._ports)
        for selection, port in staged._ports.items():
            result.register(selection, port)
        return result

    def publish(self, validated):
        self._ports = dict(validated._ports)
