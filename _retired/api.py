"""Configuration Center — HTTP API handlers.

Provides JSON REST endpoints for managing namespaces, config items,
and version history.  Designed to be mounted into the existing
``DashboardServer`` via ``install_into(server)``.
"""
from __future__ import annotations

import json
from typing import Any, Callable

from .store import ConfigStore
from .schema import Namespace, ConfigItem


# ---------------------------------------------------------------------------
# JSON helpers (mirror server._json pattern)
# ---------------------------------------------------------------------------

def _json_response(data: Any, status: int = 200) -> tuple[int, dict[str, Any]]:
    return (status, data)


def _error(msg: str, status: int = 400) -> tuple[int, dict[str, str]]:
    return (status, {"error": msg})


def _parse_body(body_str: str) -> dict[str, Any]:
    if not body_str or not body_str.strip():
        return {}
    try:
        return json.loads(body_str)
    except json.JSONDecodeError:
        raise ValueError("Invalid JSON body")


# ---------------------------------------------------------------------------
# Route table entry
# ---------------------------------------------------------------------------

RouteHandler = Callable[[dict[str, Any], dict[str, Any]], tuple[int, dict[str, Any]]]
"""(url_params, body_dict) -> (status_code, response_dict)"""


class ConfigCenterAPI:
    """Stateless API handlers wrapping a ConfigStore instance."""

    def __init__(self, store: ConfigStore):
        self.store = store

    # ---- Namespace handlers ------------------------------------------------

    def list_namespaces(self, _params: dict[str, Any], _body: dict[str, Any]) -> tuple[int, Any]:
        env = _params.get("environment")
        namespaces = self.store.list_namespaces(environment=env)
        return _json_response([ns.to_dict() for ns in namespaces])

    def create_namespace(self, _params: dict[str, Any], body: dict[str, Any]) -> tuple[int, Any]:
        name = (body.get("name") or "").strip()
        if not name:
            return _error("name is required")
        ns = self.store.create_namespace(
            name=name,
            description=str(body.get("description", "")),
            environment=str(body.get("environment", "dev")),
        )
        return _json_response(ns.to_dict(), 201)

    def get_namespace(self, params: dict[str, Any], _body: dict[str, Any]) -> tuple[int, Any]:
        ns_id = params.get("namespace_id", "")
        try:
            ns = self.store.get_namespace(ns_id)
            return _json_response(ns.to_dict())
        except KeyError:
            return _error("Namespace not found", 404)

    def update_namespace(self, params: dict[str, Any], body: dict[str, Any]) -> tuple[int, Any]:
        ns_id = params.get("namespace_id", "")
        try:
            ns = self.store.update_namespace(
                ns_id,
                name=body.get("name"),
                description=body.get("description"),
                environment=body.get("environment"),
            )
            return _json_response(ns.to_dict())
        except KeyError:
            return _error("Namespace not found", 404)

    def delete_namespace(self, params: dict[str, Any], _body: dict[str, Any]) -> tuple[int, Any]:
        ns_id = params.get("namespace_id", "")
        try:
            self.store.delete_namespace(ns_id)
            return _json_response({"deleted": ns_id})
        except KeyError:
            return _error("Namespace not found", 404)

    # ---- Config item handlers ----------------------------------------------

    def list_configs(self, params: dict[str, Any], _body: dict[str, Any]) -> tuple[int, Any]:
        ns_id = params.get("namespace_id", "")
        try:
            configs = self.store.list_configs(ns_id)
            return _json_response([c.to_dict() for c in configs])
        except KeyError:
            return _error("Namespace not found", 404)

    def create_config(self, params: dict[str, Any], body: dict[str, Any]) -> tuple[int, Any]:
        ns_id = params.get("namespace_id", "")
        key = (body.get("key") or "").strip()
        if not key:
            return _error("key is required")
        # Check uniqueness
        existing = self.store.get_config_by_key(ns_id, key)
        if existing:
            return _error(f"Config key already exists: {key}", 409)

        try:
            cfg = self.store.create_config(
                namespace_id=ns_id,
                key=key,
                value=str(body.get("value", "")),
                value_type=str(body.get("value_type", "string")),
                description=str(body.get("description", "")),
                tags=body.get("tags", "[]"),
                changed_by=str(body.get("changed_by", "system")),
                change_comment=str(body.get("change_comment", "")),
            )
            return _json_response(cfg.to_dict(), 201)
        except KeyError:
            return _error("Namespace not found", 404)
        except ValueError as e:
            return _error(str(e))

    def get_config(self, params: dict[str, Any], _body: dict[str, Any]) -> tuple[int, Any]:
        cfg_id = params.get("config_id", "")
        try:
            cfg = self.store.get_config(cfg_id)
            return _json_response(cfg.to_dict())
        except KeyError:
            return _error("Config not found", 404)

    def update_config(self, params: dict[str, Any], body: dict[str, Any]) -> tuple[int, Any]:
        cfg_id = params.get("config_id", "")
        try:
            cfg = self.store.update_config(
                cfg_id,
                value=body.get("value"),
                value_type=body.get("value_type"),
                description=body.get("description"),
                tags=body.get("tags"),
                changed_by=str(body.get("changed_by", "system")),
                change_comment=str(body.get("change_comment", "")),
            )
            return _json_response(cfg.to_dict())
        except KeyError:
            return _error("Config not found", 404)

    def delete_config(self, params: dict[str, Any], _body: dict[str, Any]) -> tuple[int, Any]:
        cfg_id = params.get("config_id", "")
        try:
            self.store.delete_config(cfg_id)
            return _json_response({"deleted": cfg_id})
        except KeyError:
            return _error("Config not found", 404)

    # ---- Version / history handlers ---------------------------------------

    def get_config_history(self, params: dict[str, Any], _body: dict[str, Any]) -> tuple[int, Any]:
        cfg_id = params.get("config_id", "")
        try:
            versions = self.store.get_config_history(cfg_id)
            return _json_response([v.to_dict() for v in versions])
        except KeyError:
            return _error("Config not found", 404)

    def rollback_config(self, params: dict[str, Any], body: dict[str, Any]) -> tuple[int, Any]:
        cfg_id = params.get("config_id", "")
        target_version = body.get("target_version")
        if target_version is None:
            return _error("target_version is required")
        try:
            cfg = self.store.rollback_config(
                cfg_id,
                target_version=int(target_version),
                changed_by=str(body.get("changed_by", "system")),
                change_comment=str(body.get("change_comment", "")),
            )
            return _json_response(cfg.to_dict())
        except KeyError as e:
            return _error(str(e), 404)
        except ValueError:
            return _error("Invalid target_version")

    # ---- Search -----------------------------------------------------------

    def search_configs(self, params: dict[str, Any], _body: dict[str, Any]) -> tuple[int, Any]:
        query = params.get("query", "")
        if not query:
            return _error("query parameter is required")
        ns_id = params.get("namespace_id")
        configs = self.store.search_configs(query, namespace_id=ns_id)
        return _json_response([c.to_dict() for c in configs])

    # ---- Overview ---------------------------------------------------------

    def overview(self, _params: dict[str, Any], _body: dict[str, Any]) -> tuple[int, Any]:
        return _json_response(self.store.overview())

    # ---- All configs ------------------------------------------------------

    def list_all_configs(self, params: dict[str, Any], _body: dict[str, Any]) -> tuple[int, Any]:
        ns_id = params.get("namespace_id")
        env = params.get("environment")
        configs = self.store.list_all_configs(namespace_id=ns_id, environment=env)
        return _json_response([c.to_dict() for c in configs])


