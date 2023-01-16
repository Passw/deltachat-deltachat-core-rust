import asyncio
import json
import os
from typing import Any, Dict, Optional


class JsonRpcError(Exception):
    pass


class Rpc:
    def __init__(self, accounts_dir: Optional[str] = None, **kwargs):
        """The given arguments will be passed to asyncio.create_subprocess_exec()"""
        if accounts_dir:
            kwargs["env"] = {
                **kwargs.get("env", os.environ),
                "DC_ACCOUNTS_PATH": str(accounts_dir),
            }

        self._kwargs = kwargs
        self.process: asyncio.subprocess.Process
        self.id: int
        self.event_queues: Dict[int, asyncio.Queue]
        # Map from request ID to `asyncio.Future` returning the response.
        self.request_events: Dict[int, asyncio.Future]
        self.reader_task: asyncio.Task

    async def start(self) -> None:
        self.process = await asyncio.create_subprocess_exec(
            "deltachat-rpc-server",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            **self._kwargs
        )
        self.id = 0
        self.event_queues = {}
        self.request_events = {}
        self.reader_task = asyncio.create_task(self.reader_loop())

    async def close(self) -> None:
        """Terminate RPC server process and wait until the reader loop finishes."""
        self.process.terminate()
        await self.reader_task

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()

    async def reader_loop(self) -> None:
        while True:
            line = await self.process.stdout.readline()  # noqa
            if not line:  # EOF
                break
            response = json.loads(line)
            if "id" in response:
                fut = self.request_events.pop(response["id"])
                fut.set_result(response)
            elif response["method"] == "event":
                # An event notification.
                params = response["params"]
                account_id = params["contextId"]
                if account_id not in self.event_queues:
                    self.event_queues[account_id] = asyncio.Queue()
                await self.event_queues[account_id].put(params["event"])
            else:
                print(response)

    async def wait_for_event(self, account_id: int) -> Optional[dict]:
        """Waits for the next event from the given account and returns it."""
        if account_id in self.event_queues:
            return await self.event_queues[account_id].get()
        return None

    def __getattr__(self, attr: str):
        async def method(*args, **kwargs) -> Any:
            self.id += 1
            request_id = self.id

            assert not (args and kwargs), "Mixing positional and keyword arguments"

            request = {
                "jsonrpc": "2.0",
                "method": attr,
                "params": kwargs or args,
                "id": self.id,
            }
            data = (json.dumps(request) + "\n").encode()
            self.process.stdin.write(data)  # noqa
            loop = asyncio.get_running_loop()
            fut = loop.create_future()
            self.request_events[request_id] = fut
            response = await fut
            if "error" in response:
                raise JsonRpcError(response["error"])
            if "result" in response:
                return response["result"]

        return method
