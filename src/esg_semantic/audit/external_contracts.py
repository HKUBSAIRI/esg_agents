"""Load A-supplied schemas offline. No public schema is authored or guessed by B."""
from pathlib import Path
import re

from .common import read_json, sha


class ExternalContracts:
    def __init__(self, directory):
        from jsonschema import Draft202012Validator
        from referencing import Registry, Resource
        from referencing.jsonschema import DRAFT202012
        self.root = Path(directory).resolve()
        if not self.root.is_dir():
            raise ValueError("A schema directory does not exist")
        self.schemas = {}
        catalog = {}
        registry = Registry()
        for path in sorted(self.root.rglob("*.json")):
            if not path.resolve().is_relative_to(self.root):
                raise ValueError("Schema symlink escapes contract directory")
            data = read_json(path.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict) and any(key in data for key in ("type", "$ref", "$defs", "$id")):
                Draft202012Validator.check_schema(data)
                uri = path.as_uri()
                resource = Resource.from_contents(data, default_specification=DRAFT202012)
                registry = registry.with_resource(uri, resource)
                if data.get("$id"):
                    registry = registry.with_resource(data["$id"], resource)
                self.schemas[path.stem.removesuffix(".schema")] = (data, uri)
                catalog[path.relative_to(self.root).as_posix()] = data
        manifest = self.root / "b_contract_map.json"
        self.names = read_json(manifest.read_text(encoding="utf-8-sig")) if manifest.exists() else {}
        if not isinstance(self.names, dict) or not all(isinstance(v, str) for v in self.names.values()):
            raise ValueError("b_contract_map.json must map object names to local schema paths")
        self.registry, self.fingerprint = registry, sha({"schemas": catalog, "map": self.names})

    def __call__(self, name, payload):
        from jsonschema import Draft202012Validator
        from referencing import Resource
        from referencing.jsonschema import DRAFT202012
        filename = self.names.get(name)
        if filename:
            target = (self.root / filename).resolve()
            if not target.is_relative_to(self.root):
                raise ValueError("Schema map escapes contract directory")
            schema = read_json(target.read_text(encoding="utf-8-sig"))
            uri = target.as_uri()
        else:
            key = re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
            if key not in self.schemas:
                raise ValueError("A schema missing for " + name)
            schema, uri = self.schemas[key]
        # Register a local base URI so relative refs stay local; no network retrieval callback.
        resource = Resource.from_contents(schema, default_specification=DRAFT202012)
        registry = self.registry.with_resource(uri, resource)
        validator = Draft202012Validator({"$ref": uri}, registry=registry)
        validator.validate(payload)
