"""Build the list of collectors from a validated Config.

Direct company sources (Greenhouse/Lever/Ashby) and GitHub repository sources
stay distinguishable via each collector's SourceType and source_id.
"""

from __future__ import annotations

from gradscout.collectors.ashby import AshbyCollector
from gradscout.collectors.base import Collector
from gradscout.collectors.github_repo import GithubRepoCollector
from gradscout.collectors.greenhouse import GreenhouseCollector
from gradscout.collectors.lever import LeverCollector
from gradscout.collectors.rippling import RipplingCollector
from gradscout.models import Config


def build_collectors(config: Config) -> list[Collector]:
    collectors: list[Collector] = []
    for s in config.greenhouse:
        collectors.append(GreenhouseCollector(s.company, s.board, s.company_priority))
    for s in config.lever:
        collectors.append(LeverCollector(s.company, s.board, s.company_priority))
    for s in config.ashby:
        collectors.append(AshbyCollector(s.company, s.board, s.company_priority))
    for s in config.rippling:
        collectors.append(RipplingCollector(s.company, s.board, s.company_priority))
    for r in config.github_repos:
        collectors.append(GithubRepoCollector(r.name, r.url, r.parser))
    return collectors
