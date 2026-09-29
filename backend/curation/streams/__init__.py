"""Reading a remote dataset's bytes on demand, instead of copying it locally first.

Kept out of ``ingest/`` and ``dataset_level/`` on purpose: those are the A-class
directories the parity guard watches, where even an added file counts as drift.
"""
