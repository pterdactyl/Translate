import json
import shutil
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from re import findall, sub

from translate.ir import SCHEMA_VERSION, normalize

JAVA_RELEASE = 8
RUNTIME_PACKAGE = "repomap.runtime"
RUNTIME_CLASS = "Numbers"
RUNTIME_FQN = f"{RUNTIME_PACKAGE}.{RUNTIME_CLASS}"

JAVA_KEYWORDS = {
    "_",
    "abstract",
    "assert",
    "boolean",
    "break",
    "byte",
    "case",
    "catch",
    "char",
    "class",
    "const",
    "continue",
    "default",
    "do",
    "double",
    "else",
    "enum",
    "extends",
    "false",
    "final",
    "finally",
    "float",
    "for",
    "goto",
    "if",
    "implements",
    "import",
    "instanceof",
    "int",
    "interface",
    "long",
    "native",
    "new",
    "null",
    "package",
    "permits",
    "private",
    "protected",
    "public",
    "record",
    "return",
    "sealed",
    "short",
    "static",
    "strictfp",
    "super",
    "switch",
    "synchronized",
    "this",
    "throw",
    "throws",
    "transient",
    "true",
    "try",
    "var",
    "void",
    "volatile",
    "while",
    "yield",
}

PRIMITIVE_JAVA = {
    "int": "long",
    "float": "double",
    "bool": "boolean",
    "str": "String",
}
BOXED = {
    "long": "Long",
    "double": "Double",
    "boolean": "Boolean",
    "String": "String",
}
UNBOX = {boxed: primitive for primitive, boxed in BOXED.items()}
EXCEPTIONS = {
    "ZeroDivisionError": "ArithmeticException",
    "ValueError": "IllegalArgumentException",
    "AssertionError": "AssertionError",
    "IndexError": "IndexOutOfBoundsException",
    "KeyError": "java.util.NoSuchElementException",
}
TYPE_NAME = {
    "long": "int",
    "double": "float",
    "boolean": "bool",
    "String": "str",
    "void": "None",
}
LONG_MIN = -9223372036854775808
LONG_MAX = 9223372036854775807
NUMERIC = {"long", "double"}

LIMITATIONS = (
    "Python int is translated as Java long. Literals that do not fit, and shifts, are rejected.",
    "Methods are static. Reading the receiver is rejected.",
    "Unsupported syntax is reported and omitted.",
)

RUNTIME_SOURCE = """\
// Numeric helpers for Python behavior that Java operators do not match.
package repomap.runtime;

public final class Numbers {
    private Numbers() {
    }

    /** Python {@code /} for two ints. A zero divisor throws ArithmeticException. */
    public static double divide(long left, long right) {
        if (right == 0L) {
            throw new ArithmeticException("division by zero");
        }
        return (double) left / (double) right;
    }

    /**
     * Python {@code abs} for an int. Long.MIN_VALUE has no positive long
     * representation, so this throws instead of returning a negative value.
     */
    public static long abs(long value) {
        if (value == Long.MIN_VALUE) {
            throw new ArithmeticException("absolute value does not fit in a long");
        }
        return Math.abs(value);
    }

    public static java.util.List<Long> listLong(long... values) {
        java.util.ArrayList<Long> items = new java.util.ArrayList<Long>();
        for (long value : values) {
            items.add(value);
        }
        return items;
    }

    public static java.util.List<Double> listDouble(double... values) {
        java.util.ArrayList<Double> items = new java.util.ArrayList<Double>();
        for (double value : values) {
            items.add(value);
        }
        return items;
    }

    public static java.util.List<Boolean> listBoolean(boolean... values) {
        java.util.ArrayList<Boolean> items = new java.util.ArrayList<Boolean>();
        for (boolean value : values) {
            items.add(value);
        }
        return items;
    }

    public static java.util.List<String> listString(String... values) {
        java.util.ArrayList<String> items = new java.util.ArrayList<String>();
        for (String value : values) {
            items.add(value);
        }
        return items;
    }

    @SuppressWarnings("unchecked")
    public static <K, V> java.util.Map<K, V> map(Object... pairs) {
        if ((pairs.length % 2) != 0) {
            throw new IllegalArgumentException("map() needs pairs of keys and values");
        }
        java.util.LinkedHashMap<K, V> items = new java.util.LinkedHashMap<K, V>();
        for (int i = 0; i < pairs.length; i += 2) {
            items.put((K) pairs[i], (V) pairs[i + 1]);
        }
        return items;
    }

    public static int index(int size, long index) {
        long resolved = index < 0L ? index + (long) size : index;
        if (resolved < 0L || resolved >= (long) size || resolved > Integer.MAX_VALUE) {
            throw new IndexOutOfBoundsException("list index out of range");
        }
        return (int) resolved;
    }

    public static <T> T at(java.util.List<T> items, long index) {
        return items.get(index(items.size(), index));
    }

    public static <T> void set(java.util.List<T> items, long index, T value) {
        items.set(index(items.size(), index), value);
    }

    public static <K, V> V get(java.util.Map<K, V> items, K key) {
        if (!items.containsKey(key)) {
            throw new java.util.NoSuchElementException(String.valueOf(key));
        }
        return items.get(key);
    }

    public static <K, V> void put(java.util.Map<K, V> items, K key, V value) {
        items.put(key, value);
    }
}
"""


class Unsupported(Exception):
    def __init__(self, reason, line=None):
        self.reason = reason
        self.line = line
        super().__init__(reason)


@dataclass
class Prepared:
    params: list
    locals: list
    return_jtype: str
    jtype: dict
    jname: dict
    receiver: str | None = None


@dataclass
class FunctionRecord:
    detail: dict
    file: str
    prepared: Prepared | None = None


@dataclass(frozen=True)
class JavaName:
    package: str
    class_name: str
    method: str

    @property
    def qualifier(self):
        if self.package:
            return f"{self.package}.{self.class_name}"
        return self.class_name

    @property
    def fqn(self):
        return f"{self.qualifier}.{self.method}"


@dataclass
class GeneratedFile:
    path: str
    source: str


@dataclass
class Translation:
    files: list
    report: dict


@dataclass
class CompileResult:
    ok: bool
    stdout: str
    stderr: str


class Expr:
    def __init__(self, code, jtype, prelude=None, atom=False):
        self.code = code
        self.jtype = jtype
        self.prelude = list(prelude or [])
        self.atom = atom

    def embed(self):
        if self.atom:
            return self.code
        return f"({self.code})"


class Writer:
    def __init__(self):
        self.lines = []
        self.level = 0

    def line(self, text=""):
        if text:
            self.lines.append("    " * self.level + text)
        else:
            self.lines.append("")

    def text(self):
        return "\n".join(self.lines)


def java_package(path):
    """Java package for a repository-relative Python path.

    A leading ``src/`` is removed, matching RepoMap's module names. A module
    at the repository root has an empty package: the Java default package.
    """
    parts = Path(path).parts[:-1]
    if parts and parts[0] == "src":
        parts = parts[1:]
    return ".".join(_package_segment(part) for part in parts)


