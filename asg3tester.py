import asyncio
from contextlib import asynccontextmanager
from ipaddress import IPv4Address, IPv4Network
import itertools
import logging
import socket
from typing import TYPE_CHECKING, Any, ClassVar, cast
from collections.abc import AsyncIterator, Coroutine
import urllib.parse

import httpx
from httpx import URL
import docker
if TYPE_CHECKING:
    import docker.models.containers, docker.models.images, docker.models.networks

# ==== ====
logger = logging.getLogger()
logging.basicConfig(level=logging.INFO)


# ==== config ====

KVS_IMAGE_NAME = 'kvs:2.0'
KVS_NETWORK_NAME = 'kv_subnet'
KVS_NETWORK_SUBNET = IPv4Network('10.10.0.0/16')
KVS_HOST_BASE_PORT = 13800
KVS_HOST_HOST = 'localhost'
# True = stop containers with stop, False = stop containers with kill
KVS_GRACEFUL_STOP = False

ALIVETEST_RETRY_DELAY = 0.1

# ==== constants ====

KVS_NETWORK_PORT = 8080


# ==== docker setup ====

docker_client = docker.from_env()

kvs_image: 'docker.models.images.Image' = docker_client.images.get(KVS_IMAGE_NAME)  # pyright: ignore[reportGeneralTypeIssues]
kvs_network: 'docker.models.networks.Network' = docker_client.networks.get(KVS_NETWORK_NAME)  # pyright: ignore[reportGeneralTypeIssues]

# remove any running containers still on the network
# for container in kvs_network.containers:
for container in cast('list[docker.models.containers.Container]', docker_client.containers.list(all=True, filters={'network': kvs_network.id})):
    # TODO: if the kvs_image == <container's image>, we can just reuse it
    logger.info('removing existing container on network...')
    if container.status not in {'created', 'exited'}:
        if KVS_GRACEFUL_STOP:
            container.stop()
        else:
            container.kill()
    container.remove()
kvs_network.reload()

# ==== httpx setup ====
# TODO: should probably refactor this to actually use the context manager stuff rather than having a global one
httpx_async_client = httpx.AsyncClient()


# ==== ====

class NodeContainer:
    __nodes: ClassVar[dict[int, 'NodeContainer']] = dict()

    host_port: int
    subnet_ip: IPv4Address
    subnet_port: int
    __started: bool
    __container: 'docker.models.containers.Container'
    base_url: URL

    @property
    def address(self) -> str:
        return f'{self.subnet_ip}:{self.subnet_port}'

    def __init__(self, num: int) -> None:
        self.host_port = KVS_HOST_BASE_PORT + num
        self.subnet_ip = KVS_NETWORK_SUBNET[num]
        self.subnet_port = KVS_NETWORK_PORT
        self.base_url = URL(scheme='http', host=KVS_HOST_HOST, port=self.host_port)
        self.__started = False
        self.__container = docker_client.containers.create(  # pyright: ignore[reportGeneralTypeIssues]
            image=kvs_image,
            network=kvs_network.name,
            # name='kvs-replica1',
            ports={self.subnet_port: self.host_port},
            environment={'ADDRESS': f'{self.subnet_ip}:{self.subnet_port}'},
        )

    @classmethod
    def _get_or_create(cls, num: int) -> 'NodeContainer':
        match cls.__nodes.get(num):
            case None:
                node = NodeContainer(num)
                cls.__nodes[num] = node
                return node
            case node:
                return node

    def __start(self):
        if self.__started:
            logger.warning("Node.__stop() called on node that was already started")
        self.__container.start()
        self.__started = True
        return self.__wait_for_start()

    async def __wait_for_start(self):
        num_tries = 0
        while True:
            try:
                response = await httpx_async_client.get(self.base_url.copy_with(path='/alivetest'))
                try:
                    body = response.json()
                except ValueError:
                    pass
                else:
                    match body:
                        case {'alive': True}:
                            print(f'container started after {num_tries} checks')
                            return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(ALIVETEST_RETRY_DELAY)
            num_tries += 1
            # TODO sleep here for a bit?

    def __stop(self):
        if not self.__started:
            logger.warning("Node.__stop() called on node that wasn't started yet")
        if KVS_GRACEFUL_STOP:
            self.__container.stop()
        else:
            self.__container.kill()
        self.__started = False

    @classmethod
    @asynccontextmanager
    async def start_one(cls) -> AsyncIterator['NodeApi']:
        node_container = None
        try:
            # get the first node container that isn't currently started, creating new ones if necessary
            node_container = next(node for node in map(cls._get_or_create, itertools.count(start=0)) if not node.__started)
            await node_container.__start()
            yield NodeApi(node_container)
        finally:
            if node_container is not None:
                node_container.__stop()


