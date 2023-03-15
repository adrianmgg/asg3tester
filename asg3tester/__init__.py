__all__ = ['setup', 'cleanup', 'start_node', 'start_client']

import asyncio
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network
import itertools
import logging
from types import TracebackType
from typing import TYPE_CHECKING, Any, Awaitable, Coroutine, Generator, Mapping
from collections.abc import AsyncIterator
import aiodocker
from aiodocker.execs import Exec
import aiohttp
from yarl import URL

if TYPE_CHECKING:
    from aiodocker.containers import DockerContainer
    from aiodocker.networks import DockerNetwork


logger = logging.getLogger(__name__)


async def _noop() -> None:
    pass


@dataclass(frozen=True, kw_only=True, match_args=False)
class Config:
    image_name: str = 'kvs:2.0'
    """name of the docker image to use"""
    network_name: str = 'kv_subnet'
    """name of the docker network to use"""
    network_subnet: IPv4Network = IPv4Network('10.10.0.0/16')
    """subnet of the docker network"""
    network_subnet_skipfirst: int = 2
    """don't use the first n ips of the subnet"""
    base_host_port: int = 13800
    """
    created containers' mapped ports will be assigned starting from this number and counting up
    (e.g. for a value of 123, the first container created will map to 123, the second to 124, etc.)
    """
    localhost: str = 'localhost'
    graceful_stop_containers: bool = False
    """True = stop containers with stop, False = stop containers with kill"""
    alivetest_retry_delay: float = 0.1
    """how many seconds to wait between pings while waiting for a container to start"""

    def _str_for_container_compare(self) -> str:
        return f'image:{self.network_name!r} network:{self.network_name!r} subnet:{self.network_subnet} skip {self.network_subnet_skipfirst} base port:{self.base_host_port} localhost:{self.localhost}'


KVS_NETWORK_PORT = 8080
CONTAINER_LABEL_CLIENTNUM = 'asg3tester.clientnum'
CONTAINER_LABEL_COMPARESTR = 'asg3tester.comparestr'


config_var: ContextVar[Config] = ContextVar('config')
docker_client_var: ContextVar[aiodocker.Docker] = ContextVar('docker_client')
kvs_image_var: ContextVar[Mapping[str, Any]] = ContextVar('kvs_image')
kvs_network_var: ContextVar['DockerNetwork'] = ContextVar('kvs_network')
http_session_var: ContextVar[aiohttp.ClientSession] = ContextVar('http_session')
nodecontainer_cache_var: ContextVar[dict[int, 'NodeContainer']] = ContextVar('nodecontainer_cache')

async def setup(config: Config | None = None) -> None:
    if config is None:
        config = Config()

    config_var.set(config)
    http_session_var.set(aiohttp.ClientSession())
    docker_client_var.set(docker_client := aiodocker.Docker())
    kvs_image_var.set(kvs_image := await docker_client.images.inspect(config.image_name))
    kvs_network_var.set(kvs_network := await docker_client.networks.get(config.network_name))
    nodecontainer_cache_var.set(dict())

    for container in await docker_client.containers.list(all=True, filters={'network': [kvs_network.id]}):
        container_info = await container.show()
        clientnum = container_info['Config']['Labels'].get(CONTAINER_LABEL_CLIENTNUM, None)
        comparestr = container_info['Config']['Labels'].get(CONTAINER_LABEL_COMPARESTR, None)
        if clientnum is None:
            if container_info['State']['Status'] == 'running':
                logger.error(f'''a container that isn't managed by asg3tester is running on the network, will likely cause issues (has id: {container_info['Id']})''')
        else:
            if (
                # if the image has been rebuilt since that container was made then we can't reuse it
                (container_info['Image'] != kvs_image['Id'])
                # if the config has changed since the container was made then we can't reuse it
                or (comparestr != config._str_for_container_compare())
            ):
                logger.info(f'deleting outdated asg3tester managed container {container.id!r}')
                # await container.kill()
                await container.delete(force=True)
            else:
                logger.debug(f'reusing container #{clientnum} ({container.id!r})')
                if container_info['State']['Status'] in {'running'}:
                    await container.kill()
                await NodeContainer._register_container(num=int(clientnum), container=container)

async def cleanup() -> None:
    docker_client = docker_client_var.get(None)
    http_session = http_session_var.get(None)
    if docker_client is not None:
        await docker_client.close()
    if http_session is not None:
        await http_session.close()