def java_class_name(path):
    """Java class for one Python file. ``pkg/__init__.py`` becomes ``PkgInit``."""
    file = Path(path)
    if file.stem == "__init__":
        parent = file.parent.name
        base = _pascal(parent) if parent not in {"", "."} else "Module"
        return base + "Init"
    return _pascal(file.stem)


def _pascal(stem):
    parts = findall(r"[A-Za-z0-9]+", stem)
    if not parts:
        return "Module"
    name = "".join(part[:1].upper() + part[1:] for part in parts)
    if name[0].isdigit():
        return "N" + name
    return name


def _package_segment(part):
    cleaned = sub(r"[^A-Za-z0-9_]", "_", part)
    if not cleaned:
        cleaned = "p"
    if cleaned[0].isdigit():
        cleaned = "p_" + cleaned
    if cleaned in JAVA_KEYWORDS:
        cleaned += "_"
    return cleaned


def sanitize_identifier(name):
    cleaned = sub(r"[^A-Za-z0-9_]", "_", name)
    if not cleaned or cleaned[0].isdigit():
        cleaned = f"_{cleaned}"
    if cleaned in JAVA_KEYWORDS:
        cleaned += "_"
    return cleaned


def describe_type(typ):
    if not isinstance(typ, dict):
        return "unresolved"
    kind = typ.get("kind")
    if kind == "primitive":
        return str(typ.get("name") or "primitive")
    if kind in {"none", "any", "unknown", "never"}:
        return kind
    if kind == "list":
        return f"list[{describe_type(typ.get('element'))}]"
    if kind == "set":
        return f"set[{describe_type(typ.get('element'))}]"
    if kind == "dict":
        return f"dict[{describe_type(typ.get('key'))}, {describe_type(typ.get('value'))}]"
    if kind == "optional":
        return f"optional[{describe_type(typ.get('element'))}]"
    if kind == "union":
        options = typ.get("options") or []
        return "union[" + ", ".join(describe_type(item) for item in options) + "]"
    if kind == "tuple":
        inner = ", ".join(describe_type(item) for item in typ.get("elements") or [])
        if typ.get("repeated"):
            return f"tuple[{inner}, ...]" if inner else "tuple[..., ...]"
        return f"tuple[{inner}]"
    if kind == "class":
        return str(typ.get("name") or "class")
    if kind == "generic":
        args = ", ".join(describe_type(item) for item in typ.get("args") or [])
        return f"{typ.get('name') or 'generic'}[{args}]"
    return str(kind or "unresolved")


def _unique(base, used):
    candidate = base
    while candidate in used:
        candidate += "_"
    used.add(candidate)
    return candidate


def java_type_name(detail, file):
    if detail.get("class_name"):
        return _pascal(detail["class_name"])
    return java_class_name(file)


def _java_class(record):
    return java_package(record.file), java_type_name(record.detail, record.file)


def prepare(detail, class_name):
    line = detail.get("line")

    def fail(reason):
        raise Unsupported(reason, line)

    if detail.get("parent_id"):
        fail("nested functions are outside the v1 subset")
    signature = detail["signature"]
    if signature.get("async"):
        fail("async def is outside the v1 subset")
    if signature.get("type_params"):
        fail("PEP 695 type parameters are outside the v1 subset")

    typed = {}
    for entry in detail["types"]["parameters"]:
        typed[entry["name"]] = entry
    used = set()
    params = []
    jtype = {}
    jname = {}
    receiver = None
    for param in signature["parameters"]:
        name = param["name"]
        kind = param["kind"]
        if kind in {"vararg", "kwarg"}:
            fail(f"parameter {name} is {kind}, which is outside the v1 subset")
        if kind == "keyword_only":
            fail(f"parameter {name} is keyword-only, which is outside the v1 subset")
        if param.get("default") is not None:
            fail(f"parameter {name} has a default, which is outside the v1 subset")
        entry = typed.get(name)
        if entry is None:
            fail(f"parameter {name} is unresolved (unknown)")
        if detail.get("class_name") and entry.get("source") == "self":
            if receiver is not None:
                fail("a method has more than one receiver")
            if kind not in {"positional", "positional_only"}:
                fail(f"parameter {name} is the receiver and must be positional")
            receiver = name
            continue
        java = _require_type(entry["type"], f"parameter {name}", fail)
        ident = _unique(sanitize_identifier(name), used)
        params.append((java, ident))
        jtype[name] = java
        jname[name] = ident

    locals_ = []
    for entry in detail["types"]["locals"]:
        name = entry["name"]
        java = _require_type(entry["type"], f"local {name}", fail)
        ident = _unique(sanitize_identifier(name), used)
        locals_.append((java, ident))
        jtype[name] = java
        jname[name] = ident

    if class_name in jname.values():
        fail(f"{class_name} cannot be used as a name because it is the generated class")

    returns = detail["types"]["returns"]["type"]
    if returns.get("kind") == "none":
        return_jtype = "void"
    elif returns.get("kind") == "never":
        fail("return type never is outside the v1 subset")
    else:
        return_jtype = _require_type(returns, "return type", fail)

    return Prepared(params, locals_, return_jtype, jtype, jname, receiver)


def _require_type(typ, where, fail):
    described = describe_type(typ)
    if typ.get("kind") == "primitive" and typ.get("name") in PRIMITIVE_JAVA:
        return PRIMITIVE_JAVA[typ["name"]]
    if typ.get("kind") == "list":
        element = typ.get("element") or {}
        if element.get("kind") != "primitive" or element.get("name") not in PRIMITIVE_JAVA:
            fail(f"{where} is unsupported ({described})")
        return "java.util.List<" + BOXED[PRIMITIVE_JAVA[element["name"]]] + ">"
    if typ.get("kind") == "dict":
        key = typ.get("key") or {}
        value = typ.get("value") or {}
        key_ok = key.get("kind") == "primitive" and key.get("name") in PRIMITIVE_JAVA
        value_ok = value.get("kind") == "primitive" and value.get("name") in PRIMITIVE_JAVA
        if not key_ok or not value_ok:
            fail(f"{where} is unsupported ({described})")
        return (
            "java.util.Map<"
            + BOXED[PRIMITIVE_JAVA[key["name"]]]
            + ","
            + BOXED[PRIMITIVE_JAVA[value["name"]]]
            + ">"
        )
    if typ.get("kind") in {"unknown", "any"}:
        hint = ""
        if where.startswith("local "):
            hint = ". Annotate it"
        fail(f"{where} is unresolved ({described}){hint}")
    fail(f"{where} is unsupported ({described})")


