"""Juicer CLI — Click-based command-line interface.

Entry point: ``juicer`` (or ``python -m juicer``).
"""

from __future__ import annotations

import json
import logging
import sys

import click

from juicer import __version__

logger = logging.getLogger("juicer")


def _setup_logging(verbose: bool) -> None:
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
        from serial.tools.list_ports import comports  # type: ignore[import-untyped]

        found = list(comports())
        if not found:
            click.echo("No serial ports found.")
            return
        for port in found:
            click.echo(f"{port.device}\t{port.description}")
    except ImportError:
        click.echo("pyserial is required: pip install pyserial", err=True)
        raise SystemExit(1)


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
        except Exception:
            pass

        # Battery
        try:
            bat = client.query_battery_status()
            click.echo(f"Battery: {bat.level}%")
        except Exception:
            pass

    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)
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
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)
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
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)
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
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)
    finally:
        transport.close()


# ──────────────────────────────────────────────────────────────────────
# juicer boot / shutdown
# ──────────────────────────────────────────────────────────────────────


def _get_config_store() -> "ConfigStore":  # type: ignore[name-defined]  # noqa: F821
    """Get the appropriate config store for the current platform."""
    import platform as plat

    from juicer.config import JsonStore, WindowsRegistryStore

    if plat.system() == "Windows":
        return WindowsRegistryStore()
    # Fallback to JSON in home dir
    from pathlib import Path

    json_path = Path.home() / ".juicer" / "config.json"
    return JsonStore(json_path)


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
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)


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
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)


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
        click.echo(cfg.model_dump_json(indent=2))
    except FileNotFoundError:
        click.echo("No configuration found.", err=True)
        raise SystemExit(1)
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)


@config.command("export")
@click.argument("file", type=click.Path())
def config_export(file: str) -> None:
    """Export configuration to a JSON file."""
    from juicer.config import JsonStore

    try:
        store = _get_config_store()
        cfg = store.load()
        export_store = JsonStore(file)
        export_store.save(cfg)
        click.echo(f"Configuration exported to {file}")
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)


@config.command("import")
@click.argument("file", type=click.Path(exists=True))
def config_import(file: str) -> None:
    """Import configuration from a JSON file."""
    from juicer.config import JsonStore

    try:
        import_store = JsonStore(file)
        cfg = import_store.load()
        target_store = _get_config_store()
        target_store.save(cfg)
        click.echo(f"Configuration imported from {file}")
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)


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
    except OSError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)


@service.command("uninstall")
def service_uninstall() -> None:
    """Uninstall the Juicer Windows service."""
    try:
        from juicer.service import uninstall_service

        uninstall_service()
        click.echo("Service uninstalled.")
    except OSError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)


@service.command("start")
def service_start() -> None:
    """Start the Juicer service."""
    try:
        from juicer.service import start_service

        start_service()
        click.echo("Service started.")
    except OSError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)


@service.command("stop")
def service_stop() -> None:
    """Stop the Juicer service."""
    try:
        from juicer.service import stop_service

        stop_service()
        click.echo("Service stopped.")
    except OSError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)


@service.command("status")
def service_status_cmd() -> None:
    """Query the Juicer service status."""
    try:
        from juicer.service import service_status

        st = service_status()
        click.echo(f"Service status: {st}")
    except OSError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
