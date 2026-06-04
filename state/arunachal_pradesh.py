"""
Uttar Pradesh VAHAN report download (Playwright, PrimeFaces reportview.xhtml).

Flow: open report page -> select state -> each RTO -> set axes/year ->
apply vehicle filters -> download Excel (3 filter sets per RTO).

Waits: after each click, wait for AJAX/POST to finish before the next step.
Default DOWNLOAD_MODE=both (combined + each RTO folder).
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Callable, Union
import time
from playwright.sync_api import Locator, Page, sync_playwright

REPORT_URL = (
    "https://vahan.parivahan.gov.in/vahan4dashboard/vahan/view/reportview.xhtml"
)

ALL_VEHICLES = [
    "E-RICKSHAW WITH CART (G)",
    "E-RICKSHAW(P)",
    "THREE WHEELER (PASSENGER)",
    "THREE WHEELER (GOODS)",
]

STATE_DOWNLOAD_SUBDIR = 'arunachal pradesh'
STATE_MENU_LABEL = 'Arunachal Pradesh(29)'
# Folder for state-wide combined RTO downloads (All Vahan4 Running Office)
COMBINED_RTO_FOLDER = "all_vahan4_running_office"

# Page selectors (CSS or label:Field:). Update here if the site changes classes/ids.
STATE_MENU = 'label:State:'
RTO_MENU = '[for="selectedRto_focus"]'
Y_AXIS_MENU = '#yaxisVar'
X_AXIS_MENU = '#xaxisVar'
YEAR_MENU = '#selectedYear'
# TOP_REFRESH = '.ui-button.ui-widget.ui-state-default.ui-corner-all.ui-button-text-icon-left.button'
# TOP_REFRESH = 'button.ui-button:has(.ui-icon-refresh)'
TOP_REFRESH = 'button.ui-button.button[onclick*="VhCatg"]'
FILTER_REFRESH = '.ui-layout-west.ui-layout-pane-west .ui-layout-unit-footer button.ui-button'
FILTER_PANEL = '.ui-layout-west.ui-layout-pane-west'
VEHICLE_CHECKBOXES = '.ui-layout-west #VhClass'
YEAR_PANEL = '#multipleYear'

# Three downloads per RTO, saved in separate subfolders
FILTER_PROFILES = [
    {
        "folder": "e_rickshaw",
        "vehicles": [
            "E-RICKSHAW WITH CART (G)",
            "E-RICKSHAW(P)",
        ],
    },
    {
        "folder": "three_wheeler",
        "vehicles": [
            "THREE WHEELER (PASSENGER)",
            "THREE WHEELER (GOODS)",
        ],
    },
    {
        "folder": "all_vehicles",
        "vehicles": list(ALL_VEHICLES),
    },
]

DEFAULT_TIMEOUT = 60_000
AJAX_TIMEOUT    = 120_000
DOWNLOAD_TIMEOUT = 300_000

Selector = Union[str, Locator]

# --- Wait until PrimeFaces/jQuery loading overlays are gone ---
_AJAX_IDLE_JS = """() => {
    const jqIdle = !window.jQuery || window.jQuery.active === 0;
    const blockersHidden = Array.from(document.querySelectorAll('.ui-blockui')).every(el => {
        return el.classList.contains('ui-helper-hidden')
            || getComputedStyle(el).display === 'none'
            || el.offsetParent === null;
    });
    const overlays = Array.from(
        document.querySelectorAll('.ui-blockui, .ui-widget-overlay')
    );
    const noOverlay = overlays.every(el => {
        const s = getComputedStyle(el);
        return s.display === 'none' || s.visibility === 'hidden' || el.offsetParent === null;
    });
    return jqIdle && blockersHidden && noOverlay;
}"""

_FIELD_LABEL_TO_MENU_ID_JS = """(labelText) => {
    const normalized = labelText.replace(/:$/, '');
    const matches = Array.from(
        document.querySelectorAll('.field-label, span.white, label.ui-outputlabel')
    );
    for (const el of matches) {
        const t = el.textContent.trim();
        if (t !== labelText && t !== normalized) continue;

        const forAttr = el.getAttribute('for');
        if (forAttr && forAttr.endsWith('_focus')) {
            return forAttr.slice(0, -'_focus'.length);
        }

        const col = el.closest('[class*="ui-grid-col"]');
        const menu = col?.querySelector('.ui-selectonemenu');
        if (menu) return menu.id;
    }
    return null;
}"""

_MENU_WIDGET_ID_FROM_SELECTOR_JS = """(selector) => {
    const el = document.querySelector(selector);
    if (!el) return null;

    if (el.classList.contains('ui-selectonemenu')) return el.id;

    const forAttr = el.getAttribute('for');
    if (forAttr && forAttr.endsWith('_focus')) {
        return forAttr.slice(0, -'_focus'.length);
    }

    const menu = el.querySelector('.ui-selectonemenu') || el.closest('.ui-selectonemenu');
    return menu?.id || null;
}"""

# --- Locators and page waits ---

def locate(page: Page, selector: Selector) -> Locator:
    """Return a Playwright locator from a CSS selector string or existing Locator."""
    if isinstance(selector, Locator):
        return selector
    return page.locator(selector)


def wait_for_selector(
    page: Page,
    selector: Selector,
    *,
    state: str = "visible",
    timeout: int = DEFAULT_TIMEOUT,
) -> Locator:
    """Wait until selector matches; accepts class, id, attribute, or Playwright locator."""
    locator = locate(page, selector)
    locator.wait_for(state=state, timeout=timeout)
    return locator


def resolve_menu_widget_id(page: Page, selector: str) -> str:
    """
    Resolve PrimeFaces SelectOneMenu widget id from any selector.

    Supports:
      - CSS selectors: '#selectedRto', '[for="selectedRto_focus"]', '.ui-selectonemenu'
      - Field labels:  'label:State:', 'label:RTO:'
    """
    if selector.startswith("label:"):
        widget_id = page.evaluate(_FIELD_LABEL_TO_MENU_ID_JS, selector[6:])
    else:
        widget_id = page.evaluate(_MENU_WIDGET_ID_FROM_SELECTOR_JS, selector)
    if not widget_id:
        raise RuntimeError(f"SelectOneMenu not found for selector: {selector}")
    return widget_id


def menu_root(page: Page, selector: str) -> Locator:
    """Root .ui-selectonemenu locator resolved from a generic selector."""
    widget_id = resolve_menu_widget_id(page, selector)
    return page.locator(".ui-selectonemenu").filter(
        has=page.locator(f"[id='{widget_id}_input']")
    )


def wait_for_ajax(page: Page, *, timeout: int = AJAX_TIMEOUT) -> None:
    page.wait_for_function(_AJAX_IDLE_JS, timeout=timeout)


def wait_for_pf_response(page: Page, action: Callable[[], None], *, timeout: int = AJAX_TIMEOUT) -> None:
    """Run action and wait for the PrimeFaces POST that follows it."""
    try:
        with page.expect_response(
            lambda r: (
                r.request.method == "POST"
                and "reportview.xhtml" in r.url
                and r.status == 200
            ),
            timeout=timeout,
        ):
            action()
    except Exception:
        action()
    wait_for_ajax(page, timeout=timeout)


def wait_for_render(locator: Locator, *, timeout: int = DEFAULT_TIMEOUT) -> Locator:
    """Wait visible; retry if PrimeFaces AJAX detaches the node mid-action."""
    locator.wait_for(state="visible", timeout=timeout)
    for _ in range(3):
        try:
            locator.scroll_into_view_if_needed(timeout=timeout)
            return locator
        except Exception as exc:
            if "not attached" not in str(exc).lower():
                raise
            locator.wait_for(state="visible", timeout=timeout)
    return locator


def click_when_ready(
    page: Page,
    selector: Selector,
    name: str,
    *,
    wait_ajax_before: bool = True,
    wait_ajax_after: bool = True,
    timeout: int = DEFAULT_TIMEOUT,
) -> None:
    locator = locate(page, selector)
    if wait_ajax_before:
        wait_for_ajax(page, timeout=timeout)
    wait_for_render(locator, timeout=timeout)
    locator.click(timeout=timeout)
    print(f"OK {name}")
    if wait_ajax_after:
        wait_for_ajax(page, timeout=timeout)


# --- Dropdown menus (State, RTO, axes, year) ---

_FORCE_CLOSE_MENU_PANEL_JS = """(widgetId) => {
    const widget = window.PrimeFaces?.widgets?.['widget_' + widgetId];
    if (widget?.hide) widget.hide();

    const root = document.getElementById(widgetId);
    const panel = document.getElementById(widgetId + '_panel');
    if (root) root.setAttribute('aria-expanded', 'false');
    if (panel) {
        panel.classList.add('ui-helper-hidden');
        panel.style.display = 'none';
        panel.style.visibility = 'hidden';
        panel.style.pointerEvents = 'none';
    }
}"""

_DISMISS_ALL_MENU_OVERLAYS_JS = """() => {
    document.querySelectorAll('.ui-selectonemenu-panel').forEach(panel => {
        panel.classList.add('ui-helper-hidden');
        panel.style.display = 'none';
        panel.style.visibility = 'hidden';
        panel.style.pointerEvents = 'none';
    });
    document.querySelectorAll('.ui-selectonemenu').forEach(root => {
        root.setAttribute('aria-expanded', 'false');
        const widget = window.PrimeFaces?.widgets?.['widget_' + root.id];
        if (widget?.hide) widget.hide();
    });
}"""


def dismiss_all_menu_overlays(page: Page) -> None:
    """Force-close every PrimeFaces dropdown overlay (RTO panel can ghost-block clicks)."""
    page.evaluate(_DISMISS_ALL_MENU_OVERLAYS_JS)
    wait_for_ajax(page)


def get_menu_selected_label(page: Page, selector: str) -> str:
    widget_id = resolve_menu_widget_id(page, selector)
    return page.evaluate(
        """(widgetId) => {
            const sel = document.getElementById(widgetId + '_input');
            if (!sel || sel.selectedIndex < 0) return '';
            return sel.options[sel.selectedIndex].textContent.trim();
        }""",
        widget_id,
    )


def wait_for_menu_value(
    page: Page,
    selector: str,
    option_text: str,
    *,
    timeout: int = AJAX_TIMEOUT,
) -> None:
    """Wait until hidden <select> shows the expected label (survives AJAX re-render)."""
    widget_id = resolve_menu_widget_id(page, selector)
    page.wait_for_function(
        """([widgetId, optionText]) => {
            const sel = document.getElementById(widgetId + '_input');
            if (!sel || sel.selectedIndex < 0) return false;
            return sel.options[sel.selectedIndex].textContent.trim() === optionText;
        }""",
        arg=[widget_id, option_text],
        timeout=timeout,
    )


def wait_for_menu_has_option(
    page: Page,
    selector: str,
    option_text: str,
    *,
    timeout: int = AJAX_TIMEOUT,
) -> None:
    """Wait until an option exists in the menu (needed after cascade AJAX updates)."""
    widget_id = resolve_menu_widget_id(page, selector)
    page.wait_for_function(
        """([widgetId, optionText]) => {
            const sel = document.getElementById(widgetId + '_input');
            if (!sel) return false;
            return Array.from(sel.options).some(
                o => o.textContent.trim() === optionText
            );
        }""",
        arg=[widget_id, option_text],
        timeout=timeout,
    )


def wait_for_menu_stable(page: Page, selector: str, *, timeout: int = DEFAULT_TIMEOUT) -> None:
    widget_id = resolve_menu_widget_id(page, selector)
    wait_for_ajax(page, timeout=timeout)
    menu_root(page, selector).wait_for(state="visible", timeout=timeout)
    page.locator(f"[id='{widget_id}_input']").wait_for(state="attached", timeout=timeout)
    page.wait_for_function(
        """(widgetId) => {
            const root = document.getElementById(widgetId);
            const sel = document.getElementById(widgetId + '_input');
            return root
                && sel
                && root.isConnected
                && sel.isConnected
                && sel.options.length > 0
                && root.getAttribute('aria-disabled') !== 'true';
        }""",
        arg=widget_id,
        timeout=timeout,
    )


def _close_menu_panel(page: Page, selector: str, *, timeout: int = DEFAULT_TIMEOUT) -> None:
    """Close an open PrimeFaces SelectOneMenu panel (Escape often fails on large RTO lists)."""
    widget_id = resolve_menu_widget_id(page, selector)
    page.evaluate(_FORCE_CLOSE_MENU_PANEL_JS, widget_id)
    try:
        page.wait_for_function(
            """(widgetId) => {
                const root = document.getElementById(widgetId);
                const panel = document.getElementById(widgetId + '_panel');
                const expanded = root?.getAttribute('aria-expanded') === 'true';
                const panelBlocks = panel
                    && getComputedStyle(panel).pointerEvents !== 'none'
                    && getComputedStyle(panel).display !== 'none'
                    && panel.offsetParent !== null;
                return !expanded && !panelBlocks;
            }""",
            arg=widget_id,
            timeout=timeout,
        )
    except Exception:
        page.evaluate(_FORCE_CLOSE_MENU_PANEL_JS, widget_id)
    wait_for_ajax(page, timeout=timeout)


def _open_menu_panel(page: Page, selector: str, name: str) -> None:
    """Open dropdown; use widget.show() first (reliable), then trigger click."""
    widget_id = resolve_menu_widget_id(page, selector)
    wait_for_menu_stable(page, selector)

    opened = page.evaluate(
        """(widgetId) => {
            const widget = window.PrimeFaces?.widgets?.['widget_' + widgetId];
            if (widget && typeof widget.show === 'function') {
                widget.show();
                return true;
            }
            return false;
        }""",
        widget_id,
    )

    if not opened:
        trigger = menu_root(page, selector).locator(".ui-selectonemenu-trigger")
        wait_for_render(trigger)
        trigger.click()

    page.locator(f"[id='{widget_id}_items'] li.ui-selectonemenu-item").first.wait_for(
        state="visible",
        timeout=DEFAULT_TIMEOUT,
    )
    print(f"OK {name} (panel open)")


def _select_one_menu_via_ui(
    page: Page,
    selector: str,
    option_text: str,
    name: str,
) -> None:
    widget_id = resolve_menu_widget_id(page, selector)
    wait_for_menu_has_option(page, selector, option_text)
    _open_menu_panel(page, selector, name)

    option = page.locator(f"[id='{widget_id}_items'] li.ui-selectonemenu-item").filter(
        has_text=option_text
    ).first
    wait_for_render(option)
    wait_for_pf_response(page, option.click)
    wait_for_menu_value(page, selector, option_text)
    _close_menu_panel(page, selector)
    print(f"OK {name} -> {option_text}")


def select_one_menu(
    page: Page,
    selector: str,
    option_text: str,
    name: str,
) -> None:
    """Select option; wait for cascade settle before and confirm value after."""
    widget_id = resolve_menu_widget_id(page, selector)
    _close_menu_panel(page, selector)
    wait_for_menu_stable(page, selector)
    wait_for_menu_has_option(page, selector, option_text)

    if get_menu_selected_label(page, selector) == option_text:
        _close_menu_panel(page, selector)
        print(f"OK {name} already -> {option_text}")
        return

    def _select_via_widget() -> None:
        ok = page.evaluate(
            """({ widgetId, optionText }) => {
                const widget = window.PrimeFaces?.widgets?.['widget_' + widgetId];
                const select = document.getElementById(widgetId + '_input');
                if (!select || !widget?.selectValue) return false;

                let value = null;
                for (const opt of select.options) {
                    if (opt.textContent.trim() === optionText) {
                        value = opt.value;
                        break;
                    }
                }
                if (value === null) return false;
                widget.selectValue(value);
                if (widget.hide) widget.hide();
                return true;
            }""",
            {"widgetId": widget_id, "optionText": option_text},
        )
        if not ok:
            raise RuntimeError(f"widget API unavailable for {selector}")

    try:
        wait_for_pf_response(page, _select_via_widget)
        wait_for_menu_value(page, selector, option_text)
        _close_menu_panel(page, selector)
        print(f"OK {name} -> {option_text}")
    except Exception:
        print(f"  widget path failed for {name}, using UI click")
        _select_one_menu_via_ui(page, selector, option_text, name)


def wait_for_year_panel(page: Page) -> None:
    wait_for_ajax(page)
    wait_for_selector(page, YEAR_PANEL)
    wait_for_menu_stable(page, YEAR_MENU)


def wait_after_state_change(page: Page) -> None:
    """State change AJAX-updates RTO + Y-Axis."""
    wait_for_menu_stable(page, RTO_MENU)

def wait_after_rto_change(page: Page) -> None:
    """RTO change AJAX-updates Y-Axis."""
    wait_for_menu_stable(page, Y_AXIS_MENU)


def wait_after_yaxis_change(page: Page) -> None:
    """Y-Axis change AJAX-replaces X-Axis."""
    wait_for_menu_stable(page, X_AXIS_MENU)
    wait_for_menu_has_option(page, X_AXIS_MENU, "Month Wise")


def wait_after_xaxis_change(page: Page) -> None:
    """X-Axis = Month Wise reveals year controls."""
    wait_for_year_panel(page)
    wait_for_menu_has_option(page, YEAR_MENU, str(datetime.now().year))

def open_filter_panel(page: Page) -> None:
    """Open the west PrimeFaces Layout panel that contains vehicle checkboxes."""
    wait_for_ajax(page)
    filter_panel = locate(page, FILTER_PANEL)
    vehicle_panel = locate(page, VEHICLE_CHECKBOXES)

    if (
        filter_panel.locator(".ui-layout-unit-content").is_visible()
        or vehicle_panel.is_visible()
    ):
        print("OK Expand - filter panel already open")
        wait_for_render(vehicle_panel)
        return

    toggler = page.locator(
        "#filterLayout-toggler, .ui-layout-toggler-west, .ui-layout-unit-expand-icon"
    ).first
    click_when_ready(page, toggler, "Expand filter panel", wait_ajax_after=False)

    filter_panel.locator(".ui-layout-unit-content").wait_for(
        state="visible", timeout=DEFAULT_TIMEOUT
    )
    wait_for_render(vehicle_panel)
    print("OK filter panel opened")


# --- Vehicle type checkboxes (e-rickshaw, three-wheeler) ---

def _vehicle_checkbox_checked(checkbox: Locator) -> bool:
    return checkbox.is_checked()


def _set_vehicle_checkbox(page: Page, vehicle_label: str, *, checked: bool) -> None:
    wait_for_ajax(page)
    container = locate(page, VEHICLE_CHECKBOXES)
    label = container.locator("label", has_text=vehicle_label).first
    wait_for_render(label)
    checkbox_id = label.get_attribute("for")
    if not checkbox_id:
        raise RuntimeError(f"Checkbox id not found for {vehicle_label}")

    checkbox = container.locator(f"[id='{checkbox_id}']")
    is_checked = _vehicle_checkbox_checked(checkbox)
    if is_checked == checked:
        state = "checked" if checked else "unchecked"
        print(f"OK {vehicle_label} (already {state})")
        return

    click_when_ready(page, label, vehicle_label, wait_ajax_after=False)
    page.wait_for_function(
        """([checkboxId, expected]) => {
            const el = document.getElementById(checkboxId);
            return !!el && el.checked === expected;
        }""",
        arg=[checkbox_id, checked],
        timeout=DEFAULT_TIMEOUT,
    )


def click_vehicle_checkbox(page: Page, vehicle_label: str) -> None:
    _set_vehicle_checkbox(page, vehicle_label, checked=True)


def set_vehicle_filters(page: Page, vehicles: list[str]) -> None:
    """Check only vehicles in the list; uncheck all others in ALL_VEHICLES."""
    selected = set(vehicles)
    for vehicle in ALL_VEHICLES:
        _set_vehicle_checkbox(page, vehicle, checked=vehicle in selected)


# --- Report ready and Excel download ---

def wait_for_report_ready(page: Page, *, timeout: int = AJAX_TIMEOUT) -> None:
    """Wait for AJAX to finish AND the download Excel button to appear."""
    wait_for_ajax(page, timeout=timeout)
    wait_for_selector(page, "img[title='Download EXCEL file']", timeout=timeout)


# --- Chromium launch options ---

def _browser_launch_args(headless: bool) -> list[str]:
    args = [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
    ]
    if headless:
        args.append("--disable-gpu")
    else:
        args.append("--start-maximized")
    return args


# --- RTO list from dropdown and folder names ---

def rto_label_to_folder_name(rto_label: str) -> str:
    """Filesystem-safe folder name from dropdown label (drops trailing date in parens)."""
    name = re.sub(r"\s*\([^)]*\)\s*$", "", rto_label).strip()
    name = re.sub(r'[<>:"/\\|?*]', "-", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:180] if name else "unknown_rto"


def _labels_from_rto_select(page: Page) -> list[str]:
    widget_id = resolve_menu_widget_id(page, RTO_MENU)
    return page.evaluate(
        """(widgetId) => {
            const sel = document.getElementById(widgetId + '_input');
            if (!sel) return [];
            return Array.from(sel.options)
                .map(o => o.textContent.trim())
                .filter(t => t.length > 0);
        }""",
        widget_id,
    )


def _is_combined_state_rto(label: str) -> bool:
    """True for state-wide option: All Vahan4 Running Office (e.g. 77/77 or 1/1)."""
    low = label.lower()
    if "all vahan4" in low and "running office" in low:
        return True
    m = re.search(r"\(\s*(\d+)\s*/\s*(\d+)\s*\)\s*$", label)
    if m and m.group(1) == m.group(2):
        return True
    return False


def _is_combined_india_state_option(label: str) -> bool:
    """True for all-India state dropdown: All Vahan4 Running States (36/36)."""
    low = label.lower()
    return "all vahan4" in low and "running states" in low


def _labels_from_rto_panel(page: Page) -> list[str]:
    """Full RTO list is in the open PrimeFaces dropdown panel."""
    widget_id = resolve_menu_widget_id(page, RTO_MENU)
    _open_menu_panel(page, RTO_MENU, "RTO list (read options)")
    labels: list[str] = page.evaluate(
        """(widgetId) => {
            return Array.from(
                document.querySelectorAll('[id="' + widgetId + '_items"] li.ui-selectonemenu-item')
            )
                .map(li => (li.getAttribute('data-label') || li.textContent || '').trim())
                .filter(t => t.length > 0);
        }""",
        widget_id,
    )
    try:
        _close_menu_panel(page, RTO_MENU)
    except Exception:
        pass
    return labels


def _wait_for_individual_rtos(page: Page) -> None:
    """Wait until the RTO menu has at least one selectable office (works for 1-RTO states too)."""
    wait_for_menu_stable(page, RTO_MENU)
    widget_id = resolve_menu_widget_id(page, RTO_MENU)

    def _has_rto_option() -> bool:
        return page.evaluate(
            """(widgetId) => {
                const sel = document.getElementById(widgetId + '_input');
                if (!sel) return false;
                const skip = new Set(['', 'select', 'select one', '-select-', 'all', 'all rto']);
                return Array.from(sel.options).some(o => {
                    const t = o.textContent.trim();
                    const low = t.toLowerCase();
                    return t.length > 0 && !skip.has(low) && !low.startsWith('select');
                });
            }""",
            widget_id,
        )

    try:
        page.wait_for_function(
            """(widgetId) => {
                const sel = document.getElementById(widgetId + '_input');
                if (!sel) return false;
                const skip = new Set(['', 'select', 'select one', '-select-', 'all', 'all rto']);
                return Array.from(sel.options).some(o => {
                    const t = o.textContent.trim();
                    const low = t.toLowerCase();
                    return t.length > 0 && !skip.has(low) && !low.startsWith('select');
                });
            }""",
            arg=widget_id,
            timeout=AJAX_TIMEOUT,
        )
    except Exception:
        for _ in range(30):
            wait_for_ajax(page)
            if _has_rto_option():
                break
            time.sleep(1)


def _normalize_rto_labels(labels: list[str]) -> list[str]:
    skip = {"", "select", "select one", "-select-", "all", "all rto"}
    seen: set[str] = set()
    out: list[str] = []
    for label in labels:
        low = label.lower()
        if low in skip or low.startswith("select"):
            continue
        if _is_combined_state_rto(label):
            continue
        if label not in seen:
            seen.add(label)
            out.append(label)
    return out


def list_merged_rto_labels(page: Page) -> list[str]:
    """All RTO dropdown labels (panel + hidden select), before filtering."""
    _wait_for_individual_rtos(page)
    panel_labels = _labels_from_rto_panel(page)
    select_labels = _labels_from_rto_select(page)
    return panel_labels + [l for l in select_labels if l not in panel_labels]


def find_combined_state_rto_label(labels: list[str]) -> str:
    """Pick All Vahan4 Running Office (N/N) for state-wide combined sales data."""
    for label in labels:
        if _is_combined_state_rto(label):
            return label
    raise RuntimeError(
        f"No 'All Vahan4 Running Office' option found for {STATE_DOWNLOAD_SUBDIR}. "
        "Check the RTO dropdown on the site."
    )


def list_rto_labels(page: Page) -> list[str]:
    """
    Individual RTO offices only (excludes All Vahan4 Running Office combined row).
    """
    merged = list_merged_rto_labels(page)
    out = _normalize_rto_labels(merged)

    if not out:
        raise RuntimeError(
            f"No individual RTO options found for {STATE_DOWNLOAD_SUBDIR}. "
            "Use DOWNLOAD_MODE=combined for state-wide data only."
        )
    return out


def save_combined_rto_info(base_download_dir: str, combined_label: str) -> str:
    """Save which combined RTO row was used (state-wide sales)."""
    state_dir = os.path.join(base_download_dir, STATE_DOWNLOAD_SUBDIR)
    os.makedirs(state_dir, exist_ok=True)
    path = os.path.join(state_dir, "combined_rto.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# State-wide combined RTO for {STATE_MENU_LABEL}\n")
        f.write(f"{combined_label}\n")
    print(f"Combined RTO saved: {path}")
    return path


def save_rto_list(base_download_dir: str, rto_labels: list[str]) -> str:
    """Write numbered RTO list for reference under the state folder."""
    state_dir = os.path.join(base_download_dir, STATE_DOWNLOAD_SUBDIR)
    os.makedirs(state_dir, exist_ok=True)
    path = os.path.join(state_dir, "rto_list.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# {STATE_MENU_LABEL} - RTOs ({len(rto_labels)} offices)\n")
        f.write("# folder_name<TAB>dropdown_label\n\n")
        for i, label in enumerate(rto_labels, start=1):
            folder = rto_label_to_folder_name(label)
            f.write(f"{i}\t{folder}\t{label}\n")
    print(f"RTO list saved: {path}")
    return path


def _goto_and_select_state(
    page: Page, url: str, state_menu_label: str | None = None
) -> None:
    """Open report page and select a state (or all-India) from the State dropdown."""
    label = state_menu_label or STATE_MENU_LABEL
    page.goto(url, wait_until="domcontentloaded", timeout=AJAX_TIMEOUT)
    wait_for_menu_stable(page, STATE_MENU)
    select_one_menu(page, STATE_MENU, label, "State")
    wait_after_state_change(page)


def _setup_rto_report(page: Page, rto_label: str) -> None:
    """Select one RTO, axes, year, refresh, and open vehicle filter panel."""
    select_one_menu(page, RTO_MENU, rto_label, "RTO")
    wait_after_rto_change(page)

    select_one_menu(page, Y_AXIS_MENU, "Maker", "Y-Axis")
    wait_after_yaxis_change(page)

    select_one_menu(page, X_AXIS_MENU, "Month Wise", "X-Axis")
    wait_after_xaxis_change(page)

    select_one_menu(page, YEAR_MENU, str(datetime.now().year), "Year")

    dismiss_all_menu_overlays(page)
    click_when_ready(page, TOP_REFRESH, "Refresh 1 (top)")
    wait_for_report_ready(page)

    open_filter_panel(page)
    print("Filter panel opened")


def _setup_report_page(page: Page, url: str, rto_label: str) -> None:
    _goto_and_select_state(page, url)
    _setup_rto_report(page, rto_label)


def _download_filename(download_dir: str, file_prefix: str) -> str:
    """New Excel path each time — never overwrites an existing file in that folder."""
    os.makedirs(download_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    return os.path.join(download_dir, f"{file_prefix}_{timestamp}.xlsx")


def _download_with_vehicles(
    page: Page,
    download_dir: str,
    vehicles: list[str],
    *,
    file_prefix: str,
) -> str:
    set_vehicle_filters(page, vehicles)

    click_when_ready(page, FILTER_REFRESH, "Refresh 2 (filter footer)")
    wait_for_report_ready(page)
    time.sleep(2)

    download_link = locate(page, "img[title='Download EXCEL file']").locator(
        "xpath=ancestor::a[1]"
    ).first

    final_path = _download_filename(download_dir, file_prefix)

    with page.expect_download(timeout=DOWNLOAD_TIMEOUT) as download_info:
        click_when_ready(
            page,
            download_link,
            "Download Excel",
            wait_ajax_after=False,
        )

    print("Download started")
    download_info.value.save_as(final_path)
    print("SUCCESS", final_path)
    return final_path


def run_once(
    download_dir: str,
    *,
    headless: bool,
    vehicles: list[str] | None = None,
    report_url: str | None = None,
    file_prefix: str = "report",
    rto_label: str | None = None,
) -> str:
    """Single browser session, one RTO, one filter set, one download."""
    url = report_url or REPORT_URL
    vehicle_list = vehicles if vehicles is not None else list(ALL_VEHICLES)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=_browser_launch_args(headless),
        )
        context = browser.new_context(
            accept_downloads=True,
            viewport={"width": 1280, "height": 720} if headless else None,
            no_viewport=not headless,
        )
        page = context.new_page()
        page.set_default_timeout(DEFAULT_TIMEOUT)

        _goto_and_select_state(page, url)
        rtos = list_rto_labels(page)
        chosen = rto_label or rtos[0]
        _setup_rto_report(page, chosen)
        path = _download_with_vehicles(
            page, download_dir, vehicle_list, file_prefix=file_prefix
        )

        context.close()
        browser.close()

    print("Browser closed")
    return path


def _download_three_filters_for_rto(
    page: Page,
    base_download_dir: str,
    rto_label: str,
    *,
    output_subfolder: str | None = None,
) -> list[str]:
    """Three vehicle-filter downloads for the current RTO (panel already open)."""
    rto_folder = output_subfolder or rto_label_to_folder_name(rto_label)
    saved: list[str] = []

    for i, profile in enumerate(FILTER_PROFILES, start=1):
        folder = profile["folder"]
        vehicles = profile["vehicles"]
        download_dir = os.path.join(
            base_download_dir,
            STATE_DOWNLOAD_SUBDIR,
            rto_folder,
            folder,
        )
        print(f"\n--- Filter {i}/3 ({rto_folder}): {folder} ---")
        print("   vehicles:", ", ".join(vehicles))
        path = _download_with_vehicles(
            page,
            download_dir,
            vehicles,
            file_prefix=folder,
        )
        saved.append(path)
        if i < len(FILTER_PROFILES):
            wait_for_ajax(page)

    return saved


def run_state_combined_downloads(
    base_download_dir: str,
    *,
    headless: bool,
    report_url: str | None = None,
) -> list[str]:
    """
    One browser session: download 3 Excel files for All Vahan4 Running Office
    (combined sales for the whole state, not per RTO).

    Paths: base_download_dir/<state>/all_vahan4_running_office/<filter>/
    """
    url = report_url or REPORT_URL
    saved_paths: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=_browser_launch_args(headless),
        )
        context = browser.new_context(
            accept_downloads=True,
            viewport={"width": 1280, "height": 720} if headless else None,
            no_viewport=not headless,
        )
        page = context.new_page()
        page.set_default_timeout(DEFAULT_TIMEOUT)

        _goto_and_select_state(page, url)
        merged = list_merged_rto_labels(page)
        combined_label = find_combined_state_rto_label(merged)
        save_combined_rto_info(base_download_dir, combined_label)

        print(f"\n========== State combined: {combined_label} ==========")
        _setup_rto_report(page, combined_label)
        paths = _download_three_filters_for_rto(
            page,
            base_download_dir,
            combined_label,
            output_subfolder=COMBINED_RTO_FOLDER,
        )
        saved_paths.extend(paths)

        context.close()
        browser.close()

    print("Browser closed")
    print(f"Total files saved: {len(saved_paths)}")
    return saved_paths


def run_all_filter_downloads(
    base_download_dir: str,
    *,
    headless: bool,
    report_url: str | None = None,
    rto_limit: int | None = None,
) -> list[str]:
    """
    One browser session: each individual RTO, 3 downloads each.

    Paths: base_download_dir/<state>/<RTO folder>/<e_rickshaw|three_wheeler|all_vehicles>/

    Set env RTO_LIMIT=N to process only the first N RTOs (for testing).
    """
    url = report_url or REPORT_URL
    limit_env = os.environ.get("RTO_LIMIT", "").strip()
    if rto_limit is None and limit_env.isdigit():
        rto_limit = int(limit_env)

    saved_paths: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=_browser_launch_args(headless),
        )
        context = browser.new_context(
            accept_downloads=True,
            viewport={"width": 1280, "height": 720} if headless else None,
            no_viewport=not headless,
        )
        page = context.new_page()
        page.set_default_timeout(DEFAULT_TIMEOUT)

        _goto_and_select_state(page, url)
        rto_labels = list_rto_labels(page)
        save_rto_list(base_download_dir, rto_labels)

        if rto_limit is not None and rto_limit > 0:
            rto_labels = rto_labels[:rto_limit]

        total = len(rto_labels)

        for rto_num, rto_label in enumerate(rto_labels, start=1):
            print(f"\n========== RTO {rto_num}/{total}: {rto_label} ==========")
            try:
                _setup_rto_report(page, rto_label)
                paths = _download_three_filters_for_rto(
                    page, base_download_dir, rto_label
                )
                saved_paths.extend(paths)
            except Exception as exc:
                print(f"FAILED RTO {rto_label}: {exc}")
                try:
                    _goto_and_select_state(page, url)
                except Exception as recover_exc:
                    print(f"Could not recover page after failure: {recover_exc}")

        context.close()
        browser.close()

    print("Browser closed")
    print(f"Total files saved: {len(saved_paths)}")
    return saved_paths


def run_downloads(
    base_download_dir: str,
    *,
    headless: bool,
    report_url: str | None = None,
    rto_limit: int | None = None,
) -> list[str]:
    """
    DOWNLOAD_MODE env:
      both       - all_vahan4_running_office/ + each RTO folder (default)
      combined   - only all_vahan4_running_office/ (3 files)
      individual - only each RTO folder (no combined row)
    """
    mode = os.environ.get("DOWNLOAD_MODE", "both").strip().lower()
    saved: list[str] = []

    if mode in ("combined", "both"):
        saved.extend(
            run_state_combined_downloads(
                base_download_dir, headless=headless, report_url=report_url
            )
        )
    if mode in ("individual", "both"):
        saved.extend(
            run_all_filter_downloads(
                base_download_dir,
                headless=headless,
                report_url=report_url,
                rto_limit=rto_limit,
            )
        )
    if not saved:
        raise ValueError(
            f"Invalid DOWNLOAD_MODE={mode!r}; use combined, individual, or both"
        )
    return saved


# --- Run when executed: python state/uttar_pradesh.py ---

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_DOWNLOAD_PATH = os.path.join(_PROJECT_ROOT, "download_reports")


def _main() -> None:
    download_path = os.environ.get("DOWNLOAD_PATH", _DEFAULT_DOWNLOAD_PATH)
    os.makedirs(download_path, exist_ok=True)

    headless = os.environ.get("HEADLESS", "0").strip().lower() in (
        "1",
        "true",
        "yes",
    )

    while True:
        try:
            mode = os.environ.get("DOWNLOAD_MODE", "both").strip().lower()
            print(
                f"\nStarting {STATE_DOWNLOAD_SUBDIR} "
                f"(DOWNLOAD_MODE={mode}): 3 filters per run"
            )
            paths = run_downloads(download_path, headless=headless)
            print("\nAll downloads complete:")
            for path in paths:
                print(" ", path)
            break
        except Exception as exc:
            print("\nERROR:", exc)
            print("Retrying in 30 min...")
            time.sleep(1800)


if __name__ == "__main__":
    _main()
