import asyncio
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from contextvars import ContextVar
from ipaddress import IPv4Address, IPv4Network
import itertools
import logging
from typing import TYPE_CHECKING, Any, Mapping, Protocol, TypeVar
from collections.abc import AsyncIterator
import aiodocker
import aiohttp
from yarl import URL

if TYPE_CHECKING:
    from aiodocker.containers import DockerContainer
    from aiodocker.networks import DockerNetwork

# ==== type stuff ====
T_covar = TypeVar('T_covar', covariant=True)
class AsyncContextManager(Protocol[T_covar]):
    async def __aenter__(self) -> T_covar: ...
    async def __aexit__(self, exc_type, exc_value, traceback) -> bool | None: ...

# ==== ====
logger = logging.getLogger(__name__)
# logging.basicConfig(level=logging.DEBUG)  # FIXME should probably move this to the test main file
logger.setLevel(logging.DEBUG)

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


# ==== internal constants ====

_ASG3TESTER_CONTAINER_NUM_LABEL = 'asg3tester.clientnum'

# ==== asnyc global setup/cleanup ====

whichloop_var: ContextVar[int] = ContextVar('whichloop')
docker_client_var: ContextVar[aiodocker.Docker] = ContextVar('docker_client')
kvs_image_var: ContextVar[Mapping[str, Any]] = ContextVar('kvs_image')
kvs_network_var: ContextVar['DockerNetwork'] = ContextVar('kvs_network')
http_session_var: ContextVar[aiohttp.ClientSession] = ContextVar('http_session')
nodecontainer_cache_var: ContextVar[dict[int, 'NodeContainer']] = ContextVar('nodecontainer_cache')

async def setup():
    whichloop_var.set(id(asyncio.get_event_loop()))
    http_session_var.set(http_session := aiohttp.ClientSession())
    docker_client_var.set(docker_client := aiodocker.Docker())
    kvs_image_var.set(kvs_image := await docker_client.images.inspect(KVS_IMAGE_NAME))
    kvs_network_var.set(kvs_network := await docker_client.networks.get(KVS_NETWORK_NAME))
    nodecontainer_cache_var.set(dict())

    for container in await docker_client.containers.list(all=True, filters={'network': [kvs_network.id]}):
        container_info = await container.show()
        clientnum = container_info['Config']['Labels'].get(_ASG3TESTER_CONTAINER_NUM_LABEL, None)
        if clientnum is None:
            if container_info['State']['Status']['Running']:
                logger.error(f'''a container that isn't managed by asg3tester is running on the network, will likely cause issues (has id: {container_info['Id']})''')
        else:
            # if the image has been rebuilt since that container was made then we can't reuse it
            if container_info['Image'] != kvs_image['Id']:
                await container.kill()
                await container.delete()
            else:
                await NodeContainer._register_container(num=int(clientnum), container=container)

async def cleanup():
    docker_client = docker_client_var.get(None)
    http_session = http_session_var.get(None)
    if docker_client is not None:
        await docker_client.close()
    if http_session is not None:
        await http_session.close()

