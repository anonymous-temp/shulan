"""Independent fixed Health Analysis entry; no legacy session or task ownership."""

from copy import deepcopy

HEALTH_ANALYSIS_EXECUTION = {
    'capability': 'health_analysis.main',
    'implementationRevision': 'health-analysis-main-v4',
}


class HealthAnalysisAgent:
    def __init__(self, generator):
        self.generator = generator

    def execution_manifest(self):
        return deepcopy(HEALTH_ANALYSIS_EXECUTION)

    async def generate(self, snapshot):
        return await self.generator.generate(snapshot)
