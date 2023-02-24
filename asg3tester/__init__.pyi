__all__ = ['setup', 'cleanup', 'start_node', 'start_client']

import aiohttp
from aiodocker.containers import DockerContainer
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from ipaddress import IPv4Address, IPv4Network
from types import TracebackType
from typing import Any, Generator, Union
from yarl import URL

class Config:
    image_name: str
    network_name: str
    network_subnet: IPv4Network
    network_subnet_skipfirst: int
    base_host_port: int
    localhost: str
    graceful_stop_containers: bool
    alivetest_retry_delay: float
    def __init__(
        self,
        *,
        image_name: str = 'kvs:2.0',
        network_name: str = 'kv_subnet',
        network_subnet: IPv4Network = IPv4Network('10.10.0.0/16'),
        network_subnet_skipfirst: int = 2,
        base_host_port: int = 13800,
        localhost: str = 'localhost',
        graceful_stop_containers: bool = False,
        alivetest_retry_delay: float = 0.1
    ) -> None: ...

async def setup(config: Union[Config, None] = None) -> None: ...
async def cleanup() -> None: ...

class NodeContainer:
    host_port: int
    subnet_ip: IPv4Address
    subnet_port: int
    container: DockerContainer
    base_url: URL
    @property
    def address(self) -> str: ...
    @classmethod
    async def start_one(cls) -> AsyncIterator['NodeApi']: ...

class NodeApi:
    container: NodeContainer
    def __init__(self, container: NodeContainer) -> None: ...
    @property
    def address(self) -> str: ...
    def view_put(self, view: list[str]) -> aiohttp.client._RequestContextManager: ...
    def view_get(self) -> aiohttp.client._RequestContextManager: ...
    def view_delete(self) -> aiohttp.client._RequestContextManager: ...
    def data_single_put(self, key: str, val: str, causal_metadata: Any) -> aiohttp.client._RequestContextManager: ...
    def data_single_get(self, key: str, causal_metadata: Any) -> aiohttp.client._RequestContextManager: ...
    def data_single_delete(self, key: str, causal_metadata: Any) -> aiohttp.client._RequestContextManager: ...
    def data_all_get(self, causal_metadata: Any) -> aiohttp.client._RequestContextManager: ...
    async def kill(self) -> None: ...

class _ClientApiResponseContextManager:
    def __init__(self, client: ClientApi, request_ctx: aiohttp.client._RequestContextManager) -> None: ...
    async def __aenter__(self) -> aiohttp.ClientResponse: ...
    async def __aexit__(self, exc_type: Union[type[BaseException], None], exc: Union[BaseException, None], tb: Union[TracebackType, None]) -> None: ...
    def __await__(self) -> Generator[Any, None, aiohttp.ClientResponse]: ...

class ClientApi:
    def __init__(self) -> None: ...
    def data_single_put(self, node: NodeApi, key: str, val: str) -> _ClientApiResponseContextManager: ...
    def data_single_get(self, node: NodeApi, key: str) -> _ClientApiResponseContextManager: ...
    def data_single_delete(self, node: NodeApi, key: str) -> _ClientApiResponseContextManager: ...
    def data_all_get(self, node: NodeApi) -> _ClientApiResponseContextManager: ...

def start_node() -> AbstractAsyncContextManager[NodeApi]: ...
def start_client() -> AbstractAsyncContextManager[ClientApi]: ...
