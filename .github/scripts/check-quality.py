"""Read-only quality checks with protected rules and source-bound exception evidence."""

from __future__ import annotations

import argparse
import ast
import builtins
from collections import Counter, deque
from contextlib import redirect_stderr, redirect_stdout
import datetime
import graphlib
import hashlib
import importlib.metadata
import importlib.util
import json
import logging
import io
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import tomllib
import tokenize

CHECKS = ("lint", "typing", "architecture", "exceptions", "policy")
REQUIRED_JOBS = (*CHECKS, "unit-tests", "container-tests")
LOGGER = logging.getLogger("quality")
TIMEOUT = 300
RECORD_NAMES = ("policy", "typing-scope", "typing-baseline", "exceptions")
STAGES = ("bootstrap", "blocking", "strict")
TOOLS = ("ruff", "mypy", "import-linter", "grimp")
NON_RUNTIME_DIRECTORIES = {"tests", "scripts", "docs", "infra", "public", "build", "dist", "node_modules"}
BUILTIN_EXCEPTIONS = {name for name, value in vars(builtins).items()
                      if isinstance(value, type) and issubclass(value, BaseException)}
PROTECTED_FILES = (".github/scripts/check-quality.py", ".github/scripts/run-unittest.py",
                   ".github/scripts/aggregate-quality.py", ".github/workflows/tests.yml",
                   ".github/CODEOWNERS", "requirements-quality.txt", ".quality/migration.json",
                   "tests/container_smoke.py")


class PolicyError(RuntimeError):
    """Invalid inputs or incomplete execution; never a passing check."""


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PolicyError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def parse_json(text):
    return json.loads(text, object_pairs_hook=object_pairs,
                      parse_constant=lambda value: invalid(f"Non-finite JSON value: {value}"))


def invalid(message):
    raise PolicyError(message)


def exact_object(value, required, optional=()):
    if not isinstance(value, dict) or set(value) - set(required) - set(optional) or set(required) - set(value):
        invalid(f"Expected exact fields: {', '.join(required)}")


def text(value):
    if not isinstance(value, str) or not value.strip():
        invalid("Expected nonempty string")


def member(value):
    text(value)
    if not value.isidentifier():
        invalid(f"Invalid member name: {value}")


def module_name(value):
    text(value)
    if not all(part.isidentifier() for part in value.split(".")):
        invalid(f"Invalid import name: {value}")


def relative_path(value):
    text(value)
    if "\\" in value or ":" in value or value.startswith("/") or any(
        part in {"", ".", ".."} for part in value.split("/")
    ):
        invalid(f"Expected repository-relative POSIX path: {value}")


def strings(value, validator=text, *, nonempty=False):
    if not isinstance(value, list) or (nonempty and not value):
        invalid("Expected list")
    for item in value:
        validator(item)
    if len(value) != len(set(value)):
        invalid("Duplicate list entry")


