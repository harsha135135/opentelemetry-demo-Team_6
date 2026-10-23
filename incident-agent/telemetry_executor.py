"""Run one synchronous telemetry call with a hard wait limit. The worker thread cannot be cancelled mid-request."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError  # noqa: F401  (re-exported)


def run_with_deadline(fn, *args, timeout: float):
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        return pool.submit(fn, *args).result(timeout=timeout)
    finally:
        pool.shutdown(wait=False)
