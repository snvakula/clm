"""Display command results as readable records or JSON."""

import json
import re
from typing import Any

LABELS = {
    "id": "API identifier",
    "version": "Generation",
    "not_before": "Valid from",
    "not_after": "Expires",
    "pending": "Interrupted update pending",
    "private_key_path": "Private key path",
    "sha256": "SHA-256",
}
UTC_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


def _value(value: Any, field: str = "") -> str:
    if value is None:
        return "None"
    if isinstance(value, bool) or field == "enabled":
        return "Yes" if value else "No"
    if field == "generation" and value == 0:
        return "Not issued"
    if field == "status":
        return str(value).capitalize()
    if isinstance(value, str) and UTC_TIMESTAMP.fullmatch(value):
        return value.replace("T", " ").removesuffix("Z") + " UTC"
    return str(value)


def _display(value: Any, indent: int = 0) -> None:
    prefix = " " * indent
    if isinstance(value, dict):
        heading = next(
            (field for field in ("identifier", "certificate_id", "name") if field in value), None
        )
        if heading is not None:
            print(prefix + _value(value[heading]))
            prefix += "  "
            indent += 2
        fields = [(key, item) for key, item in value.items() if key != heading]
        labels = {key: LABELS.get(key, key.replace("_", " ").capitalize()) for key, _ in fields}
        width = max((len(label) for label in labels.values()), default=0)
        for key, item in fields:
            if isinstance(item, (dict, list)) and item:
                print(prefix + labels[key])
                _display(item, indent + 2)
            else:
                text = "None" if isinstance(item, (dict, list)) else _value(item, key)
                print(f"{prefix}{labels[key]:<{width}}  {text}")
    elif isinstance(value, list):
        if not value:
            print(prefix + "No entries")
        for item in value:
            if isinstance(item, (dict, list)):
                _display(item, indent)
            else:
                print(f"{prefix}- {_value(item)}")
    else:
        print(prefix + _value(value))


def display(value: Any, machine: bool = False) -> None:
    if machine:
        print(json.dumps(value, indent=2, sort_keys=True))
    else:
        _display(value)