def fingerprint(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        invalid("Expected SHA256 fingerprint")


def revision(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        invalid("Expected exact Git revision")


def choice(value, options):
    if value not in options:
        invalid(f"Expected one of: {', '.join(options)}")


def test_selector(value):
    text(value)
    path, separator, symbol = value.partition("::")
    relative_path(path)
    if not separator or not path.endswith(".py") or not path.startswith("tests/"):
        invalid("Evidence must name tests/file.py::Class.test_method")
    module_name(symbol)
    if "." not in symbol or not symbol.rsplit(".", 1)[1].startswith("test"):
        invalid("Evidence must name an exact unittest method")


def validate_record(name, record):
    headers = {
        "policy": ("schema_version", "runtime_roots", "modules", "toolchain", "required_checks",
                   "forbidden", "private_allowances", "dynamic_imports", "review"),
        "typing-scope": ("schema_version", "module_ids", "coverage_stage", "planned_expansion", "move_map", "review"),
        "typing-baseline": ("schema_version", "entries"),
        "exceptions": ("schema_version", "entries"),
    }
    exact_object(record, headers[name])
    if type(record["schema_version"]) is not int or record["schema_version"] != 1:
        invalid("Unsupported schema version")
    if name == "policy":
        strings(record["runtime_roots"], relative_path, nonempty=True)
        exact_object(record["toolchain"], TOOLS)
        for version in record["toolchain"].values():
            if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+(?:\.\d+)?", version):
                invalid("Tool versions must be exact")
        strings(record["required_checks"])
        if set(record["required_checks"]) != set(REQUIRED_JOBS):
            invalid("Required check set cannot change")
        exact_object(record["review"], ("owner", "reference", "state", "reason"))
        for value in record["review"].values():
            text(value)
        choice(record["review"]["state"], ("proposed", "maintainer-approved", "active", "retired"))
        for field in ("modules", "forbidden", "private_allowances", "dynamic_imports"):
            if not isinstance(record[field], list):
                invalid(f"Expected list: {field}")
        for item in record["modules"]:
            exact_object(item, ("id", "path", "import_name", "area", "public_exports", "legacy_aliases",
                                "source_revision", "responsibilities", "private_modules",
                                "allowed_importers", "typing_status"), ("adapter",))
            text(item["id"])
            relative_path(item["path"])
            if not item["path"].endswith(".py"):
                invalid("Runtime inventory must identify Python source")
            module_name(item["import_name"])
            choice(item["area"], ("legacy", "bootstrap", "api", "services", "clients", "auth",
                                  "telemetry", "config", "util", "package"))
            text(item["responsibilities"])
            revision(item["source_revision"])
            strings(item["public_exports"], member)
            for field in ("legacy_aliases", "private_modules", "allowed_importers"):
                strings(item[field], module_name)
            choice(item["typing_status"], ("uncovered", "blocking"))
            if item["area"] == "legacy":
                if "adapter" not in item:
                    invalid("Legacy adapter needs an exact forwarding declaration")
                adapter = item["adapter"]
                exact_object(adapter, ("exports", "calls", "source_fingerprint"))
                if not isinstance(adapter["exports"], dict):
                    invalid("Expected adapter export map")
                for binding, target in adapter["exports"].items():
                    member(binding)
                    module_name(target)
                strings(adapter["calls"])
                fingerprint(adapter["source_fingerprint"])
            elif "adapter" in item:
                invalid("Only legacy modules can declare adapters")
        for item in record["forbidden"]:
            exact_object(item, ("from", "to"))
            module_name(item["from"])
            module_name(item["to"])
        for item in record["private_allowances"]:
            exact_object(item, ("importer", "target", "member"))
            module_name(item["importer"])
            module_name(item["target"])
            member(item["member"])
        for item in record["dynamic_imports"]:
            exact_object(item, ("module", "symbol", "site_fingerprint", "targets", "evidence_tests", "reason", "review"))
            module_name(item["module"])
            text(item["symbol"])
            fingerprint(item["site_fingerprint"])
            strings(item["targets"], module_name, nonempty=True)
            strings(item["evidence_tests"], test_selector, nonempty=True)
            text(item["reason"])
            text(item["review"])
    elif name == "typing-scope":
        strings(record["module_ids"], nonempty=True)
        choice(record["coverage_stage"], STAGES)
        strings(record["planned_expansion"], nonempty=True)
        text(record["review"])
        if not isinstance(record["move_map"], dict):
            invalid("Expected move map")
        for before, after in record["move_map"].items():
            relative_path(before)
            relative_path(after)
            if before == after:
                invalid("Move source equals destination")
        if len(set(record["move_map"].values())) != len(record["move_map"]):
            invalid("Move destinations must be one-to-one")
    else:
        if not isinstance(record["entries"], list):
            invalid("Expected entries list")
        for entry in record["entries"]:
            common = ("id", "module_id", "symbol", "review")
            if name == "typing-baseline":
                exact_object(entry, (*common, "source_fingerprint", "rule", "message_fingerprint",
                                     "occurrences", "introduced_at", "rationale", "removal_stage"))
                fingerprint(entry["source_fingerprint"])
                fingerprint(entry["message_fingerprint"])
                revision(entry["introduced_at"])
                if type(entry["occurrences"]) is not int or entry["occurrences"] < 1:
                    invalid("Debt occurrences must be a positive integer")
                for field in ("rule", "rationale", "removal_stage"):
                    text(entry[field])
            else:
                exact_object(entry, (*common, "handler_fingerprint", "caught_types", "boundary", "reason",
                                     "failure_outcome", "diagnostic_path", "evidence_tests", "state",
                                     "review_by_stage", "expires_on"))
                fingerprint(entry["handler_fingerprint"])
                strings(entry["caught_types"], nonempty=True)
                strings(entry["evidence_tests"], test_selector, nonempty=True)
                for field in ("boundary", "reason", "diagnostic_path"):
                    text(entry[field])
                choice(entry["failure_outcome"], ("propagation", "failure-translation", "cleanup-propagation",
                                                   "contractual-best-effort"))
                choice(entry["state"], ("proposed", "maintainer-approved", "active", "retired"))
                choice(entry["review_by_stage"], STAGES)
                text(entry["expires_on"])
                try:
                    date = datetime.date.fromisoformat(entry["expires_on"])
                except ValueError as exc:
                    raise PolicyError("Invalid exception expiry") from exc
                if date.isoformat() != entry["expires_on"]:
                    invalid("Expiry must be YYYY-MM-DD")
            for field in common:
                text(entry[field])
        ids = [entry["id"] for entry in record["entries"]]
        if len(ids) != len(set(ids)):
            invalid("Duplicate record ID")


def validate_records(records):
    exact_object(records, RECORD_NAMES)
    for name, record in records.items():
        validate_record(name, record)
    modules = records["policy"]["modules"]
    for field in ("id", "path", "import_name"):
        values = [m[field].casefold() if field == "path" else m[field] for m in modules]
        if len(values) != len(set(values)):
            invalid(f"Duplicate module {field}")
    ids = {m["id"] for m in modules}
    imports = {m["import_name"] for m in modules}
    scope = set(records["typing-scope"]["module_ids"])
    if not scope <= ids:
        invalid("Typing scope references unknown module IDs")
    for entry in modules:
        if (entry["typing_status"] == "blocking") != (entry["id"] in scope):
            invalid("Module typing status disagrees with blocking scope")
        if not set(entry["allowed_importers"]) <= imports or not set(entry["private_modules"]) <= imports:
            invalid("Private ownership references unknown modules")
    for name in ("typing-baseline", "exceptions"):
        keys = []
        for entry in records[name]["entries"]:
            if entry["module_id"] not in (scope if name == "typing-baseline" else ids):
                invalid("Debt/exception references an unknown or uncovered owner")
            keys.append(debt_key(entry) if name == "typing-baseline" else (
                entry["module_id"], entry["symbol"], entry["handler_fingerprint"]))
        if len(keys) != len(set(keys)):
            invalid("Multiple records cannot allocate the same source site")
    for entry in records["policy"]["dynamic_imports"]:
        if entry["module"] not in imports:
            invalid("Dynamic import references unknown importer")
    for entry in records["policy"]["private_allowances"]:
        if entry["importer"] not in imports or entry["target"] not in imports:
            invalid("Private allowance references unknown source")


def read_record(path):
    try:
        value = parse_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PolicyError(f"Cannot read policy record: {path.name}") from exc
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise PolicyError(f"Unsupported policy record: {path.name}")
    if path.parent.name == ".quality" and path.stem in RECORD_NAMES:
        validate_record(path.stem, value)
    return value


def execute(command, *, cwd, allowed=(0,)):
    try:
        result = subprocess.run(command, cwd=cwd, text=True, encoding="utf-8",
                                capture_output=True, timeout=TIMEOUT, check=False,
                                env=os.environ.copy())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PolicyError(f"Tool did not complete: {command[0]}") from exc
    if result.returncode not in allowed:
        raise PolicyError(f"Tool failed ({result.returncode}): {command[0]}: {result.stderr[:1000]}")
    return result


def git(root, *args):
    return execute(["git", "--no-pager", *args], cwd=root).stdout.strip()


def architecture_evidence(root, config_path):
    """Use pinned graph tools on an explicit source directory, never an installed UI."""
    import grimp
    from grimp.application.config import settings
    from importlinter import configuration
    from importlinter.application import use_cases

    package = root / "src" / "gpt_rag_ui"
    if not (package / "__init__.py").is_file():
        raise PolicyError("Canonical source package is missing")

    class SourcePackageFinder:
        def determine_package_directories(self, package_name, file_system):
            if package_name != "gpt_rag_ui":
                raise PolicyError(f"Unexpected graph root: {package_name}")
            return {str(package)}

    configuration.configure()
    previous = settings.PACKAGE_FINDER
    settings.configure(PACKAGE_FINDER=SourcePackageFinder())
    output = io.StringIO()
    try:
        graph = grimp.build_graph("gpt_rag_ui", include_external_packages=False, cache_dir=None)
        with redirect_stdout(output), redirect_stderr(output):
            passed = use_cases.lint_imports(
                config_filename=str(config_path), cache_dir=None,
                is_debug_mode=True, no_logo=True,
            )
        return {
            "graph": {name: sorted(graph.find_modules_directly_imported_by(name))
                      for name in sorted(graph.modules)},
            "passed": passed,
            "output": output.getvalue(),
        }
    finally:
        settings.configure(PACKAGE_FINDER=previous)


def isolated_architecture_evidence(root, config_path):
    worker = (
        "import json,runpy,sys; from pathlib import Path; "
        "scope=runpy.run_path(sys.argv[1]); "
        "print(json.dumps(scope['architecture_evidence'](Path(sys.argv[2]),Path(sys.argv[3]))))"
    )
    completed = execute([
        sys.executable, "-I", "-c", worker, str(Path(__file__).resolve()),
        str(root), str(config_path),
    ], cwd=config_path.parent)
    return parse_json(completed.stdout)


def base_text(root, revision, path):
    result = execute(["git", "show", f"{revision}:{path}"], cwd=root, allowed=(0, 128))
    return result.stdout if result.returncode == 0 else None


def discover(root):
    """Discover new root modules/packages independently of candidate policy inventory."""
    files = set(root.glob("*.py"))
    for directory in root.iterdir():
        if not directory.is_dir() or directory.name.startswith(".") or directory.name in NON_RUNTIME_DIRECTORIES:
            continue
        if directory.is_symlink():
            invalid("Runtime directory escapes discovery through a symlink")
        for folder, directories, filenames in directory.walk():
            directories[:] = [name for name in directories if not name.startswith(".")
                              and name not in {"__pycache__", "node_modules"}]
            for name in filenames:
                path = folder / name
                if path.is_symlink():
                    invalid("Symlink in runtime source tree")
                if path.suffix == ".py":
                    files.add(path)
    if not files:
        raise PolicyError("No runtime sources discovered")
    modules = {}
    paths = {}
    for path in sorted(files):
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise PolicyError("Runtime source escapes repository")
        relative = path.relative_to(root).as_posix()
        parts = list(path.relative_to(root).with_suffix("").parts)
        if parts[0] == "src":
            parts.pop(0)
        if parts[-1] == "__init__":
            parts.pop()
        name = ".".join(parts)
        if name in modules:
            raise PolicyError(f"Duplicate module identity: {name}")
        modules[name] = path.read_text(encoding="utf-8")
        paths[name] = relative
    return modules, paths


def finding(rule, module, line, reason, **details):
    return {"rule": rule, "module": module, "line": line, "reason": reason, **details}


def scoped_nodes(tree):
    def walk(node, scope):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            scope = ".".join(filter(None, (scope, node.name)))
        yield node, scope or "<module>"
        for child in ast.iter_child_nodes(node):
            yield from walk(child, scope)
    return walk(tree, "")


def lexical_nodes(tree, scope=()):
    yield tree, scope
    if isinstance(tree, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        body_ids = {id(statement) for statement in tree.body}
        for child in ast.iter_child_nodes(tree):
            yield from lexical_nodes(child, scope + (tree,) if id(child) in body_ids else scope)
    else:
        for child in ast.iter_child_nodes(tree):
            yield from lexical_nodes(child, scope)


class StaticBindings:
    """Resolve a bounded set of lexical bindings without importing application code."""

    def __init__(self, tree):
        self.scopes = dict(lexical_nodes(tree))
        self.parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        self.bindings = {}
        self.attribute_writes = []
        self.modules = set()
        redirects = []
        for node, scope in self.scopes.items():
            if isinstance(node, ast.Import):
                for alias in node.names:
                    parts = alias.name.split(".")
                    self.modules.update(".".join(parts[:index]) for index in range(1, len(parts) + 1))
                    self.store(scope, alias.asname or parts[0], "module",
                               alias.name if alias.asname else parts[0], ast.Import(names=[alias]), original=node)
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    normalized = ast.ImportFrom(module=node.module, names=[alias], level=node.level)
                    self.store(scope, alias.asname or alias.name, "symbol",
                               "." * node.level + f"{node.module or ''}.{alias.name}", normalized, original=node)
            elif isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = "class" if isinstance(node, ast.ClassDef) else "function"
                self.store(scope, node.name, kind, (node, scope), node)
                if kind == "function":
                    for arg in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs,
                                *[arg for arg in (node.args.vararg, node.args.kwarg) if arg]]:
                        self.store(scope + (node,), arg.arg, "unknown", arg.arg, arg)
            elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        self.store(scope, target.id, "expression", node.value, node)
                    elif isinstance(target, ast.Attribute):
                        self.attribute_writes.append((target, node.value, scope, node))
                    else:
                        for part in ast.walk(target):
                            if isinstance(part, ast.Name):
                                self.store(scope, part.id, "unknown", ast.unparse(target), node)
            elif isinstance(node, (ast.Global, ast.Nonlocal)):
                redirects.append((node, scope))
            elif isinstance(node, ast.ExceptHandler) and node.name:
                self.store(scope, node.name, "unknown", node.name, node)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                # Loop/with/exception targets also shadow outer bindings.
                parent = self.parents.get(node)
                if not isinstance(parent, (ast.Assign, ast.AnnAssign, ast.NamedExpr, ast.Tuple, ast.List)):
                    self.store(scope, node.id, "unknown", node.id, parent or node)
        for directive, scope in redirects:
            for name in directive.names:
                entries = self.bindings.get(scope, {}).pop(name, [])
                if isinstance(directive, ast.Global):
                    target = ()
                else:
                    target = next((scope[:index] for index in range(len(scope) - 1, 0, -1)
                                   if name in self.bindings.get(scope[:index], {})), scope)
                if entries:
                    token = self.token(scope, directive) + repr([entry[3] for entry in entries])
                    self.bindings.setdefault(target, {}).setdefault(name, []).append(
                        ("unknown", name, scope, token))

    @staticmethod
    def token(scope, node):
        if isinstance(node, ast.ClassDef):
            source = repr((node.name, [ast.dump(base, include_attributes=False) for base in node.bases],
                           [ast.dump(item, include_attributes=False) for item in node.keywords],
                           [ast.dump(item, include_attributes=False) for item in node.decorator_list]))
        else:
            source = ast.dump(node, include_attributes=False)
        return ".".join(part.name for part in scope) + ":" + source

    def unconditional(self, node):
        return isinstance(self.parents.get(node), (ast.Module, ast.ClassDef, ast.FunctionDef,
                                                  ast.AsyncFunctionDef, ast.arguments))

    def store(self, scope, name, kind, value, node, *, original=None):
        origin = original or node
        token = self.token(scope, node)
        if not self.unconditional(origin):
            if kind not in {"module", "symbol"}:
                kind, value = "unknown", "conditional-binding"
            parent = self.parents.get(origin)
            if parent is not None:
                token += self.token(scope, parent)
        self.bindings.setdefault(scope, {}).setdefault(name, []).append(
            (kind, value, scope, token))

    def lookup(self, scope, name, seen):
        for index in range(len(scope), -1, -1):
            current = scope[:index]
            if current and isinstance(current[-1], ast.ClassDef) and index < len(scope):
                continue
            values = self.bindings.get(current, {})
            if "*" in values:
                return ("unknown", name, tuple(entry[3] for entry in values["*"]))
            if name in values:
                return self.evaluate(values[name], (current, name), seen)
        if name in BUILTIN_EXCEPTIONS or name == "object":
            return ("symbol", f"builtins.{name}", ())
        return ("unknown", name, ())

    def evaluate(self, entries, key, seen):
        trace = tuple(entry[3] for entry in entries)
        if key in seen or len(entries) != 1:
            return ("unknown", "ambiguous-or-cyclic-binding", trace)
        kind, value, scope, _ = entries[0]
        if kind == "expression":
            resolved = self.resolve(value, scope, seen | {key})
            return (resolved[0], resolved[1], (*trace, *resolved[2]))
        return (kind, value, trace)

    def attribute(self, owner, name, seen):
        kind, value, trace = owner
        if kind not in {"class", "module"}:
            return ("unknown", name, trace)
        key = ("attribute", kind, value if kind in {"symbol", "module", "class"} else None, name)
        if key in seen:
            return ("unknown", name, trace)
        seen = seen | {key}
        entries = []
        for target, expression, scope, node in self.attribute_writes:
            if target.attr != name:
                continue
            resolved_owner = self.resolve(target.value, scope, seen)
            if resolved_owner[:2] == owner[:2]:
                dynamic = not self.unconditional(node) or any(
                    isinstance(part, (ast.FunctionDef, ast.AsyncFunctionDef)) for part in scope)
                entries.append(("unknown" if dynamic else "expression", expression, scope, self.token(scope, node)))
        if kind == "class":
            node, scope = value
            if node.decorator_list or node.keywords or node.bases:
                return ("unknown", f"{node.name}.{name}", (*trace, self.token(scope, node)))
            entries = [*self.bindings.get(scope + (node,), {}).get(name, []), *entries]
            if not entries:
                return ("unknown", f"{node.name}.{name}", trace)
        if entries:
            resolved = self.evaluate(entries, key, seen - {key})
            return (resolved[0], resolved[1], (*trace, *resolved[2]))
        if kind == "module":
            target = f"{value}.{name}"
            return ("module" if target in self.modules else "symbol", target, trace)
        return ("unknown", name, trace)

    def resolve(self, node, scope=None, seen=frozenset()):
        if node is None:
            return ("unknown", "<missing>", ())
        if scope is None:
            scope = self.scopes.get(node, ())
        if isinstance(node, ast.Name):
            return self.lookup(scope, node.id, seen)
        if isinstance(node, ast.Attribute):
            return self.attribute(self.resolve(node.value, scope, seen), node.attr, seen)
        if isinstance(node, ast.Tuple):
            return ("tuple", [self.resolve(item, scope, seen) for item in node.elts], ())
        trace = tuple(repr(self.signature(self.resolve(child, scope, seen)))
                      for child in ast.iter_child_nodes(node) if isinstance(child, ast.expr))
        return ("unknown", ast.dump(node, include_attributes=False), trace)

    @staticmethod
    def signature(resolved):
        kind, value, trace = resolved
        if kind in {"class", "function"}:
            node, scope = value
            value = ".".join([*(part.name for part in scope), node.name])
        elif kind == "tuple":
            value = [StaticBindings.signature(item) for item in value]
        return (kind, value, trace)

    def exception_types(self, resolved, seen=frozenset()):
        kind, value, _ = resolved
        if kind == "tuple":
            items = [self.exception_types(item, seen) for item in value]
            return ([name for names, _ in items for name in names], any(unknown for _, unknown in items))
        if kind == "symbol":
            name = value.removeprefix("builtins.")
            return ([name], value.startswith("builtins.") and name not in BUILTIN_EXCEPTIONS)
        if kind == "class":
            node, scope = value
            if node not in seen and not node.decorator_list and not node.keywords and node.bases:
                bases = [self.exception_types(self.resolve(base, scope), seen | {node}) for base in node.bases]
                if not any(unknown for _, unknown in bases):
                    return ([node.name], False)
        return (["<unresolved>"], True)


def alias_targets(bindings, scope, reference):
    for index in range(len(scope), -1, -1):
        current = scope[:index]
        if current and isinstance(current[-1], ast.ClassDef) and any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) for node in scope[index:]
        ):
            continue
        values = bindings.get(current, {})
        if reference in values:
            return values[reference]
        if reference.split(".")[0] in values:
            return set()
    return set()


