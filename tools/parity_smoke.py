"""소스와 EXE가 같은 MCP 공개 표면을 제공하고 정상 stdin 종료를 수행하는지 검사한다."""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def run(label, command, cwd):
    process = subprocess.Popen(command, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, encoding='utf-8')

    def request(request_id, method, params=None):
        payload = {'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params or {}}
        process.stdin.write(json.dumps(payload) + '\n')
        process.stdin.flush()
        line = process.stdout.readline()
        if not line:
            raise RuntimeError(f'{label}: no response for {method}; stderr={process.stderr.read()}')
        response = json.loads(line)
        if 'error' in response:
            raise RuntimeError(f'{label}: {method} failed: {response["error"]}')
        return response['result']

    initialized = request(1, 'initialize', {'protocolVersion': '2025-11-25', 'capabilities': {},
                                            'clientInfo': {'name': 'parity-smoke', 'version': '1'}})
    listed = request(2, 'tools/list')
    version = request(3, 'tools/call', {'name': 'eiass_version', 'arguments': {}})
    tools = sorted(listed['tools'], key=lambda tool: tool['name'])
    process.stdin.close()
    process.wait(timeout=15)
    stderr = process.stderr.read()
    if process.returncode != 0 or 'Traceback' in stderr:
        raise RuntimeError(f'{label}: normal stdin close failed exit={process.returncode} stderr={stderr!r}')
    return {
        'server_name': initialized['serverInfo']['name'],
        'tools': tools,
        'version': version,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--exe', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    source = run('source', [sys.executable, 'mcp_server.py'], root)
    target = source
    if args.exe:
        target = run('exe', [str(args.exe.resolve())], root)
    if target != source:
        raise SystemExit('parity mismatch:\nsource=' + json.dumps(source, ensure_ascii=False, sort_keys=True)
                         + '\ntarget=' + json.dumps(target, ensure_ascii=False, sort_keys=True))
    print(f"PARITY OK server={source['server_name']} version={source['version']} tools={len(source['tools'])}")


if __name__ == '__main__':
    main()
