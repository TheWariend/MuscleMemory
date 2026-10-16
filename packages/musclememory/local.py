"""Local install in ~/.musclememory: one database, one server, one key per agent.

Everything private lives in this folder with owner-only permissions, so no command
needs a key or path copied by hand.
"""

import json
import os
import signal
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

import httpx

AGENTS = ("claude", "codex", "cursor", "cleanslate")
NAMES = {"claude": "Claude Code", "codex": "Codex", "cursor": "Cursor", "cleanslate": "CleanSlate"}


def home():
    return Path(os.getenv("MM_HOME", Path.home() / ".musclememory")).expanduser()


def private_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as file:
        file.write(text)
    path.chmod(0o600)


def config():
    path = home() / "config.json"
    if not path.exists():
        raise SystemExit("MuscleMemory isn't set up yet. Run: musclememory init")
    return json.loads(path.read_text())


def default_database_url():
    path = home() / "config.json"
    if path.exists():
        return json.loads(path.read_text())["database_url"]
    return "sqlite:///" + str(home() / "memory.db")


def healthy(url):
    try:
        return httpx.get(url + "/health", timeout=1).status_code == 200
    except httpx.HTTPError:
        return False


def init(port=8080, namespace="default"):
    from .cli import migrate
    from .config import Settings
    from .retrieval import Embeddings
    from .storage import Database, ensure_vector_index

    folder = home()
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    folder.chmod(0o700)
    existing = (
        json.loads((folder / "config.json").read_text())
        if (folder / "config.json").exists()
        else {}
    )
    settings_file = {
        "url": f"http://127.0.0.1:{port}",
        "port": port,
        "tenant": existing.get("tenant", "local"),
        "namespace": existing.get("namespace", namespace),
        "database_url": existing.get("database_url", "sqlite:///" + str(folder / "memory.db")),
    }
    private_write(folder / "config.json", json.dumps(settings_file, indent=2) + "\n")
    print(f"✓ Folder        {folder}")
    migrate(settings_file["database_url"])
    database = Database(settings_file["database_url"])
    settings = Settings.from_env()
    ensure_vector_index(database.engine, settings.embedding_dim)
    database.engine.dispose()
    print("✓ Database      ready")
    print("… Search model  downloading once (about 90 MB)…", flush=True)
    Embeddings(settings).embed("MuscleMemory warm-up")
    print("✓ Search model  ready")
    print("\nNext:\n  musclememory start            start MuscleMemory in the background")
    print("  musclememory connect codex    run inside a project (or claude, cursor, cleanslate)")
    print("  musclememory dashboard        open the dashboard, already signed in")


def start():
    settings_file = config()
    url = settings_file["url"]
    if healthy(url):
        print(f"MuscleMemory is already running at {url}")
        return
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", settings_file["port"])) == 0:
            raise SystemExit(
                f"Port {settings_file['port']} is busy. Run: musclememory init --port 8090"
            )
    environment = {**os.environ, "MM_DATABASE_URL": settings_file["database_url"]}
    log = open(home() / "server.log", "ab")  # noqa: SIM115 - handed to the child process
    process = subprocess.Popen(
        [sys.executable, "-m", "musclememory.cli", "serve", "--port", str(settings_file["port"])],
        env=environment,
        stdout=log,
        stderr=log,
        start_new_session=True,
    )
    private_write(home() / "server.pid", str(process.pid))
    for _ in range(150):
        if healthy(url):
            print(f"✓ MuscleMemory is running at {url}  (logs: {home() / 'server.log'})")
            return
        if process.poll() is not None:
            raise SystemExit(f"MuscleMemory stopped while starting. See {home() / 'server.log'}")
        time.sleep(0.2)
    raise SystemExit(f"MuscleMemory didn't answer. See {home() / 'server.log'}")


def stop():
    pid_file = home() / "server.pid"
    if not pid_file.exists():
        print("MuscleMemory isn't running.")
        return
    try:
        os.kill(int(pid_file.read_text()), signal.SIGTERM)
        print("✓ Stopped")
    except ProcessLookupError:
        print("MuscleMemory wasn't running.")
    pid_file.unlink(missing_ok=True)


def status():
    settings_file = config()
    print(
        f"{'✓ Running' if healthy(settings_file['url']) else '✗ Stopped'}  {settings_file['url']}"
    )
    print(f"  Folder     {home()}")
    connected = (
        sorted(p.stem for p in (home() / "keys").glob("*.env"))
        if (home() / "keys").exists()
        else []
    )
    print("  Agents     " + (", ".join(NAMES.get(a, a) for a in connected) or "none yet"))


def agent_key(service, settings_file, agent):
    """One private key per agent, created once and reused."""
    path = home() / "keys" / f"{agent}.env"
    if path.exists():
        return path, path.read_text().split("=", 1)[1].strip()
    issued = service.issue_key(
        settings_file["tenant"],
        {settings_file["namespace"]: ["read", "write"]},
        f"{NAMES[agent]} on {socket.gethostname()}",
    )
    private_write(path, f"MM_API_KEY={issued['api_key']}\n")
    return path, issued["api_key"]


