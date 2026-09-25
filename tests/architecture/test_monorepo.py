"""Product/package boundaries, including annotations and compatibility imports."""
from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = {
    'covalent_lite': ('products/lite/service/src/covalent_lite', {'covalent_contracts', 'covalent_runtime', 'covalent_agent_kit', 'covalent_execution_native', 'covalent_execution_docker'}),
    'covalent_contracts': ('packages/python/contracts/src/covalent_contracts', set()),
    'covalent_runtime': ('packages/python/runtime/src/covalent_runtime', {'covalent_contracts'}),
    'covalent_execution_native': ('packages/python/execution-native/src/covalent_execution_native', {'covalent_runtime'}),
    'covalent_execution_docker': ('packages/python/execution-docker/src/covalent_execution_docker', {'covalent_runtime', 'covalent_execution_native'}),
    'covalent_agent_kit': ('packages/python/agent-kit/src/covalent_agent_kit', {'covalent_contracts', 'covalent_runtime', 'covalent_execution_native'}),
    'covalent_enterprise': ('products/enterprise/backend/src/covalent_enterprise', {'covalent_contracts', 'covalent_runtime', 'covalent_agent_kit', 'covalent_execution_native', 'covalent_execution_docker'}),
}


@pytest.mark.parametrize('package', PACKAGES)
def test_dependency_direction(package):
    directory, allowed = PACKAGES[package]
    files = list((ROOT / directory).rglob('*.py'))
    assert files, f'Missing implementation for {package}'
    forbidden_frameworks = {'fastapi', 'sqlalchemy', 'docker', 'openai', 'pydantic_settings'}
    for path in files:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            modules = []
            if isinstance(node, ast.Import):
                modules = [item.name for item in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                modules = [node.module]
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'import_module':
                assert node.args and isinstance(node.args[0], ast.Constant), f'Unreviewed dynamic import: {path}'
                modules = [node.args[0].value]
            for module in modules:
                top = module.split('.')[0]
                assert top != 'covalent', f'{path} depends on the legacy compatibility package'
                if top.startswith('covalent_'):
                    assert top in allowed | {package}, f'{package} -> {module}: {path}'
                if package in {'covalent_contracts', 'covalent_runtime'}:
                    assert top not in forbidden_frameworks, f'{path} imports {module}'


@pytest.mark.parametrize(('old', 'new'), [
    ('covalent.core.types', 'covalent_runtime.domain.types'),
    ('covalent.core.agent', 'covalent_contracts.agent'),
    ('covalent.infra.db', 'covalent_enterprise.infra.db'),
    ('covalent.api.app', 'covalent_enterprise.api.app'),
    ('covalent.registry.registry', 'covalent_agent_kit.registry.registry'),
    ('covalent.runtime.docker_backend', 'covalent_execution_docker.backend'),
])
def test_legacy_modules_preserve_identity(old, new):
    assert importlib.import_module(old) is importlib.import_module(new)


def test_migrations_and_runners_are_package_resources():
    from alembic.script import ScriptDirectory
    from covalent_enterprise.infra.migrations import migration_config
    from covalent_execution_native.resources import RUNNERS_DIR

    config = migration_config('postgresql+asyncpg://unused:password%25@localhost/unused')
    assert config.attributes['explicit_database_url'] is True
    assert '%25' in config.get_main_option('sqlalchemy.url')
    assert ScriptDirectory.from_config(config).get_heads()
    assert (RUNNERS_DIR / 'python_runner.py').is_file()
    assert (RUNNERS_DIR / 'node_runner.js').is_file()