def dynamic_calls(source):
    tree = ast.parse(source)
    names = {"__import__", "importlib.import_module"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "importlib":
                    names.add(f"{alias.asname or alias.name}.import_module")
                if alias.name == "builtins":
                    names.add(f"{alias.asname or alias.name}.__import__")
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if (node.module, alias.name) in {("importlib", "import_module"), ("builtins", "__import__")}:
                    names.add(alias.asname or alias.name)
    changed = True
    while changed:
        before = set(names)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None and ast.unparse(node.value) in names:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names.update(target.id for target in targets if isinstance(target, ast.Name))
        changed = names != before
    for node, symbol in scoped_nodes(tree):
        if not isinstance(node, ast.Call) or ast.unparse(node.func) not in names:
            continue
        argument = node.args[0] if node.args else next(
            (k.value for k in node.keywords if k.arg == "name"), None)
        yield node, symbol, argument


def dynamic_sites(module, source):
    sites = []
    counts = Counter()
    contexts = {scope: node for node, scope in scoped_nodes(ast.parse(source))
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module))}
    for node, symbol, argument in dynamic_calls(source):
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            continue
        identity = (symbol, ast.dump(contexts.get(symbol, node), include_attributes=False),
                    ast.dump(node, include_attributes=False))
        counts[identity] += 1
        sites.append({"module": module, "symbol": symbol,
                      "site_fingerprint": digest(repr((identity, counts[identity])))})
    return sites