def translate(document):
    document = normalize(document)
    records = {}
    failures = {}
    pending = set()
    for file_record in document["files"]:
        path = file_record["file"]
        for detail in file_record["function_details"]:
            record = FunctionRecord(detail, path)
            records[detail["id"]] = record
            try:
                record.prepared = prepare(detail, java_type_name(detail, path))
            except Unsupported as exc:
                failures[detail["id"]] = exc
            else:
                pending.add(detail["id"])

    _drop_collisions(records, pending, failures)

    emitted = {}
    names = {}
    while pending:
        names = _assign_names(records, pending)
        removed = []
        emitted = {}
        for fid in sorted(pending, key=lambda item: _order(records[item])):
            record = records[fid]
            try:
                emitted[fid] = _emit_function(record, names, records, pending, failures)
            except Unsupported as exc:
                removed.append((fid, exc))
        if not removed:
            break
        for fid, exc in removed:
            failures[fid] = exc
            pending.remove(fid)

    files = _render_files(records, emitted, names)
    files.append(GeneratedFile(_java_relative(RUNTIME_PACKAGE, RUNTIME_CLASS), RUNTIME_SOURCE))
    report = _report(records, pending, failures, names)
    return Translation(files, report)


def _drop_collisions(records, pending, failures):
    owners = defaultdict(list)
    for record in records.values():
        if record.detail["id"] not in pending:
            continue
        key = _java_class(record)
        marker = (record.file, record.detail.get("class_name"))
        if marker not in owners[key]:
            owners[key].append(marker)
    blocked = set()
    for key, markers in owners.items():
        package, class_name = key
        if key == (RUNTIME_PACKAGE, RUNTIME_CLASS):
            reason = f"this module would overwrite the generated class {RUNTIME_FQN}"
        elif len(markers) > 1:
            reason = (
                "generated class "
                f"{_display_class(package, class_name)} collides with "
                + ", ".join(file for file, _class_name in markers)
            )
        else:
            continue
        for record in records.values():
            if record.detail["id"] in pending and _java_class(record) == key:
                failures[record.detail["id"]] = Unsupported(reason, record.detail.get("line"))
                blocked.add(record.detail["id"])
    pending.difference_update(blocked)


def _display_class(package, class_name):
    if package:
        return f"{package}.{class_name}"
    return class_name


def _order(record):
    detail = record.detail
    return (record.file, detail.get("line") or 0, detail.get("name") or "", detail["id"])


def _assign_names(records, pending):
    grouped = defaultdict(list)
    for fid in pending:
        grouped[_java_class(records[fid])].append(records[fid])
    names = {}
    for (package, class_name), group in grouped.items():
        used = set()
        ordered = sorted(
            group,
            key=lambda item: (item.detail["line"], item.detail["name"], item.detail["id"]),
        )
        for record in ordered:
            base = sanitize_identifier(record.detail["name"])
            method = base if base not in used else f"{base}_{record.detail['line']}"
            while method in used:
                method += "_"
            used.add(method)
            names[record.detail["id"]] = JavaName(package, class_name, method)
    return names


def _emit_function(record, names, records, pending, failures):
    detail = record.detail
    body = _JavaBody(
        record,
        names[detail["id"]],
        names,
        records,
        pending,
        failures,
    )
    body.emit()
    if body.prepared.return_jtype != "void" and not _all_paths_return(detail["body"]):
        raise Unsupported(
            "not every path returns a value; Python would return None",
            detail.get("line"),
        )
    return body.writer.text()


def _all_paths_return(statements):
    for statement in statements:
        kind = statement.get("type")
        if kind in {"return", "raise"}:
            return True
        if kind == "if" and _all_paths_return(statement.get("body") or []) and _all_paths_return(
            statement.get("orelse") or []
        ):
            return True
        if kind == "try" and _try_returns(statement):
            return True
    return False


def _try_returns(statement):
    if statement.get("orelse") or statement.get("finalbody"):
        return False
    handlers = statement.get("handlers") or []
    if not handlers:
        return False
    if not all(_all_paths_return(handler.get("body") or []) for handler in handlers):
        return False
    return _all_paths_return(statement.get("body") or [])


