# SPDX-License-Identifier: Apache-2.0
"""Static contracts for the dedicated Incidents dashboard tab.

The Incidents tab owns three surfaces that used to live inside the Cluster
tab: SLO cards, the error budget grid, and the server-owned incident feed.
Moving them out is only half the contract — they must also keep working on a
single-node host where distributed inference was never enabled, which means
their read-only endpoints can no longer sit behind the distributed gate.
"""

import html as html_module
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

DASHBOARD_HTML = ROOT / "omlx/admin/templates/dashboard.html"
NAVBAR_HTML = ROOT / "omlx/admin/templates/dashboard/_navbar.html"
CLUSTER_HTML = ROOT / "omlx/admin/templates/dashboard/_cluster.html"
INCIDENTS_HTML = ROOT / "omlx/admin/templates/dashboard/_incidents.html"
DASHBOARD_JS = ROOT / "omlx/admin/static/js/dashboard.js"

SERVER_PY = ROOT / "omlx/server.py"


def _read(path: Path) -> str:
    return path.read_text()


def _locales() -> list[Path]:
    return sorted((ROOT / "omlx/admin/i18n").glob("*.json"))


def test_incidents_partial_is_included_exactly_once():
    dashboard = _read(DASHBOARD_HTML)

    assert dashboard.count('{% include "dashboard/_incidents.html" %}') == 1


def test_cluster_tab_no_longer_renders_the_moved_surfaces():
    cluster = _read(CLUSTER_HTML)

    assert "Service Level Objectives" not in cluster
    assert "Error Budget" not in cluster
    # The incident feed moved too — including its badge and dismissal button.
    assert "clusterActiveIncidents()" not in cluster
    assert "dismissClusterIncident(" not in cluster


def test_incidents_partial_renders_all_three_surfaces():
    incidents = _read(INCIDENTS_HTML)

    assert 'x-show="mainTab === \'incidents\'"' in incidents
    assert "Service Level Objectives" in incidents
    assert "clusterSlos" in incidents
    assert "Error Budget" in incidents
    assert "clusterErrorBudget" in incidents
    assert "clusterActiveIncidents()" in incidents
    assert "dismissClusterIncident(" in incidents


def test_incidents_navigation_exists_for_desktop_and_mobile():
    navbar = _read(NAVBAR_HTML)

    assert navbar.count("setMainTab('incidents')") == 2
    assert navbar.count("mainTab === 'incidents'") == 2
    assert navbar.count("navbar.tab.incidents") == 2


def test_incidents_navigation_is_not_gated_on_distributed_inference():
    """The whole point of the tab: visible and useful on a single node."""

    navbar = _read(NAVBAR_HTML)
    buttons = navbar.split("<button")
    incidents_buttons = [
        button for button in buttons if "setMainTab('incidents')" in button
    ]
    assert len(incidents_buttons) == 2, (
        f"expected desktop + mobile entries, found {len(incidents_buttons)}"
    )
    for position, button in enumerate(incidents_buttons):
        element = button[: button.find("</button>") + len("</button>")]
        assert "distributed_inference_active" not in element, (
            f"incidents nav entry {position} is gated on distributed inference"
        )


def test_incidents_is_a_registered_main_tab():
    javascript = _read(DASHBOARD_JS)

    tabs = javascript.split("DASHBOARD_MAIN_TABS", 1)[1].split(";", 1)[0]
    assert "'incidents'" in tabs

    # Tab activation must pull the three surfaces' data.
    assert "if (value === 'incidents')" in javascript
    incidents_block = javascript.split(
        "if (value === 'incidents')", 1
    )[1].split("if (value ===", 2)[0]
    for call in (
        "this.loadClusterIncidents()",
        "this.loadClusterSlos()",
        "this.loadClusterErrorBudget()",
    ):
        assert call in incidents_block, f"{call} missing from incidents branch"

    # And switching to it must never bounce off the distributed gate.
    set_main_tab = javascript.split("setMainTab(tab)", 1)[1].split("setSettingsTab", 1)[0]
    assert "incidents" not in set_main_tab.split("return")[0] or (
        "distributed_inference_active" not in set_main_tab
    )