def adapter_surface(source, package=None):
    tree = ast.parse(source)
    exports, calls = {}, []
    statements = []
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue
        statements.append(node)
        if isinstance(node, ast.ImportFrom):
            target = node.module or ""
            if node.level:
                if not package:
                    invalid("Relative adapter forwarding needs its package identity")
                target = importlib.util.resolve_name("." * node.level + target, package)
            for alias in node.names:
                if alias.name == "*" or (alias.asname or alias.name) in exports:
                    invalid("Adapter has wildcard or duplicate export")
                exports[alias.asname or alias.name] = f"{target}.{alias.name}"
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            call = node.value
            if not isinstance(call.func, ast.Name) or call.func.id not in exports or call.keywords or any(
                not isinstance(argument, ast.Name) or argument.id != "__file__" for argument in call.args
            ):
                invalid("Unsupported adapter initialization")
            calls.append(ast.unparse(call))
        else:
            invalid("Adapters may only explicitly import and initialize canonical owners")
    return {"exports": exports, "calls": calls,
            "source_fingerprint": digest(ast.dump(ast.Module(body=statements, type_ignores=[]), include_attributes=False))}


def module_bindings(source):
    bindings = set()
    def walk(node):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bindings.add(node.name)
            return
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bindings.add(node.id)
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bindings.add(alias.asname or (alias.name.split(".")[0] if isinstance(node, ast.Import) else alias.name))
        for child in ast.iter_child_nodes(node):
            walk(child)
    walk(ast.parse(source))
    return bindings


def validate_adapters(sources, modules):
    findings = []
    owners = {m["import_name"]: m for m in modules}
    for module in modules:
        if module["area"] != "legacy":
            continue
        name = module["import_name"]
        if name not in sources:
            findings.append(finding("adapter-surface", name, 0, "Missing adapter source"))
            continue
        try:
            actual = adapter_surface(sources[name], name if any(n.startswith(name + ".") for n in sources) else name.rpartition(".")[0])
        except PolicyError as exc:
            findings.append(finding("adapter-surface", name, 0, str(exc)))
            continue
        if actual != module.get("adapter"):
            findings.append(finding("adapter-surface", name, 0, "Forwarding exports/order/calls differ from declared adapter"))
        for binding, target in actual["exports"].items():
            owner, _, export = target.rpartition(".")
            visited = set()
            while owner in owners and owners[owner]["area"] == "legacy" and target not in visited:
                visited.add(target)
                target = owners[owner].get("adapter", {}).get("exports", {}).get(export, "")
                owner, _, export = target.rpartition(".")
            if owner not in owners or owners[owner]["area"] == "legacy" or owner not in sources or (
                export not in owners[owner].get("public_exports", []) or export not in module_bindings(sources[owner])
            ):
                findings.append(finding("adapter-owner", name, 0, f"{binding}: undeclared or missing canonical export {target}"))
    return findings


def analyze_sources(sources, policy, *, verified_tests=()):
    graph = {name: set() for name in sources}
    findings = []
    allowances = {(a["importer"], a["target"], a["member"])
                  for a in policy.get("private_allowances", [])}
    first_roots = {name.split(".")[0] for name in sources}
    dynamic = {(entry["module"], entry["symbol"], entry["site_fingerprint"]): entry
               for entry in policy.get("dynamic_imports", [])}
    used_dynamic = set()
    packages = {entry["import_name"] for entry in policy.get("modules", [])
                if entry.get("path", "").endswith("/__init__.py")}
    private_modules = {private: set(entry["allowed_importers"])
                       for entry in policy.get("modules", []) for private in entry.get("private_modules", [])}

    def add(importer, target, member, line):
        if target not in sources:
            if target.split(".")[0] in first_roots:
                findings.append(finding("unresolved-import", importer, line, target))
            return
        graph[importer].add(target)
        private = any(p.startswith("_") for p in target.split(".")) or member.startswith("_") or (
            target in private_modules and importer not in private_modules[target])
        if private and importer != target and (importer, target, member) not in allowances:
            findings.append(finding("private-import", importer, line, f"{target}.{member}"))

    for name, source in sources.items():
        tree = ast.parse(source, filename=name)
        aliases = {}
        scoped = list(lexical_nodes(tree))
        # Packages are identified from inventory; fixtures may infer a parent with children.
        package = name if name in packages or any(other.startswith(name + ".") for other in sources) else name.rpartition(".")[0]
        for node, scope in scoped:
            local = aliases.setdefault(scope, {})
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                function = aliases.setdefault(scope + (node,), {})
                for arg in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs,
                            *[arg for arg in (node.args.vararg, node.args.kwarg) if arg]]:
                    function.setdefault(arg.arg, set())
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                local.setdefault(node.id, set())
            if isinstance(node, ast.Import):
                for alias in node.names:
                    local.setdefault(alias.asname or alias.name, set()).add(alias.name)
                    add(name, alias.name, "", node.lineno)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    segments = package.split(".") if package else []
                    if node.level > len(segments):
                        findings.append(finding("unresolved-import", name, node.lineno, "Relative import escapes root"))
                        continue
                    prefix = ".".join(segments[:len(segments) - node.level + 1])
                    target = ".".join(p for p in (prefix, node.module) if p)
                else:
                    target = node.module or ""
                for alias in node.names:
                    child = f"{target}.{alias.name}"
                    if child in sources:
                        local.setdefault(alias.asname or alias.name, set()).add(child)
                        add(name, child, "", node.lineno)
                    else:
                        local.setdefault(alias.asname or alias.name, set()).add(target)
                        add(name, target, alias.name, node.lineno)
        for node, _, argument in dynamic_calls(source):
            if not isinstance(argument, ast.Constant) or not isinstance(argument.value, str):
                continue
            target = argument.value
            if target.startswith("."):
                package_argument = next((k.value for k in node.keywords if k.arg == "package"), None)
                if package_argument is None and len(node.args) > 1:
                    package_argument = node.args[1]
                if isinstance(package_argument, ast.Constant) and isinstance(package_argument.value, str):
                    target = importlib.util.resolve_name(target, package_argument.value)
                else:
                    findings.append(finding("dynamic-import", name, node.lineno, "Relative dynamic import requires literal package"))
                    continue
            add(name, target, "", node.lineno)
        for site in dynamic_sites(name, source):
            key = (name, site["symbol"], site["site_fingerprint"])
            entry = dynamic.get(key)
            if entry is None:
                findings.append(finding("dynamic-import", name, 0, "Unclassified variable import target"))
                continue
            used_dynamic.add(key)
            if not set(entry["evidence_tests"]) <= set(verified_tests):
                findings.append(finding("dynamic-evidence", name, 0, "Dynamic target behavior tests did not execute successfully"))
            for target in entry["targets"]:
                add(name, target, "", 0)
        for node, scope in scoped:
            if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
                for target in alias_targets(aliases, scope, ast.unparse(node.value)):
                    add(name, target, node.attr, node.lineno)
    for key in dynamic.keys() - used_dynamic:
        findings.append(finding("stale-dynamic-import", key[0], 0, "Unused dynamic import approval"))
    try:
        tuple(graphlib.TopologicalSorter(graph).static_order())
    except graphlib.CycleError as exc:
        path = exc.args[1]
        findings.append(finding("cycle", path[0], 0, "Static dependency cycle", dependency_path=path))
    for contract in policy.get("forbidden", []):
        starts = [n for n in graph if n == contract["from"] or n.startswith(contract["from"] + ".")]
        for start in starts:
            queue = deque([(start, [start])])
            visited = {start}
            while queue:
                current, path = queue.popleft()
                for target in sorted(graph[current]):
                    route = [*path, target]
                    if target == contract["to"] or target.startswith(contract["to"] + "."):
                        findings.append(finding("forbidden", start, 0, "Forbidden dependency direction", dependency_path=route))
                        queue.clear()
                        break
                    if target not in visited:
                        visited.add(target)
                        queue.append((target, route))
    return {"graph": graph, "findings": findings}


