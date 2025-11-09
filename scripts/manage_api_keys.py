#!/usr/bin/env python3
"""CLI tool for managing API keys."""

import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse
from datetime import datetime, timezone
from typing import Optional

from rich.console import Console
from rich.table import Table
from rich import box

from app.auth import APIKey, create_api_key, revoke_api_key
from app.store import build_store
from sqlmodel import select


console = Console()


def cmd_create(
    name: str,
    rate_limit: Optional[int] = None,
    owner: Optional[str] = None,
) -> None:
    """Create a new API key."""
    store = build_store()

    with store.session() as session:
        plain_key, api_key = create_api_key(session, name, rate_limit, owner)

        console.print("\n✅ [green]API Key created successfully![/green]\n")

        table = Table(box=box.ROUNDED, show_header=False)
        table.add_column("Field", style="cyan")
        table.add_column("Value", style="yellow")

        table.add_row("Key ID", str(api_key.id))
        table.add_row("Name", api_key.name)
        table.add_row("Prefix", api_key.key_prefix)
        table.add_row("Rate Limit", str(api_key.rate_limit) if api_key.rate_limit else "Unlimited")
        table.add_row("Owner", api_key.owner or "None")
        table.add_row("Created", api_key.created_at.isoformat())

        console.print(table)

        console.print("\n🔑 [bold red]API Key (save this, it won't be shown again):[/bold red]")
        console.print(f"\n  [bold white]{plain_key}[/bold white]\n")
        console.print(f"Use this key in requests with header: [cyan]X-API-Key: {plain_key}[/cyan]\n")


def cmd_list() -> None:
    """List all API keys."""
    store = build_store()

    with store.session() as session:
        statement = select(APIKey).order_by(APIKey.created_at.desc())
        api_keys = session.exec(statement).all()

        if not api_keys:
            console.print("\n[yellow]No API keys found.[/yellow]\n")
            return

        table = Table(title="API Keys", box=box.ROUNDED)
        table.add_column("ID", style="cyan")
        table.add_column("Name", style="yellow")
        table.add_column("Prefix", style="dim")
        table.add_column("Active", style="green")
        table.add_column("Rate Limit", style="magenta")
        table.add_column("Owner", style="blue")
        table.add_column("Created", style="dim")
        table.add_column("Last Used", style="dim")

        for key in api_keys:
            table.add_row(
                str(key.id),
                key.name,
                key.key_prefix,
                "✅" if key.is_active else "❌",
                str(key.rate_limit) if key.rate_limit else "∞",
                key.owner or "-",
                key.created_at.strftime("%Y-%m-%d %H:%M"),
                key.last_used_at.strftime("%Y-%m-%d %H:%M") if key.last_used_at else "Never",
            )

        console.print()
        console.print(table)
        console.print()


def cmd_revoke(key_id: int) -> None:
    """Revoke an API key by ID."""
    store = build_store()

    with store.session() as session:
        success = revoke_api_key(session, key_id)

        if success:
            console.print(f"\n✅ [green]API key {key_id} has been revoked.[/green]\n")
        else:
            console.print(f"\n❌ [red]API key {key_id} not found.[/red]\n")
            sys.exit(1)


def cmd_show(key_id: int) -> None:
    """Show details of a specific API key."""
    store = build_store()

    with store.session() as session:
        statement = select(APIKey).where(APIKey.id == key_id)
        api_key = session.exec(statement).first()

        if not api_key:
            console.print(f"\n❌ [red]API key {key_id} not found.[/red]\n")
            sys.exit(1)

        table = Table(title=f"API Key {key_id}", box=box.ROUNDED, show_header=False)
        table.add_column("Field", style="cyan")
        table.add_column("Value", style="yellow")

        table.add_row("ID", str(api_key.id))
        table.add_row("Name", api_key.name)
        table.add_row("Prefix", api_key.key_prefix)
        table.add_row("Active", "✅ Yes" if api_key.is_active else "❌ No")
        table.add_row("Rate Limit", str(api_key.rate_limit) if api_key.rate_limit else "Unlimited")
        table.add_row("Owner", api_key.owner or "None")
        table.add_row("Created", api_key.created_at.isoformat())
        table.add_row("Last Used", api_key.last_used_at.isoformat() if api_key.last_used_at else "Never")

        console.print()
        console.print(table)
        console.print()


def main():
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Manage API keys for Markdown Web Browser",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Create a new API key
  python scripts/manage_api_keys.py create "My Application"

  # Create with rate limit
  python scripts/manage_api_keys.py create "My App" --rate-limit 100

  # List all keys
  python scripts/manage_api_keys.py list

  # Show details of a key
  python scripts/manage_api_keys.py show 1

  # Revoke a key
  python scripts/manage_api_keys.py revoke 1
        """,
    )

    subparsers = parser.add_subparsers(dest="command", required=True, help="Command to execute")

    # Create command
    create_parser = subparsers.add_parser("create", help="Create a new API key")
    create_parser.add_argument("name", help="Name for the API key")
    create_parser.add_argument(
        "--rate-limit",
        type=int,
        help="Rate limit in requests per minute (default: unlimited)",
    )
    create_parser.add_argument("--owner", help="Owner identifier for the key")

    # List command
    subparsers.add_parser("list", help="List all API keys")

    # Show command
    show_parser = subparsers.add_parser("show", help="Show details of an API key")
    show_parser.add_argument("key_id", type=int, help="ID of the API key")

    # Revoke command
    revoke_parser = subparsers.add_parser("revoke", help="Revoke an API key")
    revoke_parser.add_argument("key_id", type=int, help="ID of the API key to revoke")

    args = parser.parse_args()

    try:
        if args.command == "create":
            cmd_create(args.name, args.rate_limit, args.owner)
        elif args.command == "list":
            cmd_list()
        elif args.command == "show":
            cmd_show(args.key_id)
        elif args.command == "revoke":
            cmd_revoke(args.key_id)
    except Exception as e:
        console.print(f"\n❌ [red]Error: {e}[/red]\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
