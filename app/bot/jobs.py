import asyncio
import logging

logger = logging.getLogger(__name__)


class BackgroundJobs:
    def __init__(self, limit=4):
        self.limit, self.tasks = limit, {}

    def start(self, key, work, on_error):
        if key in self.tasks or len(self.tasks) >= self.limit:
            return False
        if (
            isinstance(key, tuple)
            and sum(isinstance(k, tuple) and k[0] == key[0] for k in self.tasks) >= 2
        ):
            return False

        async def run():
            try:
                async with asyncio.timeout(600):
                    await work()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Background job failed (%s)", type(exc).__name__)
                try:
                    await on_error()
                except Exception:
                    logger.error("Failure notification could not be delivered")
            finally:
                self.tasks.pop(key, None)

        self.tasks[key] = asyncio.create_task(run())
        return True

    async def close(self):
        tasks = list(self.tasks.values())
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=20)
        for task in pending:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