def broad_handlers(module, source):
    tree = ast.parse(source, filename=module)
    bindings = StaticBindings(tree)
    handlers = []

    counts = Counter()
    def visit(node, symbol, protected_try=None):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            symbol = ".".join(p for p in (symbol, node.name) if p)
        if isinstance(node, ast.ExceptHandler):
            resolved = bindings.resolve(node.type)
            caught, unsupported = bindings.exception_types(resolved) if node.type is not None else (["bare"], False)
            if unsupported or any(t in {"bare", "Exception", "BaseException"} for t in caught):
                context = ast.dump(protected_try or node, include_attributes=False)
                identity = (symbol, context, ast.dump(node, include_attributes=False),
                            repr(bindings.signature(resolved)), tuple(caught), unsupported)
                counts[identity] += 1
                handlers.append({
                    "module_id": module, "symbol": symbol or "<module>", "line": node.lineno,
                    "handler_fingerprint": digest(repr((identity, counts[identity]))),
                    "caught_types": caught, "unsupported": unsupported,
                })
        for child in ast.iter_child_nodes(node):
            visit(child, symbol, node if isinstance(node, (ast.Try, ast.TryStar)) else protected_try)
    visit(tree, "")
    return handlers


def debt_key(entry):
    return tuple(entry[key] for key in (
        "module_id", "symbol", "source_fingerprint", "rule", "message_fingerprint"
    ))


def compare_debt(current, baseline):
    actual = Counter(debt_key(e) for e in current)
    allowed = Counter()
    for entry in baseline:
        allowed[debt_key(entry)] += entry["occurrences"]
    findings = []
    for key, count in (actual - allowed).items():
        findings.append(finding("new-type-debt", key[0], 0, f"{key[1]}: {key[3]} ({count} new)", identity=list(key)))
    for key, count in (allowed - actual).items():
        findings.append(finding("retire-type-debt", key[0], 0, f"Retire stale baseline allowance ({count})", identity=list(key)))
    return findings


def policy_changes(base_scope, candidate_scope, base_debt, candidate_debt):
    findings = []
    if not base_scope <= candidate_scope:
        findings.append(finding("scope-reduction", "", 0, "Blocking coverage cannot shrink"))
    old = {e["id"]: e for e in base_debt}
    if any(e["id"] not in old or old[e["id"]] != e for e in candidate_debt):
        findings.append(finding("baseline-growth", "", 0, "Candidate cannot approve new or changed debt"))
    return findings


def effective_debt(base, candidate):
    retained = {entry["id"]: entry for entry in candidate}
    return [entry for entry in base if retained.get(entry["id"]) == entry]


def validate_moves(base, candidate, move_map, before_sources=None, after_sources=None):
    findings = []
    ids = [m["id"] for m in candidate]
    paths = [m["path"] for m in candidate]
    if len(ids) != len(set(ids)) or len(paths) != len(set(paths)):
        findings.append(finding("ambiguous-move", "", 0, "Module IDs and destinations must be one-to-one"))
    current = {m["id"]: m for m in candidate}
    for old in base:
        new = current.get(old["id"])
        if new is None or (
            old["path"] != new["path"] and move_map.get(old["path"]) != new["path"]
        ):
            findings.append(finding("unreviewed-move", old["id"], 0, "Missing one-to-one move allocation"))
        elif old["path"] != new["path"]:
            before = (before_sources or {}).get(old["path"])
            after = (after_sources or {}).get(new["path"])
            if before is None or after is None or ast.dump(ast.parse(before), include_attributes=False) != (
                ast.dump(ast.parse(after), include_attributes=False)
            ):
                findings.append(finding("changed-move", old["id"], 0, "Changed/split source needs explicit protected allocation"))
    return findings


