"""Curator v2 API Daemon: FastAPI + uvicorn, single replica (design docs 00, 01, 03, 08, 09).

W4 skeleton: :func:`daemon.app.create_app`, the SQLite repository behind the C5
protocol (``daemon.repo``), Basic auth, SSE, probes, the frontend and v1 deep
links. W5 (orchestration) and W8 (secrets) plug in through the runtime hooks,
:mod:`daemon.transitions`, :mod:`daemon.events` and :mod:`daemon.idempotency`.
``python -m daemon`` runs it.
"""
