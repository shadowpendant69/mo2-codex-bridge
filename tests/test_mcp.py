import json
import subprocess
import sys
import unittest
from pathlib import Path


class MCPTests(unittest.TestCase):
    def test_real_stdio_handshake_and_tool_schema(self):
        server = Path(__file__).resolve().parents[1] / 'mcp_server.py'
        messages = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18'}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'mo2_change', 'arguments': {'action': 'install_archive', 'params': {}}}},
            {'jsonrpc': '2.0', 'id': 4, 'method': 'ping'}]
        result = subprocess.run([sys.executable, str(server)], input='\n'.join(json.dumps(x) for x in messages) + '\n', capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(replies), 4)
        self.assertEqual(replies[0]['result']['protocolVersion'], '2025-06-18')
        self.assertEqual({x['name'] for x in replies[1]['result']['tools']}, {'mo2_inspect', 'mo2_change', 'mo2_job'})
        self.assertTrue(replies[2]['result']['isError'])
        self.assertIn('snapshot', replies[2]['result']['content'][0]['text'])
        self.assertEqual(replies[3]['result'], {})


if __name__ == '__main__':
    unittest.main()
