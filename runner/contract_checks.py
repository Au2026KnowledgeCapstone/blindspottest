"""Bounded, declarative checks for application-specific evidence."""
from time import monotonic
from urllib.parse import urlsplit
from playwright.sync_api import expect


def assert_ui(page, conditions, timeout):
    for check in conditions:
        from runner.locators import scope
        locator = scope(page, check.frame).locator(check.selector)
        assertion = expect(locator)
        if check.kind == "visible":
            if check.expected:
                assertion.to_be_visible(timeout=timeout)
            else:
                assertion.to_be_hidden(timeout=timeout)
        elif check.kind == "checked":
            assertion.to_be_checked(checked=bool(check.expected), timeout=timeout)
        elif check.kind == "enabled":
            if check.expected:
                assertion.to_be_enabled(timeout=timeout)
            else:
                assertion.to_be_disabled(timeout=timeout)
        elif check.kind == "count":
            assertion.to_have_count(check.expected, timeout=timeout)
        elif check.kind == "text":
            assertion.to_have_text(check.expected, timeout=timeout)
        elif check.kind == "value":
            assertion.to_have_value(check.expected, timeout=timeout)


def act(page, actions, timeout):
    for action in actions:
        from runner.locators import scope
        locator = scope(page, action.frame).locator(action.selector)
        expect(locator).to_have_count(1, timeout=timeout)
        if action.kind == "click":
            locator.click(timeout=timeout)
        elif action.kind == "fill":
            locator.fill(action.value, timeout=timeout)
        elif action.kind == "select":
            locator.select_option(action.value, timeout=timeout)
        elif action.kind == "check":
            locator.set_checked(action.value, timeout=timeout)
        assert_ui(page, action.ready, timeout)


def api_outcomes(page, instance, contract, notes):
    evidence = []
    notes["api_evidence"] = evidence
    for check in contract.api_assertions:
        # Bind /api/... to the deployment mount, e.g. /broken/api/....
        url = instance.base_url.rstrip("/") + check.path
        if urlsplit(url).netloc != urlsplit(instance.base_url).netloc:
            raise ValueError("API assertion escaped application origin")
        deadline = monotonic() + check.timeout_ms / 1000
        last = None
        mismatch = False
        failure = "No response obtained before deadline"
        observed = False
        while monotonic() < deadline:
            try:
                remaining = max(1, int((deadline - monotonic()) * 1000))
                if observed and remaining < 50:
                    break
                response = page.context.request.get(url, timeout=remaining, max_redirects=0)
                try:
                    if response.status >= 500 or 300 <= response.status < 400:
                        raise ValueError(f"verification unavailable: HTTP {response.status}")
                    if response.status != check.status:
                        last = {"status": response.status}
                        mismatch = True
                    elif check.expected is None and not check.json_path:
                        last = {"status": response.status}
                        mismatch = False
                    else:
                        last = response.json()
                        for key in check.json_path:
                            last = last[key]
                        mismatch = last != check.expected
                    failure = None
                    observed = True
                    if not mismatch:
                        break
                finally:
                    response.dispose()
            except Exception as exc:
                failure = str(exc)
            page.wait_for_timeout(min(100, max(0, int((deadline-monotonic())*1000))))
        evidence.append({"url": url, "path": check.json_path, "expected": check.expected,
                         "actual": last, "error": failure, "matched": not mismatch and failure is None})
        if failure:
            raise ValueError(f"API verification unavailable: {failure}")
        if mismatch:
            return False
    return True
