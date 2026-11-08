"""Offline smoke using the installed Chainlit runner and existing Chromium."""
import copy
import json
import re
import unittest
from pathlib import Path

import chainlit
from playwright.sync_api import sync_playwright

from gpt_rag_ui.services.native_presentation import rest_projection, text_projection


class InlineBrowserTests(unittest.TestCase):
    def test_actual_jsx_four_views_exact_table_svg_and_followup(self):
        assets = Path(chainlit.__file__).parent / "frontend/dist/assets"
        bundle = next(assets.glob("index-*.js"))
        installed = bundle.read_text(encoding="utf-8")
        # In-memory test bootstrap only: expose the already installed runner.
        # Do not rewrite the library/renderer or modify shipped security policy.
        # Minified argument names differ between the declared 2.12 runner and
        # the earlier local runner; retain the same structural admission check.
        match = re.search(
            r"return \w+\.jsx\((?P<runner>\w+),\{code:\w+,scope:\{import:(?P<imports>\w+),props:\w+",
            installed,
        )
        self.assertIsNotNone(match, "Installed Chainlit runner layout changed; smoke harness needs qualification")
        runner, imports = match.group("runner"), match.group("imports")
        mount = re.search(r"(\w+)\.createRoot\(document\.getElementById\(\"root\"\)\)\.render\(.*?\);export", installed)
        self.assertIsNotNone(mount)
        react = re.search(r"(\w+)\.memo\(function\(\{sourceCode:\w+,props:\w+\}", installed).group(1)
        hook = f"window.nativeSmoke={{React:{react},createRoot:{mount.group(1)}.createRoot,Runner:{runner},imports:{imports}}};"
        instrumented = installed[:mount.start()] + hook + installed[mount.end()-6:]
        root = Path(__file__).parents[1]
        source = (root / "public/elements/Presentation.jsx").read_text(encoding="utf-8")
        envelope = json.loads((root / "tests/fixtures/inline-presentation/presentation-valid.json").read_text())
        envelope["contributions"][0]["blocks"][1]["rows"][1]["values"]["revenue"] = None
        envelope["contributions"][0]["blocks"][1]["rows"][2]["status"] = {
            "value": "critical", "provenance": {"origin": "source", "binding": {
                "sourceId": "source-revenue", "sourceRowId": "mar", "field": "status",
            }},
        }
        envelope["contributions"][0]["blocks"][2]["series"][0]["values"][1] = None
        envelope["markdown"] = "Revenue (USD): January 10.125, February —, March 9.750. February is a null gap, not zero."
        future = copy.deepcopy(envelope)
        future["schemaVersion"] = 2
        unknown = copy.deepcopy(envelope)
        unknown["contributions"][0]["blocks"].append({
            "type": "future", "dangerouslySetInnerHTML": {"__html": "<img src='https://example.invalid/probe'>"},
        })
        unknown["contributions"][0]["blocks"][0]["cards"][0]["label"] = "<img src=x onerror=alert(1)>"
        malformed = copy.deepcopy(envelope)
        malformed["contributions"][0]["blocks"][1]["rows"][0]["values"]["revenue"] = 10.125
        wide = copy.deepcopy(envelope)
        wide["contributions"][0]["sourceRefs"][0]["toolName"] = "customer_" + "x" * 1500
        wide["contributions"][0]["blocks"][0]["cards"][0]["label"] = "LongLabel_" + "x" * 1500
        wide["contributions"][0]["blocks"][1]["rows"][0]["values"]["month"] = "January_" + "x" * 1500
        wide["contributions"][0]["blocks"][2]["categories"][0]["label"] = "January_" + "x" * 1500
        raw = json.dumps(envelope)
        def carrier(text, role="assistant", kind="message"):
            return {"output": [{"type": kind, "role": role, "content": [{"type": "output_text", "text": text}]}]}
        cases = {
            "valid": carrier(raw),
            "unknown": carrier(json.dumps(unknown)),
            "future": carrier(json.dumps(future)),
            "malformed": carrier(json.dumps(malformed)),
            "wide": carrier(json.dumps(wide)),
            "user": carrier(raw, "user"),
            "tool": carrier(raw, "tool"),
            "progress": carrier(raw, kind="progress"),
            "primary": carrier("```text\n" + raw + "\n```"),
            "incomplete": carrier(raw[:-5]),
            "plain": carrier("Ordinary authorized text; no presentation available."),
        }
        artifacts = root / ".artifacts/inline-presentation-playwright"
        artifacts.mkdir(parents=True, exist_ok=True)
        requests, errors = [], []
        css = next(assets.glob("index-*.css")).read_text(encoding="utf-8")
        # Exactly the stylesheet and theme variables loaded by the app; no
        # screenshot-only component styling or generated utility substitutes.
        css += (root / "public/custom.css").read_text(encoding="utf-8")
        theme = json.loads((root / "public/theme.json").read_text(encoding="utf-8"))
        for mode, variables in theme["variables"].items():
            css += (":root" if mode == "light" else ".dark") + "{" + "".join(
                f"{name}:{value};" for name, value in variables.items()
            ) + "}"
        with sync_playwright() as runtime:
            browser = runtime.chromium.launch(headless=True)
            try:
                for embedded in (False, True):
                    for dark in (False, True):
                        with self.subTest(embedded=embedded, dark=dark):
                            context = browser.new_context(viewport={"width": 960, "height": 900})
                            context.set_default_timeout(5000)
                            context.grant_permissions(["clipboard-read", "clipboard-write"])
                            context.tracing.start(screenshots=True, snapshots=True, sources=True)
                            page = context.new_page()
                            page.on("pageerror", lambda error: errors.append(str(error)))
                            html = f'<html class="{"dark" if dark else ""}"><head><style>{css}</style></head><body><div id="root"></div><script type="module" src="/assets/{bundle.name}"></script></body></html>'
                            def route(request):
                                url = request.request.url
                                if url == "http://127.0.0.1/native-smoke":
                                    request.fulfill(body=html, content_type="text/html")
                                elif url.startswith("http://127.0.0.1/test-api/"):
                                    # Mock auth/data boundary, real production API projection.
                                    value = cases[url.rsplit("/", 1)[1]]
                                    projected = rest_projection(value)
                                    request.fulfill(body=json.dumps({
                                        "presentations": projected.get("presentations", []),
                                        "text": text_projection(value),
                                    }), content_type="application/json")
                                elif url.endswith("/assets/" + bundle.name):
                                    request.fulfill(body=instrumented, content_type="text/javascript")
                                elif url.startswith("http://127.0.0.1/assets/"):
                                    file = assets / url.rsplit("/", 1)[1]
                                    request.fulfill(path=str(file)) if file.is_file() else request.abort()
                                else:
                                    requests.append(url)
                                    request.abort()
                            context.route("**/*", route)
                            page.goto("http://127.0.0.1/native-smoke")
                            target = page
                            if embedded:
                                page.evaluate("html => { const frame=document.createElement('iframe'); frame.id='embedded'; frame.style.cssText='width:100%;height:1600px;border:0'; frame.srcdoc=html; document.body.replaceChildren(frame); }", html)
                                target = page.frame_locator("#embedded")
                            target.locator("#root").wait_for(state="attached")
                            frame = page.frames[-1] if embedded else page.main_frame
                            frame.wait_for_function("window.nativeSmoke")
                            chart_type = "line" if embedded else "grouped_bar" if dark else "bar"
                            frame.evaluate("""({source, chartType}) => {
                                window.sent=[]; window.failures=[];
                                const {React,createRoot,Runner,imports}=window.nativeSmoke;
                                const root=createRoot(document.getElementById('root'));
                                window.renderScenario=async name => {
                                  const packet=await (await fetch('/test-api/'+name)).json();
                                  const projection=packet.presentations[0];
                                  if(!projection) {
                                    root.render(React.createElement('pre',{id:'fallback'},packet.text));
                                    return;
                                  }
                                  for(const c of projection.contributions)
                                    for(const b of c.blocks) if(b.type==='chart') b.chartType=chartType;
                                  root.render(React.createElement(Runner, {key:name,
                                    code:source, scope:{import:imports, props:projection,
                                      sendUserMessage:prompt => {window.sent.push(prompt); return new Promise(resolve => window.release=resolve);}},
                                    onRendered:error => { if(error) window.failures.push(String(error));}
                                  }));
                                };
                                return window.renderScenario('valid');
                            }""", {"source": source, "chartType": chart_type})
                            target.get_by_text("9.750", exact=False).first.wait_for()
                            self.assertGreater(target.locator("svg").count(), 0)
                            self.assertGreater(target.locator("table").count(), 0)
                            self.assertIn("9.750", target.locator("body").inner_text())
                            self.assertIn("Trend: down", target.locator("body").inner_text())
                            self.assertIn("Revenue: — USD", target.locator("body").inner_text())
                            self.assertEqual(target.locator("svg circle" if embedded else "svg rect").count(), 2)
                            chart_value = target.locator("td[tabindex='0']").first
                            chart_value.focus()
                            chart_value.press("Tab")
                            self.assertEqual(frame.evaluate("document.activeElement.textContent"), "Revenue: — USD")
                            target.locator("td[tabindex='0']").nth(1).press("Tab")
                            self.assertEqual(frame.evaluate("document.activeElement.textContent"), "Revenue: 9.750 USD")
                            self.assertEqual(frame.evaluate("window.failures"), [])
                            style = frame.evaluate("""() => {
                              const card=document.querySelector('.presentation-block');
                              const chart=document.querySelector('svg');
                              return {radius:getComputedStyle(card).borderRadius,
                                font:getComputedStyle(card).fontFamily,
                                height:chart.getBoundingClientRect().height,
                                fill:getComputedStyle(chart.querySelector('circle,rect')).fill,
                                foreground:getComputedStyle(card).color};
                            }""")
                            self.assertEqual(style["radius"], "10px", "Production scoped CSS must be loaded")
                            self.assertIn("sans-serif", style["font"])
                            self.assertLessEqual(style["height"], 280)
                            self.assertNotEqual(style["fill"], style["foreground"])
                            self.assertIn("January", target.locator("svg").text_content())
                            self.assertIn("10.125", target.locator("svg").text_content())
                            table = target.locator("table").first
                            target.get_by_role("button", name="Sort by Revenue").click()
                            self.assertEqual(table.locator("tbody tr").first.locator("td").first.inner_text(), "March")
                            self.assertEqual(table.locator("tbody tr").last.locator("td").first.inner_text(), "February")
                            target.get_by_role("textbox").fill("March")
                            self.assertEqual(table.locator("tbody tr").count(), 1)
                            target.get_by_role("button", name="Copy visible rows").click()
                            target.get_by_text("Visible rows copied.", exact=True).wait_for()
                            copied = frame.evaluate("navigator.clipboard.readText()")
                            self.assertIn("Revenue (USD)", copied)
                            self.assertIn("9.750", copied)
                            self.assertNotIn("January", copied)
                            self.assertIn("Status\tFollow-up", copied)
                            self.assertIn("critical\tInspect month", copied)
                            target.get_by_role("textbox").fill("critical")
                            self.assertEqual(table.locator("tbody tr").count(), 1)
                            button = target.get_by_role("button", name="Inspect month")
                            button.click()
                            frame.evaluate("document.querySelector('button:disabled')?.click()")
                            self.assertEqual(frame.evaluate("window.sent"), ["Inspect revenue for month mar"])
                            frame.evaluate("window.release()")
                            target.get_by_role("button", name="Inspect month").wait_for(state="visible")
                            suggestion = target.get_by_role("button", name="Compare these months with the same period last year.")
                            suggestion.focus()
                            suggestion.press("Enter")
                            self.assertEqual(len(frame.evaluate("window.sent")), 2)
                            frame.evaluate("window.release()")
                            target.get_by_role("textbox").fill("")
                            target.get_by_text("Text alternative", exact=True).click()
                            target.get_by_text(envelope["markdown"], exact=True).wait_for()
                            name = f'{"embed" if embedded else "standalone"}-{"dark" if dark else "light"}'
                            frame.evaluate("window.scrollTo(0,0)")
                            page.evaluate("window.scrollTo(0,0)")
                            for mobile in (False, True):
                                page.set_viewport_size({"width": 390 if mobile else 960, "height": 900})
                                if embedded:
                                    height = frame.evaluate("Math.ceil(document.getElementById('root').getBoundingClientRect().height)+16")
                                    page.locator("#embedded").evaluate("(e,h) => e.style.height=h+'px'", height)
                                self.assertLessEqual(
                                    frame.evaluate("document.documentElement.scrollWidth"),
                                    frame.evaluate("document.documentElement.clientWidth") + 1,
                                )
                                suffix = "-mobile" if mobile else ""
                                page.screenshot(path=str(artifacts / f"{name}{suffix}.png"), full_page=True)
                            frame.evaluate("name => window.renderScenario(name)", "wide")
                            target.get_by_text("LongLabel_" + "x" * 1500, exact=True).wait_for()
                            layout = frame.evaluate("""() => ({
                              width:document.documentElement.clientWidth, scroll:document.documentElement.scrollWidth,
                              wrap:getComputedStyle(document.querySelector('#root>div')).overflowWrap,
                              offenders:[...document.querySelectorAll('#root *')].filter(e=>e.getBoundingClientRect().right>document.documentElement.clientWidth+1)
                                .slice(0,6).map(e=>[e.tagName,e.className,e.getBoundingClientRect().width])
                            })""")
                            self.assertLessEqual(layout["scroll"], layout["width"] + 1, str(layout))
                            self.assertEqual(target.get_by_role("button", name="Copy visible rows").count(), 1)
                            self.assertGreater(
                                target.locator(".presentation-scroll").first.evaluate("e => e.scrollWidth"),
                                target.locator(".presentation-scroll").first.evaluate("e => e.clientWidth"),
                                "Wide data must scroll locally on small screens",
                            )
                            for scenario in ("unknown", "malformed", "future", "user", "tool", "progress", "primary", "incomplete", "plain"):
                                frame.evaluate("name => window.renderScenario(name)", scenario)
                                if scenario in ("unknown", "malformed", "future"):
                                    target.get_by_text("Text alternative", exact=True).wait_for()
                                    target.get_by_text("Text alternative", exact=True).click()
                                    target.get_by_text(envelope["markdown"], exact=True).wait_for()
                                    if scenario == "unknown":
                                        target.get_by_text("<img src=x onerror=alert(1)>", exact=True).wait_for()
                                        self.assertEqual(target.locator("img").count(), 0)
                                        self.assertEqual(target.locator("svg").count(), 1)
                                    elif scenario == "malformed":
                                        self.assertEqual(target.get_by_role("button", name="Copy visible rows").count(), 0)
                                    else:
                                        self.assertEqual(target.locator("svg,table,dl").count(), 0)
                                        page.screenshot(path=str(artifacts / f"{name}-fallback.png"), full_page=True)
                                else:
                                    target.locator("#fallback").wait_for()
                                    self.assertEqual(target.locator("svg,table,dl,button").count(), 0)
                            self.assertEqual(frame.evaluate("window.failures"), [])
                            context.tracing.stop(path=str(artifacts / f"{name}.zip"))
                            context.close()
                self.assertEqual(errors, [])
                self.assertEqual(requests, [], "Renderer must not request external resources")
            finally:
                browser.close()
