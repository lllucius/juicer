"""Juicer CLI — Click-based command-line interface.

Entry point: ``juicer`` (or ``python -m juicer``).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import click

from juicer import __version__
from juicer.config import ConfigStore, TomlStore, dump_config_toml

logger = logging.getLogger("juicer")


def _setup_logging(verbose: bool) -> None:
    """Configure process-wide logging for CLI commands."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )


# ──────────────────────────────────────────────────────────────────────
# Root group
# ──────────────────────────────────────────────────────────────────────


@click.group()
@click.version_option(version=__version__, prog_name="juicer")
@click.option("-v", "--verbose", is_flag=True, help="Enable debug logging")
@click.pass_context
def main(ctx: click.Context, verbose: bool) -> None:
    """Juicer — Furman F1500-UPS E controller."""
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    _setup_logging(verbose)


# ──────────────────────────────────────────────────────────────────────
# juicer ports
# ──────────────────────────────────────────────────────────────────────


@main.command()
def ports() -> None:
    """List available serial ports."""
    try:
        from serial.tools.list_ports import comports

        found = list(comports())
        if not found:
            click.echo("No serial ports found.")
            return
        for port in found:
            click.echo(f"{port.device}\t{port.description}")
    except ImportError:
        raise click.ClickException("pyserial is required: pip install pyserial") from None


# ──────────────────────────────────────────────────────────────────────
# juicer status
# ──────────────────────────────────────────────────────────────────────


@main.command()
@click.option("--port", required=True, help="Serial port (e.g. COM3)")
def status(port: str) -> None:
    """Query device status (outlet states, power, battery)."""
    from juicer.protocol import JuicerClient, SerialTransport

    transport = SerialTransport(port=port)
    try:
        transport.open()
        client = JuicerClient(transport)

        # Outlet status
        outlets = client.query_outlet_status()
        for bank_num in sorted(outlets.banks):
            click.echo(f"Bank {bank_num}: {outlets.banks[bank_num].value}")

        # Power status
        try:
            pwr = client.query_power_status()
            click.echo(f"Power: {pwr.status.value}")
        except Exception as exc:
            click.echo(f"Power: (unavailable — {exc})", err=True)

        # Battery
        try:
            bat = client.query_battery_status()
            click.echo(f"Battery: {bat.level}%")
        except Exception as exc:
            click.echo(f"Battery: (unavailable — {exc})", err=True)

    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        transport.close()


# ──────────────────────────────────────────────────────────────────────
# juicer all-on / all-off
# ──────────────────────────────────────────────────────────────────────


@main.command("all-on")
@click.option("--port", required=True, help="Serial port")
def all_on(port: str) -> None:
    """Turn on all outlet banks."""
    from juicer.protocol import JuicerClient, SerialTransport

    transport = SerialTransport(port=port)
    try:
        transport.open()
        client = JuicerClient(transport)
        responses = client.all_on()
        for r in responses:
            click.echo(str(r))
        click.echo("All banks ON.")
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        transport.close()


@main.command("all-off")
@click.option("--port", required=True, help="Serial port")
def all_off(port: str) -> None:
    """Turn off all outlet banks."""
    from juicer.protocol import JuicerClient, SerialTransport

    transport = SerialTransport(port=port)
    try:
        transport.open()
        client = JuicerClient(transport)
        responses = client.all_off()
        for r in responses:
            click.echo(str(r))
        click.echo("All banks OFF.")
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        transport.close()


# ──────────────────────────────────────────────────────────────────────
# juicer switch
# ──────────────────────────────────────────────────────────────────────


@main.command()
@click.option("--port", required=True, help="Serial port")
@click.option("--bank", required=True, type=click.IntRange(1, 4), help="Bank number (1–4)")
@click.option("--state", required=True, type=click.Choice(["on", "off"], case_sensitive=False))
def switch(port: str, bank: int, state: str) -> None:
    """Switch a specific outlet bank on or off."""
    from juicer.protocol import JuicerClient, SerialTransport

    transport = SerialTransport(port=port)
    try:
        transport.open()
        client = JuicerClient(transport)
        responses = client.switch(bank, state.upper())
        for r in responses:
            click.echo(str(r))
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        transport.close()