def add_browser(agent, project, hooks):
    """Register the MuscleMemory browser MCP server in the agent's project config."""
    import shutil
    import sys

    command = [sys.executable, "-m", "musclememory", "browser", "--config", str(hooks)]
    root = Path(project).resolve()
    if agent == "codex":
        path = root / ".codex" / "config.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        text = path.read_text() if path.exists() else ""
        if "[mcp_servers.musclememory_browser]" in text:
            return path
        if path.exists():
            shutil.copy2(path, path.with_name(path.name + ".musclememory-backup"))
        args = ", ".join(json.dumps(part) for part in command[1:])
        path.write_text(
            text.rstrip("\n")
            + ("\n\n" if text.strip() else "")
            + "[mcp_servers.musclememory_browser]\n"
            + f"command = {json.dumps(command[0])}\nargs = [{args}]\n"
        )
        return path
    path = root / (".mcp.json" if agent == "claude" else ".cursor/mcp.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(path.read_text()) if path.exists() else {}
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".musclememory-backup"))
    data.setdefault("mcpServers", {})["musclememory-browser"] = {
        "command": command[0],
        "args": command[1:],
    }
    path.write_text(json.dumps(data, indent=2) + "\n")
    return path


def connect(agent, project=".", browser=False):
    from .client_hooks import install_hooks
    from .config import Settings
    from .service import MemoryService

    settings_file = config()
    service = MemoryService(Settings.from_env())
    key_file, key = agent_key(service, settings_file, agent)
    service.db.engine.dispose()
    if agent == "cleanslate":
        host = home() / "cleanslate.json"
        private_write(
            host,
            json.dumps(
                {
                    "url": settings_file["url"],
                    "api_key": key,
                    "namespace": settings_file["namespace"],
                },
                indent=2,
            )
            + "\n",
        )
        module = Path(__file__).resolve().parents[1] / "sdk-typescript" / "dist" / "node.js"
        print("✓ CleanSlate is connected. Start CleanSlate from a terminal with:\n")
        print(f"  export CLEANSLATE_MUSCLEMEMORY_CONFIG={host}")
        if module.exists():
            print(f"  export CLEANSLATE_MUSCLEMEMORY_NODE_MODULE={module}")
        else:
            node = "<path to node_modules/@thewariend/musclememory/dist/node.js>"
            print(f"  export CLEANSLATE_MUSCLEMEMORY_NODE_MODULE={node}")
        return
    hooks = home() / "hooks" / f"{agent}.json"
    private_write(
        hooks,
        json.dumps(
            {
                "url": settings_file["url"],
                "namespace": settings_file["namespace"],
                "env_file": str(key_file),
                "state_dir": str(home() / "hooks" / f"{agent}-state"),
            },
            indent=2,
        )
        + "\n",
    )
    installed = install_hooks(project, [agent], str(hooks))
    print(f"✓ {NAMES[agent]} is connected in {Path(project).resolve()}")
    for path in installed:
        print(f"  hooks: {path}")
    if browser:
        print(f"  browser: {add_browser(agent, project, hooks)}")
        print('  (needs: pip install "thewariend-musclememory[browser]")')
    print(f"\nRestart {NAMES[agent]}, then give it a task. Sessions show up under Activity.")
    if not healthy(settings_file["url"]):
        print("MuscleMemory isn't running yet. Run: musclememory start")


def dashboard(open_browser=True):
    from .config import Settings
    from .service import MemoryService

    settings_file = config()
    if not healthy(settings_file["url"]):
        start()
    service = MemoryService(Settings.from_env())
    code = service.create_sign_in_code(
        settings_file["tenant"],
        {settings_file["namespace"]: ["read", "write", "delete", "admin"]},
    )
    service.db.engine.dispose()
    link = f"{settings_file['url']}/dashboard/#/sign-in/{code}"
    print("Opening the dashboard. This sign-in link works once, for 5 minutes.")
    if not (open_browser and webbrowser.open(link)):
        print(link)


def export(skill, target):
    """Write a Ready skill into Codex's or Claude Code's skills folder as SKILL.md."""
    from .config import Settings
    from .core import DomainError
    from .security import Principal
    from .service import MemoryService
    from .skill_export import AGENTS

    settings_file = config()
    namespace = settings_file["namespace"]
    owner = Principal(
        "local-cli", settings_file["tenant"], {namespace: ["read", "write", "delete", "admin"]}
    )
    service = MemoryService(Settings.from_env())
    try:
        result = service.export_skill(owner, skill, target)
        verb = "already has" if result["unchanged"] else "now has"
        print(f"✓ {AGENTS[target]} {verb} the proven skill {result['name']}.")
    except DomainError as error:
        raise SystemExit(error.message) from None
    finally:
        service.db.engine.dispose()
