import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from translate import MapError, java_class_name, java_package, load_repository_map, translate

TRANSLATE_ROOT = Path(__file__).resolve().parents[1]
REPOMAP_ROOT = TRANSLATE_ROOT.parent / "RepoMap"


def _scan(root):
    """Build a map with the sibling RepoMap checkout. Translate does not import it."""
    scanner = REPOMAP_ROOT / "app" / "scanner.py"
    if not scanner.is_file():
        pytest.fail(f"RepoMap checkout not found at {REPOMAP_ROOT}")
    if str(REPOMAP_ROOT) not in sys.path:
        sys.path.insert(0, str(REPOMAP_ROOT))
    from app.scanner import build_repository_map, repository_map_document

    return repository_map_document(build_repository_map(root))


def _write_repo(root, files):
    for relative, source in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")
    return root


def _reason(report, file, name):
    matches = [
        item for item in report["unsupported"]
        if item["file"] == file and item["name"] == name
    ]
    assert len(matches) == 1, report["unsupported"]
    return matches[0]["reason"]


def _source(translation, suffix):
    matches = [item for item in translation.files if item.path.endswith(suffix)]
    assert len(matches) == 1, [item.path for item in translation.files]
    return matches[0].source


def test_java_names_follow_repo_paths():
    assert java_package("src/pkg/calc.py") == "pkg"
    assert java_class_name("src/pkg/calc.py") == "Calc"
    assert java_package("calc.py") == ""
    assert java_class_name("calc.py") == "Calc"
    assert java_package("my-pkg/util.py") == "my_pkg"
    assert java_class_name("my_util.py") == "MyUtil"
    assert java_package("class/util.py") == "class_"
    assert java_class_name("pkg/__init__.py") == "PkgInit"
    assert java_class_name("__main__.py") == "Main"


def test_loader_rejects_a_map_that_is_not_v1(tmp_path):
    path = tmp_path / "map.json"
    path.write_text("{", encoding="utf-8")
    with pytest.raises(MapError):
        load_repository_map(path)

    with pytest.raises(MapError) as caught:
        translate({"schema_version": "9.9.9", "files": []})
    assert "9.9.9" in str(caught.value)

    empty = translate({"schema_version": "1.3.0", "files": []})
    assert empty.report["translated"] == []
    assert empty.report["unsupported"] == []
    assert any(item.path.endswith("repomap/runtime/Numbers.java") for item in empty.files)


def test_rejected_functions_stay_out_of_the_java(tmp_path):
    root = _write_repo(tmp_path, {
        "sample/__init__.py": "",
        "other/__init__.py": "",
        "top.py": """
            def inc(value: int) -> int:
                return value + 1
        """,
        "other/util.py": """
            def twice(value: int) -> int:
                return value * 2
        """,
        "sample/more.py": """
            from sample.calc import add
            import top

            def twice(value: int) -> int:
                return add(value, value)

            def via_root(value: int) -> int:
                return top.inc(value)
        """,
        "sample/calc.py": """
            from other.util import twice as twice_value

            SCALE = 2

            def add(left: int, right: int) -> int:
                return left + right

            def use_twice(value: int) -> int:
                return twice_value(value)

            def box(default: int) -> int:
                return default

            def untyped(value):
                return value

            def uses_default(value: int = 1) -> int:
                return value

            def shifted(value: int) -> int:
                return value << 1

            def scaled(value: int) -> int:
                return value * SCALE

            def maybe(n: int) -> int:
                if n > 0:
                    return n

            def huge() -> int:
                return 9223372036854775808

            def seq(value: int) -> int:
                return value

            def long_chain(value: int) -> bool:
                return 0 < seq(value) < seq(value) < 10

            class Box:
                def get(self) -> int:
                    return 1
        """,
    })
    document = _scan(root)
    translation = translate(document)
    legacy = translate(document["files"])
    assert [item["id"] for item in legacy.report["translated"]] == [
        item["id"] for item in translation.report["translated"]
    ]

    report = translation.report
    limitations = " ".join(report["limitations"])
    assert "Java long" in limitations
    assert "Unsupported syntax" in limitations

    assert "default package" in _reason(report, "sample/more.py", "via_root")
    assert "unresolved" in _reason(report, "sample/calc.py", "untyped")
    assert "default" in _reason(report, "sample/calc.py", "uses_default")
    assert "shift" in _reason(report, "sample/calc.py", "shifted")
    assert "SCALE" in _reason(report, "sample/calc.py", "scaled")
    assert "not every path" in _reason(report, "sample/calc.py", "maybe")
    assert "does not fit" in _reason(report, "sample/calc.py", "huge")
    assert "chained comparison" in _reason(report, "sample/calc.py", "long_chain")

    translated_ids = {item["id"] for item in report["translated"]}
    unsupported_ids = {item["id"] for item in report["unsupported"]}
    assert translated_ids.isdisjoint(unsupported_ids)
    for name in ("add", "use_twice", "box", "get"):
        assert any(item["name"] == name and item["file"] == "sample/calc.py" for item in report["translated"])

    calc_source = _source(translation, "sample/Calc.java")
    assert "long default_" in calc_source
    assert "other.Util.twice" in calc_source
    assert "9223372036854775808" not in calc_source
    assert "long_chain" not in calc_source
    assert "method:sample/calc.py:Box:get" not in calc_source
    more_source = _source(translation, "sample/More.java")
    assert "sample.Calc.add" in more_source
    assert "via_root" not in more_source
    top_source = _source(translation, "Top.java")
    assert "package " not in top_source


def test_cli_compiles_a_project(tmp_path):
    root = _write_repo(tmp_path / "srcpkg", {
        "pkg/calc.py": """
            def add(left: int, right: int) -> int:
                return left + right
        """,
    })
    document = _scan(root)
    map_path = tmp_path / "repository_map.json"
    map_path.write_text(json.dumps(document), encoding="utf-8")
    output = tmp_path / "out"
    completed = subprocess.run(
        [sys.executable, "-m", "translate", str(map_path), "-o", str(output)],
        cwd=TRANSLATE_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "compiled" in completed.stdout
    report = json.loads((output / "translation_report.json").read_text(encoding="utf-8"))
    assert report["compile"]["ok"] is True
    assert report["translated"][0]["java"] == "pkg.Calc.add"
    assert (output / "classes" / "pkg" / "Calc.class").is_file()
