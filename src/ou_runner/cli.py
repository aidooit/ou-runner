#!/usr/bin/env python3
"""
ou-runner — Odoo Migration CLI using OpenUpgrade.

Walks an Odoo database through OpenUpgrade major versions (14 → 19)
inside Docker, one hop at a time. All working artifacts (dumps, backups,
openupgrade_<n>/ clones, generated Dockerfiles, docker-compose.yml,
migration_times.json) are created in the user's current working
directory, so the recommended workflow is:

    mkdir my-odoo-migration && cd my-odoo-migration
    ou-runner setup
"""

import json
import re
import shutil
import subprocess
import sys
import time
from importlib.resources import files as _resource_files
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text
from rich.live import Live

app = typer.Typer(help="ou-runner — Odoo migration tool using OpenUpgrade")
console = Console()

# Set to True while a brave-mode chain is running so failure handlers halt
# immediately instead of asking the user whether to continue.
_BRAVE_MODE = False

# Configuration
DB_NAME = "odoo_migration"
DB_USER = "odoo"
DB_PASSWORD = "odoo"
POSTGRES_CONTAINER = "odoo_postgres"
DUMPS_DIR = Path("dumps")
BACKUPS_DIR = Path("backups")
ADDONS_DIR = Path("addons")
LOGS_DIR = Path("logs")
MIGRATION_TIMES_FILE = Path("migration_times.json")
OPENUPGRADE_REPO_URL = "https://github.com/OCA/OpenUpgrade.git"


def _read_template(name: str) -> str:
    """Load a packaged template from ou_runner/templates/ by filename."""
    return _resource_files("ou_runner.templates").joinpath(name).read_text()

# Supported versions and their configurations
VERSIONS = {
    14: {"port": 8014, "container": "odoo14", "profile": "odoo14"},
    15: {"port": 8015, "container": "openupgrade15", "profile": "upgrade15"},
    16: {"port": 8016, "container": "openupgrade16", "profile": "upgrade16"},
    17: {"port": 8017, "container": "openupgrade17", "profile": "upgrade17"},
    18: {"port": 8018, "container": "openupgrade18", "profile": "upgrade18"},
    19: {"port": 8019, "container": "openupgrade19", "profile": "upgrade19"},
}


def version_to_filename(version: int) -> str:
    """Convert version number to dump filename: 14 -> 'odoo_14_db.dump'"""
    return f"odoo_{version}_db.dump"


def load_migration_times() -> dict:
    """Load migration times from JSON file"""
    if MIGRATION_TIMES_FILE.exists():
        with open(MIGRATION_TIMES_FILE, 'r') as f:
            return json.load(f)
    return {}


def save_migration_time(version: int, duration_seconds: int):
    """Save migration time for a specific version"""
    times = load_migration_times()
    times[str(version)] = duration_seconds

    with open(MIGRATION_TIMES_FILE, 'w') as f:
        json.dump(times, f, indent=2)


def format_duration(seconds: int) -> str:
    """Format duration in seconds to human-readable format"""
    minutes = seconds // 60
    secs = seconds % 60
    if minutes > 0:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


# ============================================
# TODO LIST MANAGEMENT
# ============================================

class MigrationTodoList:
    """Manages and displays a TODO list for migration tasks"""

    def __init__(self):
        self.tasks = []

    def create_tasks(self, skip_backup: bool = False):
        """Create the list of migration tasks"""
        self.tasks = [
            {"name": "Ensure PostgreSQL is running", "status": "pending"},
            {"name": "Stop any running Odoo containers", "status": "pending"},
            {"name": "Restore source dump", "status": "pending"},
        ]

        if not skip_backup:
            self.tasks.append({"name": "Create backup before migration", "status": "pending"})

        self.tasks.extend([
            {"name": "Start target version container", "status": "pending"},
            {"name": "Run migration", "status": "pending"},
            {"name": "Restart container in normal mode", "status": "pending"},
            {"name": "Create final dump", "status": "pending"},
        ])

    def create_table(self):
        """Create a Rich table with the current task states"""
        table = Table(show_header=False, box=None, padding=(0, 1), expand=False)

        for task in self.tasks:
            if task["status"] == "completed":
                text = Text(f"✓ {task['name']}", style="strike dim")
            elif task["status"] == "in_progress":
                text = Text(f"▶ {task['name']}", style="bold cyan")
            else:
                text = Text(f"○ {task['name']}", style="white")

            table.add_row(text)

        return table

    def update_task(self, index: int, status: str):
        if 0 <= index < len(self.tasks):
            self.tasks[index]["status"] = status

    def mark_in_progress(self, index: int):
        self.update_task(index, "in_progress")

    def mark_completed(self, index: int):
        self.update_task(index, "completed")


def display_migration_times_table():
    """Display a table of all migration times"""
    times = load_migration_times()

    if not times:
        return None

    table = Table(title="Migration Times Summary")
    table.add_column("Version", style="cyan", no_wrap=True)
    table.add_column("Duration", style="green")

    total_seconds = 0

    version_order = list(VERSIONS.keys())

    def sort_key(v_str: str) -> int:
        try:
            v_int = int(v_str)
        except ValueError:
            return 999
        return version_order.index(v_int) if v_int in VERSIONS else 999

    for version_str in sorted(times.keys(), key=sort_key):
        duration_seconds = times[version_str]
        total_seconds += duration_seconds
        try:
            display = str(int(version_str))
        except ValueError:
            display = version_str
        table.add_row(display, format_duration(duration_seconds))

    if len(times) > 1:
        table.add_row("", "", style="dim")
        table.add_row("[bold]Total", f"[bold]{format_duration(total_seconds)}", style="bold yellow")

    return table


def run_command(cmd: list, capture_output: bool = False, check: bool = True) -> Optional[subprocess.CompletedProcess]:
    """Execute a shell command"""
    try:
        if capture_output:
            result = subprocess.run(cmd, capture_output=True, text=True, check=check)
            return result
        else:
            result = subprocess.run(cmd, check=check)
            return result
    except subprocess.CalledProcessError as e:
        console.print(f"[red]Error executing command: {' '.join(cmd)}[/red]")
        console.print(f"[red]{e}[/red]")
        if capture_output and e.stderr:
            console.print(f"[red]{e.stderr}[/red]")
        raise


