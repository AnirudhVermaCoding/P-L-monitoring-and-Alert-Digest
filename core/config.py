"""Load and validate the business-rules config (config.yaml)."""

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

RUNTIME_API_VERSION = 1

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config.yaml"

# The six cost line items, in the canonical order used everywhere.
LINE_ITEMS = [
    "Manpower",
    "Packaging",
    "Power and Fuel",
    "FC Rent",
    "Equipment Rentals",
    "Overheads",
]

# Defaults for the optional `notifications` section (keeps old configs working).
DEFAULT_NOTIFICATIONS = {
    "scope": "latest_day",       # latest_day | all_days
    "materiality_pp": 1.0,       # min pp beyond range for a line-item breach to page its owner
    "always_page_cm_breach": True,
}


@dataclass
class Config:
    targets: dict[str, dict[str, float]]
    margins: dict[str, dict[str, float]]
    colors: dict[str, float]
    owners: dict[str, str]
    fc_managers: dict[str, str]
    targets_by_fc: dict[str, dict[str, dict[str, float]]] = field(default_factory=dict)
    notifications: dict = field(default_factory=lambda: dict(DEFAULT_NOTIFICATIONS))
    raw: dict = field(default_factory=dict)

    def target_for(self, fc: str, line_item: str) -> dict[str, float]:
        """Return the FC-specific target when supplied, otherwise the global target."""
        return self.targets_by_fc.get(str(fc), {}).get(line_item, self.targets[line_item])

    def target_range_str(self, line_item: str, fc: str | None = None) -> str:
        t = self.target_for(fc, line_item) if fc is not None else self.targets[line_item]
        return f"{t['min']:g}-{t['max']:g}%"

    def owner_for(self, line_item: str) -> str:
        return self.owners.get(line_item, self.fc_managers.get("default", ""))

    def manager_for(self, fc: str) -> str:
        return self.fc_managers.get(fc, self.fc_managers.get("default", ""))


def load_config(path: str | os.PathLike | None = None) -> Config:
    """Read config.yaml into a validated Config. Raises ValueError on missing keys."""
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not path.exists():
        raise ValueError(f"Config file not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    for key in ("targets", "margins", "colors", "owners", "fc_managers"):
        if key not in data:
            raise ValueError(f"config.yaml is missing required section '{key}'")

    for item in LINE_ITEMS:
        if item not in data["targets"]:
            raise ValueError(f"config.yaml targets is missing line item '{item}'")

    notifications = {**DEFAULT_NOTIFICATIONS, **(data.get("notifications") or {})}

    return Config(
        targets=data["targets"],
        margins=data["margins"],
        colors=data["colors"],
        owners=data["owners"],
        fc_managers=data["fc_managers"],
        notifications=notifications,
        raw=data,
    )
