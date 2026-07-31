#!/usr/bin/env python3
"""A small, self-contained, stdlib-only fallback test runner for this repo.

**This is a fallback convenience, not a pytest replacement.** Real ``pytest``
is authoritative for this project -- CI and David's own machine should run
the real thing (``pip install -e ".[dev]" && pytest``). This script exists
solely because the build sandbox this project was developed in has no PyPI
access, so a previous agent needed *something* that could run this repo's
existing, unmodified pytest-style test suite to prove the code works.
Multiple earlier agents independently rebuilt a throwaway version of this
exact idea in ``/tmp``; this is that tool made permanent and committed,
instead of reinvented (and lost) every time.

It provides just enough of pytest's surface for this repo's tests:
``fixture`` (including fixtures that depend on other fixtures), ``raises``,
``mark`` (a no-op passthrough), ``approx``, ``importorskip``, ``skip``, and
the builtin ``tmp_path``, ``monkeypatch``, and ``caplog`` fixtures. It is
NOT a general pytest reimplementation -- no parametrize expansion, no
conftest.py discovery, no plugins. If a future test needs more than this,
extend this file or (better) get real pytest installed.

Usage::

    python3 scripts/minipytest.py [path ...]

With no arguments, discovers and runs every ``tests/test_*.py`` file under
the repo's ``tests/`` directory. Exits non-zero if any test failed (or if a
test module failed to import).

-----------------------------------------------------------------------------
IMPORTANT -- invoke this as a script, never as ``python3 -m``
-----------------------------------------------------------------------------

Always run this as ``python3 scripts/minipytest.py``. Do **not** run it as
``python3 -m scripts.minipytest``.

The reason is a known trap: this file installs a fake ``pytest`` module into
``sys.modules`` *before* importing any test file, so that ``import pytest``
inside test modules picks up this shim instead of failing outright. If this
file itself gets imported twice under two different module identities in the
same process -- e.g. once as ``__main__`` (direct script execution) and once
as ``scripts.minipytest`` (via ``-m`` or a stray ``import scripts.minipytest``
elsewhere) -- Python treats those as two unrelated modules and re-executes
every class definition in this file twice, producing two distinct
``MonkeyPatch`` classes, two distinct ``raises`` classes, and so on. Test code
loaded against one copy's shim (fixture values, exception types) can then fail
``isinstance``-shaped checks against the other copy even though the code
looks identical. Direct script invocation never triggers this because the
file is only ever executed once, as ``__main__``. As defence in depth,
``_install_pytest_shim`` below is also idempotent (it reuses an
already-installed shim rather than blindly replacing it) in case this module
ever does end up imported more than once.
"""

from __future__ import annotations

import contextlib
import importlib.util
import inspect
import logging
import os
import re
import shutil
import sys
import tempfile
import traceback
import types
from pathlib import Path
from typing import Any, Callable, Iterable

# ---------------------------------------------------------------------------
# pytest-compatible shim
# ---------------------------------------------------------------------------

_SHIM_SENTINEL = "_is_minipytest_shim"


class Skipped(Exception):
    """Raised by ``skip()``/``importorskip()`` to mark a test as skipped."""

    def __init__(self, reason: str = "") -> None:
        super().__init__(reason)
        self.reason = reason


def skip(reason: str = "") -> None:
    raise Skipped(reason)


def importorskip(modname: str, *, reason: str | None = None) -> Any:
    try:
        return importlib.import_module(modname)
    except ImportError as exc:
        raise Skipped(reason or f"could not import {modname!r}: {exc}") from exc


class raises:
    """Minimal stand-in for ``pytest.raises`` (context-manager form only)."""

    def __init__(self, expected_exception: type[BaseException] | tuple, *, match: str | None = None):
        self.expected_exception = expected_exception
        self.match = match
        self.value: BaseException | None = None

    def __enter__(self) -> "raises":
        return self

    def __exit__(self, exc_type, exc_value, tb) -> bool:
        if exc_type is None:
            raise AssertionError(f"DID NOT RAISE {self.expected_exception!r}")
        if not issubclass(exc_type, self.expected_exception):
            return False  # let it propagate -- wrong exception type
        if self.match is not None and not re.search(self.match, str(exc_value)):
            raise AssertionError(
                f"Pattern {self.match!r} not found in exception message {str(exc_value)!r}"
            )
        self.value = exc_value
        return True  # suppress: this is the expected exception


class _MarkDecorator:
    """``pytest.mark.<anything>`` -- a no-op passthrough for common marks.

    Supports both ``@mark.name`` and ``@mark.name(...)`` forms; in both
    cases the decorated function/class is returned unmodified. This does not
    implement ``parametrize`` expansion -- none of this repo's tests use it.
    """

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if len(args) == 1 and not kwargs and (inspect.isfunction(args[0]) or inspect.isclass(args[0])):
            return args[0]

        def _decorator(obj: Any) -> Any:
            return obj

        return _decorator


