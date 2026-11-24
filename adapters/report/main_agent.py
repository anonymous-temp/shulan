"""Production main-Agent composition; data queries do not depend on this port."""

from __future__ import annotations

from dataclasses import asdict

from modules.report.agent import REPORT_FOLLOWUP_SELECTION
from modules.report.agent import REPORT_MAIN_SELECTION
from modules.report.agent import REPORT_NARRATIVE_SELECTION
from modules.report.agent import ReportAgent
from modules.report.agent import UnavailableReportAgent
from modules.report.artifacts import content_hash

REPORT_CHILD_SELECTIONS = (REPORT_NARRATIVE_SELECTION, REPORT_FOLLOWUP_SELECTION)


def build_report_main_agent(runtime, pipeline):
    def dependencies():
        selected = {}
        for selection in REPORT_CHILD_SELECTIONS:
            registration = runtime.registry.get(selection.capability, selection=selection)
            selected[selection.capability] = asdict(registration.selection)
        profiles = {}
        for selection in REPORT_CHILD_SELECTIONS:
            name = selection.capability
            profile = runtime.profiles.get(name)
            profiles[name] = {
                'profileDigest': content_hash(asdict(profile)),
                'promptDigest': content_hash(runtime.llm_gateway.render_prompt(profile)),
                'providerModel': list(runtime.llm_gateway.request_model(profile)),
            }
        value = {'children': selected, 'modelProfiles': profiles}
        research = getattr(pipeline, 'research_agent', None)
        if research is not None:
            value['research'] = research.manifest
        return value

    installed = {}

    def configure(staged):
        agent = ReportAgent(pipeline, dependencies=dependencies(), verify_dependencies=dependencies)
        staged.ports.register(REPORT_MAIN_SELECTION, agent.port)
        installed['agent'] = agent

    if not runtime.install_module('report-main', configure):
        return UnavailableReportAgent()
    return installed['agent']
