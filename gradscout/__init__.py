"""GradScout: a focused, reliable job-monitoring utility.

Pipeline stages (kept deliberately separate and testable):
    collect -> normalize -> store/dedupe -> eligibility -> prioritize
            -> resume-select -> notify -> health-report

The deterministic core never depends on the optional LLM enrichment.
"""

__version__ = "0.1.0"