def source_policy_changes(old, new, *, preserve_annotations=True):
    def suppressions(source):
        tree = ast.parse(source)
        nodes = list(scoped_nodes(tree))
        bindings = StaticBindings(tree)
        result = Counter()
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type != tokenize.COMMENT or not re.search(r"\b(?:noqa|type:\s*ignore|mypy:|ruff:|pyright:)", token.string):
                continue
            enclosing = [(n, scope) for n, scope in nodes if isinstance(n, ast.stmt) and
                         n.lineno <= token.start[0] <= n.end_lineno]
            node, scope = min(enclosing, key=lambda pair: pair[0].end_lineno - pair[0].lineno) if enclosing else (None, "<module>")
            result[(scope, ast.dump(node, include_attributes=False) if node else "<file>", token.string)] += 1
        for node, scope in nodes:
            if isinstance(node, ast.ImportFrom) and node.module in {"typing", "typing_extensions"}:
                for alias in node.names:
                    if alias.name in {"no_type_check", "no_type_check_decorator"}:
                        result[(scope, "typing-bypass", ast.dump(node, include_attributes=False))] += 1
            if isinstance(node, (ast.Name, ast.Attribute)) and isinstance(node.ctx, ast.Load):
                resolved = bindings.resolve(node)
                if resolved[0] == "symbol" and resolved[1] in {
                    f"{module}.{member}" for module in ("typing", "typing_extensions")
                    for member in ("no_type_check", "no_type_check_decorator")
                }:
                    enclosing = [(parent, owner) for parent, owner in nodes if isinstance(parent, ast.stmt)
                                 and parent.lineno <= node.lineno <= parent.end_lineno]
                    parent, owner = min(enclosing, key=lambda pair: pair[0].end_lineno - pair[0].lineno) if enclosing else (node, scope)
                    result[(owner, "typing-bypass-use", ast.dump(parent, include_attributes=False),
                            repr(bindings.signature(resolved)))] += 1
        return result
    extra = suppressions(new) - suppressions(old)
    findings = [finding("suppression-growth", "", 0, "New suppression exceeds protected policy")] if extra else []
    if not preserve_annotations:
        return findings

    def signatures(source):
        result = {}
        tree = ast.parse(source)
        provider_bindings = StaticBindings(tree)
        annotations = []
        occurrences = Counter()
        for node, scope in scoped_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                occurrences[scope] += 1
                scope = f"{scope}[{occurrences[scope]}]"
                result[(scope, "decorator-bindings")] = tuple(
                    (ast.dump(decorator, include_attributes=False),
                     repr(provider_bindings.signature(provider_bindings.resolve(decorator))))
                    for decorator in node.decorator_list)
                args = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
                args += [a for a in (node.args.vararg, node.args.kwarg) if a]
                for arg in args:
                    if arg.annotation is not None:
                        annotations.append(arg.annotation)
                        result[(scope, "argument", arg.arg)] = ast.dump(arg.annotation, include_attributes=False)
                if node.returns is not None:
                    annotations.append(node.returns)
                    result[(scope, "return")] = ast.dump(node.returns, include_attributes=False)
                if any(a.annotation for a in args) or node.returns:
                    result[(scope, "shape")] = (
                        type(node).__name__, tuple(a.arg for a in node.args.posonlyargs),
                        tuple(a.arg for a in node.args.args), tuple(a.arg for a in node.args.kwonlyargs),
                        node.args.vararg.arg if node.args.vararg else None,
                        node.args.kwarg.arg if node.args.kwarg else None,
                        len(node.args.defaults), tuple(d is not None for d in node.args.kw_defaults),
                        tuple(ast.dump(d, include_attributes=False) for d in node.decorator_list))
            elif isinstance(node, ast.AnnAssign):
                annotations.append(node.annotation)
                result[(scope, "variable", ast.unparse(node.target))] = ast.dump(node.annotation, include_attributes=False)
            elif isinstance(node, ast.ClassDef):
                result[(scope, "class-decorators")] = tuple(ast.dump(d, include_attributes=False) for d in node.decorator_list)
                result[(scope, "decorator-bindings")] = tuple(
                    repr(provider_bindings.signature(provider_bindings.resolve(decorator))) for decorator in node.decorator_list)
        bindings = {}
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    bindings[alias.asname or alias.name.split(".")[0]] = (
                        ast.ImportFrom(module=node.module, names=[alias], level=node.level)
                        if isinstance(node, ast.ImportFrom) else ast.Import(names=[alias]))
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        bindings[target.id] = node
            elif isinstance(node, ast.TypeAlias):
                bindings[node.name.id] = node
        pending = set()
        for annotation in annotations:
            if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
                annotation = ast.parse(annotation.value, mode="eval")
            pending.update(n.id for n in ast.walk(annotation) if isinstance(n, ast.Name))
        visited = set()
        while pending:
            name = pending.pop()
            if name in visited or name not in bindings:
                continue
            visited.add(name)
            binding = bindings[name]
            result[("<module>", "annotation-binding", name)] = ast.dump(binding, include_attributes=False)
            pending.update(n.id for n in ast.walk(binding) if isinstance(n, ast.Name))
        return result
    before, after = signatures(old), signatures(new)
    for name, annotations in before.items():
        if annotations != after.get(name):
            findings.append(finding("annotation-removal", "", 0, ".".join(name)))
    return findings


