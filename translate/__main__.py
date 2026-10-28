import argparse
import json
import sys
from pathlib import Path

from translate.ir import MapError, load_repository_map
from translate.java import compile_project, translate, write_project


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Translate a RepoMap repository_map.json (schema 1.3.0) into a Java project."
        )
    )
    parser.add_argument("map", type=Path, help="Path to repository_map.json")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        required=True,
        help="Directory to write the Java project into. src/main/java is replaced.",
    )
    args = parser.parse_args(argv)
    try:
        translation = translate(load_repository_map(args.map))
    except MapError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except OSError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    write_project(translation, args.output)
    compiled = compile_project(args.output)
    report_path = args.output / "translation_report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["compile"] = {"ok": compiled.ok, "stderr": compiled.stderr}
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    translated = len(translation.report["translated"])
    skipped = len(translation.report["unsupported"])
    status = "compiled" if compiled.ok else "compile failed"
    print(f"translated {translated}, unsupported {skipped}, {status}")
    if not compiled.ok:
        if compiled.stderr:
            print(compiled.stderr, file=sys.stderr, end="" if compiled.stderr.endswith("\n") else "\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