def stream_logs(container_name: str, follow: bool = False):
    """Stream logs from a Docker container"""
    cmd = ["docker", "logs"]
    if follow:
        cmd.append("-f")
    cmd.append(container_name)

    try:
        subprocess.run(cmd)
    except KeyboardInterrupt:
        console.print("\n[yellow]Log streaming interrupted[/yellow]")


def check_docker():
    """Check if Docker is running"""
    try:
        run_command(["docker", "ps"], capture_output=True)
        return True
    except Exception:
        console.print("[red]Error: Docker is not running. Please start Docker and try again.[/red]")
        return False


def ensure_directories():
    """Create the working directories the migration workflow expects."""
    for d in (DUMPS_DIR, BACKUPS_DIR, ADDONS_DIR, LOGS_DIR):
        d.mkdir(exist_ok=True)


def custom_addons_dir(major: int) -> Path:
    """Host-side custom-addons folder mounted into openupgrade<major>."""
    return ADDONS_DIR / f"custom_addons_{major}"


def ensure_custom_addons_dirs(majors: list[int]) -> list[tuple[int, str]]:
    """Create addons/custom_addons_<major>/ for each major, with a .gitkeep.

    Idempotent — never wipes user content. Returns a list of
    (major, 'created' | 'exists') so callers can render a summary.
    """
    ADDONS_DIR.mkdir(exist_ok=True)
    results: list[tuple[int, str]] = []
    for major in majors:
        target = custom_addons_dir(major)
        status = "exists" if target.exists() else "created"
        target.mkdir(exist_ok=True)
        keep = target / ".gitkeep"
        if not keep.exists():
            keep.touch()
        results.append((major, status))
    return results


def ensure_postgres_running():
    """Ensure PostgreSQL container is running"""
    console.print("[cyan]Checking PostgreSQL status...[/cyan]")

    result = run_command(
        ["docker", "ps", "--filter", f"name={POSTGRES_CONTAINER}", "--format", "{{.Names}}"],
        capture_output=True
    )

    if POSTGRES_CONTAINER not in result.stdout:
        console.print("[yellow]Starting PostgreSQL...[/yellow]")
        run_command(["docker", "compose", "up", "-d", "postgres"])
        console.print("[cyan]Waiting for PostgreSQL to be ready...[/cyan]")
        time.sleep(5)

        max_attempts = 30
        for attempt in range(max_attempts):
            result = run_command(
                ["docker", "exec", POSTGRES_CONTAINER, "pg_isready", "-U", DB_USER],
                capture_output=True,
                check=False
            )
            if result.returncode == 0:
                console.print("[green]✓ PostgreSQL is ready[/green]")
                return
            time.sleep(1)

        console.print("[red]Error: PostgreSQL failed to start properly[/red]")
        sys.exit(1)
    else:
        console.print("[green]✓ PostgreSQL is running[/green]")


def stop_odoo_containers():
    """Stop any running Odoo/OpenUpgrade containers"""
    console.print("[cyan]Stopping any running Odoo containers...[/cyan]")

    for version_info in VERSIONS.values():
        container = version_info["container"]
        result = run_command(
            ["docker", "ps", "--filter", f"name={container}", "--format", "{{.Names}}"],
            capture_output=True,
            check=False
        )

        if container in result.stdout:
            console.print(f"[yellow]Stopping {container}...[/yellow]")
            run_command(["docker", "stop", container], check=False)
            run_command(["docker", "rm", container], check=False)


def create_database():
    """Create the migration database if it doesn't exist"""
    console.print(f"[cyan]Checking if database '{DB_NAME}' exists...[/cyan]")

    result = run_command(
        ["docker", "exec", POSTGRES_CONTAINER, "psql", "-U", DB_USER, "-d", "postgres", "-tAc",
         f"SELECT 1 FROM pg_database WHERE datname='{DB_NAME}'"],
        capture_output=True
    )

    if "1" not in result.stdout:
        console.print(f"[yellow]Creating database '{DB_NAME}'...[/yellow]")
        run_command(
            ["docker", "exec", POSTGRES_CONTAINER, "createdb", "-U", DB_USER, DB_NAME]
        )
        console.print(f"[green]✓ Database '{DB_NAME}' created[/green]")
    else:
        console.print(f"[green]✓ Database '{DB_NAME}' exists[/green]")


def drop_database():
    """Drop the migration database"""
    console.print(f"[yellow]Dropping database '{DB_NAME}'...[/yellow]")
    run_command(
        ["docker", "exec", POSTGRES_CONTAINER, "dropdb", "-U", DB_USER, "--if-exists", DB_NAME]
    )


def restore_dump(dump_file: Path):
    """Restore a PostgreSQL dump file"""
    if not dump_file.exists():
        console.print(f"[red]Error: Dump file not found: {dump_file}[/red]")
        sys.exit(1)

    console.print(f"[cyan]Restoring dump: {dump_file.name}[/cyan]")

    drop_database()
    create_database()

    with open(dump_file, 'rb') as f:
        result = subprocess.run(
            ["docker", "exec", "-i", POSTGRES_CONTAINER, "pg_restore",
             "-U", DB_USER, "-d", DB_NAME, "--no-owner", "--no-acl"],
            stdin=f,
            capture_output=True,
            text=False
        )

        if result.returncode != 0 and "error" in result.stderr.decode().lower():
            console.print("[red]Error restoring dump:[/red]")
            console.print(result.stderr.decode())
            sys.exit(1)

    console.print(f"[green]✓ Database restored from {dump_file.name}[/green]")


