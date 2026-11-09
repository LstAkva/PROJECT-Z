"""
Regression: production (Railway) runs Python 3.13, which evaluates module, class and
function annotations eagerly at import time. Local tests run on Python 3.14, where
annotations are lazy (PEP 649), so a name used only in an annotation but never imported
(e.g. `Dict` in api/auth.py) passed every test yet crashed the server on startup with
`NameError: name 'Dict' is not defined`.

This test forces eager evaluation of every annotation in the app modules, reproducing
the Python 3.13 behaviour on any interpreter.
"""
import importlib
import inspect
import sys

import pytest

APP_MODULES = [
    "main",
    "database",
    "models",
    "telegram_extractor",
    "api.auth",
    "api.daily",
    "api.drafts",
    "api.owner",
    "api.play",
    "api.questions",
    "api.quizzes",
    "services.daily_question",
    "services.eski_savollar_extractor",
    "services.gameplay",
    "services.svoyak_extractor",
]


def _eager_annotations(obj):
    if sys.version_info >= (3, 14):
        import annotationlib

        return annotationlib.get_annotations(obj, format=annotationlib.Format.VALUE)
    # On <= 3.13 annotations were already evaluated when the module was imported.
    return inspect.get_annotations(obj)


@pytest.mark.parametrize("module_name", APP_MODULES)
def test_all_annotations_resolve_like_python_313(module_name):
    module = importlib.import_module(module_name)
    failures = []

    def check(label, obj):
        try:
            _eager_annotations(obj)
        except NameError as exc:
            failures.append(f"{label}: {exc}")

    check(module_name, module)
    for attr_name, attr in vars(module).items():
        if getattr(attr, "__module__", None) != module_name:
            continue
        if inspect.isclass(attr):
            check(f"{module_name}.{attr_name}", attr)
            for meth_name, meth in vars(attr).items():
                fn = meth.__func__ if isinstance(meth, (staticmethod, classmethod)) else meth
                if inspect.isfunction(fn):
                    check(f"{module_name}.{attr_name}.{meth_name}", fn)
        elif inspect.isfunction(attr):
            check(f"{module_name}.{attr_name}", inspect.unwrap(attr))

    assert not failures, "Annotations that crash on Python 3.13:\n" + "\n".join(failures)
