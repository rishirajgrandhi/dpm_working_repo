"""Evidence collection — schema diff, load history, git history, prior incidents.

Arrives in M3 (deterministic collectors) and M4 (redact.py as a graph node).

Ordering is the whole point: evidence is collected BEFORE any LLM call, which is the
structural answer to risk R5 (the model inventing a root cause). It never gets to guess,
because the facts are already in the prompt (09 §2).
"""