def create_dump(version: int):
    """Create a PostgreSQL dump file"""
    dump_file = DUMPS_DIR / version_to_filename(version)
    console.print(f"[cyan]Creating dump: {dump_file.name}[/cyan]")

    with open(dump_file, 'wb') as f:
        result = subprocess.run(
            ["docker", "exec", POSTGRES_CONTAINER, "pg_dump",
             "-U", DB_USER, "-Fc", DB_NAME],
            stdout=f,
            stderr=subprocess.PIPE
        )

        if result.returncode != 0:
            console.print("[red]Error creating dump:[/red]")
            console.print(result.stderr.decode())
            sys.exit(1)

    console.print(f"[green]✓ Dump created: {dump_file}[/green]")
    console.print(f"[dim]Size: {dump_file.stat().st_size / 1024 / 1024:.2f} MB[/dim]")


def create_backup(version: int):
    """Create a timestamped backup"""
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    backup_file = BACKUPS_DIR / f"odoo_{version}_backup_{timestamp}.dump"

    console.print(f"[cyan]Creating backup: {backup_file.name}[/cyan]")

    with open(backup_file, 'wb') as f:
        result = subprocess.run(
            ["docker", "exec", POSTGRES_CONTAINER, "pg_dump",
             "-U", DB_USER, "-Fc", DB_NAME],
            stdout=f,
            stderr=subprocess.PIPE
        )

        if result.returncode != 0:
            console.print("[red]Error creating backup:[/red]")
            console.print(result.stderr.decode())
            sys.exit(1)

    console.print(f"[green]✓ Backup created: {backup_file.name}[/green]")


def start_odoo_container(version: int):
    """Start the OpenUpgrade container for a specific version"""
    version_info = VERSIONS[version]
    container = version_info["container"]
    profile = version_info["profile"]
    port = version_info["port"]

    console.print(f"[cyan]Starting {container} (port {port})...[/cyan]")
    run_command(["docker", "compose", "--profile", profile, "up", "-d"])

    console.print("[cyan]Waiting for container to be ready...[/cyan]")
    time.sleep(3)

    console.print(f"[green]✓ Container {container} is running on http://localhost:{port}[/green]")


def run_migration(to_version: int):
    """Run the OpenUpgrade migration"""
    container = VERSIONS[to_version]["container"]
    docker_version = f"{to_version}.0"

    console.print(f"[cyan]Running OpenUpgrade migration to version {to_version}...[/cyan]")
    console.print("[dim]This may take several minutes depending on your database size...[/dim]")

    try:
        result = subprocess.run(
            ["docker", "exec", "-e", f"OPENUPGRADE_TARGET_VERSION={docker_version}",
             container, "odoo",
             "-d", DB_NAME,
             "-u", "all",
             "--stop-after-init",
             "--load=base,web,openupgrade_framework",
             "--upgrade-path=/mnt/openupgrade/openupgrade_scripts/scripts",
             "--addons-path=/mnt/openupgrade,/mnt/custom_addons,/usr/lib/python3/dist-packages/odoo/addons",
             "--db_host=postgres",
             "--db_user=odoo",
             "--db_password=odoo"],
            check=False
        )

        if result.returncode != 0:
            console.print(f"[red]Migration returned exit code {result.returncode}[/red]")
            console.print("[yellow]Checking logs for details...[/yellow]")
            stream_logs(container)

            if _BRAVE_MODE:
                raise RuntimeError(f"Migration to {to_version} returned exit code {result.returncode}")

            if not Confirm.ask("\n[yellow]Migration may have encountered issues. Continue anyway?[/yellow]"):
                sys.exit(1)
    except KeyboardInterrupt:
        console.print("\n[red]Migration interrupted by user[/red]")
        sys.exit(1)

    console.print(f"[green]✓ Migration to version {to_version} completed[/green]")


def _run_single_migration(
    from_version: int,
    to_version: int,
    skip_backup: bool,
    auto_yes: bool,
    version_list: list,
    to_idx: int,
):
    """Execute a single from→to migration. Raises on failure."""
    console.print()
    console.print(Panel.fit(
        f"[bold cyan]Odoo Migration: {from_version} → {to_version}[/bold cyan]\n\n"
        f"Source dump: [yellow]{version_to_filename(from_version)}[/yellow]\n"
        f"Target dump: [green]{version_to_filename(to_version)}[/green]\n"
        f"Target port: [cyan]http://localhost:{VERSIONS[to_version]['port']}[/cyan]",
        title="Migration Plan"
    ))

    console.print("\n[bold]Tasks to be executed:[/bold]")
    preview_todo = MigrationTodoList()
    preview_todo.create_tasks(skip_backup=skip_backup)
    console.print(preview_todo.create_table())

    if not auto_yes:
        if not Confirm.ask("\n[bold]Proceed with migration?[/bold]"):
            console.print("[yellow]Migration cancelled[/yellow]")
            sys.exit(0)

    console.print("\n[bold]Running pre-flight checks...[/bold]")

    if not check_docker():
        sys.exit(1)

    ensure_directories()

    source_dump = DUMPS_DIR / version_to_filename(from_version)
    if not source_dump.exists():
        raise FileNotFoundError(
            f"Source dump not found: {source_dump}. "
            f"Please ensure {source_dump} exists in the dumps/ directory"
        )

    console.print("\n[bold cyan]Starting migration process...[/bold cyan]\n")

    todo = MigrationTodoList()
    todo.create_tasks(skip_backup=skip_backup)

    migration_start_time = time.time()

    def execute_task(task_index: int, func, *args, **kwargs):
        todo.mark_in_progress(task_index)
        console.print(todo.create_table())
        console.print()
        result = func(*args, **kwargs)
        todo.mark_completed(task_index)
        return result

    task_index = 0

    execute_task(task_index, ensure_postgres_running)
    task_index += 1

    execute_task(task_index, stop_odoo_containers)
    task_index += 1

    execute_task(task_index, restore_dump, source_dump)
    task_index += 1

    if not skip_backup:
        execute_task(task_index, create_backup, from_version)
        task_index += 1

    execute_task(task_index, start_odoo_container, to_version)
    task_index += 1

    execute_task(task_index, run_migration, to_version)
    task_index += 1

    def restart_container():
        console.print("[cyan]Restarting container in normal mode...[/cyan]")
        container = VERSIONS[to_version]["container"]
        profile = VERSIONS[to_version]["profile"]
        run_command(["docker", "compose", "--profile", profile, "restart", container])
        time.sleep(3)

    execute_task(task_index, restart_container)
    task_index += 1

    execute_task(task_index, create_dump, to_version)

    console.print()
    console.print(todo.create_table())
    console.print()

    migration_end_time = time.time()
    duration_seconds = int(migration_end_time - migration_start_time)

    save_migration_time(to_version, duration_seconds)

    port = VERSIONS[to_version]["port"]
    next_hint = version_list[to_idx + 1] if to_idx + 1 < len(version_list) else "COMPLETE"
    console.print(Panel.fit(
        f"[bold green]Migration completed successfully! ✓[/bold green]\n\n"
        f"Your Odoo {to_version} instance is running at:\n"
        f"[cyan]http://localhost:{port}[/cyan]\n\n"
        f"Output dump: [green]{version_to_filename(to_version)}[/green]\n\n"
        f"[bold]This Migration:[/bold] {format_duration(duration_seconds)}\n\n"
        f"[dim]Please test your instance before proceeding to the next version.[/dim]\n"
        f"[dim]When ready, run: ou-runner migrate --from {to_version} --to {next_hint}[/dim]\n\n"
        f"[dim]Run 'ou-runner logs -f {to_version}' to view logs[/dim]\n",
        title="✓ Success",
        border_style="green"
    ))

    console.print()
    times_table = display_migration_times_table()
    if times_table:
        console.print(times_table)


