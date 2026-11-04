import json
from pathlib import Path
import pytest
from jsonschema import ValidationError

from esg_semantic.audit.external_contracts import ExternalContracts


def write(path,data):
    path.write_text(json.dumps(data),encoding="utf-8")


def test_local_refs_and_explicit_schema_mapping(tmp_path):
    write(tmp_path/"common.schema.json",{"$schema":"https://json-schema.org/draft/2020-12/schema","$defs":{"pid":{"type":"string","minLength":1}}})
    write(tmp_path/"document_context.schema.json",{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"object","required":["pid"],"properties":{"pid":{"$ref":"common.schema.json#/$defs/pid"}}})
    contracts=ExternalContracts(tmp_path)
    contracts("DocumentContext",{"pid":"x"})
    with pytest.raises(ValidationError): contracts("DocumentContext",{"pid":1})
    with pytest.raises(ValueError): contracts("UnknownObject",{})
    write(tmp_path/"b_contract_map.json",{"CustomName":"document_context.schema.json"})
    assert ExternalContracts(tmp_path).fingerprint != contracts.fingerprint
    ExternalContracts(tmp_path)("CustomName",{"pid":"x"})


def test_external_refs_never_downloaded(tmp_path,monkeypatch):
    write(tmp_path/"document_context.schema.json",{"type":"object","properties":{"pid":{"$ref":"https://not-delivered.invalid/schema.json"}}})
    contracts=ExternalContracts(tmp_path)
    with pytest.raises(Exception): contracts("DocumentContext",{"pid":"x"})


def test_schema_id_registry(tmp_path):
    write(tmp_path/"common.schema.json",{"$id":"https://schemas.example/common","type":"string"})
    write(tmp_path/"document_context.schema.json",{"$id":"https://schemas.example/document","type":"object","properties":{"pid":{"$ref":"https://schemas.example/common"}}})
    ExternalContracts(tmp_path)("DocumentContext",{"pid":"x"})


def test_invalid_directory_map_and_escape(tmp_path):
    with pytest.raises(ValueError): ExternalContracts(tmp_path/"missing")
    write(tmp_path/"b_contract_map.json",[])
    with pytest.raises(ValueError): ExternalContracts(tmp_path)
    write(tmp_path/"b_contract_map.json",{"DocumentContext":"../outside.schema.json"})
    with pytest.raises(ValueError): ExternalContracts(tmp_path)("DocumentContext",{})


def test_invalid_schema_and_non_schema_json(tmp_path):
    write(tmp_path/"metadata.json",{"owner":"fixture"})
    assert ExternalContracts(tmp_path).schemas=={}
    write(tmp_path/"bad.json",{"type":"invented"})
    with pytest.raises(Exception): ExternalContracts(tmp_path)
