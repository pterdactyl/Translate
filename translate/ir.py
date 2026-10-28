import json
from pathlib import Path

from jsonschema import Draft202012Validator

SCHEMA_VERSION = "1.3.0"
SCHEMA_FILE = Path(__file__).resolve().parent.parent / "schema" / "repository_map.schema.json"
_ERROR_LIMIT = 40


class MapError(Exception):
    """The file is not a frozen RepoMap v1 document."""

    def __init__(self, errors, hidden=0):
        self.errors = list(errors)
        self.hidden = hidden
        message = "\n".join(self.errors)
        if hidden:
            message = f"{message}\n{hidden} more errors"
        super().__init__(message or "repository map is not valid")


def _validator():
    schema = json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _location(error):
    parts = [str(part) for part in error.absolute_path]
    return "/".join(parts) if parts else "<root>"


def normalize(data):
    """Validate a versioned document or a legacy file array.

    A valid legacy array is returned as a versioned document. The file
    records themselves are not copied.
    """
    if not isinstance(data, (dict, list)):
        raise MapError(["repository map must be a file list or a versioned document"])
    errors = []
    hidden = 0
    for error in _validator().iter_errors(data):
        if len(errors) < _ERROR_LIMIT:
            errors.append(f"schema {_location(error)}: {error.message}")
        else:
            hidden += 1
    if errors or hidden:
        raise MapError(errors, hidden)
    if isinstance(data, list):
        return {"schema_version": SCHEMA_VERSION, "files": data}
    return data


def load_repository_map(path):
    file = Path(path)
    try:
        text = file.read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise MapError([f"map is not UTF-8: {exc}"]) from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MapError([f"map is not JSON: {exc}"]) from exc
    return normalize(data)