# ============================================
# SETUP COMMAND HELPERS
# ============================================

def _prompt_version(prompt_text: str, default: Optional[int] = None) -> int:
    """Prompt user for a version, validating against the supported set."""
    choices = [str(v) for v in VERSIONS]
    default_str = str(default) if default is not None else None
    while True:
        answer = Prompt.ask(prompt_text, choices=choices, default=default_str)
        try:
            value = int(answer)
        except (TypeError, ValueError):
            console.print(f"[red]Unknown version '{answer}'. Choose one of: {', '.join(choices)}[/red]")
            continue
        if value in VERSIONS:
            return value
        console.print(f"[red]Unknown version '{answer}'. Choose one of: {', '.join(choices)}[/red]")


def _render_dockerfile(major: int) -> str:
    """Render the Dockerfile for an OpenUpgrade version."""
    template = _read_template("Dockerfile.openupgrade.tmpl")
    break_flag = "--break-system-packages " if major >= 18 else ""
    if major == 19:
        extra_pip_line = (
            "    pip3 install --break-system-packages html2text google-auth "
            "google-auth-oauthlib google-auth-httplib2 google-api-python-client "
            "oauth2client && \\\n"
        )
    else:
        extra_pip_line = ""
    return (
        template
        .replace("{{BASE_IMAGE}}", f"odoo:{major}.0")
        .replace("{{BREAK_SYS_PACKAGES}}", break_flag)
        .replace("{{EXTRA_PIP_LINE}}", extra_pip_line)
    )


def _render_compose(majors: list[int]) -> str:
    """Render docker-compose.yml for the given list of openupgrade majors."""
    header = _read_template("docker-compose.header.tmpl")
    service_tmpl = _read_template("docker-compose.service.tmpl")

    services = "".join(
        service_tmpl.replace("{{MAJOR}}", str(m)) for m in majors
    )

    volume_lines = ["  postgres_data:", "  pgadmin_data:"]
    for m in majors:
        volume_lines.append(f"  odoo{m}_data:")

    volumes_block = "volumes:\n" + "\n".join(volume_lines) + "\n"
    networks_block = "networks:\n  odoo_network:\n    driver: bridge\n"

    return header + services + volumes_block + networks_block


def _clone_openupgrade(major: int) -> str:
    """Clone (or skip) an OpenUpgrade branch into openupgrade_<major>/.

    Returns one of: 'created', 'exists'.
    """
    target = Path(f"openupgrade_{major}")
    branch = f"{major}.0"

    if target.exists():
        if (target / ".git").exists():
            return "exists"
        console.print(f"[red]{target}/ exists but is not a git repo.[/red]")
        sys.exit(1)

    console.print(f"[cyan]Cloning OpenUpgrade branch {branch} → {target}/[/cyan]")
    run_command([
        "git", "clone", "--depth", "1",
        "--branch", branch,
        OPENUPGRADE_REPO_URL,
        str(target),
    ])
    return "created"


def _update_openupgrade(major: int) -> tuple[str, str]:
    """Run `git pull --depth=1` inside openupgrade_<major>/.

    Returns (status, detail) where status is one of:
      'updated'     — pull succeeded and brought new commits
      'up-to-date'  — pull succeeded, nothing new
      'not-a-repo'  — folder exists but has no .git
      'error'       — git command failed (detail = stderr first line)
    """
    target = Path(f"openupgrade_{major}")
    if not (target / ".git").exists():
        return "not-a-repo", ""

    result = run_command(
        ["git", "-C", str(target), "pull", "--depth=1"],
        capture_output=True,
        check=False,
    )
    if result is None or result.returncode != 0:
        stderr = (result.stderr if result else "").strip().splitlines()
        return "error", stderr[0] if stderr else "git pull failed"

    stdout = (result.stdout or "").strip()
    if "Already up to date" in stdout or "Already up-to-date" in stdout:
        return "up-to-date", ""
    return "updated", stdout.splitlines()[0] if stdout else ""


def _write_dockerfile(major: int) -> str:
    """Render and write Dockerfile.openupgrade<major>. Returns 'wrote' or 'exists'."""
    path = Path(f"Dockerfile.openupgrade{major}")
    if path.exists():
        return "exists"
    path.write_text(_render_dockerfile(major))
    return "wrote"


def _write_compose(majors: list[int]) -> str:
    """Render and write docker-compose.yml. Returns 'wrote' or 'exists'."""
    path = Path("docker-compose.yml")
    if path.exists():
        return "exists"
    path.write_text(_render_compose(majors))
    return "wrote"


# ============================================
# COMMANDS
# ============================================