# ---------------------------------------------------------------------------
# Route installation helper (for use in existing server.py)
# ---------------------------------------------------------------------------

def install_routes(server_instance: Any, api: ConfigCenterAPI) -> None:
    """Patch route maps into an existing DashboardServer instance.

    The server's Handler class must define class-level dicts ``GET_ROUTES``
    and ``POST_ROUTES`` that map URL path regexps to handler methods.

    Usage::

        api = ConfigCenterAPI(ConfigStore(db_path))
        install_routes(server, api)
    """
    # --- GET routes ---
    get_routes: dict[str, tuple[str, RouteHandler]] = {
        "/api/config-center/overview": ("", api.overview),
        "/api/config-center/namespaces": ("environment", api.list_namespaces),
        r"/api/config-center/namespaces/(?P<namespace_id>[^/]+)/configs": ("namespace_id", api.list_configs),
        r"/api/config-center/namespaces/(?P<namespace_id>[^/]+)": ("namespace_id", api.get_namespace),
        r"/api/config-center/configs/(?P<config_id>[^/]+)/history": ("config_id", api.get_config_history),
        r"/api/config-center/configs/(?P<config_id>[^/]+)": ("config_id", api.get_config),
        "/api/config-center/configs": ("", api.list_all_configs),
        "/api/config-center/search": ("query,namespace_id", api.search_configs),
    }

    # --- POST routes ---
    post_routes: dict[str, tuple[str, RouteHandler]] = {
        "/api/config-center/namespaces": ("", api.create_namespace),
        r"/api/config-center/namespaces/(?P<namespace_id>[^/]+)": ("namespace_id", api.update_namespace),
        r"/api/config-center/namespaces/(?P<namespace_id>[^/]+)/configs": ("namespace_id", api.create_config),
        r"/api/config-center/configs/(?P<config_id>[^/]+)": ("config_id", api.update_config),
        r"/api/config-center/configs/(?P<config_id>[^/]+)/rollback": ("config_id", api.rollback_config),
    }

    # --- DELETE routes ---
    delete_routes: dict[str, tuple[str, RouteHandler]] = {
        r"/api/config-center/namespaces/(?P<namespace_id>[^/]+)": ("namespace_id", api.delete_namespace),
        r"/api/config-center/configs/(?P<config_id>[^/]+)": ("config_id", api.delete_config),
    }

    if not hasattr(server_instance, "_config_center_get_routes"):
        server_instance._config_center_get_routes = get_routes
        server_instance._config_center_post_routes = post_routes
        server_instance._config_center_delete_routes = delete_routes
        server_instance._config_center_api = api


