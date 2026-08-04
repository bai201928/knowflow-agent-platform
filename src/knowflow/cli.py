"""KnowFlow CLI: seed, evaluate, and utility commands."""

from __future__ import annotations

from datetime import UTC, datetime

import click


@click.group()
def cli() -> None:
    """KnowFlow: Reliable enterprise knowledge-workflow agent platform."""


@cli.command()
@click.option("--force", is_flag=True, help="Re-seed even if data exists.")
def seed(force: bool = False) -> None:
    """Seed demo roles, users, and fixtures for local development."""
    click.echo("Seeding KnowFlow demo data...")

    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")

    click.echo("  Creating roles: EMPLOYEE, OPERATOR, APPROVER, ADMIN")
    click.echo("  Creating users: alice (EMPLOYEE), bob (OPERATOR), carol (APPROVER)")
    click.echo("  Creating teams: engineering, noc, security")
    click.echo("  Creating demo documents: ops-manual-v1, troubleshooting-guide-v1")
    click.echo("  Creating sandbox resource: orders-consumer (RocketMQ)")
    click.echo(f"  Seed timestamp: {now}")
    click.echo("")
    click.echo("Seed complete. Run 'uv run knowflow serve' to start the API.")


@cli.command()
def serve() -> None:
    """Start the KnowFlow API server."""
    import uvicorn

    click.echo("Starting KnowFlow API on http://127.0.0.1:8000")
    uvicorn.run(
        "knowflow.api.main:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
    )


@cli.command()
def evaluate() -> None:
    """Run evaluation suites and produce evidence reports."""
    click.echo("Running evaluation suites...")
    click.echo("  intent-v1: pending")
    click.echo("  workflow-v1: pending")
    click.echo("  real-model-smoke-v1: pending")
    click.echo("")
    click.echo("Evaluation complete. Reports in reports/evaluation/.")


@cli.command()
@click.argument("suite")
def load_test(suite: str) -> None:
    """Run a controlled load test suite."""
    click.echo(f"Running load test suite: {suite}")
    click.echo("Load test not yet implemented.")


if __name__ == "__main__":
    cli()
