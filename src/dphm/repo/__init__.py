"""Repo reader — clone, parse, grain candidates, ownership, blame (04).

Arrives in M6, except `dedup_extract.py` which arrives in M2B: the transform SQL usually
*contains* the dedup contract (a QUALIFY / ROW_NUMBER / DISTINCT ON), so it is read
deterministically before any model is asked anything (07B §6.1).
"""
