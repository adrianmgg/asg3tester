import unittest
from asg3tester import start_node, start_client, setup_view


class KVSTest(unittest.IsolatedAsyncioTestCase):
    async def test_view_put(self):
        async with start_node() as a:
            r = await a.view_put([a.container.address])
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.content, b'')

    async def test_view_get(self):
        async with start_node() as a, start_node() as b, start_node() as c:
            for view_nodes in [
                [a, b],
                [a, c],
                [a],
                [a, b, c],
                [a, c, b],
            ]:
                view = [node.container.address for node in view_nodes]
                with self.subTest(view=view):
                    await a.view_put(view)
                    r = await a.view_get()
                    self.assertEqual(r.status_code, 200)
                    self.assertListEqual(r.json(), view)

    async def test_view_uninitialized(self):
        async with start_node() as a:
            for testname, funcname, funcargs in [
                ('view GET', 'view_get', []),
                ('data single PUT', 'data_single_put', ['k', 'v']),
                ('data single DELETE', 'data_single_delete', ['k']),
                ('data single GET', 'data_single_get', ['k']),
                ('data all GET', 'data_all_get', []),
            ]:
                with self.subTest(testname):
                    r = await getattr(a, funcname)(*funcargs)
                    self.assertEqual(r.status_code, 418)
                    self.assertDictEqual(r.json(), {'error': 'uninitialized'})

    async def test_001(self):
        async with start_node() as a, start_node() as b:
            # TODO setup view
            await a.data_single_put('k', 'v')
            r = await b.data_single_get('k')
            self.assertEqual(r.json()['val'], 'v')
            await a.data_single_put('k', 'v2')
            r = await b.data_single_get('k')
            self.assertEqual(r.json()['val'], 'v')

    async def test_002(self):
        async with start_node() as a, start_node() as b, start_client() as client:
            await setup_view(a, b)
            await client.data_single_put(a, 'foo', 'bar')
            r = await client.data_single_get(b, 'foo')
            self.assertEqual(r.json()['val'], 'bar')

if __name__ == '__main__':
    unittest.main()
    # suite = unittest.TestSuite()
    # loader = unittest.TestLoader()
    # suite.addTest(loader.loadTestsFromTestCase(KVSTest))
    # runner = unittest.TextTestRunner()
    # runner.run(suite)
