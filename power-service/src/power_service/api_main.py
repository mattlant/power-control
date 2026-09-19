import argparse, ssl
from aiohttp import web
from .config import load_api_config
from .auth import TokenAuthenticator
from .broker_client import StatusBrokerClient
from .api import ApiApplication


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--config', required=True)
    a = p.parse_args()
    c = load_api_config(__import__('pathlib').Path(a.config))
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(c.tls_cert, c.tls_key)
    web.run_app(ApiApplication.create(c, TokenAuthenticator(c.credentials),
                                      StatusBrokerClient(c.broker_socket, c.request_timeout_seconds)),
                host=str(c.listen_host), port=c.listen_port, ssl_context=ctx)
