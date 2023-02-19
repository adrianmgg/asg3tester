import asyncio
import logging
import unittest

import asg3tester
from asg3tester import start_node, start_client


class KVSTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        await asg3tester.setup()

    async def asyncTearDown(self) -> None:
        await asg3tester.cleanup()

    async def test_view_put(self):
        async with start_node() as a, start_client() as client:
            # await client.view_put(a, [a.container.address])
            async with client.view_put(a, [a.container.address]) as r:
                self.assertEqual(r.status, 200)
                self.assertEqual(await r.read(), b'')

    async def test_view_get(self):
        async with start_client() as client, start_node() as a, start_node() as b, start_node() as c:
            for view_nodes in [
                [a, b],
                [a, c],
                [a],
                [a, b, c],
                [a, c, b],
            ]:
                view = [node.container.address for node in view_nodes]
                with self.subTest(view=view):
                    await client.view_put(a, view)
                    r = await client.view_get(a)
                    self.assertEqual(r.status, 200)
                    self.assertListEqual(await r.json(), view)

    async def test_view_uninitialized(self):
        async with start_client() as client, start_node() as a:
            for testname, funcname, funcargs in [
                ('view GET', 'view_get', []),
                ('data single PUT', 'data_single_put', ['k', 'v']),
                ('data single DELETE', 'data_single_delete', ['k']),
                ('data single GET', 'data_single_get', ['k']),
                ('data all GET', 'data_all_get', []),
            ]:
                with self.subTest(testname):
                    async with getattr(client, funcname)(a, *funcargs) as r:
                        self.assertEqual(r.status, 418)
                        self.assertDictEqual(r.json(), {'error': 'uninitialized'})


    async def test_001(self):
        async with start_client() as client, start_node() as a, start_node() as b:
            # TODO setup view
            await client.data_single_put(a, 'k', 'v')
            r = await client.data_single_get(b, 'k')
            self.assertEqual((await r.json())['val'], 'v')
            await client.data_single_put(a, 'k', 'v2')
            r = await client.data_single_get(b, 'k')
            self.assertEqual((await r.json())['val'], 'v')

    async def test_002(self):
        async with start_node() as a, start_node() as b, start_client() as client:
            await client.setup_view(a, b)
            await client.data_single_put(a, 'foo', 'bar')
            r = await client.data_single_get(b, 'foo')
            self.assertEqual((await r.json())['val'], 'bar')

    async def test_003(self):
        async with start_node() as alice, start_node() as bob, start_node() as carol, start_client() as client:
            for node in alice, bob, carol:
                r = await client.data_all_get(node)
                self.assertEqual(r.status, 418)
                self.assertDictEqual(await r.json(), {'error': 'uninitialized'})
            await client.setup_view(alice, bob, carol)
            await client.data_single_put(alice, 'k1', 'v1')
            await client.data_single_put(bob, 'k2', 'v2')
            await client.data_single_put(carol, 'k3', 'v3')
            for node in alice, bob, carol:
                r = await client.data_all_get(node)
                self.assertSetEqual(set((await r.json())['keys']), {'k1', 'k2', 'k3'})

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    unittest.main()
    # suite = unittest.TestSuite()
    # loader = unittest.TestLoader()
    # suite.addTest(loader.loadTestsFromTestCase(KVSTest))
    # runner = unittest.TextTestRunner()
    # runner.run(suite)