class NodeContainer:
    _num: int
    host_port: int
    subnet_ip: IPv4Address
    subnet_port: int
    container: 'DockerContainer'
    base_url: URL
    _started: bool  # TODO should maybe use a state enum instead of just a bool -- do we need to distinguish between 'waiting for start' and 'started'

    @property
    def address(self) -> str:
        return f'{self.subnet_ip}:{self.subnet_port}'

    @classmethod
    async def _create(cls, num: int, container: 'DockerContainer | None' = None) -> 'NodeContainer':
        config = config_var.get()
        node = NodeContainer()
        node._num = num
        node.host_port = config.base_host_port + num
        node.subnet_ip = config.network_subnet[num + config.network_subnet_skipfirst]
        node.subnet_port = KVS_NETWORK_PORT
        node.base_url = URL.build(scheme='http', host=config.localhost, port=node.host_port)
        node._started = False
        if container is not None:
            node.container = container
        else:
            node.container = await node._make_container_for()
        return node

    async def _make_container_for(self) -> 'DockerContainer':
        docker_client = docker_client_var.get()
        kvs_image = kvs_image_var.get()
        kvs_network = kvs_network_var.get()
        config = config_var.get()
        container: 'DockerContainer' = await docker_client.containers.create(
            # name=f'asg3tester-{self._num}',
            config={
                'Env': [f'ADDRESS={self.subnet_ip}:{self.subnet_port}'],
                'Image': kvs_image['Id'],
                'ExposedPorts': { f'{self.subnet_port}/tcp': {} },
                'AttachStdout': False, 'AttachStderr': False,
                'HostConfig': {
                    'NetworkMode': config.network_name,
                    'PortBindings': { f'{self.subnet_port}/tcp': [ { 'HostPort': f'{self.host_port}' } ] },
                },
                'Labels': {
                    CONTAINER_LABEL_CLIENTNUM: f'{self._num}',
                    CONTAINER_LABEL_COMPARESTR: config._str_for_container_compare(),
                },
            }
        )
        await kvs_network.connect(config={
            'Container': container.id,
            'EndpointConfig': {
                'IPAddress': f'{self.subnet_ip}',
            },
        })
        return container

    @classmethod
    async def _get_or_create(cls, num: int) -> 'NodeContainer':
        nodes = nodecontainer_cache_var.get()
        match nodes.get(num):
            case None:
                node = nodes[num] = await NodeContainer._create(num=num, container=None)
                return node
            case node:
                return node

    @classmethod
    async def _register_container(cls, num: int, container: 'DockerContainer') -> None:
        nodes = nodecontainer_cache_var.get()
        assert num not in nodes
        nodes[num] = await NodeContainer._create(num=num, container=container)

    def _start(self) -> Coroutine[Any, Any, None]:
        if self._started:
            logger.warning('Node._start() called on node that was already marked as started')
        self._started = True
        return self.__wait_for_start()

    async def __wait_for_start(self) -> None:
        retry_delay = config_var.get().alivetest_retry_delay
        http_session = http_session_var.get()
        await self.container.start()
        num_tries = 0
        while True:
            try:
                async with http_session.get(self.base_url/'asg3tester'/'alivetest') as resp:
                    try:
                        body = await resp.json()
                    except ValueError:
                        pass
                    else:
                        match body:
                            case {'alive': True}:
                                if num_tries > 0:
                                    logger.debug(f'container started after {num_tries} failed checks')
                                return
            except (aiohttp.ClientConnectionError):
                pass
            await asyncio.sleep(retry_delay)
            num_tries += 1

    def _stop(self) -> Coroutine[Any, Any, None]:
        if not self._started:
            # logger.warning("Node._stop() called on node that wasn't marked as started")
            return _noop()
        self._started = False
        return self.__stop()

    async def __stop(self) -> None:
        config = config_var.get()
        if config.graceful_stop_containers:
            await self.container.stop()
        else:
            await self.container.kill()

    @classmethod
    @asynccontextmanager
    async def start_one(cls) -> AsyncIterator['NodeApi']:
        node = None
        try:
            for n in itertools.count(start=0):
                node = await cls._get_or_create(n)
                if not node._started:
                    await node._start()
                    yield NodeApi(node)
                    break
        finally:
            if node is not None:
                await node._stop()

    async def _exec(self, *cmd: str, privileged: bool = False) -> Exec:
        execute: Exec = await self.container.exec(cmd=cmd, privileged=privileged)
        async with execute.start(detach=False) as stream:
            out = await stream.read_out()
        return execute



