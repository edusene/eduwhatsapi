"""Execução em segundo plano sem infraestrutura extra (adequado ao Render).

- ``submit(fn, *args)``: executa ``fn`` numa thread do pool, dentro de um
  app context. Usado para entregar webhooks de saída sem atrasar a resposta
  à Meta. Se o processo morrer antes da entrega, a linha continua ``pending``
  no banco e o worker de retries a recupera (nada é perdido silenciosamente).
- Worker periódico (opcional, BACKGROUND_WORKER_ENABLED): a cada N segundos
  reivindica entregas vencidas com ``FOR UPDATE SKIP LOCKED`` (função SQL
  ``claim_due_webhook_deliveries``), seguro com vários workers do Gunicorn.
- Com TASKS_EAGER=true (testes) tudo roda de forma síncrona.

Limitação conhecida: no plano gratuito do Render o serviço hiberna sem
tráfego; retries só rodam com o processo ativo. Para garantir, configure um
cron externo chamando POST /internal/cron/deliver-webhooks (ver docs).
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from flask import Flask, current_app

logger = logging.getLogger(__name__)

_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()
_worker_started = False


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="autozap-task")
        return _executor


def submit(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
    app = current_app._get_current_object()

    def run() -> None:
        with app.app_context():
            try:
                fn(*args, **kwargs)
            except Exception:
                logger.exception("Falha em tarefa de segundo plano %s", getattr(fn, "__name__", fn))

    if app.config.get("TASKS_EAGER"):
        run()
        return
    _get_executor().submit(run)


def init_app(app: Flask) -> None:
    """Inicia o worker de retries na primeira requisição (por processo)."""
    if not app.config.get("BACKGROUND_WORKER_ENABLED"):
        return

    @app.before_request
    def _ensure_worker() -> None:
        global _worker_started
        if _worker_started:
            return
        with _executor_lock:
            if _worker_started:
                return
            _worker_started = True
        thread = threading.Thread(target=_worker_loop, args=(app,), daemon=True,
                                  name="autozap-webhook-worker")
        thread.start()


def _worker_loop(app: Flask) -> None:
    from app.services.outbound_webhooks import process_pending_retries

    interval = max(5, int(app.config.get("BACKGROUND_WORKER_INTERVAL", 30)))
    stop = threading.Event()
    logger.info("Worker de retries de webhooks iniciado (intervalo %ss).", interval)
    while not stop.wait(interval):
        with app.app_context():
            try:
                process_pending_retries()
            except Exception:
                logger.exception("Falha no ciclo do worker de retries.")