class _JavaBody:
    def __init__(self, record, java_name, names, records, pending, failures):
        self.record = record
        self.detail = record.detail
        self.prepared = record.prepared
        self.java_name = java_name
        self.names = names
        self.records = records
        self.pending = pending
        self.failures = failures
        self.writer = Writer()
        self.loop_depth = 0
        self.temp_index = 0
        self.used_names = set(self.prepared.jname.values())
        self.statement_line = self.detail.get("line")
        self.resolved = {
            call["name"]: call for call in self.detail.get("resolved_calls") or []
        }

    def fail(self, reason):
        raise Unsupported(reason, self.statement_line)

    def fresh(self, hint):
        while True:
            name = f"_py_{hint}{self.temp_index}"
            self.temp_index += 1
            if name not in self.used_names:
                self.used_names.add(name)
                return name

    def emit(self):
        prepared = self.prepared
        rendered = ", ".join(f"{jtype} {name}" for jtype, name in prepared.params)
        self.writer.line(f"// {self.detail['id']}")
        self.writer.line(
            f"public static {prepared.return_jtype} {self.java_name.method}({rendered}) {{"
        )
        self.writer.level += 1
        for jtype, name in prepared.locals:
            self.writer.line(f"{jtype} {name};")
        if prepared.locals and self.detail["body"]:
            self.writer.line("")
        self._block(self.detail["body"])
        self.writer.level -= 1
        self.writer.line("}")

    def _block(self, statements):
        for statement in statements:
            self.statement_line = statement.get("line", self.detail.get("line"))
            self._statement(statement)

    def _statement(self, statement):
        kind = statement.get("type")
        if kind == "pass":
            return
        if kind == "break":
            if self.loop_depth <= 0:
                self.fail("break is outside a loop")
            self.writer.line("break;")
            return
        if kind == "continue":
            if self.loop_depth <= 0:
                self.fail("continue is outside a loop")
            self.writer.line("continue;")
            return
        if kind == "return":
            self._return(statement)
            return
        if kind == "assign":
            self._assign(statement["targets"], statement["value"])
            return
        if kind == "annassign":
            if statement.get("value") is None:
                target = statement["target"]
                if target.get("type") != "name" or target.get("id") not in self.prepared.jtype:
                    self.fail("annotated name has no translated type")
                return
            self._assign([statement["target"]], statement["value"])
            return
        if kind == "augassign":
            self._augassign(statement)
            return
        if kind == "expr":
            self._expr_statement(statement["value"])
            return
        if kind == "if":
            self._if(statement)
            return
        if kind == "while":
            self._while(statement)
            return
        if kind == "for":
            self._for(statement)
            return
        if kind == "assert":
            self._assert(statement)
            return
        if kind == "raise":
            self._raise(statement)
            return
        if kind == "try":
            self._try(statement)
            return
        if kind == "unknown":
            self.fail(f"unmodeled syntax {statement.get('node')}")
        self.fail(f"{kind} is outside the v1 subset")

    def _return(self, statement):
        value = statement.get("value")
        if value is None or _is_null(value):
            if self.prepared.return_jtype != "void":
                self.fail("return is missing a value")
            self.writer.line("return;")
            return
        if self.prepared.return_jtype == "void":
            self.fail("void function cannot return a value")
        expr = self._expression(value)
        if expr.jtype == "void":
            self.fail("void function cannot be used as a value")
        self._prelude(expr)
        self.writer.line(f"return {self._as_type(expr, self.prepared.return_jtype)};")

    def _assign(self, targets, value):
        if not targets:
            self.fail("assignment has no targets")
        if len(targets) == 1 and targets[0].get("type") == "subscript":
            self._set_subscript(targets[0], value)
            return
        for target in targets:
            if target.get("type") != "name" or target.get("id") not in self.prepared.jtype:
                self.fail("assignment target must be a typed name")
        expr = self._expression(value)
        if expr.jtype == "void":
            self.fail("void function cannot be used as a value")
        self._prelude(expr)
        if len(targets) == 1:
            target = targets[0]
            rendered = self._as_type(expr, self.prepared.jtype[target["id"]])
            self.writer.line(f"{self.prepared.jname[target['id']]} = {rendered};")
            return
        temp = self.fresh("value")
        self.writer.line(f"{expr.jtype} {temp} = {expr.embed()};")
        bound = Expr(temp, expr.jtype, atom=True)
        for target in targets:
            rendered = self._as_type(bound, self.prepared.jtype[target["id"]])
            self.writer.line(f"{self.prepared.jname[target['id']]} = {rendered};")

    def _augassign(self, statement):
        target = statement["target"]
        if target.get("type") != "name" or target.get("id") not in self.prepared.jtype:
            self.fail("augmented assignment target must be a typed name")
        op = statement["op"]
        if not op.endswith("="):
            self.fail(f"{op} is outside the v1 subset")
        expr = self._expression(
            {
                "type": "binop",
                "op": op[:-1],
                "left": {"type": "name", "id": target["id"]},
                "right": statement["value"],
            }
        )
        if expr.jtype == "void":
            self.fail("void function cannot be used as a value")
        self._prelude(expr)
        rendered = self._as_type(expr, self.prepared.jtype[target["id"]])
        self.writer.line(f"{self.prepared.jname[target['id']]} = {rendered};")

    def _expr_statement(self, value):
        if value.get("type") == "constant":
            return
        if value.get("type") != "call":
            self.fail("expression statement must be a call")
        expr = self._expression(value)
        self._prelude(expr)
        self.writer.line(f"{expr.embed()};")

    def _if(self, statement):
        test = self._condition(statement["test"])
        self._prelude(test)
        self.writer.line(f"if ({test.embed()}) {{")
        self.writer.level += 1
        self._block(statement.get("body") or [])
        self.writer.level -= 1
        orelse = statement.get("orelse") or []
        if orelse:
            self.writer.line("} else {")
            self.writer.level += 1
            self._block(orelse)
            self.writer.level -= 1
        self.writer.line("}")

    def _while(self, statement):
        if statement.get("orelse"):
            self.fail(
                "while-else is outside the v1 subset; else runs only when the loop does not break"
            )
        test = self._condition(statement["test"])
        self.loop_depth += 1
        if test.prelude:
            # The test's temporaries have to be created on every iteration.
            self.writer.line("while (true) {")
            self.writer.level += 1
            self._prelude(test)
            self.writer.line(f"if (!{test.embed()}) {{")
            self.writer.level += 1
            self.writer.line("break;")
            self.writer.level -= 1
            self.writer.line("}")
            self._block(statement.get("body") or [])
            self.writer.level -= 1
            self.writer.line("}")
        else:
            self.writer.line(f"while ({test.embed()}) {{")
            self.writer.level += 1
            self._block(statement.get("body") or [])
            self.writer.level -= 1
            self.writer.line("}")
        self.loop_depth -= 1

    def _for(self, statement):
        if statement.get("async"):
            self.fail("async for is outside the v1 subset")
        if statement.get("orelse"):
            self.fail(
                "for-else is outside the v1 subset; else runs only when the loop does not break"
            )
        target = statement.get("target") or {}
        if target.get("type") != "name" or self.prepared.jtype.get(target.get("id")) != "long":
            self.fail("range() loop variable must be int")
        iterator = statement.get("iter") or {}
        if not self._is_direct_builtin(iterator, "range"):
            self.fail("for-loop iterator must be a direct range() call")
        if iterator.get("keywords") or any(
            arg.get("type") == "starred" for arg in iterator.get("args") or []
        ):
            self.fail("range() arguments must be positional ints")
        args = iterator.get("args") or []
        if not 1 <= len(args) <= 3:
            self.fail("range() must have 1 to 3 int arguments")
        typed = [self._expression(arg) for arg in args]
        for arg in typed:
            if arg.jtype != "long":
                self.fail("range() arguments must be int")
        if len(typed) == 1:
            start, stop, step = _long_expr(0), typed[0], _long_expr(1)
        elif len(typed) == 2:
            start, stop, step = typed[0], typed[1], _long_expr(1)
        else:
            start, stop, step = typed
        start_name = self._bind_long("start", start)
        stop_name = self._bind_long("stop", stop)
        step_name = self._bind_long("step", step)
        variable = self.prepared.jname[target["id"]]
        self.writer.line(f"if ({step_name} == 0L) {{")
        self.writer.level += 1
        self.writer.line(
            'throw new IllegalArgumentException("range() arg 3 must not be zero");'
        )
        self.writer.level -= 1
        self.writer.line("}")
        # Cursor is separate so the loop variable stays on the last Python value.
        # Using it as the Java index would also change the trip count if the body assigns it.
        cursor = self.fresh("cursor")
        self.writer.line(f"long {cursor} = {start_name};")
        self.writer.line(
            f"if (({step_name} > 0L ? {cursor} < {stop_name} : {cursor} > {stop_name})) {{"
        )
        self.writer.level += 1
        self.writer.line(
            f"for (; "
            f"({step_name} > 0L ? {cursor} < {stop_name} : {cursor} > {stop_name}); "
            f"{cursor} += {step_name}) {{"
        )
        self.writer.level += 1
        self.loop_depth += 1
        self.writer.line(f"{variable} = {cursor};")
        self._block(statement.get("body") or [])
        self.loop_depth -= 1
        self.writer.level -= 1
        self.writer.line("}")
        self.writer.level -= 1
        self.writer.line("}")

    def _bind_long(self, hint, expr):
        self._prelude(expr)
        name = self.fresh(hint)
        self.writer.line(f"long {name} = {expr.embed()};")
        return name

    def _assert(self, statement):
        test = self._condition(statement["test"])
        message = statement.get("message")
        self._prelude(test)
        self.writer.line(f"if (!{test.embed()}) {{")
        self.writer.level += 1
        if message is None:
            self.writer.line("throw new AssertionError();")
        else:
            expr = self._expression(message)
            if expr.jtype != "String":
                self.fail("assert message must be str")
            self._prelude(expr)
            self.writer.line(f"throw new AssertionError({expr.embed()});")
        self.writer.level -= 1
        self.writer.line("}")

    def _raise(self, statement):
        if statement.get("cause") is not None:
            self.fail("raise from is outside the v1 subset")
        exc = statement.get("exc")
        if not isinstance(exc, dict) or exc.get("type") != "call":
            self.fail("raise must construct ZeroDivisionError, ValueError, AssertionError, IndexError, or KeyError")
        func = exc.get("func") or {}
        java_type = EXCEPTIONS.get(func.get("id")) if func.get("type") == "name" else None
        if java_type is None:
            self.fail("raise must construct ZeroDivisionError, ValueError, AssertionError, IndexError, or KeyError")
        if exc.get("keywords") or any(arg.get("type") == "starred" for arg in exc.get("args") or []):
            self.fail("raise arguments must be one positional message")
        args = exc.get("args") or []
        if len(args) > 1:
            self.fail("raise takes at most a message")
        if not args:
            self.writer.line(f"throw new {java_type}();")
            return
        message = self._expression(args[0])
        if message.jtype != "String":
            self.fail("exception message must be str")
        self._prelude(message)
        self.writer.line(f"throw new {java_type}({message.embed()});")

    def _try(self, statement):
        if statement.get("star"):
            self.fail("except* is outside the v1 subset")
        if statement.get("orelse"):
            self.fail("try-else is outside the v1 subset")
        if statement.get("finalbody"):
            self.fail("finally is outside the v1 subset")
        handlers = statement.get("handlers") or []
        if not handlers:
            self.fail("try needs an except handler")
        parsed = []
        for handler in handlers:
            exc = handler.get("exception")
            if not isinstance(exc, dict) or exc.get("type") != "name":
                self.fail("bare except is outside the v1 subset")
            java_type = EXCEPTIONS.get(exc.get("id"))
            if java_type is None:
                self.fail(f"except {exc.get('id')} is outside the v1 subset")
            if handler.get("name"):
                self.fail("except-as is outside the v1 subset")
            parsed.append((java_type, handler.get("body") or []))
        self.writer.line("try {")
        self.writer.level += 1
        self._block(statement.get("body") or [])
        self.writer.level -= 1
        for java_type, body in parsed:
            caught = self.fresh("caught")
            self.writer.line(f"}} catch ({java_type} {caught}) {{")
            self.writer.level += 1
            self._block(body)
            self.writer.level -= 1
        self.writer.line("}")

    def _condition(self, expr):
        test = self._expression(expr)
        if test.jtype != "boolean":
            self.fail("condition must be bool; Python truthiness is outside the v1 subset")
        return test

    def _prelude(self, expr):
        for line in expr.prelude:
            self.writer.line(line)

    def _as_type(self, expr, dest):
        if expr.jtype == dest:
            return expr.embed()
        if expr.jtype == "long" and dest == "double":
            return f"((double) {expr.embed()})"
        self.fail(
            f"cannot use {TYPE_NAME.get(expr.jtype, expr.jtype)} "
            f"where {TYPE_NAME.get(dest, dest)} is required"
        )

    def _expression(self, expr):
        kind = expr.get("type")
        if kind == "constant":
            return self._constant(expr)
        if kind == "name":
            return self._name(expr)
        if kind == "unary":
            return self._unary(expr)
        if kind == "binop":
            return self._binop(expr)
        if kind == "boolop":
            return self._boolop(expr)
        if kind == "compare":
            return self._compare(expr)
        if kind == "ifexp":
            return self._ifexp(expr)
        if kind == "call":
            return self._call(expr)
        if kind == "list":
            return self._list(expr)
        if kind == "dict":
            return self._dict(expr)
        if kind == "subscript":
            return self._subscript(expr)
        if kind == "attribute":
            self.fail("attribute access is outside the v1 subset")
        if kind == "unknown":
            self.fail(f"unmodeled syntax {expr.get('node')}")
        self.fail(f"{kind} is outside the v1 subset")

    def _constant(self, expr):
        kind = expr.get("kind")
        value = expr.get("value")
        if kind == "bool":
            return Expr("true" if value else "false", "boolean", atom=True)
        if kind == "int":
            return _long_expr(value, self.fail)
        if kind == "float":
            return Expr(_format_double(value, self.fail), "double", atom=True)
        if kind == "str":
            return Expr(_java_string(value), "String", atom=True)
        if kind == "null":
            self.fail("None is outside the v1 subset except as a bare return")
        self.fail(f"{kind} literals are outside the v1 subset")

    def _name(self, expr):
        ident = expr.get("id")
        if ident == self.prepared.receiver:
            self.fail("using the receiver as a value is outside the v1 subset")
        if ident not in self.prepared.jtype:
            self.fail(f"name {ident} is not a parameter or local with a supported type")
        return Expr(self.prepared.jname[ident], self.prepared.jtype[ident], atom=True)

    def _list(self, expr):
        elements = expr.get("elts") or []
        if any(item.get("type") == "starred" for item in elements):
            self.fail("starred list elements are outside the v1 subset")
        if not elements:
            self.fail("empty lists are outside the v1 subset")
        typed = [self._expression(item) for item in elements]
        kinds = {item.jtype for item in typed}
        if len(kinds) != 1 or typed[0].jtype not in BOXED:
            self.fail("list elements must share one supported type")
        element = typed[0].jtype
        prelude = []
        args = []
        for item in typed:
            prelude.extend(item.prelude)
            args.append(item.embed())
        helper = {
            "long": "listLong",
            "double": "listDouble",
            "boolean": "listBoolean",
            "String": "listString",
        }[element]
        code = f"{RUNTIME_FQN}.{helper}({', '.join(args)})"
        return Expr(code, _list_jtype(element), prelude, atom=True)

    def _dict(self, expr):
        keys = expr.get("keys") or []
        values = expr.get("values") or []
        if any(key is None for key in keys):
            self.fail("dict unpacking is outside the v1 subset")
        if len(keys) != len(values):
            self.fail("dict is malformed")
        if not keys:
            self.fail("empty dicts are outside the v1 subset")
        typed_keys = [self._expression(key) for key in keys]
        typed_values = [self._expression(value) for value in values]
        key_kinds = {item.jtype for item in typed_keys}
        value_kinds = {item.jtype for item in typed_values}
        if len(key_kinds) != 1 or len(value_kinds) != 1:
            self.fail("dict entries must share one key type and one value type")
        if typed_keys[0].jtype not in BOXED or typed_values[0].jtype not in BOXED:
            self.fail("dict key or value type is outside the v1 subset")
        key_j = typed_keys[0].jtype
        value_j = typed_values[0].jtype
        prelude = []
        args = []
        for key, value in zip(typed_keys, typed_values):
            prelude.extend(key.prelude)
            prelude.extend(value.prelude)
            args.append(key.embed())
            args.append(value.embed())
        code = f"{RUNTIME_FQN}.<{BOXED[key_j]}, {BOXED[value_j]}>map({', '.join(args)})"
        return Expr(code, _map_jtype(key_j, value_j), prelude, atom=True)

    def _subscript(self, expr):
        sl = expr.get("slice") or {}
        if sl.get("type") == "slice":
            self.fail("slicing is outside the v1 subset")
        container = self._expression(expr.get("value"))
        index = self._expression(sl)
        prelude = container.prelude + index.prelude
        element = _list_element(container.jtype)
        if element is not None:
            if index.jtype != "long":
                self.fail("list index must be int")
            access = (
                f"({RUNTIME_FQN}.<{BOXED[element]}>at({container.embed()}, {index.embed()}))"
            )
            return Expr(_unbox(access, element), element, prelude, atom=True)
        mapped = _map_types(container.jtype)
        if mapped is None:
            self.fail("subscript is outside the v1 subset")
        key_j, value_j = mapped
        if index.jtype != key_j:
            self.fail("dict key has the wrong type")
        access = (
            f"({RUNTIME_FQN}.<{BOXED[key_j]}, {BOXED[value_j]}>get"
            f"({container.embed()}, {self._boxed(index, key_j)}))"
        )
        return Expr(_unbox(access, value_j), value_j, prelude, atom=True)

    def _set_subscript(self, target, value):
        expr = self._expression(value)
        if expr.jtype == "void":
            self.fail("void function cannot be used as a value")
        self._prelude(expr)
        temp = self.fresh("stored")
        self.writer.line(f"{expr.jtype} {temp} = {expr.embed()};")
        stored = Expr(temp, expr.jtype, atom=True)
        sl = target.get("slice") or {}
        if sl.get("type") == "slice":
            self.fail("slicing is outside the v1 subset")
        container = self._expression(target.get("value"))
        index = self._expression(sl)
        self._prelude(container)
        self._prelude(index)
        element = _list_element(container.jtype)
        if element is not None:
            if index.jtype != "long":
                self.fail("list index must be int")
            boxed = self._boxed(stored, element)
            self.writer.line(
                f"{RUNTIME_FQN}.<{BOXED[element]}>set"
                f"({container.embed()}, {index.embed()}, {boxed});"
            )
            return
        mapped = _map_types(container.jtype)
        if mapped is None:
            self.fail("subscript assignment is outside the v1 subset")
        key_j, value_j = mapped
        if index.jtype != key_j:
            self.fail("dict key has the wrong type")
        self.writer.line(
            f"{RUNTIME_FQN}.<{BOXED[key_j]}, {BOXED[value_j]}>put"
            f"({container.embed()}, {self._boxed(index, key_j)}, {self._boxed(stored, value_j)});"
        )

    def _boxed(self, expr, dest):
        converted = self._as_type(expr, dest)
        if dest == "long":
            return f"Long.valueOf({converted})"
        if dest == "double":
            return f"Double.valueOf({converted})"
        if dest == "boolean":
            return f"Boolean.valueOf({converted})"
        if dest == "String":
            return converted
        self.fail(f"cannot box {dest}")

    def _unary(self, expr):
        op = expr.get("op")
        operand = self._expression(expr["operand"])
        if op == "not":
            if operand.jtype != "boolean":
                self.fail("not requires a bool operand; other values use Python truthiness")
            return Expr(f"!{operand.embed()}", "boolean", operand.prelude)
        if op == "~":
            if operand.jtype != "long":
                self.fail("~ requires an int operand")
            return Expr(f"~{operand.embed()}", "long", operand.prelude)
        if op in {"+", "-"}:
            if operand.jtype not in NUMERIC:
                self.fail(f"unary {op} requires a number")
            return Expr(f"{op}{operand.embed()}", operand.jtype, operand.prelude)
        self.fail(f"unary {op} is outside the v1 subset")

    def _binop(self, expr):
        op = expr.get("op")
        if op in {"<<", ">>"}:
            self.fail(
                "shift operators are outside the v1 subset because Java masks the shift count"
            )
        if op == "**":
            self.fail("exponentiation is outside the v1 subset")
        if op == "@":
            self.fail("matrix multiplication is outside the v1 subset")
        left = self._expression(expr["left"])
        right = self._expression(expr["right"])
        prelude = left.prelude + right.prelude
        if op == "+" and left.jtype == "String" and right.jtype == "String":
            return Expr(f"{left.embed()} + {right.embed()}", "String", prelude)
        if op == "*" and "String" in {left.jtype, right.jtype}:
            self.fail("string repetition is outside the v1 subset")
        if left.jtype not in NUMERIC or right.jtype not in NUMERIC:
            self.fail(f"operator {op} is not supported for these types")
        if op in {"//", "%"}:
            if left.jtype != "long" or right.jtype != "long":
                self.fail("floor division and modulo of floats are outside the v1 subset")
            func = "Math.floorDiv" if op == "//" else "Math.floorMod"
            return Expr(
                f"{func}({left.embed()}, {right.embed()})",
                "long",
                prelude,
                atom=True,
            )
        if op == "/":
            if left.jtype == "long" and right.jtype == "long":
                return Expr(
                    f"{RUNTIME_FQN}.divide({left.embed()}, {right.embed()})",
                    "double",
                    prelude,
                    atom=True,
                )
            return Expr(
                f"{self._as_type(left, 'double')} / {self._as_type(right, 'double')}",
                "double",
                prelude,
            )
        if op in {"|", "^", "&"}:
            if left.jtype != "long" or right.jtype != "long":
                self.fail(f"operator {op} requires ints")
            return Expr(f"{left.embed()} {op} {right.embed()}", "long", prelude)
        if op in {"+", "-", "*"}:
            common = "double" if "double" in {left.jtype, right.jtype} else "long"
            return Expr(
                f"{self._as_type(left, common)} {op} {self._as_type(right, common)}",
                common,
                prelude,
            )
        self.fail(f"operator {op} is outside the v1 subset")

    def _boolop(self, expr):
        parts = []
        for value in expr.get("values") or []:
            item = self._expression(value)
            if item.prelude:
                self.fail(
                    "and/or cannot contain a short-circuit operand that needs a temporary"
                )
            if item.jtype != "boolean":
                self.fail(
                    "and/or is only translated when every operand is bool; "
                    "Python returns the operand, not a boolean"
                )
            parts.append(item.embed())
        if expr.get("op") not in {"and", "or"} or len(parts) < 2:
            self.fail("and/or needs at least two operands")
        joiner = " && " if expr.get("op") == "and" else " || "
        return Expr(joiner.join(parts), "boolean")

    def _compare(self, expr):
        ops = list(expr.get("ops") or [])
        operands = [expr["left"], *(expr.get("comparators") or [])]
        if len(ops) != len(operands) - 1 or not ops:
            self.fail("comparison is malformed")
        for op in ops:
            if op in {"is", "is not", "in", "not in"}:
                self.fail(f"{op} is outside the v1 subset")
        if len(ops) > 2 and any(not _is_pure(item) for item in operands[1:-1]):
            self.fail("chained comparison with side effects is outside the v1 subset")
        typed = [self._expression(item) for item in operands]
        common = self._compare_type(typed, ops)
        prelude = []
        codes = []
        for index, item in enumerate(typed):
            needs_temp = (
                len(operands) >= 3
                and 0 < index < len(operands) - 1
                and not _is_pure(operands[index])
            )
            if needs_temp:
                prelude.extend(item.prelude)
                temp = self.fresh("cmp")
                prelude.append(f"{item.jtype} {temp} = {item.embed()};")
                codes.append(Expr(temp, item.jtype, atom=True))
                continue
            if index == len(operands) - 1:
                if item.prelude:
                    self.fail(
                        "comparison operand needs a temporary in a short-circuit position"
                    )
                codes.append(item)
                continue
            if item.prelude:
                self.fail("comparison operand needs a temporary in a short-circuit position")
            prelude.extend(item.prelude)
            codes.append(item)
        parts = []
        for op, left, right in zip(ops, codes, codes[1:]):
            parts.append(f"({self._compare_one(op, left, right, common)})")
        return Expr(" && ".join(parts), "boolean", prelude)

    def _compare_type(self, typed, ops):
        kinds = {item.jtype for item in typed}
        if kinds <= NUMERIC:
            return "double" if "double" in kinds else "long"
        if kinds == {"String"}:
            return "String"
        if kinds == {"boolean"}:
            if any(op not in {"==", "!="} for op in ops):
                self.fail("bool only supports == and !=")
            return "boolean"
        self.fail("comparison operands have incompatible types")

    def _compare_one(self, op, left, right, common):
        left_code = self._as_type(left, common)
        right_code = self._as_type(right, common)
        if common == "String":
            if op == "==":
                return f"{left_code}.equals({right_code})"
            if op == "!=":
                return f"!{left_code}.equals({right_code})"
            if op in {"<", "<=", ">", ">="}:
                return f"{left_code}.compareTo({right_code}) {op} 0"
            self.fail(f"{op} is outside the v1 subset")
        if op not in {"==", "!=", "<", "<=", ">", ">="}:
            self.fail(f"{op} is outside the v1 subset")
        return f"{left_code} {op} {right_code}"

    def _ifexp(self, expr):
        test = self._expression(expr["test"])
        body = self._expression(expr["body"])
        other = self._expression(expr["orelse"])
        if test.prelude or body.prelude or other.prelude:
            self.fail("conditional expression needs a temporary")
        if test.jtype != "boolean":
            self.fail("conditional expression test must be bool")
        if body.jtype == "void" or other.jtype == "void":
            self.fail("void function cannot be used as a value")
        if body.jtype == other.jtype:
            common = body.jtype
        elif {body.jtype, other.jtype} <= NUMERIC:
            common = "double"
        else:
            self.fail("conditional expression branches have incompatible types")
        code = (
            f"{test.embed()} ? {self._as_type(body, common)} : {self._as_type(other, common)}"
        )
        return Expr(code, common)

    def _call(self, expr):
        if expr.get("keywords") or any(arg.get("type") == "starred" for arg in expr.get("args") or []):
            self.fail("starred and keyword arguments are outside the v1 subset")
        name = _callee_name(expr.get("func"))
        if not name:
            self.fail("call target is not a dotted name")
        record = self.resolved.get(f"{name}()")
        if record is None:
            self.fail(f"call {name}() is not in resolved_calls")
        classification = record.get("classification")
        if classification == "python_builtin":
            return self._builtin(name, expr)
        if classification != "resolved":
            self.fail(f"call {name}() is {classification}")
        targets = record.get("targets") or []
        if len(targets) != 1:
            self.fail(f"call {name}() does not resolve to one function")
        return self._resolved_call(name, expr, targets[0])

    def _builtin(self, name, expr):
        if name not in {"abs", "min", "max"}:
            self.fail(f"builtin {name}() is outside the v1 subset")
        args = [self._expression(arg) for arg in expr.get("args") or []]
        prelude = []
        for arg in args:
            prelude.extend(arg.prelude)
        if name == "abs":
            if len(args) != 1:
                self.fail("abs() takes one argument")
            arg = args[0]
            if arg.jtype == "long":
                return Expr(f"{RUNTIME_FQN}.abs({arg.embed()})", "long", prelude, atom=True)
            if arg.jtype == "double":
                return Expr(f"Math.abs({arg.embed()})", "double", prelude, atom=True)
            self.fail("abs() requires a number")
        if len(args) < 2:
            self.fail(f"{name}() needs at least two arguments")
        kinds = {arg.jtype for arg in args}
        if kinds == {"long"} or kinds == {"double"}:
            func = "Math.min" if name == "min" else "Math.max"
            code = args[0].embed()
            for arg in args[1:]:
                code = f"{func}({code}, {arg.embed()})"
            return Expr(code, args[0].jtype, prelude, atom=True)
        self.fail(f"{name}() arguments must be the same numeric type")

    def _resolved_call(self, name, expr, target):
        target_id = target.get("id")
        if not isinstance(target_id, str) or not (
            target_id.startswith("function:") or target_id.startswith("method:")
        ):
            self.fail(f"call {name}() does not resolve to a function")
        if target_id not in self.pending:
            reason = self.failures.get(target_id)
            detail = reason.reason if isinstance(reason, Unsupported) else "it is outside the v1 subset"
            self.fail(f"call {name}() was not translated: {detail}")
        callee = self.names[target_id]
        if self.java_name.package and not callee.package:
            self.fail(
                f"call {name}() targets {callee.class_name} in the Java default package, "
                "which a named package cannot reference"
            )
        prepared = self.records[target_id].prepared
        args = expr.get("args") or []
        if len(args) != len(prepared.params):
            self.fail(
                f"call {name}() passes {len(args)} arguments, expected {len(prepared.params)}"
            )
        typed = [self._expression(arg) for arg in args]
        prelude = []
        rendered = []
        for arg, (dest, _param_name) in zip(typed, prepared.params):
            prelude.extend(arg.prelude)
            if arg.jtype == "void":
                self.fail("void function cannot be used as a value")
            rendered.append(self._as_type(arg, dest))
        code = f"{callee.qualifier}.{callee.method}({', '.join(rendered)})"
        return Expr(code, prepared.return_jtype, prelude, atom=True)

    def _is_direct_builtin(self, expr, expected):
        if expr.get("type") != "call":
            return False
        name = _callee_name(expr.get("func"))
        if name != expected:
            return False
        record = self.resolved.get(f"{name}()")
        return bool(record) and record.get("classification") == "python_builtin"