class NodeApi:
    container: NodeContainer
    _endpoint_view: URL
    _endpoint_data_all: URL

    def _endpoint_data_single(self, key: str) -> URL:
        return self.container.base_url/'kvs'/'data'/key

    @property
    def address(self) -> str:
        return self.container.address

    def __init__(self, container: NodeContainer) -> None:
        self.container = container
        self._endpoint_view = self.container.base_url/'kvs'/'admin'/'view'
        self._endpoint_data_all = self.container.base_url/'kvs'/'data'

    def _request(self, method: str, url: URL, *, json: Any | None = None) -> 'aiohttp.client._RequestContextManager':
        return http_session_var.get().request(url=url, method=method, json=json)

    def view_put(self, view: list[str]) -> 'aiohttp.client._RequestContextManager':
        return self._request('PUT', self._endpoint_view, json={'view': view})
    def view_get(self) -> 'aiohttp.client._RequestContextManager':
        return self._request('GET', self._endpoint_view)
    def view_delete(self) -> 'aiohttp.client._RequestContextManager':
        return self._request('DELETE', self._endpoint_view)
    def data_single_put(self, key: str, val: str, causal_metadata: Any) -> 'aiohttp.client._RequestContextManager':
        return self._request('PUT', self._endpoint_data_single(key), json={'val': val, 'causal-metadata': causal_metadata})
    def data_single_get(self, key: str, causal_metadata: Any) -> 'aiohttp.client._RequestContextManager':
        return self._request('GET', self._endpoint_data_single(key), json={'causal-metadata': causal_metadata})
    def data_single_delete(self, key: str, causal_metadata: Any) -> 'aiohttp.client._RequestContextManager':
        return self._request('DELETE', self._endpoint_data_single(key), json={'causal-metadata': causal_metadata})
    def data_all_get(self, causal_metadata: Any) -> 'aiohttp.client._RequestContextManager':
        return self._request('GET', self._endpoint_data_all, json={'causal-metadata': causal_metadata})

    async def kill(self) -> None:
        await self.container._stop()

    # TODO give those params actual names
    def _iptables_exec(self, other: 'NodeApi', foo: str, bar: str) -> Awaitable[Exec]:
        return self.container._exec('iptables', foo, bar, '--source', str(other.container.subnet_ip), '--jump', 'DROP', privileged=True)

    async def create_partition(self, other: 'NodeApi') -> None:
        # TODO check to make sure commands actually ran successfully
        await self._iptables_exec(other, '--append', 'INPUT')
        await self._iptables_exec(other, '--append', 'OUTPUT')
        await other._iptables_exec(self, '--append', 'INPUT')
        await other._iptables_exec(self, '--append', 'OUTPUT')

    async def heal_partition(self, other: 'NodeApi') -> None:
        # TODO check to make sure commands actually ran successfully
        await self._iptables_exec(other, '--delete', 'INPUT')
        await self._iptables_exec(other, '--delete', 'OUTPUT')
        await other._iptables_exec(self, '--delete', 'INPUT')
        await other._iptables_exec(self, '--delete', 'OUTPUT')

class _ClientApiResponseContextManager:
    __client: 'ClientApi'
    __request_ctx: 'aiohttp.client._RequestContextManager'

    def __init__(self, client: 'ClientApi', request_ctx: 'aiohttp.client._RequestContextManager') -> None:
        self.__client = client
        self.__request_ctx = request_ctx

    async def __aenter__(self) -> aiohttp.ClientResponse:
        response = await self.__request_ctx.__aenter__()
        await self.__client._update_causal_metadata_from(response)
        return response

    async def __aexit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None) -> None:
        await self.__request_ctx.__aexit__(exc_type, exc, tb)

    async def __await_impl(self) -> aiohttp.ClientResponse:
        response = await self.__request_ctx
        await self.__client._update_causal_metadata_from(response)
        return response

    def __await__(self) -> Generator[Any, None, aiohttp.ClientResponse]:
        return self.__await_impl().__await__()

class ClientApi:
    __last_seen_causal_metadata: Any

    def __init__(self) -> None:
        self.__last_seen_causal_metadata = {}

    async def _update_causal_metadata_from(self, response: aiohttp.ClientResponse) -> None:
        try:
            body = await response.json()
        except (ValueError, aiohttp.ContentTypeError):
            pass
        else:
            if isinstance(body, dict) and ('causal-metadata' in body):
                self.__last_seen_causal_metadata = body['causal-metadata']

    # def view_put(self, node: NodeApi, view: list[str]) -> 'aiohttp.client._RequestContextManager':
    #     return node.view_put(view=view)
    # def view_get(self, node: NodeApi) -> 'aiohttp.client._RequestContextManager':
    #    return node.view_get()
    # def view_delete(self, node: NodeApi) -> 'aiohttp.client._RequestContextManager':
    #    return node.view_delete()
    def data_single_put(self, node: NodeApi, key: str, val: str) -> _ClientApiResponseContextManager:
       return _ClientApiResponseContextManager(self, node.data_single_put(key=key, val=val, causal_metadata=self.__last_seen_causal_metadata))
    def data_single_get(self, node: NodeApi, key: str) -> _ClientApiResponseContextManager:
       return _ClientApiResponseContextManager(self, node.data_single_get(key=key, causal_metadata=self.__last_seen_causal_metadata))
    def data_single_delete(self, node: NodeApi, key: str) -> _ClientApiResponseContextManager:
       return _ClientApiResponseContextManager(self, node.data_single_delete(key=key, causal_metadata=self.__last_seen_causal_metadata))
    def data_all_get(self, node: NodeApi) -> _ClientApiResponseContextManager:
       return _ClientApiResponseContextManager(self, node.data_all_get(causal_metadata=self.__last_seen_causal_metadata))

def start_node() -> AbstractAsyncContextManager[NodeApi]:
    return NodeContainer.start_one()

@asynccontextmanager
async def start_client() -> AsyncIterator[ClientApi]:
    try:
        yield ClientApi()
    finally:
        pass
