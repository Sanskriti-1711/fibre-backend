"""Permits AI advisory package.

Separation rule (enforced across the module):

* Deterministic code decides: permit identification, geometry intersections,
  road-class -> authority resolution, required evidence checklist,
  construction-blocking flag, final readiness (status_for_readiness +
  _refresh_readiness). Those live in `permits/rules/` + `permits/models.py`
  and are NEVER overwritten by anything in `permits/ai/`.

* AI code advises only: drafting narrative, expanding authority requirement
  checklists, explaining completeness gaps, narrating risk, estimating
  timelines. Every AI output is tagged `is_ai_generated=True` + a
  disclaimer, stored separately from the matrix, and requires human review
  before it can influence a submission.

If no LLM credentials are configured the package degrades to deterministic
heuristics (template + rule-derived text) so the UI remains useful offline
and tests stay deterministic.
"""