class _Mark:
    def __getattr__(self, _name: str) -> _MarkDecorator:
        return _MarkDecorator()


mark = _Mark()


class approx:
    """Minimal stand-in for ``pytest.approx`` -- scalar and container forms."""

    def __init__(self, expected: Any, rel: float = 1e-6, abs: float = 1e-12) -> None:
        self.expected = expected
        self.rel = rel
        self.abs_tol = abs

    def _scalar_eq(self, expected: float, other: Any) -> bool:
        try:
            diff = abs(other - expected)
        except TypeError:
            return NotImplemented  # type: ignore[return-value]
        tolerance = max(self.rel * abs(expected), self.abs_tol)
        return diff <= tolerance

    def __eq__(self, other: Any) -> bool:
        if isinstance(self.expected, dict):
            if not isinstance(other, dict) or set(other) != set(self.expected):
                return False
            return all(approx(v, self.rel, self.abs_tol) == other[k] for k, v in self.expected.items())
        if isinstance(self.expected, (list, tuple)):
            if not isinstance(other, (list, tuple)) or len(other) != len(self.expected):
                return False
            return all(
                approx(e, self.rel, self.abs_tol) == o for e, o in zip(self.expected, other)
            )
        return self._scalar_eq(self.expected, other)

    def __ne__(self, other: Any) -> bool:
        result = self.__eq__(other)
        return not result if result is not NotImplemented else NotImplemented

    def __repr__(self) -> str:
        return f"approx({self.expected!r})"


class _FixtureFunction:
    """Wraps a function decorated with ``@pytest.fixture()``.

    Kept as a distinct wrapper type (rather than the bare function) so
    collection can tell fixtures apart from plain module-level helpers with
    unrelated names.
    """

    __slots__ = ("func", "name")

    def __init__(self, func: Callable) -> None:
        self.func = func
        self.name = func.__name__


def fixture(func: Callable | None = None, *, scope: str = "function", **_kwargs: Any):
    """Minimal stand-in for ``pytest.fixture`` (function scope only)."""

    def _wrap(fn: Callable) -> _FixtureFunction:
        return _FixtureFunction(fn)

    if func is not None:
        return _wrap(func)
    return _wrap


# ---------------------------------------------------------------------------
# Builtin fixtures: tmp_path, monkeypatch, caplog
# ---------------------------------------------------------------------------

_NOTSET = object()


class MonkeyPatch:
    """Minimal stand-in for ``pytest.MonkeyPatch``.

    Supports the calling conventions this repo's tests actually use:
    ``setattr(obj, name, value)``, ``setenv``/``delenv``, and
    ``setitem``/``delitem`` on any mutable mapping (incl. ``sys.modules``).
    All changes are undone, in reverse order, when the fixture tears down.
    """

    def __init__(self) -> None:
        self._undo: list[Callable[[], None]] = []

    def setattr(self, target: Any, name: str, value: Any = _NOTSET, raising: bool = True) -> None:
        if value is _NOTSET:
            raise NotImplementedError(
                "this fallback monkeypatch only supports the 3-argument form "
                "setattr(target, name, value) -- the dotted-string 2-argument "
                "form is not implemented."
            )
        had_attr = hasattr(target, name)
        old_value = getattr(target, name, _NOTSET)
        if raising and not had_attr:
            raise AttributeError(f"{target!r} has no attribute {name!r}")
        setattr(target, name, value)
        self._undo.append(
            lambda: setattr(target, name, old_value) if had_attr else _safe_delattr(target, name)
        )

    def delattr(self, target: Any, name: str, raising: bool = True) -> None:
        if not hasattr(target, name):
            if raising:
                raise AttributeError(f"{target!r} has no attribute {name!r}")
            return
        old_value = getattr(target, name)
        delattr(target, name)
        self._undo.append(lambda: setattr(target, name, old_value))

    def setenv(self, name: str, value: str) -> None:
        had = name in os.environ
        old_value = os.environ.get(name)
        os.environ[name] = str(value)
        self._undo.append(
            lambda: os.environ.__setitem__(name, old_value) if had else os.environ.pop(name, None)
        )

    def delenv(self, name: str, raising: bool = True) -> None:
        if name not in os.environ:
            if raising:
                raise KeyError(name)
            return
        old_value = os.environ[name]
        del os.environ[name]
        self._undo.append(lambda: os.environ.__setitem__(name, old_value))

    def setitem(self, mapping: Any, key: Any, value: Any) -> None:
        had = key in mapping
        old_value = mapping.get(key, _NOTSET) if hasattr(mapping, "get") else _NOTSET
        mapping[key] = value
        self._undo.append(
            lambda: mapping.__setitem__(key, old_value) if had else mapping.pop(key, None)
        )

    def delitem(self, mapping: Any, key: Any, raising: bool = True) -> None:
        if key not in mapping:
            if raising:
                raise KeyError(key)
            return
        old_value = mapping[key]
        del mapping[key]
        self._undo.append(lambda: mapping.__setitem__(key, old_value))

    def undo(self) -> None:
        while self._undo:
            action = self._undo.pop()
            try:
                action()
            except Exception:
                pass  # best-effort teardown -- never let cleanup mask the real result


