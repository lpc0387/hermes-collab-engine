"""Configuration Center — centralized configuration management module.

Provides namespace-based configuration storage with full version history,
rollback support, and a RESTful HTTP API.  Designed to plug into the
existing Hermes Collab Engine dashboard server.

Quick start::

    from hermes_collab_engine.config_center import ConfigCenterAPI, ConfigStore

    store = ConfigStore("/path/to/db.sqlite3")
    api = ConfigCenterAPI(store)
    # Then install into your server:
    #   install_routes(server, api)
"""

from .schema import Namespace, ConfigItem, ConfigVersion
from .store import ConfigStore
from .api import ConfigCenterAPI, ConfigCenterHandlerMixin, install_routes

__all__ = [
    "Namespace",
    "ConfigItem",
    "ConfigVersion",
    "ConfigStore",
    "ConfigCenterAPI",
    "ConfigCenterHandlerMixin",
    "install_routes",
]