# ---------------------------------------------------------------------------
# Direct server handler mixin (for the existing http.server pattern)
# ---------------------------------------------------------------------------

class ConfigCenterHandlerMixin:
    """Mixin that adds config-center route handling to a BaseHTTPRequestHandler.

    Usage::

        class MyHandler(ConfigCenterHandlerMixin, BaseHTTPRequestHandler):
            ...

        # Or simply copy the dispatch logic into your do_GET/do_POST.
    """

    def _handle_config_center(self, method: str, path: str, params: dict,
                                body: dict) -> tuple[int, dict] | None:
        server = getattr(self, "server", None) or getattr(self, "outer", None)
        if server is None:
            return None
        api: ConfigCenterAPI | None = getattr(server, "_config_center_api", None)
        if api is None:
            return None

        import re

        route_table = {
            "GET": getattr(server, "_config_center_get_routes", {}),
            "POST": getattr(server, "_config_center_post_routes", {}),
            "DELETE": getattr(server, "_config_center_delete_routes", {}),
        }.get(method, {})

        for pattern, (param_names, handler) in route_table.items():
            m = re.fullmatch(pattern, path)
            if m:
                url_params = m.groupdict()
                # Merge query params for simple routes
                if param_names:
                    for pname in param_names.split(","):
                        pname = pname.strip()
                        if pname and pname not in url_params:
                            url_params[pname] = params.get(pname, "")
                return handler(url_params, body)
        return None


__all__ = [
    "ConfigCenterAPI",
    "ConfigCenterHandlerMixin",
    "install_routes",
]