@app.command()
def setup():
    """
    Roll out the welcome mat: clone the OpenUpgrade branches you need,
    render their Dockerfiles, and stitch together a docker-compose.yml so
    `migrate` has everything it needs. No dump required yet — that comes later.

    `setup` is interactive — it will walk you through the version range
    before writing anything to disk.

    Examples:
        ou-runner setup
    """
    console.print()
    console.print(Panel.fit(
        "[bold cyan]Setup — what's about to happen[/bold cyan]\n\n"
        "We're going to bootstrap a sandbox so you can migrate an Odoo database\n"
        "across versions using OpenUpgrade. For each version between [yellow]from[/yellow]\n"
        "and [yellow]to[/yellow] we'll:\n\n"
        "  • Shallow-clone the matching [green]OpenUpgrade[/green] branch into\n"
        "    [dim]openupgrade_<version>/[/dim]\n"
        "  • Render a [green]Dockerfile.openupgrade<version>[/green]\n"
        "  • Stitch together a [green]docker-compose.yml[/green] with one service\n"
        "    per version (plus postgres + pgadmin)\n\n"
        "[bold]That's why we need to know your source and target versions:[/bold]\n"
        "  • [yellow]from[/yellow]: the Odoo version your existing dump was taken from\n"
        "  • [yellow]to[/yellow]:   the Odoo version you ultimately want to land on\n\n"
        "[dim]Supported versions: 14, 15, 16, 17, 18, 19[/dim]",
        title="🛠  Quick overview",
        border_style="cyan",
    ))
    console.print()

    from_version = _prompt_version("From which Odoo version?", default=14)
    to_version = _prompt_version("To which Odoo version?", default=19)

    version_list = list(VERSIONS.keys())
    from_idx = version_list.index(from_version)
    to_idx = version_list.index(to_version)
    if to_idx <= from_idx:
        console.print(
            f"[red]--to ({to_version}) must be a later version than --from ({from_version})[/red]"
        )
        sys.exit(1)

    # Targets that need OpenUpgrade assets: every version *after* `from`
    # up to and including `to`.
    target_majors = list(version_list[from_idx + 1 : to_idx + 1])

    console.print()
    console.print(Panel.fit(
        f"[bold cyan]Setting up your sandbox: {from_version} → {to_version}[/bold cyan]\n"
        f"[dim]Grab a coffee — the first clone is the slowest.[/dim]\n\n"
        f"Working directory: [yellow]{Path.cwd()}[/yellow]\n"
        f"OpenUpgrade folders to create: "
        f"[green]{', '.join(f'openupgrade_{m}' for m in target_majors)}[/green]\n"
        f"Dockerfiles to render: "
        f"[green]{', '.join(f'Dockerfile.openupgrade{m}' for m in target_majors)}[/green]\n"
        f"Compose file: [green]docker-compose.yml[/green] (postgres + pgadmin + "
        f"{len(target_majors)} openupgrade services)",
        title="🛠  Setup Plan",
        border_style="cyan",
    ))

    ensure_directories()
    console.print(f"[green]✓ Working dirs ready: dumps/, backups/, addons/, logs/[/green]")

    addons_results = ensure_custom_addons_dirs(target_majors)
    for major, status in addons_results:
        if status == "created":
            console.print(f"[green]✓ Created addons/custom_addons_{major}/[/green]")
        else:
            console.print(f"[dim]✓ addons/custom_addons_{major}/ already present[/dim]")

    summary_rows = []
    for major in target_majors:
        console.print()
        console.print(f"[bold cyan]── Version {major} ──[/bold cyan]")
        clone_status = _clone_openupgrade(major)
        if clone_status == "exists":
            console.print(f"[dim]✓ openupgrade_{major}/ already present[/dim]")
        else:
            console.print(f"[green]✓ Cloned openupgrade_{major}/[/green]")

        dockerfile_status = _write_dockerfile(major)
        if dockerfile_status == "wrote":
            console.print(f"[green]✓ Wrote Dockerfile.openupgrade{major}[/green]")
        else:
            console.print(f"[dim]✓ Dockerfile.openupgrade{major} already present[/dim]")

        summary_rows.append((major, clone_status, dockerfile_status))

    compose_status = _write_compose(target_majors)
    console.print()
    if compose_status == "wrote":
        console.print(f"[green]✓ Wrote docker-compose.yml[/green]")
    else:
        console.print(f"[dim]✓ docker-compose.yml already present[/dim]")

    # Summary table
    table = Table(title="Setup Summary", show_header=True, header_style="bold cyan")
    table.add_column("Version")
    table.add_column("openupgrade_X/")
    table.add_column(f"Dockerfile.openupgradeX")
    for major, clone_status, dockerfile_status in summary_rows:
        table.add_row(str(major), clone_status, dockerfile_status)

    console.print()
    console.print(table)

    console.print()
    console.print(Panel.fit(
        f"[bold green]All set — your sandbox is ready to roll. ✓[/bold green]\n\n"
        f"Three small things stand between you and a migration:\n"
        f"  1. Drop your source dump at [yellow]dumps/{version_to_filename(from_version)}[/yellow]\n"
        f"  2. Take the safe path, one hop at a time: "
        f"[cyan]ou-runner migrate --from {from_version} --to {version_list[from_idx + 1]}[/cyan]\n"
        f"  3. Or go all-in (and brave): "
        f"[cyan]ou-runner migrate --from {from_version} --to {to_version} --hold-my-drink[/cyan]",
        title="✓ Ready when you are",
        border_style="green",
    ))


