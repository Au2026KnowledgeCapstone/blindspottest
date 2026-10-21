"""Strict, frame-aware locators shared by declarative checks and actions."""
from runner.persistence_runner import StepError


def scope(page, frame=None):
    if not frame:
        return page
    if page.locator(frame).count() != 1:
        raise StepError(f"frame {frame!r} must resolve uniquely")
    return page.frame_locator(frame)


def resolve(page, ref, timeout):
    strategy = ref.locator_strategy or {}
    surface = scope(page, strategy.get("frame"))
    kind = strategy.get("type")
    if kind == "role" and strategy.get("name"):
        locator = surface.get_by_role(strategy["role"], name=strategy["name"], exact=True)
    elif kind == "label":
        locator = surface.get_by_label(strategy["name"], exact=True)
    elif kind == "placeholder":
        locator = surface.get_by_placeholder(strategy["name"], exact=True)
    elif kind == "test_id":
        locator = surface.get_by_test_id(strategy["value"])
    else:
        locator = surface.locator(ref.selector)
    if locator.count() > 1:
        raise StepError(f"ambiguous locator for {ref.label!r}")
    locator.wait_for(state="visible", timeout=timeout)
    if locator.count() != 1:
        raise StepError(f"locator for {ref.label!r} must resolve uniquely")
    return locator