class NodeApi:
    container: NodeContainer
    endpoint_view: URL
    endpoint_data_all: URL

    def endpoint_data_single(self, key: str) -> URL:
        return self.container.base_url.copy_with(path=urllib.parse.urljoin('/kvs/data/', urllib.parse.quote(key)))

    def __init__(self, container: NodeContainer) -> None:
        self.container = container
        self.endpoint_view = self.container.base_url.copy_with(path='/kvs/admin/view')
        self.endpoint_data_all = self.container.base_url.copy_with(path='/kvs/data')

    def view_put(self, view: list[str]) -> Coroutine[Any, Any, httpx.Response]:
        return httpx_async_client.request('PUT', self.endpoint_view, json={'view': view})
    def view_get(self) -> Coroutine[Any, Any, httpx.Response]:
        return httpx_async_client.request('GET', self.endpoint_view)
    def view_delete(self) -> Coroutine[Any, Any, httpx.Response]:
        return httpx_async_client.request('DELETE', self.endpoint_view)
    def data_single_put(self, key: str, val: str, causal_metadata: Any = {}) -> Coroutine[Any, Any, httpx.Response]:
        return httpx_async_client.request('PUT', self.endpoint_data_single(key), json={'val': val, 'causal-metadata': causal_metadata})
    def data_single_get(self, key: str, causal_metadata: Any = {}) -> Coroutine[Any, Any, httpx.Response]:
        return httpx_async_client.request('GET', self.endpoint_data_single(key), json={'causal-metadata': causal_metadata})
    def data_single_delete(self, key: str, causal_metadata: Any = {}) -> Coroutine[Any, Any, httpx.Response]:
        return httpx_async_client.request('DELETE', self.endpoint_data_single(key), json={'causal-metadata': causal_metadata})
    def data_all_get(self, causal_metadata: Any = {}) -> Coroutine[Any, Any, httpx.Response]:
        return httpx_async_client.request('GET', self.endpoint_data_all, json={'causal-metadata': causal_metadata})


class ClientApi:
    __last_seen_causal_metadata: Any

    def __init__(self) -> None:
        self.__last_seen_causal_metadata = {}

    def __and_update_metadata(self, response: httpx.Response) -> httpx.Response:
        body = response.json()
        if isinstance(body, dict) and ('causal-metadata' in body):
            self.__last_seen_causal_metadata = body['causal-metadata']
        return response

    async def data_single_put(self, node: NodeApi, key: str, val: str) -> httpx.Response:
        return self.__and_update_metadata(await node.data_single_put(key=key, val=val, causal_metadata=self.__last_seen_causal_metadata))
    async def data_single_get(self, node: NodeApi, key: str) -> httpx.Response:
        return self.__and_update_metadata(await node.data_single_get(key=key, causal_metadata=self.__last_seen_causal_metadata))
    async def data_single_delete(self, node: NodeApi, key: str) -> httpx.Response:
        return self.__and_update_metadata(await node.data_single_delete(key=key, causal_metadata=self.__last_seen_causal_metadata))
    async def data_all_get(self, node: NodeApi) -> httpx.Response:
        return self.__and_update_metadata(await node.data_all_get(causal_metadata=self.__last_seen_causal_metadata))


start_node = NodeContainer.start_one

@asynccontextmanager
async def start_client() -> AsyncIterator[ClientApi]:
    yield ClientApi()

async def setup_view(*nodes: NodeApi):
    view = [node.container.address for node in nodes]
    # TODO don't do one at a time
    for node in nodes:
        await node.view_put(view)


