
# prerequisites
you need to add a new endpoint `GET /asg3tester/alivetest` that responds with `{"alive": true}`,
it'll be used by this code to find out when the server has actually fully started.

# setup
requires python >= 3.11. install via:
```
pip install git+https://github.com/adrianmgg/asg3tester.git
```

# usage / sample code
within an event loop, call `asg3tester.setup()` before any of the other asg3tester functions, and
call `asg3tester.cleanup()` before leaving the loop. you can pass some configuration stuff to
`setup`, but if you've followed the same container/network naming/options as in the spec then the
defaults should work fine.

i'll probably write out docs for the rest it eventually but for now you can just look at `NodeApi`
and `ClientApi` in [`asg3tester/__init__.pyi`](asg3tester/__init__.pyi) for a list of the functions

```python
import unittest
import asg3tester
from asg3tester import start_client, start_node

class TestAssignment3(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        await asg3tester.setup()

    async def asyncTearDown(self) -> None:
        await asg3tester.cleanup()

    async def test_get_2nodes(self):
        async with start_node() as a, start_node() as b, start_client() as client:
            await a.view_put(view=[a.address, b.address])
            await client.data_single_put(a, key='k', val='v')
            response = await client.data_single_get(b, key='k')
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())['val'], 'v')

    async def test_view_get(self):
        async with start_node() as a, start_node() as b:
            view = [a.container.address, b.container.address]
            await a.view_put(view=view)
            response = await a.view_get()
            self.assertEqual(response.status, 200)
            self.assertListEqual((await response.json())['view'], view)

    async def test_partition(self):
        async with start_node() as a, start_node() as b, start_client() as client:
            await a.view_put(view=[a.address, b.address])
            # (requires iptables to be installed in container, currently just fails silently if it isn't)
            await a.create_partition(b)
            ...  # do some stuff
            await a.heal_partition(b)
            ...  # do some more stuff

if __name__ == '__main__':
    unittest.main()
```