def _long_expr(value, fail=None):
    if isinstance(value, bool) or not isinstance(value, int):
        if fail is None:
            raise Unsupported("int literal is not an integer")
        fail("int literal is not an integer")
    if value < LONG_MIN or value > LONG_MAX:
        message = f"int literal {value} does not fit in a Java long"
        if fail is None:
            raise Unsupported(message)
        fail(message)
    return Expr(f"{value}L", "long", atom=True)


def _format_double(value, fail):
    if value == "nan":
        return "Double.NaN"
    if value == "inf":
        return "Double.POSITIVE_INFINITY"
    if value == "-inf":
        return "Double.NEGATIVE_INFINITY"
    try:
        number = float(value)
    except (TypeError, ValueError):
        fail("float literal is not a number")
    if number != number:
        return "Double.NaN"
    if number == float("inf"):
        return "Double.POSITIVE_INFINITY"
    if number == float("-inf"):
        return "Double.NEGATIVE_INFINITY"
    text = repr(number)
    if "e" not in text and "E" not in text and "." not in text:
        text += ".0"
    return text


def _java_string(value):
    if not isinstance(value, str):
        raise Unsupported("str literal is not a string")
    pieces = ['"']
    for char in value:
        code = ord(char)
        if char == "\\":
            pieces.append("\\\\")
        elif char == '"':
            pieces.append('\\"')
        elif char == "\n":
            pieces.append("\\n")
        elif char == "\r":
            pieces.append("\\r")
        elif char == "\t":
            pieces.append("\\t")
        elif code < 32 or code == 127:
            pieces.append(f"\\u{code:04x}")
        else:
            pieces.append(char)
    pieces.append('"')
    return "".join(pieces)


