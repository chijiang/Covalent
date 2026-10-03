"""Exercise Enterprise startup/auth/run/SSE against an explicitly disposable DB.

Usage: uv run python tooling/smoke_enterprise.py --database-url <disposable PostgreSQL URL>
The script migrates and seeds the supplied database. No real model API is called.
"""
import argparse
import os, tempfile, subprocess, sys
from pathlib import Path
from fastapi.testclient import TestClient
from covalent_enterprise.api.app import create_app
from covalent_runtime.ports.model import ModelAdapter
from covalent_runtime.domain.types import Capability, GenerationResponse

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--database-url', required=True)
args = parser.parse_args()


class Model(ModelAdapter):
    capabilities = {Capability.CHAT}
    async def generate(self, request):
        return GenerationResponse(output_text='Enterprise monorepo smoke passed')

with tempfile.TemporaryDirectory(prefix='covalent-enterprise-smoke-') as tmp:
    os.chdir(tmp)
    os.environ.update({
      'AGENT_FRAMEWORK_DATABASE_URL':args.database_url,
      'AGENT_FRAMEWORK_DATABASE_SCHEMA':'public',
      'AGENT_FRAMEWORK_CONSOLE_AUTH_MODE':'local',
      'AGENT_FRAMEWORK_CONSOLE_SEED_ADMIN_PASSWORD':'monorepo-smoke-only-password',
      'AGENT_FRAMEWORK_CONSOLE_SESSION_SECRET':'monorepo-smoke-session-secret-only',
      'AGENT_FRAMEWORK_API_TOKEN_HASH_PEPPER':'monorepo-smoke-pepper-only',
      'AGENT_FRAMEWORK_EXECUTION_BACKEND_KIND':'filesystem',
      'AGENT_FRAMEWORK_MCP_ENABLED':'false',
      'AGENT_FRAMEWORK_SKILLS_ROOT_DIR':str(Path(tmp)/'skills'),
    })
    subprocess.run([sys.executable,'-m','covalent_enterprise','migrate'],check=True)
    app=create_app()
    with TestClient(app, base_url="https://testserver") as client:
        assert client.get('/healthz').status_code==200
        assert client.get('/agents').status_code==401
        login=client.post('/auth/login',json={'identifier':'admin','password':'monorepo-smoke-only-password'})
        assert login.status_code==200, login.text
        for endpoint in ('/agents','/sessions','/skills','/config/providers','/config/mcp','/api-tokens'):
            response=client.get(endpoint)
            assert response.status_code==200,(endpoint,response.text)
        registry=app.state.registry
        agent=next(iter(registry.agents.values()))
        registry.model_providers[agent.provider.cache_key()]=Model(agent.provider)
        response=client.post(f'/agents/{agent.name}/runs',json={'input':'smoke'})
        assert response.status_code==200,response.text
        ids=response.json()
        with client.stream('GET',f'/agents/{agent.name}/runs/{ids["run_id"]}/events') as response:
            stream=''.join(response.iter_text())
            assert 'Enterprise monorepo smoke passed' in stream,stream
            assert 'event: final' in stream,stream
        replay=client.get(f'/agents/{agent.name}/runs/{ids["run_id"]}/events')
        assert 'Enterprise monorepo smoke passed' in replay.text,replay.text
        runs=client.get(f'/agents/{agent.name}/runs',params={'session_id':ids['session_id']})
        assert runs.status_code==200,runs.text
        print('Enterprise login, management APIs, durable Agent execution and SSE replay passed')
