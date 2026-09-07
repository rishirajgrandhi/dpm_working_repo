"""Check engine — templates + params -> SQL -> CheckResult (04, 06).

Arrives in M1 (render, scope, columns, execute, result) and M2 (the SCD templates).

Constraint: this package must never import langchain, langgraph, agents/, or reporting/,
and it may never hold a DPHM_WRITER connection. Everything that decides pass/fail is
deterministic SQL, and it must not be able to change what it is judging.
Enforced by .importlinter contracts 1, 2 and by the writer allowlist in state/connection.
"""
