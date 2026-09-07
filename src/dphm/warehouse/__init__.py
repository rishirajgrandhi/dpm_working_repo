"""Warehouse connectivity — the layer both `catalog/` and `state/` sit on.

Extracted from `state/` because `catalog/` needs connections too, and 04 §2 places
`catalog` *below* `state`. Leaving the connection factory in `state/` made `catalog`
import upward, which the import-linter layering contract correctly rejected.

This package owns the writer-role allowlist (04 rule 6). It imports only `config` and
`util`, so it cannot reach anything that decides a verdict.
"""
