from __future__ import annotations

import asyncio
import os
import threading
import unittest

from workers import WorkerConfig, Workers


def wait_for_release(started: threading.Event, release: threading.Event) -> str:
    started.set()
    release.wait(timeout=2)
    return "external-finished"


def process_id() -> int:
    return os.getpid()


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.workers = Workers(
            WorkerConfig(
                external_workers=1,
                gpu_workers=1,
                network_concurrency=2,
            )
        )

    async def asyncTearDown(self) -> None:
        self.workers.shutdown()

    async def test_inline_work_continues_while_external_worker_is_busy(self) -> None:
        started = threading.Event()
        release = threading.Event()
        external = asyncio.create_task(
            self.workers.run_external(wait_for_release, started, release)
        )

        await asyncio.wait_for(asyncio.to_thread(started.wait), timeout=1)
        try:
            result = await self.workers.run("inline", str.upper, "ready")
            self.assertEqual(result, "READY")
            self.assertEqual(self.workers.stats()["external"]["active"], 1)
        finally:
            release.set()

        self.assertEqual(await external, "external-finished")

    async def test_network_calls_respect_shared_limit(self) -> None:
        active = 0
        maximum = 0
        release = asyncio.Event()

        async def network_call() -> None:
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            try:
                await release.wait()
            finally:
                active -= 1

        calls = [
            asyncio.create_task(self.workers.run_network(network_call))
            for _ in range(3)
        ]
        while self.workers.stats()["network"]["active"] < 2:
            await asyncio.sleep(0)

        self.assertEqual(self.workers.stats()["network"]["waiting"], 1)
        release.set()
        await asyncio.gather(*calls)
        self.assertEqual(maximum, 2)

    async def test_gpu_work_runs_in_spawned_process(self) -> None:
        child_pid = await self.workers.run_gpu(process_id)
        self.assertNotEqual(child_pid, os.getpid())


if __name__ == "__main__":
    unittest.main()