def _is_null(expr):
    return isinstance(expr, dict) and expr.get("type") == "constant" and expr.get("kind") == "null"


def _is_pure(expr):
    if not isinstance(expr, dict):
        return False
    kind = expr.get("type")
    if kind in {"constant", "name"}:
        return True
    if kind == "unary":
        return _is_pure(expr.get("operand"))
    if kind == "binop":
        return _is_pure(expr.get("left")) and _is_pure(expr.get("right"))
    if kind == "compare":
        return _is_pure(expr.get("left")) and all(
            _is_pure(item) for item in expr.get("comparators") or []
        )
    if kind == "boolop":
        return all(_is_pure(item) for item in expr.get("values") or [])
    if kind == "ifexp":
        return _is_pure(expr.get("test")) and _is_pure(expr.get("body")) and _is_pure(expr.get("orelse"))
    return False


def _callee_name(expr):
    if not isinstance(expr, dict):
        return None
    if expr.get("type") == "name" and isinstance(expr.get("id"), str):
        return expr["id"]
    if expr.get("type") == "attribute" and isinstance(expr.get("attr"), str):
        base = _callee_name(expr.get("value"))
        if base is None:
            return None
        return f"{base}.{expr['attr']}"
    return None


def _list_jtype(element):
    return "java.util.List<" + BOXED[element] + ">"


