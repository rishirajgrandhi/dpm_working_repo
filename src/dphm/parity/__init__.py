"""Lane B — the tiered RDS <-> Snowflake comparison (07).

Arrives in M5. Only `parity/tier2_land.py` may hold a writer connection, and only to
land suspect rows into DPHM_SCRATCH.

Note Q0e in `16`: the design doc would have *borrowed* a diff engine here rather than
building one. That reversal is recorded as deviation D5 and is not yet endorsed.
"""
