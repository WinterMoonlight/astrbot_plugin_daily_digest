"""Task administration and bounded preview workers.

Formal delivery is deliberately not enabled by this development milestone.
"""
import asyncio

from .domain import TaskConfig, parse_time, utcnow


class Service:
    def __init__(self, store, generator):
        self.store, self.generator = store, generator
        self.active = {}
        self.wake = asyncio.Event()
        self.stopping = False
        self.pump_task = None

    async def start(self):
        await self.store.initialize()
        await self.store.cleanup((await self.store.settings())['retention_days'])
        self.pump_task = asyncio.create_task(self.pump(), name='digest-preview-queue')

    async def stop(self):
        self.stopping = True
        self.wake.set()
        if self.pump_task:
            await self.pump_task
        jobs = list(self.active.values())
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)

    async def preview(self, task_id):
        task = await self.store.get_task(task_id)
        config = TaskConfig.from_dict(task['config'])
        config.validate()
        # Fail promptly for invalid host configuration; no model request is made here.
        await self.generator.models.prepare(config)
        run_id = await self.store.enqueue(task_id, utcnow(), 'preview')
        self.wake.set()
        return run_id

    async def archive(self, task_id, version):
        result = await self.store.set_enabled(task_id, version, False, archive=True)
        active = self.active.get(task_id)
        if active:
            active.cancel()
            await asyncio.gather(active, return_exceptions=True)
        return result

    async def execute(self, run, settings):
        try:
            if (await self.store.get_task(run['task_id']))['archived']:
                await self.store.finish(run['id'], 'cancelled')
                return
            task = TaskConfig.from_dict(run['snapshot']['config'])
            planned = parse_time(run['snapshot']['planned_at'])
            async with asyncio.timeout(settings['generation_timeout']):
                result = await self.generator.run(task, planned, settings)
            current = await self.store.get_task(run['task_id'])
            if current['archived']:
                await self.store.finish(run['id'], 'cancelled')
            else:
                await self.store.finish(run['id'], 'ready' if result['summary'] else 'empty', result)
        except asyncio.CancelledError:
            # Leave generating in durable state; startup recovers it as queued.
            if not self.stopping:
                await self.store.finish(run['id'], 'cancelled')
            raise
        except TimeoutError:
            await self.store.finish(run['id'], 'failed', error='生成超过整体时间预算')
        except (ValueError, RuntimeError) as exc:
            await self.store.finish(run['id'], 'failed', error=str(exc))
        except Exception:
            await self.store.finish(run['id'], 'failed', error='生成失败，请检查模型或历史接口')
        finally:
            self.active.pop(run['task_id'], None)
            self.wake.set()

    async def pump(self):
        last_cleanup = asyncio.get_running_loop().time()
        while not self.stopping:
            self.wake.clear()
            settings = await self.store.settings()
            if asyncio.get_running_loop().time() - last_cleanup >= 3600:
                await self.store.cleanup(settings['retention_days'])
                last_cleanup = asyncio.get_running_loop().time()
            while not self.stopping and len(self.active) < settings['concurrency']:
                run = await self.store.claim(set(self.active))
                if run is None:
                    break
                self.active[run['task_id']] = asyncio.create_task(self.execute(run, settings))
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=2)
            except TimeoutError:
                pass