def _map_jtype(key, value):
    return "java.util.Map<" + BOXED[key] + "," + BOXED[value] + ">"


def _generic_args(jtype, raw):
    prefix = f"java.util.{raw}<"
    if isinstance(jtype, str) and jtype.startswith(prefix) and jtype.endswith(">"):
        return jtype[len(prefix):-1]
    return None


def _list_element(jtype):
    boxed = _generic_args(jtype, "List")
    if boxed is None:
        return None
    return UNBOX.get(boxed)


def _map_types(jtype):
    inner = _generic_args(jtype, "Map")
    if inner is None or "," not in inner:
        return None
    key_box, value_box = inner.split(",", 1)
    key = UNBOX.get(key_box)
    value = UNBOX.get(value_box)
    if key is None or value is None:
        return None
    return key, value


def _unbox(access, jtype):
    if jtype == "long":
        return f"{access}.longValue()"
    if jtype == "double":
        return f"{access}.doubleValue()"
    if jtype == "boolean":
        return f"{access}.booleanValue()"
    return access


def _render_files(records, emitted, names):
    grouped = defaultdict(list)
    for fid, source in emitted.items():
        record = records[fid]
        java_name = names[fid]
        grouped[(java_name.package, java_name.class_name, record.file)].append(
            (record.detail["line"], record.detail["name"], source)
        )
    files = []
    for package, class_name, path in sorted(grouped):
        methods = sorted(grouped[(package, class_name, path)])
        text = "\n\n".join(source for _line, _name, source in methods)
        files.append(
            GeneratedFile(
                _java_relative(package, class_name),
                _render_class(package, class_name, path, text),
            )
        )
    return files


