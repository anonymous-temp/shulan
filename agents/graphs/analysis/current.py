"""Current report extraction registration; default wiring is switched after provider integration."""

from dataclasses import replace

from .graph import build_analysis_health_report_graph
from .profile import create_analysis_health_report_profile

ANALYSIS_CURRENT_CAPABILITY = 'analysis.health_report.current'
ANALYSIS_CURRENT_GRAPH_VERSION = 'analysis-health-report-v13'


def register_current_analysis_health_report(runtime, *, model=None):
    profile = runtime.profiles.register(
        replace(
            create_analysis_health_report_profile(model=model),
            name=ANALYSIS_CURRENT_CAPABILITY,
            prompt_name='analysis/health_report_current.txt',
        )
    )
    return runtime.register_graph(
        ANALYSIS_CURRENT_CAPABILITY,
        build_analysis_health_report_graph(gateway=runtime.llm_gateway, profile=profile),
        version=ANALYSIS_CURRENT_GRAPH_VERSION,
        checkpointed=False,
    )
