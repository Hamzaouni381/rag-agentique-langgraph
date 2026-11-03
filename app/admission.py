"""Controle d'admission : le LLM sur CPU ne traite efficacement qu'une question a la fois.

Les questions suivantes attendent leur tour dans une file bornee. File pleine ou attente trop longue :
refus immediat et explicite (503), plutot qu'une saturation qui ferait echouer tout le monde."""
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from app.config import get_settings

BUSY_MESSAGE = "Le service traite trop de demandes pour le moment. Réessayez dans quelques instants."


class QueueFull(Exception):
    """Pas de place dans la file, ou attente depassee."""


class AdmissionController:
    def __init__(self, max_active: int, max_waiting: int, wait_seconds: float):
        self._slots = threading.Semaphore(max_active)
        self._lock = threading.Lock()
        self._waiting = 0
        self.max_waiting = max_waiting
        self.wait_seconds = wait_seconds

    @contextmanager
    def slot(self) -> Iterator[None]:
        with self._lock:
            if self._waiting >= self.max_waiting:
                raise QueueFull
            self._waiting += 1
        try:
            acquired = self._slots.acquire(timeout=self.wait_seconds)
        finally:
            with self._lock:
                self._waiting -= 1
        if not acquired:
            raise QueueFull
        try:
            yield
        finally:
            self._slots.release()


@lru_cache
def get_admission() -> AdmissionController:
    s = get_settings()
    return AdmissionController(s.max_active_requests, s.request_queue_size, s.request_wait_seconds)