def _render_class(package, class_name, path, methods_text):
    lines = [
        f"// Translated from {path} by Translate (RepoMap schema {SCHEMA_VERSION}).",
        "",
    ]
    if package:
        lines.append(f"package {package};")
        lines.append("")
    lines.append(f"public final class {class_name} {{")
    lines.append(f"    private {class_name}() {{")
    lines.append("    }")
    if methods_text:
        lines.append("")
        lines.append(_indent(methods_text))
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def _indent(text):
    return "\n".join(("    " + line) if line else "" for line in text.splitlines())


def _java_relative(package, class_name):
    segments = ["src", "main", "java"]
    if package:
        segments.extend(package.split("."))
    segments.append(f"{class_name}.java")
    return "/".join(segments)


def _report(records, pending, failures, names):
    translated = []
    for fid in sorted(pending):
        record = records[fid]
        detail = record.detail
        java_name = names[fid]
        translated.append(
            {
                "id": fid,
                "name": detail["name"],
                "file": record.file,
                "line": detail["line"],
                "java": java_name.fqn,
            }
        )
    unsupported = []
    for fid in sorted(failures):
        record = records[fid]
        detail = record.detail
        exc = failures[fid]
        unsupported.append(
            {
                "id": fid,
                "name": detail["name"],
                "file": record.file,
                "line": exc.line if exc.line is not None else detail.get("line"),
                "reason": exc.reason,
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "target": "java",
        "java_release": JAVA_RELEASE,
        "limitations": list(LIMITATIONS),
        "translated": translated,
        "unsupported": unsupported,
    }


def write_project(translation, output):
    """Write sources and ``translation_report.json``. ``src/main/java`` is replaced."""
    root = Path(output)
    java_root = root / "src" / "main" / "java"
    if java_root.exists():
        shutil.rmtree(java_root)
    for generated in translation.files:
        destination = root.joinpath(*Path(generated.path).parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(generated.source, encoding="utf-8", newline="\n")
    report_path = root / "translation_report.json"
    report_path.write_text(
        json.dumps(translation.report, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return root


def compile_project(output):
    root = Path(output)
    java_root = root / "src" / "main" / "java"
    classes = root / "classes"
    if classes.exists():
        shutil.rmtree(classes)
    classes.mkdir(parents=True, exist_ok=True)
    sources = sorted(java_root.rglob("*.java")) if java_root.exists() else []
    if not sources:
        return CompileResult(True, "", "")
    javac = shutil.which("javac")
    if javac is None:
        return CompileResult(False, "", "javac was not found on PATH")
    command = [
        javac,
        "--release",
        str(JAVA_RELEASE),
        "-encoding",
        "UTF-8",
        "-d",
        str(classes),
        *[str(source) for source in sources],
    ]
    completed = subprocess.run(command, capture_output=True, text=True)
    return CompileResult(completed.returncode == 0, completed.stdout, completed.stderr)