# ==== ====

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

    def __init__(self) -> None:
        pass

    @classmethod
    async def _create(cls, num: int, container: 'DockerContainer | None' = None) -> 'NodeContainer':
        node = NodeContainer()
        node._num = num
        node.host_port = KVS_HOST_BASE_PORT + num
        node.subnet_ip = KVS_NETWORK_SUBNET[num]
        node.subnet_port = KVS_NETWORK_PORT
        node.base_url = URL.build(scheme='http', host=KVS_HOST_HOST, port=node.host_port)
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
        container: 'DockerContainer' = await docker_client.containers.create(
            config={
                'Env': [f'ADDRESS={self.subnet_ip}:{self.subnet_port}'],
                'Image': kvs_image['Id'],
                'ExposedPorts': { f'{self.subnet_port}/tcp': {} },
                'AttachStdout': False, 'AttachStderr': False,
                'HostConfig': {
                    'NetworkMode': KVS_NETWORK_NAME,
                    'PortBindings': { f'{self.subnet_port}/tcp': [ { 'HostPort': f'{self.host_port}' } ] },
                },
                'Labels': {
                    _ASG3TESTER_CONTAINER_NUM_LABEL: f'{self._num}',
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

    def _start(self):
        if self._started:
            logger.warning('Node._start() called on node that was already marked as started')
        self._started = True
        return self.__wait_for_start()

    async def __wait_for_start(self):
        await self.container.start()
        num_tries = 0
        async with aiohttp.ClientSession() as session:
            while True:
                try:
                    async with session.get(self.base_url / 'alivetest') as resp:
                        try:
                            body = await resp.json()
                        except ValueError:
                            pass
                        else:
                            match body:
                                case {'alive': True}:
                                    logger.debug(f'container started after {num_tries} failed checks')
                                    return
                except (aiohttp.ClientConnectionError):
                    pass
                num_tries += 1

    def _stop(self):
        if not self._started:
            logger.warning("Node._stop() called on node that wasn't marked as started")
        self._started = False
        return self.__stop()

    async def __stop(self):
        if KVS_GRACEFUL_STOP:
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


class NodeApi:
    container: NodeContainer
    endpoint_view: URL
    endpoint_data_all: URL

    def endpoint_data_single(self, key: str) -> URL:
        return self.container.base_url/'kvs'/'data'/key

    def __init__(self, container: NodeContainer) -> None:
        self.container = container
        self.endpoint_view = self.container.base_url/'kvs'/'admin'/'view'
        self.endpoint_data_all = self.container.base_url/'kvs'/'data'

    async def _request(self, session: aiohttp.ClientSession, method: str, url: URL, **kwargs):
        async with session.request(url=url, method=method, **kwargs) as response:
            return response

    async def view_put(self, session: aiohttp.ClientSession, view: list[str]):
        return await self._request(session, 'PUT', self.endpoint_view, json={'view': view})
    async def view_get(self, session: aiohttp.ClientSession):
        return await self._request(session,'GET', self.endpoint_view)
    async def view_delete(self, session: aiohttp.ClientSession):
        return await self._request(session,'DELETE', self.endpoint_view)
    async def data_single_put(self, session: aiohttp.ClientSession, key: str, val: str, causal_metadata: Any = {}):
        return await self._request(session,'PUT', self.endpoint_data_single(key), json={'val': val, 'causal-metadata': causal_metadata})
    async def data_single_get(self, session: aiohttp.ClientSession, key: str, causal_metadata: Any = {}):
        return await self._request(session,'GET', self.endpoint_data_single(key), json={'causal-metadata': causal_metadata})
    async def data_single_delete(self, session: aiohttp.ClientSession, key: str, causal_metadata: Any = {}):
        return await self._request(session,'DELETE', self.endpoint_data_single(key), json={'causal-metadata': causal_metadata})
    async def data_all_get(self, session: aiohttp.ClientSession, causal_metadata: Any = {}):
        return await self._request(session,'GET', self.endpoint_data_all, json={'causal-metadata': causal_metadata})


class _ClientApiResponseContextManager:
    __client: 'ClientApi'
    __foo: 'aiohttp.client._RequestContextManager'

    def __init__(self, client: 'ClientApi', foo: 'aiohttp.client._RequestContextManager') -> None:
        self.__client = client
        self.__foo = foo

    async def __aenter__(self):
        response = await self.__foo.__aenter__()
        await self.__client._update_causal_metadata_from(response)
        return response

    async def __aexit__(self, exc_type, exc, tb):
        await self.__foo.__aexit__(exc_type, exc, tb)

    async def __await_impl(self):
        response = await self.__foo
        await self.__client._update_causal_metadata_from(response)
        return response

    def __await__(self):
        return self.__await_impl().__await__()

class ClientApi:
    __last_seen_causal_metadata: Any

    def __init__(self) -> None:
        self.__last_seen_causal_metadata = {}

    async def _update_causal_metadata_from(self, response: aiohttp.ClientResponse):
        try:
            body = await response.json()
        except (ValueError, aiohttp.ContentTypeError):
            pass
        else:
            if isinstance(body, dict) and ('causal-metadata' in body):
                self.__last_seen_causal_metadata = body['causal-metadata']

    def _request(self, method: str, url: URL, **kwargs):
        http_session = http_session_var.get()
        return _ClientApiResponseContextManager(self, http_session.request(url=url, method=method, **kwargs))

    def view_put(self, node: NodeApi, view: list[str]):
       return self._request('PUT', node.endpoint_view, json={'view': view})
    def view_get(self, node: NodeApi):
       return self._request('GET', node.endpoint_view)
    def view_delete(self, node: NodeApi):
       return self._request('DELETE', node.endpoint_view)
    def data_single_put(self, node: NodeApi, key: str, val: str):
       return self._request('PUT', node.endpoint_data_single(key), json={'val': val, 'causal-metadata': self.__last_seen_causal_metadata})
    def data_single_get(self, node: NodeApi, key: str):
       return self._request('GET', node.endpoint_data_single(key), json={'causal-metadata': self.__last_seen_causal_metadata})
    def data_single_delete(self, node: NodeApi, key: str):
       return self._request('DELETE', node.endpoint_data_single(key), json={'causal-metadata': self.__last_seen_causal_metadata})
    def data_all_get(self, node: NodeApi):
       return self._request('GET', node.endpoint_data_all, json={'causal-metadata': self.__last_seen_causal_metadata})

    async def setup_view(self, *nodes: NodeApi):
        """ make a view containing the provided nodes (in the provided order), and send that view to all of those nodes """
        view = [node.container.address for node in nodes]
        # TODO can (should?) probably do this parallel
        for node in nodes:
            await self.view_put(node, view)

def start_node() -> AbstractAsyncContextManager[NodeApi]:
    return NodeContainer.start_one()

@asynccontextmanager
async def start_client() -> AsyncIterator[ClientApi]:
    try:
        yield ClientApi()
    finally:
        pass



