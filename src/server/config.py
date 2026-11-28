"""Helpers for validating local YAML configuration."""

from pathlib import Path
from typing import Any

import yaml

from .filesystem import read_regular


class UniqueLoader(yaml.SafeLoader):
    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict:
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result:
                raise ValueError("YAML keys must be unique strings")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def load_config(path: Path) -> dict[str, Any]:
    try:
        value = yaml.load(read_regular(path, 64 * 1024), Loader=UniqueLoader)
    except yaml.YAMLError:
        raise ValueError(f"Invalid YAML configuration in {path.name}") from None
    if not isinstance(value, dict):
        raise ValueError(f"Expected a mapping in {path.name}")
    return value
