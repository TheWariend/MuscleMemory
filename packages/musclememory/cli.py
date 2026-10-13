import argparse
import json
import os
from pathlib import Path

from alembic import command
from alembic.config import Config

from .config import Settings
from .mcp_server import build_mcp
from .service import MemoryService

ROOT = Path(__file__).resolve().parents[2]


def migrate(url, revision="head", version_table_schema=None):
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).with_name("migrations")))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    if version_table_schema:
        config.set_main_option("version_table_schema", version_table_schema)
    command.upgrade(config, revision)


def main():
    parser = argparse.ArgumentParser(description="MuscleMemory by TheWariend")
    sub = parser.add_subparsers(dest="command", required=True)
    hooks = sub.add_parser("hooks", help="Install native client hooks and manage captured traces")
    hooks.add_argument(
        "action",
        choices=["install", "receive", "list", "flush", "attest", "reconcile", "learn", "purge"],
    )
    hooks.add_argument("--client", choices=["claude", "codex", "cursor", "all"], default="claude")
    hooks.add_argument("--config", required=True, help="JSON config (no embedded API keys)")
    hooks.add_argument("--project", default=".")
    hooks.add_argument("--run")
    hooks.add_argument("--succeeded", action="store_true", help="Human attestation of task success")
    hooks.add_argument("--summary", default="Task outcome verified by the operator")
    hooks.add_argument("--policy", help="Optional learning-policy JSON file")
    hooks.add_argument(
        "--receipts", help="Actual Codex exec --json output for outcome reconciliation"
    )
    hooks.add_argument("--older-days", type=int, default=7)
    init = sub.add_parser("init", help="Set up MuscleMemory in ~/.musclememory")
    init.add_argument("--port", type=int, default=8080)
    init.add_argument("--namespace", default="default")
    sub.add_parser("start", help="Start MuscleMemory in the background")
    sub.add_parser("stop", help="Stop the background MuscleMemory")
    sub.add_parser("status", help="Show whether MuscleMemory is running and what's connected")
    connect = sub.add_parser("connect", help="Connect an agent in this project, no keys to copy")
    connect.add_argument("agent", choices=["claude", "codex", "cursor", "cleanslate"])
    connect.add_argument("--project", default=".")
    dash = sub.add_parser("dashboard", help="Open the dashboard, already signed in")
    dash.add_argument("--no-browser", action="store_true", help="Print the sign-in link instead")
    export = sub.add_parser("export", help="Add a Ready skill to Codex or Claude Code as SKILL.md")
    export.add_argument("skill", help="The skill id (procedure token)")
    export.add_argument("--to", dest="target", choices=["codex", "claude"], required=True)
    sub.add_parser("migrate", help="Apply explicit database migrations")
    key = sub.add_parser("key", help="Create an API key; plaintext is shown once")
    key.add_argument("--tenant", help="Defaults to your local setup")
    key.add_argument("--namespace", help="Defaults to your local setup")
    key.add_argument("--label", default="agent")
    key.add_argument("--permissions", default="read,write", help="read,write,delete,admin")
    stdio = sub.add_parser("stdio", help="Run the local MCP transport using MM_API_KEY")
    stdio.add_argument("--migrate", action="store_true")
    stdio.add_argument("--url", help="Forward stdio to the authenticated REST service")
    stdio.add_argument("--config", help="Private native hook config for gateway credentials")
    serve = sub.add_parser("serve", help="Run REST and Streamable HTTP MCP")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    sub.add_parser("purge", help="Remove expired execution traces (schedule externally)")
    reindex = sub.add_parser("reindex", help="Rebuild search data without relearning")
    reindex.add_argument("--namespace", required=True)
    sub.add_parser("doctor", help="Check database and configured retrieval provider")
    arguments = parser.parse_args()
    if arguments.command == "export":
        from . import local

        local.export(arguments.skill, arguments.target)
        return
    if arguments.command in {"init", "start", "stop", "status", "connect", "dashboard"}:
        from . import local

        if arguments.command == "init":
            local.init(arguments.port, arguments.namespace)
        elif arguments.command == "connect":
            local.connect(arguments.agent, arguments.project)
        elif arguments.command == "dashboard":
            local.dashboard(open_browser=not arguments.no_browser)
        else:
            getattr(local, arguments.command)()
        return
    if arguments.command == "key" and not (arguments.tenant and arguments.namespace):
        from .local import config

        local_settings = config()
        arguments.tenant = arguments.tenant or local_settings["tenant"]
        arguments.namespace = arguments.namespace or local_settings["namespace"]
    if arguments.command == "hooks":
        if arguments.client == "all" and arguments.action != "install":
            parser.error("--client all is only supported for install")
        if arguments.action in {"attest", "reconcile", "learn"} and not arguments.run:
            parser.error("--run is required")
        if arguments.action == "reconcile" and not arguments.receipts:
            parser.error("--receipts is required for reconciliation")
        if arguments.older_days < 1:
            parser.error("--older-days must be positive")
        from .client_hooks import hooks_command

        hooks_command(arguments)
        return
    settings = Settings.from_env()
    if arguments.command == "stdio" and (arguments.url or arguments.config):
        from .gateway import RESTGateway

        if arguments.migrate:
            parser.error("Apply migrations separately when using the REST gateway")
        if arguments.config:
            from .client_hooks import load_config

            config, _, _, client = load_config(arguments.config, "codex")
            key = client.http.headers["Authorization"].removeprefix("Bearer ")
            gateway = RESTGateway(str(client.http.base_url), key, client=client)
        else:
            key = os.getenv("MM_API_KEY", "")
            gateway = RESTGateway(arguments.url, key)
        try:
            gateway.authenticate(key)
            build_mcp(gateway, stdio_key=key).run(transport="stdio")
        finally:
            gateway.close()
        return
    if arguments.command == "migrate":
        migrate(settings.database_url)
        from .storage import Database, ensure_vector_index

        database = Database(settings.database_url)
        ensure_vector_index(database.engine, settings.embedding_dim)
        database.engine.dispose()
        return
    if arguments.command == "serve":
        import uvicorn

        uvicorn.run(
            "musclememory.api:create_app", factory=True, host=arguments.host, port=arguments.port
        )
        return
    service = MemoryService(settings)
    if arguments.command == "key":
        print(
            json.dumps(
                service.issue_key(
                    arguments.tenant,
                    {arguments.namespace: arguments.permissions.split(",")},
                    arguments.label,
                )
            )
        )
    elif arguments.command == "purge":
        print(json.dumps(service.purge_expired()))
    elif arguments.command == "reindex":
        principal = service.authenticate(os.getenv("MM_API_KEY"), rate_limit=False)
        print(json.dumps(service.reindex_embeddings(principal, arguments.namespace)))
    elif arguments.command == "doctor":
        from sqlalchemy import text

        with service.db.session() as session:
            revision = session.scalar(text("SELECT version_num FROM alembic_version"))
        provider = service.embeddings.metadata()
        provider["verified_dimensions"] = len(service.embeddings.embed("provider health check"))
        print(
            json.dumps(
                {
                    "database_revision": revision,
                    "embedding": provider,
                    "guidance_token_budget": settings.guidance_token_budget,
                }
            )
        )
    elif arguments.command == "stdio":
        if arguments.migrate:
            migrate(settings.database_url)
        key = os.getenv("MM_API_KEY")
        service.authenticate(key, rate_limit=False)
        build_mcp(service, stdio_key=key).run(transport="stdio")


if __name__ == "__main__":
    main()