def seal_report(report):
    payload = {key: value for key, value in report.items() if key != "artifact_integrity"}
    report["artifact_integrity"] = digest(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return report


def intact_report(report):
    if not isinstance(report, dict):
        return False
    copy = dict(report)
    return seal_report(copy)["artifact_integrity"] == report.get("artifact_integrity")


def run_context():
    if os.environ.get("GITHUB_RUN_ID"):
        return f"{os.environ['GITHUB_RUN_ID']}:{os.environ.get('GITHUB_RUN_ATTEMPT', '')}"
    return os.environ.get("QUALITY_RUN_ID")


def source_digest(root):
    _, paths = discover(root)
    files = set(paths.values()) | set(PROTECTED_FILES)
    files.update(path.relative_to(root).as_posix() for directory in ("tests", ".quality") for path in
                 (root / directory).rglob("*") if path.is_file() and path.suffix in {".py", ".json"})
    files.update(("pyproject.toml", "requirements.txt"))
    content = []
    for name in sorted(files):
        path = root / name
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            invalid("Evidence source escapes repository")
        if path.is_file():
            content.append((name, digest(path.read_text(encoding="utf-8"))))
    return digest(json.dumps(content, separators=(",", ":")))


def evidence_test_metadata(root, selector):
    test_selector(selector)
    filename, _, symbol = selector.partition("::")
    path = root / filename
    if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        invalid("Missing or escaping evidence test")
    source = path.read_text(encoding="utf-8")
    nodes = [node for node, scope in scoped_nodes(ast.parse(source))
             if scope == symbol and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    if len(nodes) != 1:
        invalid("Missing or ambiguous exact evidence test")
    return {"selector": selector, "source_fingerprint": digest(source),
            "symbol_fingerprint": digest(ast.dump(nodes[0], include_attributes=False))}


def verified_test_evidence(root, report, head, base, sources, run_id):
    if not intact_report(report) or report.get("schema_version") != 1 or report.get("repository") != "Azure/gpt-rag-ui":
        invalid("Invalid unittest evidence integrity/schema")
    for field, expected in (("head_sha", head), ("base_sha", base), ("source_digest", sources), ("run_id", run_id)):
        if not expected or report.get(field) != expected:
            invalid(f"Unittest evidence has wrong/stale {field}")
    if report.get("status") != "passed" or type(report.get("tests_run")) is not int or report["tests_run"] < 1:
        invalid("Unittest execution did not succeed")
    entries = report.get("tests")
    if not isinstance(entries, list) or len(entries) != report["tests_run"]:
        invalid("Incomplete unittest evidence")
    outcomes = {}
    seen = set()
    for entry in entries:
        exact_object(entry, ("test_id", "selector", "source_fingerprint", "symbol_fingerprint", "status"))
        if entry["test_id"] in seen:
            invalid("Duplicate executed test identity")
        seen.add(entry["test_id"])
        if entry["status"] not in {"passed", "skipped", "expected_failure"}:
            invalid("Failed unittest evidence")
        metadata = evidence_test_metadata(root, entry["selector"])
        if any(entry[field] != value for field, value in metadata.items()):
            invalid("Test source changed after execution")
        outcomes.setdefault(entry["selector"], set()).add(entry["status"])
    return {selector for selector, states in outcomes.items() if states == {"passed"}}


def aggregate(results, reports, head, base, *, test_report=None, run_id=None, toolchain=None):
    failures = [name for name in REQUIRED_JOBS if results.get(name) != "success"]
    if set(reports) != set(CHECKS):
        failures.append("Missing or unexpected quality reports")
    if not intact_report(test_report) or test_report.get("status") != "passed" or (
        test_report.get("tests_run", 0) < 1
    ):
        failures.append("Missing/failed unittest execution artifact")
        test_report = {}
    for field, expected in (("head_sha", head), ("base_sha", base), ("run_id", run_id)):
        if not expected or test_report.get(field) != expected:
            failures.append(f"Unittest evidence: stale {field}")
    for name in CHECKS:
        report = reports.get(name, {})
        valid = intact_report(report) and report.get("schema_version") == 1 and report.get("repository") == "Azure/gpt-rag-ui"
        valid = valid and report.get("status") == "passed" and report.get("head_sha") == head and report.get("base_sha") == base
        valid = valid and report.get("policy_sha") == base and report.get("run_id") == run_id and bool(run_id)
        valid = valid and report.get("toolchain") == toolchain and bool(toolchain)
        valid = valid and report.get("source_digest") == test_report.get("source_digest") and bool(report.get("source_digest"))
        valid = valid and report.get("test_evidence_digest") == test_report.get("artifact_integrity") and bool(test_report)
        valid = valid and report.get("checks") == {name: {"status": "passed", "findings": []}}
        if not valid:
            failures.append(f"{name}: missing, failed, altered or stale report")
    return failures


def diagnostic_identity(diagnostic, source, module_id):
    tree = ast.parse(source)
    line = diagnostic["line"]
    symbol = "<module>"
    context = tree
    def visit(node, path):
        nonlocal symbol, context
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            path = ".".join(p for p in (path, node.name) if p)
            if node.lineno <= line <= node.end_lineno:
                symbol, context = path, node
        for child in ast.iter_child_nodes(node):
            visit(child, path)
    visit(tree, "")
    return {
        "module_id": module_id, "symbol": symbol,
        "source_fingerprint": digest(ast.dump(context, include_attributes=False)),
        "rule": diagnostic["code"], "message_fingerprint": digest(diagnostic["message"]),
    }


def mypy_diagnostics(output, returncode):
    diagnostics = [parse_json(line) for line in output.splitlines() if line.strip()]
    for entry in diagnostics:
        exact_object(entry, ("file", "line", "column", "end_line", "end_column", "message", "hint", "code", "severity"))
        choice(entry["severity"], ("error", "note"))
        text(entry["file"])
        text(entry["message"])
        if type(entry["line"]) is not int or entry["line"] < 1 or type(entry["column"]) is not int or entry["column"] < 0:
            invalid("Invalid mypy diagnostic location")
        for key in ("end_line", "end_column"):
            if entry[key] is not None and (type(entry[key]) is not int or entry[key] < 0):
                invalid("Invalid mypy diagnostic end location")
        if entry["hint"] is not None:
            text(entry["hint"])
        if entry["severity"] == "error":
            text(entry["code"])
        elif entry["code"] is not None:
            text(entry["code"])
    if returncode and not any(entry["severity"] == "error" for entry in diagnostics):
        invalid("Mypy failed without an error diagnostic")
    return diagnostics


def validate_exception_records(handlers, records, root, *, verified_tests=(), stage="bootstrap", today=None):
    root = root.resolve()
    fields = ("module_id", "symbol", "handler_fingerprint")
    actual = {tuple(h[k] for k in fields): h for h in handlers}
    findings, used = [], []
    today = today or datetime.datetime.now(datetime.timezone.utc).date()
    for entry in records:
        key = tuple(entry[k] for k in fields)
        handler = actual.pop(key, None)
        evidence = entry.get("evidence_tests", [])
        valid = (handler is not None and entry.get("state") == "active"
                 and handler.get("unsupported", True) is False
                 and entry.get("caught_types") == handler["caught_types"]
                 and all(entry.get(k) for k in ("reason", "boundary", "failure_outcome", "diagnostic_path", "review"))
                 and evidence and set(evidence) <= set(verified_tests))
        if valid:
            try:
                valid = (datetime.date.fromisoformat(entry.get("expires_on", "")) >= today and
                         STAGES.index(entry.get("review_by_stage")) >= STAGES.index(stage))
            except ValueError:
                valid = False
        if not valid:
            findings.append(finding("exception-review", entry["module_id"], handler["line"] if handler else 0,
                                    f"Inactive, changed, stale or unevidenced exception: {entry['id']}"))
        else:
            used.append(entry["id"])
    findings.extend(finding("unapproved-handler", h["module_id"], h["line"], h["symbol"]) for h in actual.values())
    return findings, used


def effective_scope(base_modules, modules, protected_scope, candidate_scope, sources):
    by_import = {m["import_name"]: m["id"] for m in base_modules}
    by_id = {m["id"] for m in base_modules}
    scope = set(protected_scope) | set(candidate_scope)
    identities = {}
    for module in modules:
        name = module["import_name"]
        identity = by_import.get(name, module["id"])
        identities[name] = identity
        if identity not in by_id:
            scope.add(identity)
    for name in set(sources) - identities.keys():
        identities[name] = name
        scope.add(name)
    return identities, scope


def run_checks(root, base, requested, *, test_evidence=None):
    root = root.resolve()
    base_sha = git(root, "rev-parse", "--verify", f"{base}^{{commit}}")
    head_sha = git(root, "rev-parse", "HEAD")
    records = {name: read_record(root / ".quality" / f"{name}.json")
               for name in RECORD_NAMES}
    validate_records(records)
    base_policy_text = base_text(root, base_sha, ".quality/policy.json")
    base_records = {}
    if base_policy_text:
        for name in RECORD_NAMES:
            value = base_text(root, base_sha, f".quality/{name}.json")
            if value is None:
                invalid(f"Protected base missing {name}")
            base_records[name] = parse_json(value)
        validate_records(base_records)
    minimum = base_records.get("policy", records["policy"])
    versions = {}
    for name, version in minimum["toolchain"].items():
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError as exc:
            raise PolicyError(f"Missing pinned tool: {name}") from exc
        if versions[name] != version:
            raise PolicyError(f"Wrong {name} version: expected {version}, found {versions[name]}")
    sources, paths = discover(root)
    modules = records["policy"]["modules"]
    if any(paths.get(m["import_name"]) != m["path"] for m in modules):
        raise PolicyError("Module inventory name/path does not match discovered source")
    unknown = set(sources) - {m["import_name"] for m in modules}
    module_ids, scope = effective_scope(
        minimum["modules"] if base_policy_text else modules, modules,
        base_records.get("typing-scope", records["typing-scope"])["module_ids"],
        records["typing-scope"]["module_ids"], sources)
    source_hash = source_digest(root)
    run_id = run_context()
    evidence, verified = None, set()
    if test_evidence is not None:
        evidence = read_record(test_evidence)
        verified = verified_test_evidence(root, evidence, head_sha, base_sha, source_hash, run_id)
    findings = {name: [] for name in requested}
    reports = {}
    started = time.monotonic()
    if "policy" in requested:
        output = findings["policy"]
        if not base_policy_text:
            output.append(finding("bootstrap-review", "", 0, "Protected base has no policy; adopt the protected evaluator and verify required checks before claiming enforcement"))
        else:
            output.extend(policy_changes(set(base_records["typing-scope"]["module_ids"]),
                                         set(records["typing-scope"]["module_ids"]),
                                         base_records["typing-baseline"]["entries"], records["typing-baseline"]["entries"]))
            before_sources = {m["path"]: base_text(root, base_sha, m["path"]) for m in minimum["modules"]}
            output.extend(validate_moves(minimum["modules"], modules, records["typing-scope"]["move_map"],
                                         before_sources, {paths[name]: source for name, source in sources.items()}))
            for field in ("runtime_roots", "toolchain", "forbidden", "private_allowances", "required_checks", "dynamic_imports", "review"):
                if minimum[field] != records["policy"][field]:
                    output.append(finding("policy-change", "", 0, f"Protected policy differs: {field}"))
            protected_modules = {m["id"]: m for m in minimum["modules"]}
            for module in modules:
                old_module = protected_modules.get(module["id"])
                if old_module is None and module["id"] not in records["typing-scope"]["module_ids"]:
                    output.append(finding("unrecorded-coverage", module["id"], 0,
                                          "Persist automatically blocking new modules in scope for subsequent PRs"))
                if old_module:
                    for field in set(old_module) | set(module):
                        if field in {"path", "import_name"}:
                            continue
                        if field == "typing_status" and module[field] == "blocking":
                            continue
                        if old_module.get(field) != module.get(field):
                            output.append(finding("policy-change", module["id"], 0, f"Protected module surface: {field}"))
            if STAGES.index(records["typing-scope"]["coverage_stage"]) < STAGES.index(base_records["typing-scope"]["coverage_stage"]):
                output.append(finding("policy-change", "", 0, "Typing stage cannot regress"))
            for name in PROTECTED_FILES:
                old = base_text(root, base_sha, name)
                current = (root / name).read_text(encoding="utf-8") if (root / name).exists() else None
                if old != current:
                    output.append(finding("policy-change", "", 0, f"Protected file differs: {name}"))
            old_config = tomllib.loads(base_text(root, base_sha, "pyproject.toml"))
            current_config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
            if old_config.get("tool", {}).get("mypy") != current_config.get("tool", {}).get("mypy") or (
                old_config.get("tool", {}).get("ruff") != current_config.get("tool", {}).get("ruff")
            ) or old_config.get("tool", {}).get("importlinter") != current_config.get("tool", {}).get("importlinter"):
                output.append(finding("policy-change", "", 0, "Tool settings differ from protected policy"))
        output.extend(validate_moves([], modules, {}))
        output.extend(finding("unclassified-module", name, 0, "New runtime modules need inventory and blocking typing") for name in sorted(unknown))
        inventory_paths = {m["path"] for m in modules}
        if inventory_paths - set(paths.values()):
            output.append(finding("missing-module", "", 0, "Inventoried runtime source missing"))
        for name, source in sources.items():
            old = base_text(root, base_sha, paths[name]) or ""
            if base_policy_text:
                old_module = next((m for m in minimum["modules"] if m["id"] == module_ids[name]), None)
                if old_module:
                    old = base_text(root, base_sha, old_module["path"]) or ""
            if not old and not base_policy_text:
                # Bootstrap move records retain the original source's suppression history.
                migration = read_record(root / ".quality" / "migration.json")
                previous = next((m for m in migration["modules"] if m["import_name"] == name), None)
                if previous:
                    old = base_text(root, base_sha, previous["source_path"]) or ""
            area = next((m["area"] for m in modules if m["import_name"] == name), None)
            output.extend({**f, "module": name} for f in source_policy_changes(
                old, source, preserve_annotations=module_ids[name] in scope and area != "legacy"))
        # Nested config must not silently change tool discovery/suppression behavior.
        folders = {parent for path in paths.values() for parent in (root / path).parents if parent.is_relative_to(root)}
        for folder in sorted(folders):
            for name in ("pyproject.toml", "ruff.toml", ".ruff.toml", "mypy.ini", ".mypy.ini", "setup.cfg", "tox.ini"):
                if (folder / name).is_file() and not (folder == root and name == "pyproject.toml"):
                    output.append(finding("nested-config", folder.relative_to(root).as_posix(), 0, name))
    config_text = base_text(root, base_sha, "pyproject.toml") if base_policy_text else None
    config_text = config_text or (root / "pyproject.toml").read_text(encoding="utf-8")
    approved_handler_sites = set()
    if {"lint", "exceptions"} & set(requested):
        handler_sites = [(paths[name], dict(h, module_id=module_ids.get(name, name)))
                         for name, source in sources.items() for h in broad_handlers(name, source)]
        handlers = [handler for _, handler in handler_sites]
        # Registry data is eligible for technical validation, not approval authority.
        # Bootstrap still needs an adopted protected evaluator; candidate records
        # cannot change the rules, toolchain, evidence runner or aggregate.
        entries = records["exceptions"]["entries"] if base_policy_text else []
        exception_findings, used = validate_exception_records(
            handlers, entries, root, verified_tests=verified,
            stage=records["typing-scope"]["coverage_stage"])
        fields = ("module_id", "symbol", "handler_fingerprint")
        approved = {tuple(entry[field] for field in fields)
                    for entry in entries if entry["id"] in used}
        approved_handler_sites = {
            (path, handler["line"])
            for path, handler in handler_sites if tuple(handler[field] for field in fields) in approved
        }
    with tempfile.TemporaryDirectory(prefix="gpt-rag-quality-") as temporary:
        config_path = Path(temporary) / "pyproject.toml"
        config_path.write_text(config_text, encoding="utf-8")
        if "lint" in requested:
            result = execute([sys.executable, "-I", "-m", "ruff", "check", "--config", str(config_path),
                              "--no-cache", "--output-format", "json", *paths.values()], cwd=root, allowed=(0, 1))
            diagnostics = parse_json(result.stdout)
            if result.returncode and not diagnostics:
                raise PolicyError("Ruff failed without structured diagnostics")
            for d in diagnostics:
                path = Path(d["filename"]).relative_to(root).as_posix()
                if d["code"] == "BLE001" and (path, d["location"]["row"]) in approved_handler_sites:
                    continue
                findings["lint"].append(finding(d["code"], path,
                                                 d["location"]["row"], d["message"]))
        if "typing" in requested:
            targets = [paths[n] for n in sources if module_ids.get(n, n) in scope]
            if not targets:
                raise PolicyError("Empty blocking typing scope")
            result = execute([sys.executable, "-I", "-m", "mypy", "--config-file", str(config_path),
                              "--no-incremental", "--output", "json", *targets], cwd=root, allowed=(0, 1))
            diagnostics = mypy_diagnostics(result.stdout, result.returncode)
            reverse = {path: name for name, path in paths.items()}
            current = []
            imported = []
            for d in diagnostics:
                if d["severity"] != "error":
                    continue
                path = d["file"].replace("\\", "/")
                if Path(path).is_absolute():
                    path = Path(path).resolve().relative_to(root).as_posix()
                name = reverse.get(path)
                if name is None:
                    invalid("Mypy returned an unmapped error; refusing to treat it as uncovered debt")
                if name and module_ids.get(name, name) in scope:
                    current.append(diagnostic_identity(d, sources[name], module_ids.get(name, name)))
                else:
                    imported.append(d)
            debt = effective_debt(base_records["typing-baseline"]["entries"],
                                  records["typing-baseline"]["entries"]) if base_policy_text else []
            findings["typing"].extend(compare_debt(current, debt))
            reports["imported_type_diagnostics"] = imported
        if "architecture" in requested:
            result = analyze_sources(sources, minimum, verified_tests=verified)
            findings["architecture"].extend(result["findings"])
            # The protected surface is authoritative even if the candidate rewrites its declaration.
            findings["architecture"].extend(validate_adapters(sources, minimum["modules"]))
            findings["architecture"].extend(validate_adapters(sources, [
                m for m in modules if m["id"] not in {entry["id"] for entry in minimum["modules"]}
            ] + [m for m in modules if m["area"] != "legacy"]))
            graph_report = isolated_architecture_evidence(root, config_path)
            for importer, targets in graph_report["graph"].items():
                for target in targets:
                    if importer in sources and target in sources and target not in result["graph"][importer]:
                        findings["architecture"].append(finding("graph-disagreement", importer, 0, target))
            if not graph_report["passed"]:
                findings["architecture"].append(finding("import-linter", "", 0, graph_report["output"][-3000:]))
        if "exceptions" in requested:
            findings["exceptions"].extend(exception_findings)
            reports["exception_ids_used"] = used
            reports["handler_inventory"] = handlers
    if source_digest(root) != source_hash:
        invalid("Source inputs changed during quality execution")
    reports.update({
        "schema_version": 1, "repository": "Azure/gpt-rag-ui",
        "base_sha": base_sha, "head_sha": head_sha, "policy_sha": base_sha if base_policy_text else None,
        "toolchain": versions, "duration_seconds": round(time.monotonic() - started, 3),
        "checks": {name: {"status": "violations" if value else "passed", "findings": value}
                   for name, value in findings.items()},
        "coverage": {"blocking": sorted(scope), "uncovered": sorted(set(module_ids.values()) - scope)},
        "status": "violations" if any(findings.values()) else "passed",
        "source_digest": source_hash, "run_id": run_id,
        "test_evidence_digest": evidence["artifact_integrity"] if evidence else None,
    })
    return seal_report(reports)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", choices=("all", *CHECKS), default="all")
    parser.add_argument("--base-ref", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--test-evidence", type=Path, help="Bound report from the separate unittest job")
    args = parser.parse_args()
    requested = CHECKS if args.check == "all" else (args.check,)
    try:
        report = run_checks(Path.cwd(), args.base_ref, requested, test_evidence=args.test_evidence)
    except (PolicyError, ValueError, KeyError, TypeError, SyntaxError, OSError) as exc:
        LOGGER.error("Quality execution incomplete: %s", exc)
        report = seal_report({"schema_version": 1, "status": "error", "error": str(exc)})
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    for name, check in report.get("checks", {}).items():
        LOGGER.warning("%s: %s (%s findings)", name, check["status"], len(check["findings"]))
    return {"passed": 0, "violations": 1, "error": 2}[report["status"]]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    raise SystemExit(main())
