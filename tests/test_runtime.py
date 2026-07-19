"""Deployment/runtime compatibility checks for Streamlit Cloud."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent.graph as graph
import agent.insights as insights
import agent.notifier as notifier
import core.config as config
import core.engine as engine
import core.intake as intake

REPO = Path(__file__).resolve().parent.parent


def test_runtime_api_versions_match():
    modules = (config, engine, intake, insights, notifier, graph)
    assert {module.RUNTIME_API_VERSION for module in modules} == {1}
    assert hasattr(notifier, "NotificationRouting")
    assert hasattr(notifier, "deliver_notifications")
    assert hasattr(graph, "run_pipeline")
    print("PASS test_runtime_api_versions_match")


def test_config_dataclass_survives_unregistered_module_execution():
    """Reproduce Streamlit's former import edge case from the deployment traceback."""
    path = REPO / "core" / "config.py"
    spec = importlib.util.spec_from_file_location("detached_config_test", path)
    module = importlib.util.module_from_spec(spec)
    # Intentionally do not register module in sys.modules before executing it.
    spec.loader.exec_module(module)
    assert hasattr(module.Config, "target_for")
    print("PASS test_config_dataclass_survives_unregistered_module_execution")


def test_deprecated_component_renderer_removed():
    source = (REPO / "app.py").read_text(encoding="utf-8")
    assert "streamlit.components" not in source
    assert "components.html" not in source
    assert "st.iframe" in source
    print("PASS test_deprecated_component_renderer_removed")


def run_all():
    test_runtime_api_versions_match()
    test_config_dataclass_survives_unregistered_module_execution()
    test_deprecated_component_renderer_removed()
    print("\nALL RUNTIME TESTS PASSED")


if __name__ == "__main__":
    run_all()
