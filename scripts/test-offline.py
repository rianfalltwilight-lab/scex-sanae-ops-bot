"""Run public regressions in isolated processes with external connections blocked."""
from pathlib import Path
import json
import os
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'build/offline-tests'
OUTPUT.mkdir(parents=True, exist_ok=True)
BOOTSTRAP = r'''
import json, os, socket, sys, tempfile, unittest
from pathlib import Path
repo, name = Path(sys.argv[1]), sys.argv[2]
sys.path[:0] = [str(repo), str(repo / 'tests')]
original_connect = socket.socket.connect
socket._sanae_test_endpoints = set()
def forbidden(*args, **kwargs):
    raise AssertionError('External connection forbidden in publication checks')
def isolated_connect(sock, address):
    if (isinstance(address, tuple) and len(address) == 2
            and address[0] == '127.0.0.1' and address in socket._sanae_test_endpoints):
        return original_connect(sock, address)
    return forbidden()
socket.socket.connect = isolated_connect
socket.socket.connect_ex = forbidden
with tempfile.TemporaryDirectory(prefix='sanae-public-test-', dir=repo / 'build/offline-tests') as tmp:
    # Keep new default_path() state, nested fixture dirs and child processes inside this run.
    os.environ.update(TEMP=tmp, TMP=tmp, TMPDIR=tmp)
    tempfile.tempdir = tmp
    os.environ['SCE_BOT_TEST_MODE'] = '1'
    registry = json.loads((repo / 'examples/servers.example.json').read_text(encoding='utf-8-sig'))
    for server in registry['servers']:
        server['server_dir'] = str(Path(tmp) / 'server' / server['id'])
        server['password_file'] = str(Path(tmp) / 'no-live-password.txt')
    registry_path = Path(tmp) / 'servers.json'
    registry_path.write_text(json.dumps(registry), encoding='utf-8')
    os.environ['SCE_SERVER_REGISTRY'] = str(registry_path)
    os.environ['SCE_SERVER_EXTRA_REGISTRY'] = ''
    os.environ['LLBOT_GROUP'] = '1001'
    os.environ['SANAE_GROUPS'] = '1001'
    os.environ['SOCIAL_LITE_GROUP'] = '1001'
    os.environ['SOCIAL_LITE_GROUPS'] = '1001'
    os.environ['SANAE_BOT_QQ'] = '1002'
    os.environ['SANAE_QQ'] = '1002'
    os.environ['SANAE_HISTORICAL_BOT_QQ'] = '1004'
    os.environ['LLBOT_API_TOKEN'] = ''
    for key in (
        'SOCIAL_LITE_STATE', 'SLANG_PATH', 'FEEDBACK_PATH', 'SOCIAL_MEMORY_PATH',
        'STICKER_MANIFEST', 'SANAE_SHARED_KNOWLEDGE_PATH', 'SOCIAL_LITE_ACTIVITY',
        'PLAYER_BINDINGS_PATH', 'SOCIAL_ARCHIVE_ROOT', 'OPS_STATE_DIR', 'OPS_STATE_BASE',
        'SCE_SERVER_SELECTION_PATH', 'CONVERSATION_MEMORY_PATH', 'EXPRESSION_MEMORY_PATH',
        'REPLY_LESSONS_PATH', 'OPS_RECIPE_IMAGE_DIR', 'BLUEMAP_SHOT_DIR',
    ):
        os.environ[key] = str(Path(tmp) / key)
    os.chdir(tmp)
    try:
        result = unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.loadTestsFromName(name))
        print('PUBLIC_RESULT', result.testsRun, len(result.failures), len(result.errors), len(result.skipped))
        code = int(not result.wasSuccessful())
    finally:
        os.chdir(repo)
    sys.exit(code)
'''

names = sys.argv[1:] or [p.stem for p in sorted((ROOT / 'tests').glob('test_*.py'))]
results = []
# Imported module constants must not inherit settings from an operator's live shell.
config_prefix = re.compile(r'^(SANAE_|SOCIAL_|GEMINI_|GOOGLE_GEMINI_|DEEPSEEK_|GPT6LUNA_|ZHIPU_|SCE_|LLBOT_|EXPRESSION_MEMORY|REPLY_LESSONS|CONVERSATION_MEMORY|SLANG_|FEEDBACK_|STICKER_|OPS_|FUN_MUTE_|RELAY_|LEGACY_|GROUP_CHAT_)')
clean_env = {key: value for key, value in os.environ.items() if not config_prefix.match(key)}
clean_env.update(PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
for name in names:
    if not re.fullmatch(r'test_[A-Za-z0-9_]+', name) or not (ROOT / 'tests' / (name + '.py')).is_file():
        raise SystemExit('Unknown test module: ' + name)
    try:
        process = subprocess.run(
            [sys.executable, '-X', 'utf8', '-c', BOOTSTRAP, str(ROOT), name],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8',
            env=clean_env, timeout=100,
        )
        output, code = process.stdout, process.returncode
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or b''
        if isinstance(output, bytes):
            output = output.decode('utf-8', errors='replace')
        output += '\nOffline test timed out after 100 seconds.\n'
        code = 124
    (OUTPUT / (name + '.log')).write_text(output, encoding='utf-8')
    match = re.search(r'PUBLIC_RESULT (\d+) (\d+) (\d+) (\d+)', output)
    row = {'test': name, 'exitCode': code, 'counts': list(map(int, match.groups())) if match else None}
    results.append(row)
    print(json.dumps(row), flush=True)
    if code:
        print(output, flush=True)
(OUTPUT / 'sanae-test-results.json').write_text(json.dumps(results, indent=2) + '\n', encoding='utf-8')
sys.exit(any(row['exitCode'] for row in results))
