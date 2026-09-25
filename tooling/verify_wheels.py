"""Verify wheels outside the checkout, with no editable or legacy dependencies.

Run after `uv build --all-packages --wheel` using `uv run python tooling/verify_wheels.py`.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
UV = shutil.which('uv') or str(Path.home() / '.local/bin/uv')


def check(names: list[str], source: str) -> None:
    with tempfile.TemporaryDirectory(prefix='covalent-wheel-check-') as tmp:
        directory = Path(tmp)
        subprocess.run([UV, 'venv', str(directory / 'venv'), '--python', '3.12'], check=True)
        python = directory / 'venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        wheels = []
        for name in names:
            matches = list((ROOT / 'dist').glob(f'{name}-0.1.0-*.whl'))
            if len(matches) != 1:
                raise RuntimeError(f'Build exactly one current wheel for {name}: {matches}')
            wheels.append(str(matches[0]))
        subprocess.run([UV, 'pip', 'install', '--python', str(python), *wheels], check=True)
        subprocess.run([str(python), '-I', '-c', source], cwd=directory, check=True)


if __name__ == '__main__':
    common = ['covalent_contracts', 'covalent_runtime']
    check(common + ['covalent_desktop'], '''
import importlib.util
import covalent_desktop.api, covalent_desktop.application, covalent_desktop.infra
for name in ('covalent', 'covalent_enterprise', 'covalent_lite', 'fastapi', 'sqlalchemy', 'docker'):
    assert importlib.util.find_spec(name) is None, name
print('Isolated Desktop scaffold import passed (sidecar not yet implemented)')
''')
    check(common + ['covalent_lite'], '''
import importlib.util
import covalent_lite.api, covalent_lite.cli, covalent_lite.application, covalent_lite.config
for name in ('covalent', 'covalent_enterprise', 'fastapi', 'sqlalchemy', 'docker'):
    assert importlib.util.find_spec(name) is None, name
print('Isolated Lite scaffold import passed (CLI/API not yet implemented)')
''')
    check(common, '''
import asyncio, importlib.util
from covalent_contracts.agent import AgentSpec
from covalent_contracts.model import ProviderConfig
from covalent_runtime.engine.react import ReactAgentRuntime
from covalent_runtime.domain.types import Capability, GenerationResponse
from covalent_runtime.ports.model import ModelAdapter
from covalent_runtime.services.run_manager import RunManager
for name in ('covalent', 'covalent_enterprise', 'covalent_agent_kit', 'fastapi', 'sqlalchemy', 'docker', 'openai'):
    assert importlib.util.find_spec(name) is None, name
class Model(ModelAdapter):
    capabilities = {Capability.CHAT}
    async def generate(self, request):
        return GenerationResponse(output_text='standalone runtime works')
class Registry:
    local_tools = {}
    agents = {}
    skills = {}
    def get_model_provider(self, config): return Model(config)
    async def resolve_tools_for_agent(self, agent): return []
agent = AgentSpec(name='smoke', description='', system_prompt='', provider=ProviderConfig(provider='test', model='test'))
response = asyncio.run(ReactAgentRuntime(Registry()).run(agent, 'hello'))
assert response.output_text == 'standalone runtime works'
print('Isolated Runtime execution passed')
''')
    check(common + ['covalent_execution_native', 'covalent_agent_kit'], '''
import importlib.util
from covalent_agent_kit.registry.registry import FrameworkRegistry
from covalent_agent_kit.skills.process import SkillProcessManager
from covalent_agent_kit.skills import sdk
from pathlib import Path
from covalent_execution_native.resources import RUNNERS_DIR
for name in ('covalent', 'covalent_enterprise', 'fastapi', 'sqlalchemy', 'docker', 'pymupdf', 'playwright'):
    assert importlib.util.find_spec(name) is None, name
assert (RUNNERS_DIR / 'node_runner.js').is_file()
assert (RUNNERS_DIR / 'python_runner.py').is_file()
assert (Path(sdk.__file__).parent / 'nodejs/skill_sdk.js').is_file()
assert (Path(sdk.__file__).parent / 'python/skill_sdk.py').is_file()
assert FrameworkRegistry().agents == {}
print('Minimal Agent Kit and runner resources passed')
''')
    check(common + ['covalent_execution_native', 'covalent_execution_docker', 'covalent_agent_kit', 'covalent_enterprise'], '''
import importlib.util, subprocess, sys
from alembic.script import ScriptDirectory
from covalent_enterprise.infra.migrations import migration_config
from covalent_enterprise.api.app import create_app
assert importlib.util.find_spec('covalent') is None
assert create_app().openapi()['paths']['/healthz']
assert ScriptDirectory.from_config(migration_config('postgresql+asyncpg://unused@localhost/unused')).get_heads()
subprocess.run([sys.executable, '-m', 'covalent_enterprise', '--help'], check=True)
print('Isolated Enterprise API, CLI and migration resources passed')
''')
