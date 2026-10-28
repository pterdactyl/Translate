"""Compare a Python app with the Java Translate generates from its RepoMap."""

import importlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from translate.java import JAVA_RELEASE

TRANSLATE_ROOT = Path(__file__).resolve().parents[1]
REPOMAP_ROOT = TRANSLATE_ROOT.parent / "RepoMap"
APPS = Path(__file__).resolve().parent / "apps"
APP_ORDER = (
    "calculator",
    "loops",
    "exceptions",
    "collections",
    "classes",
    "cross_file",
)

PYTHON_EXCEPTIONS = {
    "ArithmeticException": "ZeroDivisionError",
    "IllegalArgumentException": "ValueError",
    "AssertionError": "AssertionError",
    "IndexOutOfBoundsException": "IndexError",
    "NoSuchElementException": "KeyError",
}
JAVA_EXCEPTIONS = {python: java for java, python in PYTHON_EXCEPTIONS.items()}
MESSAGE_TYPES = {"AssertionError", "ValueError", "IndexError"}


@dataclass
class Outcome:
    ok: bool
    value: object = None
    exc_type: str = ""
    exc_message: str = ""


@dataclass
class CaseResult:
    label: str
    passed: bool
    detail: str = ""


@dataclass
class AppResult:
    name: str
    cases: list = field(default_factory=list)
    setup_error: str = ""

    @property
    def ok(self):
        return not self.setup_error and bool(self.cases) and all(case.passed for case in self.cases)

    def report(self):
        lines = [f"{self.name}: {sum(case.passed for case in self.cases)}/{len(self.cases)} passed"]
        if self.setup_error:
            lines.append(self.setup_error.rstrip())
        for case in self.cases:
            if not case.passed:
                lines.append(f"FAIL {case.label}")
                lines.append(case.detail.rstrip())
        return "\n".join(lines)


def discover_apps():
    found = {
        path.name: path
        for path in APPS.iterdir()
        if path.is_dir() and (path / "cases.json").is_file()
    }
    ordered = [found[name] for name in APP_ORDER if name in found]
    ordered.extend(found[name] for name in sorted(found) if name not in APP_ORDER)
    return ordered


