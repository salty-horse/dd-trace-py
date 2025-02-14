import json
import os
import sys

import pytest

from ddtrace.contrib.pytest.plugin import _extract_span as _extract_feature_span
from ddtrace.contrib.pytest_bdd.constants import FRAMEWORK
from ddtrace.contrib.pytest_bdd.constants import STEP_KIND
from ddtrace.ext import test
from ddtrace.internal.ci_visibility import CIVisibility as _CIVisibility
from ddtrace.internal.logger import get_logger


log = get_logger(__name__)


def _extract_span(item):
    """Extract span from `step_func`."""
    return getattr(item, "_datadog_span", None)


def _store_span(item, span):
    """Store span at `step_func`."""
    setattr(item, "_datadog_span", span)


def pytest_configure(config):
    if config.pluginmanager.hasplugin("pytest-bdd"):
        config.pluginmanager.register(_PytestBddPlugin(), "_datadog-pytest-bdd")


class _PytestBddPlugin:
    def __init__(self):
        import pytest_bdd

        self.framework_version = pytest_bdd.__version__

    @staticmethod
    @pytest.hookimpl(tryfirst=True)
    def pytest_bdd_before_scenario(request, feature, scenario):
        if _CIVisibility.enabled:
            span = _extract_feature_span(request.node)
            if span is not None:
                location = os.path.relpath(scenario.feature.filename, str(request.config.rootdir))
                span.set_tag(test.NAME, scenario.name)
                span.set_tag(test.SUITE, location)  # override test suite name with .feature location

                _CIVisibility.set_codeowners_of(location, span=span)

    @pytest.hookimpl(tryfirst=True)
    def pytest_bdd_before_step(self, request, feature, scenario, step, step_func):
        if _CIVisibility.enabled:
            feature_span = _extract_feature_span(request.node)
            span = _CIVisibility._instance.tracer.start_span(
                step.type,
                resource=step.name,
                span_type=STEP_KIND,
                child_of=feature_span,
                activate=True,
            )
            span.set_tag_str("component", "pytest_bdd")

            span.set_tag(test.FRAMEWORK, FRAMEWORK)
            span.set_tag(test.FRAMEWORK_VERSION, self.framework_version)

            # store parsed step arguments
            try:
                parsers = [step_func.parser]
            except AttributeError:
                try:
                    # pytest-bdd >= 6.0.0
                    parsers = step_func._pytest_bdd_parsers
                except AttributeError:
                    parsers = []
            for parser in parsers:
                if parser is not None:
                    converters = getattr(step_func, "converters", {})
                    parameters = {}
                    try:
                        for arg, value in parser.parse_arguments(step.name).items():
                            try:
                                if arg in converters:
                                    value = converters[arg](value)
                            except Exception:
                                log.debug("Ignoring invalid converter")
                            parameters[arg] = value
                    except Exception:
                        log.debug("Encountered error while parsing arguments, ignoring", exc_info=True)
                    if parameters:
                        span.set_tag(test.PARAMETERS, json.dumps(parameters))

            location = os.path.relpath(step_func.__code__.co_filename, str(request.config.rootdir))
            span.set_tag(test.FILE, location)
            _CIVisibility.set_codeowners_of(location, span=span)

            _store_span(step_func, span)

    @staticmethod
    @pytest.hookimpl(trylast=True)
    def pytest_bdd_after_step(request, feature, scenario, step, step_func, step_func_args):
        span = _extract_span(step_func)
        if span is not None:
            span.finish()

    @staticmethod
    def pytest_bdd_step_error(request, feature, scenario, step, step_func, step_func_args, exception):
        span = _extract_span(step_func)
        if span is not None:
            if hasattr(exception, "__traceback__"):
                tb = exception.__traceback__
            else:
                # PY2 compatibility workaround
                _, _, tb = sys.exc_info()
            span.set_exc_info(type(exception), exception, tb)
            span.finish()