@app.command()
def update():
    """
    Refresh every openupgrade_<version>/ folder by pulling the latest
    commits from its tracked branch on origin.

    Walks this script's directory, finds every openupgrade_<major>/ folder
    created by `setup`, and runs `git pull --depth=1` inside each one.
    Folders that aren't git repos are flagged in the summary; failed pulls
    don't stop the run.

    Example:
        ou-runner update
    """
    discovered: list[int] = []
    for path in Path.cwd().iterdir():
        if not path.is_dir():
            continue
        name = path.name
        if not name.startswith("openupgrade_"):
            continue
        suffix = name[len("openupgrade_"):]
        if suffix.isdigit():
            discovered.append(int(suffix))
    discovered.sort()

    if not discovered:
        console.print()
        console.print(Panel.fit(
            "[yellow]No openupgrade_<version>/ folders found.[/yellow]\n\n"
            "Run [cyan]ou-runner setup[/cyan] first to clone the\n"
            "OpenUpgrade branches you need.",
            title="Nothing to update",
            border_style="yellow",
        ))
        return

    console.print()
    console.print(Panel.fit(
        f"[bold cyan]Updating OpenUpgrade clones[/bold cyan]\n\n"
        f"Folders found: "
        f"[green]{', '.join(f'openupgrade_{m}' for m in discovered)}[/green]\n"
        f"Strategy: [yellow]git pull --depth=1[/yellow] on each folder's "
        f"current branch.",
        title="🔄  Update Plan",
        border_style="cyan",
    ))

    summary_rows: list[tuple[int, str, str]] = []
    for major in discovered:
        console.print()
        console.print(f"[bold cyan]── openupgrade_{major}/ ──[/bold cyan]")
        status, detail = _update_openupgrade(major)
        if status == "updated":
            console.print(
                f"[green]✓ Updated openupgrade_{major}/[/green]"
                + (f" [dim]({detail})[/dim]" if detail else "")
            )
        elif status == "up-to-date":
            console.print(f"[dim]✓ openupgrade_{major}/ already up to date[/dim]")
        elif status == "not-a-repo":
            console.print(
                f"[yellow]⚠ openupgrade_{major}/ is not a git repo — skipped[/yellow]"
            )
        else:
            console.print(
                f"[red]✗ openupgrade_{major}/ failed: {detail}[/red]"
            )
        summary_rows.append((major, status, detail))

    table = Table(title="Update Summary", show_header=True, header_style="bold cyan")
    table.add_column("Version")
    table.add_column("Status")
    table.add_column("Detail")
    for major, status, detail in summary_rows:
        table.add_row(str(major), status, detail or "")

    console.print()
    console.print(table)


# ============================================
# CLEAN COMMAND HELPERS
# ============================================

def _format_size(num_bytes: int) -> str:
    """Human-readable byte size."""
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024 or unit == "GB":
            return f"{num_bytes:.1f} {unit}" if unit != "B" else f"{num_bytes} B"
        num_bytes /= 1024
    return f"{num_bytes:.1f} GB"


def _dir_size(path: Path) -> int:
    """Sum the size of every file under path."""
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def _clean_targets() -> list[tuple[Path, str]]:
    """Enumerate every artifact `setup` (and `run`) produce. Returns a list
    of (path, kind) tuples where kind is 'file' or 'dir'. Only existing
    paths are included."""
    targets: list[tuple[Path, str]] = []

    # Globbed files / dirs
    for p in sorted(Path(".").glob("openupgrade_*")):
        if p.is_dir():
            targets.append((p, "dir"))
    for p in sorted(Path(".").glob("Dockerfile.openupgrade*")):
        if p.is_file():
            targets.append((p, "file"))

    # Fixed files
    for name in ("docker-compose.yml", "migration_times.json"):
        p = Path(name)
        if p.is_file():
            targets.append((p, "file"))

    # Per-version custom addons folders — listed individually so users
    # see them in the clean preview, then the parent addons/ sweeps any
    # stragglers (and the .gitkeep files).
    if ADDONS_DIR.is_dir():
        for p in sorted(ADDONS_DIR.glob("custom_addons_*")):
            if p.is_dir():
                targets.append((p, "dir"))

    # Fixed dirs
    for d in (DUMPS_DIR, BACKUPS_DIR, ADDONS_DIR, LOGS_DIR):
        if d.is_dir():
            targets.append((d, "dir"))

    return targets


@app.command()
def clean(
    auto_yes: bool = typer.Option(
        False, "-y", "--yes", help="Skip the confirmation prompt"
    ),
):
    """
    Sweep the sandbox: undo everything `setup` (and `migrate`) put on disk —
    openupgrade_*/ clones, Dockerfile.openupgrade*, docker-compose.yml,
    dumps/, backups/, addons/ (including every custom_addons_<major>/),
    logs/, migration_times.json.

    Note: only artifacts in the current working directory are touched.
    Docker containers and volumes are NOT — run `docker compose down -v`
    separately if you want those gone too.

    Example:
        ou-runner clean
        ou-runner clean -y
    """
    targets = _clean_targets()

    if not targets:
        console.print(
            "[green]Sandbox is already squeaky clean — nothing to do here. ✨[/green]"
        )
        return

    # Build the preview table
    preview = Table(title="Clean Plan", show_header=True, header_style="bold cyan")
    preview.add_column("Target")
    preview.add_column("Kind")
    preview.add_column("Size", justify="right")

    total_bytes = 0
    for path, kind in targets:
        if kind == "dir":
            size = _dir_size(path)
            label = f"{path}/"
        else:
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            label = str(path)
        total_bytes += size
        preview.add_row(label, kind, _format_size(size))

    console.print()
    console.print(preview)
    console.print(
        f"\n[bold]Total to reclaim:[/bold] [yellow]{_format_size(total_bytes)}[/yellow]"
    )
    console.print(
        "[dim]Note: dumps/ and backups/ are included — your DB snapshots will be lost.[/dim]"
    )

    if not auto_yes:
        if not Confirm.ask(
            "\n[bold]Sweep it all away?[/bold]", default=False
        ):
            console.print(
                "[yellow]All good — nothing changed.[/yellow]"
            )
            return

    # Perform deletions
    summary = Table(title="Cleanup Summary", show_header=True, header_style="bold cyan")
    summary.add_column("Target")
    summary.add_column("Status")

    any_failed = False
    for path, kind in targets:
        label = f"{path}/" if kind == "dir" else str(path)
        try:
            if not path.exists():
                summary.add_row(label, "[dim]skipped (missing)[/dim]")
                continue
            if kind == "dir":
                shutil.rmtree(path)
            else:
                path.unlink()
            summary.add_row(label, "[green]removed[/green]")
        except Exception as e:
            any_failed = True
            summary.add_row(label, f"[red]failed: {e}[/red]")

    console.print()
    console.print(summary)
    console.print()
    if not any_failed:
        console.print(Panel.fit(
            "[bold green]Sparkling clean. ✓[/bold green]\n\n"
            "Ready for a fresh start? "
            "[cyan]ou-runner setup[/cyan]\n"
            "[dim]Docker containers and volumes were left alone — "
            "run `docker compose down -v` if you want those gone too.[/dim]",
            title="🧹 All swept up",
            border_style="green",
        ))
    else:
        console.print(Panel.fit(
            "[bold yellow]Mostly clean, but a few stragglers stuck around.[/bold yellow]\n"
            "Check the failures above and try again, or remove them by hand.\n\n"
            "[dim]Docker containers and volumes were not touched.[/dim]",
            title="⚠ Partial cleanup",
            border_style="yellow",
        ))

    if any_failed:
        sys.exit(1)