def run_app(app, work):
    app = Path(app)
    work = Path(work)
    result = AppResult(app.name)
    try:
        cases = json.loads((app / "cases.json").read_text(encoding="utf-8"))["cases"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        result.setup_error = f"could not read cases.json: {exc}"
        return result
    if not cases:
        result.setup_error = "cases.json has no cases"
        return result

    map_dir = work / "map"
    java_dir = work / "java"
    map_dir.mkdir(parents=True, exist_ok=True)
    try:
        map_path = _run_repomap(app, map_dir)
    except RuntimeError as exc:
        result.setup_error = str(exc)
        return result

    translated = _run_translate(map_path, java_dir)
    if translated.setup_error:
        result.setup_error = translated.setup_error
        return result

    try:
        python_outcomes = _run_python(app, cases)
    except Exception as exc:
        result.setup_error = f"Python cases failed to run: {exc}"
        return result

    report = json.loads((java_dir / "translation_report.json").read_text(encoding="utf-8"))
    runnable = []
    for case, outcome in zip(cases, python_outcomes):
        label = _label(case)
        try:
            java_name = _translated(report, case)
        except KeyError as exc:
            result.cases.append(CaseResult(label, False, str(exc)))
            continue
        runnable.append((case, outcome, java_name, label))

    if not runnable:
        return result

    driver = work / "Check.java"
    driver.write_text(_driver_source(runnable), encoding="utf-8")
    compiled = _compile_driver(driver, java_dir / "classes")
    if compiled.returncode != 0:
        detail = compiled.stderr or compiled.stdout or "javac failed"
        for _case, _outcome, translated, label in runnable:
            result.cases.append(CaseResult(label, False, detail))
        return result

    completed = subprocess.run(
        ["java", "-cp", str(java_dir / "classes"), "Check"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr or completed.stdout or "java failed"
        for _case, _outcome, translated, label in runnable:
            result.cases.append(CaseResult(label, False, detail))
        return result

    lines = completed.stdout.splitlines()
    if len(lines) != len(runnable):
        detail = (
            f"expected {len(runnable)} Java result lines, got {len(lines)}\n"
            + completed.stdout
        )
        for _case, _outcome, translated, label in runnable:
            result.cases.append(CaseResult(label, False, detail))
        return result

    for (_case, outcome, translated, label), line in zip(runnable, lines):
        passed, detail = _compare(outcome, line)
        result.cases.append(CaseResult(label, passed, detail))
    return result


def _label(case):
    owner = case.get("class")
    name = f"{owner}.{case['function']}" if owner else case["function"]
    rendered = ", ".join(json.dumps(arg) for arg in case["args"])
    return f"{case['file']} {name}({rendered})"


def _run_repomap(app, map_dir):
    if not (REPOMAP_ROOT / "app" / "repomap.py").is_file():
        raise RuntimeError(f"RepoMap checkout not found at {REPOMAP_ROOT}")
    interpreters = [sys.executable]
    venv = REPOMAP_ROOT / ".venv" / "bin" / "python"
    if venv.is_file() and str(venv) not in interpreters:
        interpreters.append(str(venv))
    errors = []
    for python in interpreters:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(REPOMAP_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        completed = subprocess.run(
            [python, "-m", "app.repomap", str(app.resolve())],
            cwd=map_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        json_path = map_dir / "repository_map.json"
        if json_path.is_file() and _repomap_ok(completed):
            return json_path
        errors.append(completed.stderr or completed.stdout or f"{python} failed")
        if "No module named" not in (completed.stderr or ""):
            break
    raise RuntimeError("RepoMap failed:\n" + "\n".join(errors))


def _repomap_ok(completed):
    if completed.returncode == 0:
        return True
    text = f"{completed.stderr}\n{completed.stdout}".lower()
    return "dot" in text or "graphviz" in text


def _run_translate(map_path, java_dir):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(TRANSLATE_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    completed = subprocess.run(
        [sys.executable, "-m", "translate", str(map_path), "-o", str(java_dir)],
        cwd=TRANSLATE_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    result = AppResult("translate")
    report_path = java_dir / "translation_report.json"
    if not report_path.is_file():
        result.setup_error = "Translate did not write translation_report.json\n" + (
            completed.stderr or completed.stdout
        )
        return result
    if completed.returncode != 0:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        compile_error = (report.get("compile") or {}).get("stderr") or completed.stderr
        result.setup_error = "generated Java failed to compile\n" + (compile_error or completed.stdout)
    return result


def _run_python(app, cases):
    app_path = str(app.resolve())
    inserted = app_path not in sys.path
    if inserted:
        sys.path.insert(0, app_path)
    roots = sorted({case["module"].split(".", 1)[0] for case in cases})
    for name in list(sys.modules):
        if name in roots or any(name.startswith(root + ".") for root in roots):
            sys.modules.pop(name, None)
    try:
        modules = {}
        outcomes = []
        for case in cases:
            module_name = case["module"]
            if module_name not in modules:
                modules[module_name] = importlib.import_module(module_name)
            outcomes.append(_call_python(modules[module_name], case))
        return outcomes
    finally:
        if inserted and app_path in sys.path:
            sys.path.remove(app_path)
        for name in list(sys.modules):
            if name in roots or any(name.startswith(root + ".") for root in roots):
                sys.modules.pop(name, None)


def _call_python(module, case):
    try:
        if case.get("class"):
            target = getattr(module, case["class"])()
            function = getattr(target, case["function"])
        else:
            function = getattr(module, case["function"])
        return Outcome(True, function(*case["args"]))
    except Exception as exc:
        return Outcome(False, exc_type=type(exc).__name__, exc_message=str(exc))


def _translated(report, case):
    matches = []
    for item in report.get("translated") or []:
        if item.get("file") != case["file"] or item.get("name") != case["function"]:
            continue
        identity = item.get("id") or ""
        if case.get("class"):
            if identity.startswith(f"method:{case['file']}:{case['class']}:"):
                matches.append(item)
        elif identity.startswith("function:"):
            matches.append(item)
    if len(matches) == 1:
        return matches[0]["java"]
    unsupported = [
        item for item in report.get("unsupported") or []
        if item.get("file") == case["file"] and item.get("name") == case["function"]
    ]
    if case.get("class"):
        unsupported = [
            item for item in unsupported
            if str(item.get("id") or "").startswith(f"method:{case['file']}:{case['class']}:")
        ]
    elif unsupported:
        unsupported = [
            item for item in unsupported if str(item.get("id") or "").startswith("function:")
        ]
    if len(unsupported) == 1:
        raise KeyError(f"not translated: {unsupported[0]['reason']}")
    if not matches and not unsupported:
        raise KeyError("not found in the translation report")
    raise KeyError(f"ambiguous translation ({len(matches)} translated, {len(unsupported)} unsupported)")


def _driver_source(runnable):
    lines = [
        "public class Check {",
        "    public static void main(String[] args) {",
    ]
    for index, _row in enumerate(runnable):
        lines.append(f"        case{index}();")
    lines.extend(["    }", ""])
    for index, (case, _outcome, java_name, _label) in enumerate(runnable):
        rendered = ", ".join(_render_arg(arg) for arg in case["args"])
        call = f"{java_name}({rendered})"
        lines.extend([
            f"    static void case{index}() {{",
            "        try {",
            f"            System.out.println(\"ok \" + {call});",
            "        } catch (Throwable ex) {",
            "            String message = ex.getMessage();",
            "            if (message == null) {",
            "                message = \"\";",
            "            }",
            "            System.out.println(\"err \" + ex.getClass().getSimpleName() + \"\\t\" + message);",
            "        }",
            "    }",
            "",
        ])
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def _render_arg(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return f"{value}L"
    if isinstance(value, float):
        return _java_double(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return _render_list(value)
    if isinstance(value, dict):
        return _render_dict(value)
    raise TypeError(f"unsupported case argument {value!r}")


def _kind(value):
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "long"
    if isinstance(value, float):
        return "double"
    if isinstance(value, str):
        return "string"
    raise TypeError(f"unsupported case value {value!r}")


def _render_list(values):
    if not values:
        raise TypeError("empty list arguments are not supported")
    kind = _kind(values[0])
    if any(_kind(item) != kind for item in values):
        raise TypeError("list arguments must share one type")
    helper = {
        "long": "listLong",
        "double": "listDouble",
        "boolean": "listBoolean",
        "string": "listString",
    }[kind]
    inner = ", ".join(_render_arg(item) for item in values)
    return f"repomap.runtime.Numbers.{helper}({inner})"


def _render_dict(value):
    items = list(value.items())
    if not items:
        raise TypeError("empty dict arguments are not supported")
    key_kind = _kind(items[0][0])
    value_kind = _kind(items[0][1])
    boxes = {
        "long": "Long",
        "double": "Double",
        "boolean": "Boolean",
        "string": "String",
    }
    args = []
    for key, item in items:
        if _kind(key) != key_kind or _kind(item) != value_kind:
            raise TypeError("dict arguments must share one key type and one value type")
        args.append(_render_arg(key))
        args.append(_render_arg(item))
    return (
        f"repomap.runtime.Numbers.<{boxes[key_kind]}, {boxes[value_kind]}>map("
        + ", ".join(args)
        + ")"
    )


def _java_double(value):
    text = repr(value)
    if "e" not in text and "E" not in text and "." not in text:
        text += ".0"
    return text


def _compile_driver(driver, classes):
    javac = shutil.which("javac")
    if javac is None:
        return subprocess.CompletedProcess(["javac"], 1, "", "javac was not found on PATH")
    return subprocess.run(
        [
            javac,
            "--release",
            str(JAVA_RELEASE),
            "-encoding",
            "UTF-8",
            "-cp",
            str(classes),
            "-d",
            str(classes),
            str(driver),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _compare(outcome, line):
    if outcome.ok:
        if not line.startswith("ok "):
            return False, f"python returned {_format_value(outcome.value)!r}\njava {line!r}"
        body = line[3:]
        if _same_value(outcome.value, body):
            return True, ""
        return False, f"python {_format_value(outcome.value)!r}\njava {body!r}"
    if not line.startswith("err "):
        return False, f"python raised {outcome.exc_type}: {outcome.exc_message}\njava {line!r}"
    simple, _, message = line[4:].partition("\t")
    expected = JAVA_EXCEPTIONS.get(outcome.exc_type)
    if simple != expected:
        return False, (
            f"python raised {outcome.exc_type}: {outcome.exc_message}\n"
            f"java raised {simple}: {message}"
        )
    if outcome.exc_type in MESSAGE_TYPES and message != outcome.exc_message:
        return False, (
            f"exception message python {outcome.exc_message!r}\n"
            f"java {message!r}"
        )
    return True, ""


def _same_value(value, body):
    rendered = _format_value(value)
    if body == rendered:
        return True
    if isinstance(value, float):
        try:
            return float(body) == value
        except ValueError:
            return False
    return False


def _format_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "[" + ", ".join(_format_value(item) for item in value) + "]"
    if isinstance(value, dict):
        parts = [f"{_format_value(key)}={_format_value(item)}" for key, item in value.items()]
        return "{" + ", ".join(parts) + "}"
    return repr(value)


def main(argv=None):
    apps = discover_apps()
    if argv:
        wanted = set(argv)
        apps = [app for app in apps if app.name in wanted]
    if not apps:
        print("no applications found", file=sys.stderr)
        return 1
    failed = False
    root = Path(os.environ.get("TMPDIR", "/tmp")) / "translate-integration"
    for app in apps:
        result = run_app(app, root / app.name)
        print(result.report())
        print()
        failed = failed or not result.ok
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
