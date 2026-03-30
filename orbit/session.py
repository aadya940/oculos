"""Session owns the OculOS daemon and shared ADK session state."""

import logging
from typing import Optional

from google.adk.sessions import InMemorySessionService

from .daemon import OculOSManager

log = logging.getLogger("orbit.session")


class Session:
    """Async context manager that owns a single OculOS daemon and ADK session.

    When multiple verbs share a session, they share the same ADK conversation
    so the planner retains context across calls.

    Usage::

        async with Session() as s:
            await Do("open Notepad", session=s).run()
            await Do("type hello", session=s).run()  # planner knows Notepad is open
    """

    def __init__(self):
        self._daemon = OculOSManager()
        self._started = False
        self._session_service = InMemorySessionService()
        self._adk_session = None

    async def __aenter__(self) -> "Session":
        await self._daemon.start()
        self._started = True
        return self

    async def __aexit__(self, *exc):
        self._daemon.stop()
        self._started = False

    @property
    def started(self) -> bool:
        return self._started

    @property
    def session_service(self) -> InMemorySessionService:
        return self._session_service

    @property
    def adk_session(self):
        return self._adk_session

    @adk_session.setter
    def adk_session(self, value):
        self._adk_session = value


def session() -> Session:
    """Factory for ``async with orbit.session() as s:``"""
    return Session()