@app.command()
def migrate(
    from_version: int = typer.Option(..., "--from", help="Source version (e.g., 14)"),
    to_version: int = typer.Option(..., "--to", help="Target version (e.g., 15)"),
    skip_backup: bool = typer.Option(False, "--skip-backup", help="Skip creating backup before migration"),
    auto_yes: bool = typer.Option(False, "-y", "--yes", help="Automatically answer yes to prompts"),
    i_am_brave: bool = typer.Option(False, "--hold-my-drink", help="Chain all intermediate migrations from --from to --to in one go"),
):
    """
    Migrate Odoo database from one version to another using OpenUpgrade.

    Example:
        ou-runner migrate --from 14 --to 15
        ou-runner migrate --from 14 --to 19 --hold-my-drink
    """

    if from_version not in VERSIONS:
        console.print(f"[red]Error: Unsupported source version '{from_version}'[/red]")
        console.print(f"Supported versions: {', '.join(str(v) for v in VERSIONS)}")
        sys.exit(1)

    if to_version not in VERSIONS:
        console.print(f"[red]Error: Unsupported target version '{to_version}'[/red]")
        console.print(f"Supported versions: {', '.join(str(v) for v in VERSIONS)}")
        sys.exit(1)

    version_list = list(VERSIONS.keys())
    from_idx = version_list.index(from_version)
    to_idx = version_list.index(to_version)

    if i_am_brave:
        if to_idx <= from_idx:
            console.print(f"[red]Error: --hold-my-drink needs --to to be a later version than --from[/red]")
            console.print(f"Got: --from {from_version} --to {to_version}")
            sys.exit(1)
    else:
        if to_idx != from_idx + 1:
            console.print(f"[red]Error: Can only migrate to the next version sequentially[/red]")
            console.print(f"To migrate from {from_version} to {to_version}, run:")
            for i in range(from_idx, to_idx):
                console.print(f"  ou-runner migrate --from {version_list[i]} --to {version_list[i+1]}")
            console.print(f"\n[dim]Or run them all at once with: ou-runner migrate --from {from_version} --to {to_version} --hold-my-drink[/dim]")
            sys.exit(1)

    if i_am_brave:
        path_arrows = " → ".join(str(v) for v in version_list[from_idx:to_idx + 1])
        console.print()
        console.print(Panel.fit(
            f"[bold cyan]I see you, a brave (and surely wise) soul,[/bold cyan]\n"
            f"about to run the migration from [yellow]{from_version}[/yellow] all the way to "
            f"[green]{to_version}[/green] in one go. 🚀\n\n"
            f"This may take a while — got a good cup of coffee ready? \n\n"
            f"[bold]Versions to traverse:[/bold] {path_arrows} ",
            title="💪 Brave Mode",
            border_style="cyan"
        ))
        if not Confirm.ask("\n[bold]Continue?[/bold]"):
            console.print("[yellow]Brave mode aborted — no shame in playing it safe.[/yellow]")
            sys.exit(0)

        global _BRAVE_MODE
        _BRAVE_MODE = True
        try:
            hops = [(version_list[i], version_list[i + 1]) for i in range(from_idx, to_idx)]
            for src, dst in hops:
                try:
                    _run_single_migration(
                        from_version=src,
                        to_version=dst,
                        skip_backup=skip_backup,
                        auto_yes=True,
                        version_list=version_list,
                        to_idx=version_list.index(dst),
                    )
                except Exception as e:
                    console.print(f"\n[red]Migration {src} → {dst} failed: {e}[/red]")
                    console.print(Panel.fit(
                        f"[yellow]It's good that you are brave and confident, but we found an error "
                        f"migrating from [bold]{src}[/bold] → [bold]{dst}[/bold].[/yellow]\n"
                        f"[yellow]I recommend you try from this version step by step. "
                        f"Divide and conquer.[/yellow]\n\n"
                        f"[dim]Resume with: ou-runner migrate --from {src} --to {dst}[/dim]",
                        title="⚠ Brave Mode Halted",
                        border_style="yellow"
                    ))
                    sys.exit(1)
                if (src, dst) != hops[-1]:
                    console.print("\n[dim]Pausing 5 seconds before next migration...[/dim]")
                    time.sleep(5)
        finally:
            _BRAVE_MODE = False

        console.print()
        console.print(Panel.fit(
            f"[bold green]💪 Brave mode complete![/bold green]\n\n"
            f"You went all the way: [yellow]{from_version}[/yellow] → [green]{to_version}[/green]\n"
            f"Your Odoo {to_version} instance is running at: "
            f"[cyan]http://localhost:{VERSIONS[to_version]['port']}[/cyan]\n",
            title="✓ Brave Mode Complete",
            border_style="green"
        ))
        console.print()
        times_table = display_migration_times_table()
        if times_table:
            console.print(times_table)
        return

    try:
        _run_single_migration(
            from_version=from_version,
            to_version=to_version,
            skip_backup=skip_backup,
            auto_yes=auto_yes,
            version_list=version_list,
            to_idx=to_idx,
        )
    except Exception as e:
        console.print(f"\n[red]Migration failed: {e}[/red]")
        console.print("[yellow]Check the logs above for details[/yellow]")
        sys.exit(1)


