"""Azure Service Health notifications for Slack.

Azure Monitor posts Service Health alerts to ``POST /api/service-health``;
the app routes each incident to a Slack channel and keeps a single message
per incident up to date.
"""
import logging
import os
import threading

from dotenv import load_dotenv
from flask import Flask
from slack_sdk import WebClient

from service_health.routes import create_service_health_blueprint
from service_health.runtime import create_service_health_runtime
from service_health.telemetry import configure_telemetry

load_dotenv(dotenv_path=".env")

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO))
logging.getLogger("azure").setLevel(logging.WARNING)

configure_telemetry()

slack_client = WebClient(os.environ["SLACK_BOT_TOKEN"])
web_app = Flask(__name__)

_service_health_runtime = None
_service_health_runtime_lock = threading.Lock()


def get_service_health_runtime():
    global _service_health_runtime
    if _service_health_runtime is None:
        with _service_health_runtime_lock:
            if _service_health_runtime is None:
                _service_health_runtime = create_service_health_runtime(slack_client)
    return _service_health_runtime


web_app.register_blueprint(
    create_service_health_blueprint(get_service_health_runtime))


if __name__ == "__main__":
    web_app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
