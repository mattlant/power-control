import argparse
import asyncio
from pathlib import Path
import signal

from .broker_server import BrokerServer
from .config import load_broker_config
from .host_status import CpuFreqReader, HostStatusCollector, LogindReader, NvidiaReader


def install_signal_handlers(loop, server, stop_event):
    """Route lifecycle control signals without blocking the signal callback."""

    def reload_configuration():
        asyncio.create_task(server.runtime.reload())

    def reconcile_resume():
        asyncio.create_task(server.runtime.reconcile_resume())

    loop.add_signal_handler(signal.SIGHUP, reload_configuration)
    loop.add_signal_handler(signal.SIGUSR1, reconcile_resume)
    loop.add_signal_handler(signal.SIGTERM, stop_event.set)
    loop.add_signal_handler(signal.SIGINT, stop_event.set)


async def run(path):
    config_path = Path(path)
    config = load_broker_config(config_path)
    collector = HostStatusCollector(
        CpuFreqReader(),
        NvidiaReader(None, config.gpu_index, config.query_timeout_seconds),
        LogindReader(),
    )
    server = BrokerServer(config, collector)
    server.runtime.config_path = config_path
    await server.start()
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    install_signal_handlers(loop, server, stop_event)
    try:
        await stop_event.wait()
    finally:
        for signum in (signal.SIGHUP, signal.SIGUSR1, signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(signum)
        await server.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    asyncio.run(run(args.config))