def test_every_locale_defines_navbar_tab_incidents():
    locales = _locales()
    assert len(locales) >= 9, f"expected at least 9 locales, found {len(locales)}"

    missing = [
        locale.name
        for locale in locales
        if "navbar.tab.incidents" not in json.loads(locale.read_text())
    ]
    assert missing == [], f"locales missing navbar.tab.incidents: {missing}"


def test_every_alpine_expression_in_the_incidents_template_parses_as_javascript():
    """A syntax error in an Alpine attribute is silent until someone opens the tab."""

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required to parse Alpine expressions")

    source = _read(INCIDENTS_HTML)
    attribute = re.compile(
        r'(?P<name>(?:x-[a-z:.\-]+|@[A-Za-z0-9:.\-]+|:[A-Za-z0-9:.\-]+))\s*=\s*"(?P<value>[^"]*)"',
        re.S,
    )
    statement_attributes = {"x-init", "x-data", "x-effect"}
    checks = []
    for match in attribute.finditer(source):
        name = match.group("name")
        if name == "x-cloak" or name.startswith("x-transition"):
            continue
        value = html_module.unescape(match.group("value")).strip()
        if not value:
            continue
        base = name.split(".")[0].split(":")[0]
        checks.append(
            {
                "name": name,
                "line": source[: match.start()].count("\n") + 1,
                "value": value,
                "statement": base in statement_attributes or base.startswith("@"),
            }
        )
    assert len(checks) > 20, "expected the incidents template to carry Alpine"

    script = """
const checks = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const failures = [];
for (const check of checks) {
    const body = check.statement ? check.value : `(${check.value})`;
    try {
        new Function('$event', '$el', '$refs', '$store', '$dispatch', '$nextTick', body);
    } catch (error) {
        failures.push(`${check.name} line ${check.line}: ${error.message} :: ${check.value}`);
    }
}
console.log(JSON.stringify(failures));
"""
    result = subprocess.run(
        [node, "-e", script],
        input=json.dumps(checks),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []


def test_observability_router_carries_the_three_readonly_surfaces():
    from omlx.cluster.observability_api import slo_router

    paths = {
        (getattr(route, "path"), tuple(sorted(getattr(route, "methods", []))))
        for route in slo_router.routes
    }
    assert ("/admin/api/cluster/slos", ("GET",)) in paths
    assert ("/admin/api/cluster/error-budget", ("GET",)) in paths
    assert ("/admin/api/cluster/incidents", ("GET",)) in paths
    assert ("/admin/api/cluster/incidents/{incident_id}/dismiss", ("POST",)) in paths


def test_observability_routes_never_depend_on_the_distributed_gate():
    from omlx.cluster.observability_api import slo_router

    dependencies = [
        getattr(dependency, "__name__", str(dependency))
        for route in slo_router.routes
        for dependency in getattr(route, "dependencies", [])
    ]
    assert not any("distributed" in name for name in dependencies), dependencies


def test_server_registers_observability_routes_unconditionally():
    """Registration must precede the gated block and live outside it."""

    source = _read(SERVER_PY)
    register_at = source.find("register_observability_routes(app")
    gated_at = source.find("def _register_cluster_routes")

    assert register_at != -1, "server.py never registers the observability router"
    assert gated_at != -1
    assert register_at < gated_at, (
        "observability routes registered inside/below the gated cluster block"
    )


def test_cluster_router_no_longer_serves_the_moved_surfaces():
    """One owner per path: the gated router keeps everything except these four."""

    from omlx.cluster.routes import router as cluster_router

    moved = {
        "/admin/api/cluster/slos",
        "/admin/api/cluster/error-budget",
        "/admin/api/cluster/incidents",
        "/admin/api/cluster/incidents/{incident_id}/dismiss",
    }
    served = {getattr(route, "path", None) for route in cluster_router.routes}
    overlap = moved & served
    assert overlap == set(), f"duplicate ownership after the move: {overlap}"


def test_incidents_pills_use_dark_safe_badge_colors():
    """bg-*-100/text-*-800 pairs are not remapped by the dark CSS section."""

    incidents = _read(INCIDENTS_HTML)

    for bad_pair in (
        "bg-green-100 text-green-800",
        "bg-red-100 text-red-800",
        "bg-amber-100 text-amber-800",
    ):
        assert bad_pair not in incidents, f"unmapped dark-mode pill pair: {bad_pair}"
