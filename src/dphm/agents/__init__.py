"""The two LangGraph graphs (09). Authoring arrives in M6, reporting in M4.

**This is the only package permitted to import langchain / langgraph** (04 rule 1,
enforced by .importlinter contract 1). Neither graph can decide a pass/fail verdict:
the authoring agent's only output channel is a git PR, and the reporting agent runs
after the SQL has already decided.
"""
