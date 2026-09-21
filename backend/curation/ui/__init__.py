"""Leftovers of the retired v1 web UI (Gradio app and terminal removed in F1.2).

What remains is logic the v2 port still needs as a reference: HTTP Basic auth
(``auth``), deep-link and address parsing plus the task runner (``runner``),
report data shaping (``manifest``, ``episode_detail``) and the child reaper
(``reaper``). New code must not import from here; the package is deleted once
W4/W10 have ported it. Retired tests: docs/v1/retired-ui-tests.md.
"""