def _safe_delattr(obj: Any, name: str) -> None:
    with contextlib.suppress(AttributeError):
        delattr(obj, name)


def _tmp_path_fixture():
    d = Path(tempfile.mkdtemp(prefix="minipytest-"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _monkeypatch_fixture():
    mp = MonkeyPatch()
    try:
        yield mp
    finally:
        mp.undo()


class _CaplogHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        record.message = record.getMessage()
        self.records.append(record)


class _Caplog:
    """Minimal stand-in for ``pytest``'s ``caplog`` fixture."""

    def __init__(self) -> None:
        self.handler = _CaplogHandler()
        self.handler.setLevel(logging.NOTSET)
        self._root = logging.getLogger()
        self._old_root_level = self._root.level
        self._root.addHandler(self.handler)

    @property
    def records(self) -> list[logging.LogRecord]:
        return self.handler.records

    @contextlib.contextmanager
    def at_level(self, level: int | str, logger: str | None = None):
        target = logging.getLogger(logger) if logger else self._root
        level_num = logging._checkLevel(level)  # accepts int or name; stdlib helper
        old_level = target.level
        target.setLevel(level_num)
        try:
            yield
        finally:
            target.setLevel(old_level)

    def close(self) -> None:
        self._root.removeHandler(self.handler)
        self._root.setLevel(self._old_root_level)


def _caplog_fixture():
    cap = _Caplog()
    try:
        yield cap
    finally:
        cap.close()


_BUILTIN_FIXTURES: dict[str, _FixtureFunction] = {
    "tmp_path": _FixtureFunction(_tmp_path_fixture),
    "monkeypatch": _FixtureFunction(_monkeypatch_fixture),
    "caplog": _FixtureFunction(_caplog_fixture),
}


# ---------------------------------------------------------------------------
# Installing the shim into sys.modules
# ---------------------------------------------------------------------------


def _install_pytest_shim() -> types.ModuleType:
    """Install this shim as ``sys.modules["pytest"]``, idempotently.

    Must run before any test module is imported, so their ``import pytest``
    resolves here. See the module docstring for why this is guarded against
    double-installation rather than assumed to run exactly once.
    """

    existing = sys.modules.get("pytest")
    if existing is not None and getattr(existing, _SHIM_SENTINEL, False):
        return existing

    shim = types.ModuleType("pytest")
    shim.__dict__.update(
        {
            _SHIM_SENTINEL: True,
            "fixture": fixture,
            "raises": raises,
            "mark": mark,
            "approx": approx,
            "importorskip": importorskip,
            "skip": skip,
            "Skipped": Skipped,
            "MonkeyPatch": MonkeyPatch,
        }
    )
    sys.modules["pytest"] = shim
    return shim


# ---------------------------------------------------------------------------
# Fixture resolution
# ---------------------------------------------------------------------------


class _FixtureError(RuntimeError):
    pass


def _lookup_fixture(name: str, module_fixtures: dict[str, _FixtureFunction]) -> _FixtureFunction:
    if name in module_fixtures:
        return module_fixtures[name]
    if name in _BUILTIN_FIXTURES:
        return _BUILTIN_FIXTURES[name]
    raise _FixtureError(f"fixture {name!r} not found")


def _resolve_fixture(
    name: str,
    module_fixtures: dict[str, _FixtureFunction],
    cache: dict[str, Any],
    finalizers: list[Callable[[], None]],
) -> Any:
    if name in cache:
        return cache[name]

    fixture_func = _lookup_fixture(name, module_fixtures)
    sig = inspect.signature(fixture_func.func)
    kwargs = {}
    for param in sig.parameters.values():
        if param.name == "self":
            continue
        kwargs[param.name] = _resolve_fixture(param.name, module_fixtures, cache, finalizers)

    result = fixture_func.func(**kwargs)
    if inspect.isgenerator(result):
        gen = result
        try:
            value = next(gen)
        except StopIteration as exc:
            raise _FixtureError(f"fixture {name!r} yielded no value") from exc

        def _finalize(gen=gen, name=name) -> None:
            try:
                next(gen)
            except StopIteration:
                pass
            else:
                print(
                    f"warning: fixture {name!r} yielded more than once; only the first "
                    "value is used by this fallback runner",
                    file=sys.stderr,
                )

        finalizers.append(_finalize)
    else:
        value = result

    cache[name] = value
    return value


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


class _TestItem:
    def __init__(self, test_id: str, func: Callable, module_fixtures: dict[str, _FixtureFunction]):
        self.test_id = test_id
        self.func = func
        self.module_fixtures = module_fixtures


def _collect_module(module: types.ModuleType) -> tuple[dict[str, _FixtureFunction], list[_TestItem]]:
    module_fixtures: dict[str, _FixtureFunction] = {}
    items: list[_TestItem] = []

    for name, obj in vars(module).items():
        if isinstance(obj, _FixtureFunction):
            module_fixtures[name] = obj

    for name, obj in vars(module).items():
        if getattr(obj, "__module__", None) != module.__name__:
            continue
        if inspect.isfunction(obj) and name.startswith("test_"):
            items.append(_TestItem(f"{module.__name__}::{name}", obj, module_fixtures))
        elif inspect.isclass(obj) and name.startswith("Test"):
            for meth_name, meth in vars(obj).items():
                if inspect.isfunction(meth) and meth_name.startswith("test_"):
                    instance = obj()
                    bound = getattr(instance, meth_name)
                    items.append(
                        _TestItem(f"{module.__name__}::{name}::{meth_name}", bound, module_fixtures)
                    )

    return module_fixtures, items


def _load_module(path: Path, index: int) -> types.ModuleType:
    module_name = f"_minipytest_tests.{index}_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise _FixtureError(f"could not build an import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _discover_test_files(paths: Iterable[Path], repo_root: Path) -> list[Path]:
    files: list[Path] = []
    for p in paths:
        if p.is_dir():
            files.extend(sorted(p.rglob("test_*.py")))
        elif p.is_file():
            files.append(p)
        else:
            print(f"warning: path {p} does not exist, skipping", file=sys.stderr)
    # De-duplicate while preserving order.
    seen: set[Path] = set()
    unique: list[Path] = []
    for f in files:
        resolved = f.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(f)
    return unique


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


class _Result:
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


def _run_item(item: _TestItem) -> tuple[str, str]:
    """Returns (status, detail). ``detail`` is a traceback or skip reason."""

    cache: dict[str, Any] = {}
    finalizers: list[Callable[[], None]] = []
    try:
        sig = inspect.signature(item.func)
        kwargs = {}
        for param in sig.parameters.values():
            kwargs[param.name] = _resolve_fixture(param.name, item.module_fixtures, cache, finalizers)
        item.func(**kwargs)
        return _Result.PASSED, ""
    except Skipped as exc:
        return _Result.SKIPPED, (exc.reason or "skipped")
    except BaseException:
        return _Result.FAILED, traceback.format_exc()
    finally:
        for finalize in reversed(finalizers):
            finalize()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    repo_root = Path(__file__).resolve().parent.parent
    src_dir = repo_root / "src"
    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))

    _install_pytest_shim()

    if argv:
        raw_paths = [Path(a) for a in argv]
    else:
        raw_paths = [repo_root / "tests"]
    test_files = _discover_test_files(raw_paths, repo_root)

    if not test_files:
        print("No test files found.", file=sys.stderr)
        return 1

    passed = failed = skipped = 0
    failures: list[tuple[str, str]] = []
    skips: list[tuple[str, str]] = []

    for index, path in enumerate(test_files):
        try:
            module = _load_module(path, index)
        except BaseException:
            failed += 1
            failures.append((str(path), traceback.format_exc()))
            print(f"ERROR collecting {path}", file=sys.stderr)
            continue

        try:
            module_fixtures, items = _collect_module(module)
        except BaseException:
            failed += 1
            failures.append((str(path), traceback.format_exc()))
            continue

        for item in items:
            status, detail = _run_item(item)
            if status == _Result.PASSED:
                passed += 1
                print(f"PASS  {item.test_id}")
            elif status == _Result.SKIPPED:
                skipped += 1
                skips.append((item.test_id, detail))
                print(f"SKIP  {item.test_id}  ({detail})")
            else:
                failed += 1
                failures.append((item.test_id, detail))
                print(f"FAIL  {item.test_id}")

    print()
    print("=" * 70)
    if failures:
        print(f"{len(failures)} failure(s):")
        for test_id, tb in failures:
            print()
            print(f"--- {test_id} ---")
            print(tb)
    print(f"{passed} passed, {failed} failed, {skipped} skipped")
    print("=" * 70)

    return 1 if failed else 0


if __name__ == "__main__":
    # See the module docstring's "IMPORTANT" section: this file must be run
    # as a script (`python3 scripts/minipytest.py`), never as
    # `python3 -m scripts.minipytest`, to avoid a double-import of this
    # module under two identities.
    sys.exit(main())