@app.command()
def logs(
    version: int = typer.Argument(..., help="Version to show logs for (e.g., 15)"),
    follow: bool = typer.Option(False, "-f", "--follow", help="Follow log output"),
):
    """
    Show logs for a specific Odoo version container.

    Example:
        ou-runner logs 15 -f
    """
    if version not in VERSIONS:
        console.print(f"[red]Error: Unknown version '{version}'[/red]")
        console.print(f"Available versions: {', '.join(str(v) for v in VERSIONS)}")
        sys.exit(1)

    container = VERSIONS[version]["container"]
    console.print(f"[cyan]Showing logs for {container}...[/cyan]\n")
    stream_logs(container, follow=follow)


@app.command()
def status():
    """
    Show the state of the migration itself: how far along the version chain
    you are, recorded migration times, dumps on disk, and backups grouped by
    source version.

    Docker container state is intentionally not shown — run `docker ps` for that.
    """
    version_list = list(VERSIONS.keys())

    # ── 1. Migration progress chain ─────────────────────────────────────
    existing = {v for v in version_list if (DUMPS_DIR / version_to_filename(v)).exists()}
    current = max(existing) if existing else None

    chain_parts = []
    for v in version_list:
        if v in existing and v != current:
            chain_parts.append(f"[green]{v} ✓[/green]")
        elif v == current:
            chain_parts.append(f"[bold yellow]{v} ●[/bold yellow]")
        else:
            chain_parts.append(f"[dim]{v} ○[/dim]")
    chain = " → ".join(chain_parts)

    console.print()
    console.print(Panel.fit(chain, title="Migration Progress", border_style="cyan"))

    if current is None:
        console.print(
            "[dim]No dumps found yet. Drop your source dump at "
            f"dumps/{version_to_filename(version_list[0])} to get started.[/dim]"
        )
    elif current == version_list[-1]:
        console.print(
            f"[bold green]You've reached v{current} — migration chain complete. 🎉[/bold green]"
        )
    else:
        next_v = version_list[version_list.index(current) + 1]
        console.print(
            f"[bold]Current:[/bold] v{current}   "
            f"[bold]Next:[/bold] [cyan]ou-runner migrate --from {current} --to {next_v}[/cyan]"
        )

    # ── 2. Migration times ──────────────────────────────────────────────
    console.print()
    times_table = display_migration_times_table()
    if times_table is not None:
        console.print(times_table)
    else:
        console.print("[dim]No migration times recorded yet.[/dim]")

    # ── 3. Dumps on disk ────────────────────────────────────────────────
    console.print()
    dumps_table = Table(title="Dumps", show_header=True, header_style="bold cyan")
    dumps_table.add_column("Version", style="cyan", no_wrap=True)
    dumps_table.add_column("File")
    dumps_table.add_column("Size", justify="right")
    dumps_table.add_column("Modified", style="dim")

    dumps_found = False
    for v in version_list:
        dump = DUMPS_DIR / version_to_filename(v)
        if not dump.exists():
            continue
        dumps_found = True
        stat = dump.stat()
        dumps_table.add_row(
            str(v),
            dump.name,
            _format_size(stat.st_size),
            time.strftime("%Y-%m-%d %H:%M", time.localtime(stat.st_mtime)),
        )

    if dumps_found:
        console.print(dumps_table)
    else:
        console.print("[dim]No dumps in dumps/.[/dim]")

    # ── 4. Backups grouped by version ───────────────────────────────────
    console.print()
    backups_table = Table(title="Backups", show_header=True, header_style="bold cyan")
    backups_table.add_column("Version", style="cyan", no_wrap=True)
    backups_table.add_column("Count", justify="right")
    backups_table.add_column("Total size", justify="right")
    backups_table.add_column("Latest", style="dim")

    # Pattern from create_backup(): odoo_<version>_backup_<YYYYMMDD_HHMMSS>.dump
    backup_pattern = re.compile(r"^odoo_(\d+)_backup_(\d{8}_\d{6})\.dump$")
    grouped: dict[int, list[tuple[Path, str, int]]] = {}
    if BACKUPS_DIR.exists():
        for path in BACKUPS_DIR.glob("odoo_*_backup_*.dump"):
            m = backup_pattern.match(path.name)
            if not m:
                continue
            version = int(m.group(1))
            timestamp = m.group(2)
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            grouped.setdefault(version, []).append((path, timestamp, size))

    if grouped:
        total_count = 0
        total_size = 0
        for v in sorted(grouped.keys()):
            entries = grouped[v]
            count = len(entries)
            size_sum = sum(s for _, _, s in entries)
            latest_ts = max(ts for _, ts, _ in entries)
            # Pretty-print 20260518_143205 → 2026-05-18 14:32
            latest_pretty = (
                f"{latest_ts[0:4]}-{latest_ts[4:6]}-{latest_ts[6:8]} "
                f"{latest_ts[9:11]}:{latest_ts[11:13]}"
            )
            backups_table.add_row(
                str(v), str(count), _format_size(size_sum), latest_pretty
            )
            total_count += count
            total_size += size_sum
        backups_table.add_row("", "", "", "", style="dim")
        backups_table.add_row(
            "[bold]Total",
            f"[bold]{total_count}",
            f"[bold]{_format_size(total_size)}",
            "",
            style="bold yellow",
        )
        console.print(backups_table)
    else:
        console.print("[dim]No backups in backups/.[/dim]")


@app.command()
def start(
    version: int = typer.Argument(14, help="Initial Odoo version (default: 14)"),
):
    """
    Start a fresh Odoo instance for the given version (useful for testing).

    Example:
        ou-runner start 14
    """
    if version not in VERSIONS:
        console.print(f"[red]Error: Unknown version '{version}'[/red]")
        sys.exit(1)

    console.print(f"[cyan]Starting Odoo {version}...[/cyan]\n")

    ensure_directories()
    ensure_postgres_running()
    stop_odoo_containers()
    create_database()
    start_odoo_container(version)

    port = VERSIONS[version]["port"]
    console.print(Panel.fit(
        f"[bold green]Odoo {version} started![/bold green]\n\n"
        f"Access it at: [cyan]http://localhost:{port}[/cyan]",
        title="✓ Started",
        border_style="green"
    ))


if __name__ == "__main__":
    app()
