from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright


class StepError(RuntimeError):
    pass


def origin(url):
    parts = urlsplit(url)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
    ):
        raise StepError("Use an HTTP or HTTPS URL without embedded credentials.")
    try:
        port = parts.port
    except ValueError:
        raise StepError("The URL contains an invalid port.") from None
    host = parts.hostname.lower().rstrip(".")
    default_port = 443 if parts.scheme == "https" else 80
    host = f"[{host}]" if ":" in host else host
    return f"{parts.scheme}://{host}" + (f":{port}" if port and port != default_port else "")


SELECTOR = (
    "a[href],button,input:not([type=hidden]),select,textarea,[role=button],[role=link],"
    "[role=checkbox],[role=combobox],[role=textbox],[role=tab],[contenteditable=true],summary"
)
DESCRIBE = r"""el => {
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || (tag === 'button' ? el.type : '')).toLowerCase();
    const label = el.getAttribute('aria-labelledby')?.split(/\s+/)
        .map(id => document.getElementById(id)?.innerText || '').join(' ');
    const name = el.getAttribute('aria-label') || label || Array.from(el.labels || [])
        .map(item => item.innerText).join(' ') || el.getAttribute('placeholder')
        || el.innerText || el.value && (type === 'button' || type === 'submit') && el.value
        || el.title || el.name || '';
    const rect = el.getBoundingClientRect();
    if (!rect.width || !rect.height || getComputedStyle(el).visibility !== 'visible'
        || el.closest('[inert],[aria-hidden="true"]') || type === 'password'
        || /password|one-time-code|cc-/i.test(el.getAttribute('autocomplete') || '')) return null;
    const role = el.getAttribute('role') || ({
        a:'link', button:'button', textarea:'textbox', select:'combobox', summary:'button'
    }[tag]) || (type === 'submit' || type === 'button' ? 'button'
        : type === 'checkbox' || type === 'radio' ? type
        : el.isContentEditable || tag === 'input' ? 'textbox' : tag);
    return {name: String(name).trim().slice(0, 160), role, tag, type, href: el.href || '',
        disabled: el.matches(':disabled,[aria-disabled=true]'), edit: el.isContentEditable,
        form: el.form?.action || '', id: el.id || ''};
}"""


@dataclass
class BrowserState:
    tab: str
    url: str
    controls: dict
    text: str
    title: str
    challenge: bool

    def planner_data(self):
        return {
            "tab": self.tab,
            "url": self.url,
            "title": self.title,
            "controls": self.controls,
            "visible_text": self.text,
        }


