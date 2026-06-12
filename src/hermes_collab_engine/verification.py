"""Local verification helpers for v4.5 capabilities."""
from __future__ import annotations

from dataclasses import dataclass, asdict


@dataclass
class VerificationCheck:
    name: str
    status: str
    detail: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class VerificationReport:
    status: str
    checks: list[VerificationCheck]
    skipped: list[str]

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "checks": [check.to_dict() for check in self.checks],
            "skipped": self.skipped,
        }


def verify_v45_capabilities() -> VerificationReport:
    """Verify v4.5 registries and dashboard metadata without launching workers."""
    from .server import DashboardServer, INDEX_HTML
    from .skills import get_default_registry
    from .tools import get_default_tool_registry

    checks: list[VerificationCheck] = []

    skill_registry = get_default_registry()
    skill_names = {skill.name for skill in skill_registry.list_all()}
    expected_skills = {"implementation-focus", "test-verify", "search-verify"}
    missing_skills = sorted(expected_skills - skill_names)
    checks.append(VerificationCheck(
        "skill registry builtins",
        "failed" if missing_skills else "passed",
        "missing: " + ", ".join(missing_skills) if missing_skills else "core built-in skills are registered",
    ))

    selected_skills = skill_registry.select_for_node("implementation", "implement code and verify with unittest")
    selected_skill_names = {skill.name for skill in selected_skills}
    skill_selection_ok = {"implementation-focus", "test-verify"}.issubset(selected_skill_names)
    checks.append(VerificationCheck(
        "implementation skill selection",
        "passed" if skill_selection_ok else "failed",
        "selected: " + ", ".join(skill.name for skill in selected_skills),
    ))

    tool_registry = get_default_tool_registry()
    profile_names = {profile.name for profile in tool_registry.list_all()}
    expected_profiles = {"file-edit", "git-local", "python-tests", "mcp-readonly"}
    missing_profiles = sorted(expected_profiles - profile_names)
    checks.append(VerificationCheck(
        "tool profile builtins",
        "failed" if missing_profiles else "passed",
        "missing: " + ", ".join(missing_profiles) if missing_profiles else "core built-in tool profiles are registered",
    ))

    selected_profiles = tool_registry.select_for_node("verification", "verify an MCP read-only tool integration with tests")
    selected_profile_names = {profile.name for profile in selected_profiles}
    profile_selection_ok = {"python-tests", "mcp-readonly"}.issubset(selected_profile_names)
    checks.append(VerificationCheck(
        "verification tool selection",
        "passed" if profile_selection_ok else "failed",
        "selected: " + ", ".join(profile.name for profile in selected_profiles),
    ))

    server = DashboardServer("127.0.0.1", 0, ":memory:", ".")
    skills_payload = server.skills_payload("implementation", "implement code and verify with unittest")
    tools_payload = server.tools_payload("verification", "mcp read-only tests")
    api_payload_ok = bool(skills_payload) and bool(tools_payload) and "allowed_tools" in tools_payload[0]
    checks.append(VerificationCheck(
        "dashboard API payloads",
        "passed" if api_payload_ok else "failed",
        f"skills={len(skills_payload)} tools={len(tools_payload)}",
    ))

    html = INDEX_HTML.read_text(encoding="utf-8")
    dashboard_ok = all(token in html for token in ("/api/skills", "/api/tools"))
    checks.append(VerificationCheck(
        "dashboard capability panel",
        "passed" if dashboard_ok else "failed",
        "dashboard references skill/tool preview APIs" if dashboard_ok else "dashboard panel or API references missing",
    ))

    status = "ok" if all(check.status == "passed" for check in checks) else "failed"
    skipped = [
        "Did not launch a long-running dashboard server or browser UI session.",
        "Did not invoke real Claude/Codex/OpenCode workers; worker behavior is covered by unit tests.",
    ]
    return VerificationReport(status, checks, skipped)
