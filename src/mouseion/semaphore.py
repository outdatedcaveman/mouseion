"""
Safe Semaphore implementation.

Avoids propagating "ValueError: Semaphore released too many times" during complex
cancellation or timeout flows, which can stall lookups or enrichment loops.
"""

import asyncio

class SafeSemaphore(asyncio.Semaphore):
    """
    A subclass of asyncio.Semaphore that catches ValueError on release()
    to prevent crashes under heavy concurrency and timeout cancellations.
    """
    def release(self) -> None:
        try:
            super().release()
        except ValueError:
            # Ignore "Semaphore released too many times" to keep the loop robust.
            pass