class Browser:
    def __init__(self, profile: Path, guard, *, executable=None, headless=False):
        self.profile = profile
        self.guard = guard
        self.executable = executable
        self.headless = headless
        self.playwright = self.context = self.page = None
        self.tabs = {}
        self.handles = {}
        self.next_tab = 1
        self.state = None
        self.context_closed = False

    def _ensure(self):
        if self.context_closed:
            self.close()
        if self.context is not None:
            return
        self.guard()
        self.profile.mkdir(parents=True, exist_ok=True)
        self.playwright = sync_playwright().start()
        params = {"executable_path": self.executable} if self.executable else {"channel": "chrome"}
        try:
            self.context = self.playwright.chromium.launch_persistent_context(
                str(self.profile),
                headless=self.headless,
                timeout=12000,
                accept_downloads=False,
                chromium_sandbox=not self.headless,
                ignore_default_args=[
                    "--disable-client-side-phishing-detection",
                    "--unsafely-disable-devtools-self-xss-warnings",
                ],
                args=["--no-first-run", "--no-default-browser-check"],
                **params,
            )
        except PlaywrightError:
            self.close()
            raise StepError(
                "Chrome could not start. Install Chrome and close any other Jarvis Chrome window."
            ) from None
        self.context.set_default_timeout(5000)
        self.context.set_default_navigation_timeout(9000)
        self.context.on("close", lambda _: self._on_close())

    def _on_close(self):
        self.page = None
        self.context_closed = True

    def _register(self, page):
        for tab, existing in self.tabs.items():
            if existing == page:
                return tab
        tab = f"tab-{self.next_tab}"
        self.next_tab += 1
        self.tabs[tab] = page
        return tab

    def start_page(self, url, *, new_tab=False):
        self._ensure()
        self.guard()
        if new_tab or self.page is None or self.page.is_closed():
            self.page = next((p for p in self.context.pages if p.url == "about:blank"), None)
            if self.page is None:
                self.page = self.context.new_page()
        self._register(self.page)
        self.page.bring_to_front()
        if self.page.url != url:
            self.guard()
            try:
                self.page.goto(url, wait_until="domcontentloaded")
            except PlaywrightError:
                if self.page.url == "about:blank":
                    raise StepError(
                        "Chrome could not load the page. Check the address or connection."
                    ) from None
        return self.page

    def switch(self, target):
        self.available_tabs()
        page = self.tabs.get(target)
        if not page or page.is_closed():
            raise StepError("That tab has closed. Select an open tab.")
        self.guard()
        page.bring_to_front()
        self.page = page

    def available_tabs(self):
        if self.context:
            for page in self.context.pages:
                self._register(page)
        return {t: p.url for t, p in self.tabs.items() if not p.is_closed()}

    def observe(self):
        if not self.page or self.page.is_closed():
            self.state = None
            return None
        page = self.page
        url = page.url
        title = page.title()
        challenge = (
            urlsplit(url).hostname == "accounts.google.com"
            or bool(page.locator("input[type=password]:visible").count())
            or bool(page.get_by_text("Verify you are human", exact=True).count())
        )
        can_inspect = not challenge and urlsplit(url).scheme in {"http", "https"}
        controls = {}
        self.handles.clear()
        if can_inspect:
            for index, handle in enumerate(
                page.locator(f":is({SELECTOR}):visible").element_handles()[:110]
            ):
                description = handle.evaluate(DESCRIBE)
                if description and not description["disabled"]:
                    key = f"b{index + 1}"
                    controls[key] = description
                    self.handles[key] = handle
            text = page.locator("body").inner_text(timeout=3000)[:4500]
        else:
            text = ""
        self.state = BrowserState(self._register(page), url, controls, text, title, challenge)
        return self.state

    def target(self, key):
        old = self.state
        if not old or not self.page or self.page.is_closed() or self.page.url != old.url:
            raise StepError("Chrome changed pages. Observe it again before acting.")
        handle = self.handles.get(key)
        if handle is None:
            raise StepError("That browser control is absent. Use a current control ID.")
        current = handle.evaluate(f"el => el.isConnected ? ({DESCRIBE})(el) : null")
        if current != old.controls[key]:
            raise StepError("That browser control changed. Observe again before acting.")
        return handle, current

    def click(self, target):
        handle, _ = self.target(target)
        self.guard()
        handle.click(timeout=1800, no_wait_after=True)

    def fill(self, target, text):
        handle, details = self.target(target)
        if not (details["edit"] or details["tag"] in {"input", "textarea", "select"}):
            raise StepError("The target is not an editable browser field.")
        if details["type"] in {"file", "submit", "button", "reset", "image", "radio", "checkbox"}:
            raise StepError("Use an action appropriate to that browser control.")
        self.guard()
        if details["tag"] == "select":
            chosen = handle.select_option(label=text, timeout=1800)
            actual = handle.evaluate("el => el.value")
            if not chosen or actual != chosen[0]:
                raise StepError("The selected option could not be verified.")
        else:
            handle.fill(text, timeout=1800)
            actual = handle.evaluate("el => el.isContentEditable ? el.innerText : el.value")
            if actual != text:
                raise StepError("The browser field did not retain the requested value.")

    def upload(self, target, paths):
        handle, details = self.target(target)
        if details["type"] != "file":
            raise StepError("Select an observed file input.")
        self.guard()
        handle.set_input_files([str(path) for path in paths], timeout=1800)
        names = handle.evaluate("el => Array.from(el.files, f => f.name)")
        if names != [path.name for path in paths]:
            raise StepError("The browser file input did not report the expected attachments.")

    def scroll(self, direction):
        if direction not in {"up", "down"}:
            raise StepError("Scroll direction must be up or down.")
        self.guard()
        self.page.mouse.wheel(0, 650 if direction == "down" else -650)

    def close(self):
        try:
            if self.context:
                self.context.close()
        except PlaywrightError:
            pass
        finally:
            if self.playwright:
                self.playwright.stop()
            self.page = self.context = self.playwright = self.state = None
            self.tabs.clear()
            self.handles.clear()
            self.context_closed = False