# ──────────────────────────────────────────────────────────────────────
# juicer boot / shutdown
# ──────────────────────────────────────────────────────────────────────


def _get_config_store() -> ConfigStore:
    """Get the config store."""
    return TomlStore()


@main.command()
def boot() -> None:
    """Run the boot sequence from configuration."""
    from juicer.protocol import JuicerClient, SerialTransport
    from juicer.sequence import run_boot

    try:
        store = _get_config_store()
        config = store.load()

        transport = SerialTransport(port=config.port)
        transport.open()
        try:
            client = JuicerClient(transport)
            run_boot(config, client)
            click.echo("Boot sequence complete.")
        finally:
            transport.close()
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc


@main.command()
def shutdown() -> None:
    """Run the shutdown sequence from configuration."""
    from juicer.protocol import JuicerClient, SerialTransport
    from juicer.sequence import run_shutdown

    try:
        store = _get_config_store()
        config = store.load()

        transport = SerialTransport(port=config.port)
        transport.open()
        try:
            client = JuicerClient(transport)
            run_shutdown(config, client)
            click.echo("Shutdown sequence complete.")
        finally:
            transport.close()
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc


# ──────────────────────────────────────────────────────────────────────
# juicer config (subgroup)
# ──────────────────────────────────────────────────────────────────────


@main.group()
def config() -> None:
    """Manage Juicer configuration."""


@config.command("show")
def config_show() -> None:
    """Display the current configuration."""
    try:
        store = _get_config_store()
        cfg = store.load()
        click.echo(dump_config_toml(cfg), nl=False)
    except FileNotFoundError as exc:
        raise click.ClickException("No configuration found.") from exc
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc


@config.command("export")
@click.argument("file", type=click.Path())
def config_export(file: str) -> None:
    """Export configuration to a TOML file."""
    from juicer.config import TomlStore

    try:
        store = _get_config_store()
        cfg = store.load()
        export_store = TomlStore(file)
        export_store.save(cfg)
        click.echo(f"Configuration exported to {file}")
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc


@config.command("import")
@click.argument("file", type=click.Path(exists=True))
def config_import(file: str) -> None:
    """Import configuration from a TOML file."""
    from juicer.config import TomlStore

    try:
        import_store = TomlStore(file)
        cfg = import_store.load()
        target_store = _get_config_store()
        target_store.save(cfg)
        click.echo(f"Configuration imported from {file}")
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc


# ──────────────────────────────────────────────────────────────────────
# juicer service (subgroup)
# ──────────────────────────────────────────────────────────────────────


@main.group()
def service() -> None:
    """Manage the Juicer Windows service."""


@service.command("install")
def service_install() -> None:
    """Install the Juicer Windows service."""
    try:
        from juicer.service import install_service

        install_service()
        click.echo("Service installed.")
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc


@service.command("uninstall")
def service_uninstall() -> None:
    """Uninstall the Juicer Windows service."""
    try:
        from juicer.service import uninstall_service

        uninstall_service()
        click.echo("Service uninstalled.")
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc


@service.command("start")
def service_start() -> None:
    """Start the Juicer service."""
    try:
        from juicer.service import start_service

        start_service()
        click.echo("Service started.")
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc


@service.command("stop")
def service_stop() -> None:
    """Stop the Juicer service."""
    try:
        from juicer.service import stop_service

        stop_service()
        click.echo("Service stopped.")
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc


@service.command("status")
def service_status_cmd() -> None:
    """Query the Juicer service status."""
    try:
        from juicer.service import service_status

        st = service_status()
        click.echo(f"Service status: {st}")
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc


if __name__ == "__main__":
    main()
