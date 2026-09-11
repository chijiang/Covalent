import os

import typer
import uvicorn

app = typer.Typer(help="Run the FastAPI server")


@app.callback(invoke_without_command=True)
def serve(
    host: str = typer.Option("0.0.0.0", help="Bind host"),
    port: int | None = typer.Option(
        None,
        help="Bind port (defaults to AGENT_FRAMEWORK_BACKEND_PORT, fallback 5170)",
    ),
) -> None:
    if port is None:
        port = int(os.getenv("AGENT_FRAMEWORK_BACKEND_PORT", "5170"))
    uvicorn.run("covalent.api.app:create_app", factory=True, host=host, port=port, reload=False)
