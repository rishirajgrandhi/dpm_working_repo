"""Scheduler and worker pool (12 §5). Arrives in M1.

Jobs are durable rows in DPHM_STATE.JOBS; the in-process pool is only the dispatcher, so
scale-out is a dispatcher swap rather than a redesign.
"""
