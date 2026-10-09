import { afterEach, beforeEach, vi } from "vitest";
import { cleanup, configure } from "@testing-library/react";
import { hydrateAuth } from "../api/auth";
import { takeContractViolations, takeUnmockedCalls } from "./render";
import { unnamedControls } from "./a11y";

// One async budget for every findBy*/waitFor, so a starved runner gets the same slack
// everywhere and nobody sprinkles per-call { timeout } overrides.
configure({ asyncUtilTimeout: 5000 });

// jsdom lacks ResizeObserver, which Fluent's MessageBar reflow logic uses.
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
globalThis.ResizeObserver ??= ResizeObserverStub as unknown as typeof ResizeObserver;

// jsdom has no layout: every element's offsetParent is null and <body> measures 0x0.
// Tabster (Fluent's focus manager) reads both as "not visible", so an opening Dialog
// finds nothing focusable and focuses its own surface instead of its first button.
// The surface's modalizer is then never activated, and tabster's deferred (250 ms)
// aria-hidden pass hides the dialog ITSELF as an inactive modal, so role queries can
// no longer see it. Whether a test clicked before that timer fired depended on CPU
// speed: the intermittent CI failures. Give tabster a minimal browser-like answer so
// focus enters the dialog synchronously on open, as it does in a real browser.
Object.defineProperty(HTMLElement.prototype, "offsetParent", {
  configurable: true,
  get(this: HTMLElement) {
    if (window.getComputedStyle(this).display === "none") return null;
    for (let e = this.parentElement; e; e = e.parentElement) {
      if (window.getComputedStyle(e).display === "none") return null;
    }
    return this.parentElement;
  },
});
const realGetBoundingClientRect = Element.prototype.getBoundingClientRect;
Element.prototype.getBoundingClientRect = function (this: Element) {
  return this === this.ownerDocument.body ? new DOMRect(0, 0, 1024, 768) : realGetBoundingClientRect.call(this);
};
// Consequence for tests: while a modal is open the rest of the page is aria-hidden (as
// in a browser), and tabster lifts that ~250 ms after it closes. Query the page by role
// after closing a dialog with findBy*, not getBy*.

// Guard: a mounted dialog that gets aria-hidden is unreachable by role queries. Fail
// the test that caused it instead of letting a later timing-dependent query flake.
const DIALOG = '[role="dialog"], [role="alertdialog"]';
let hiddenDialogs: string[] = [];
const noteHiddenDialogs = (records: MutationRecord[]) => {
  for (const r of records) {
    const el = r.target as Element;
    if (el.matches(DIALOG) && el.getAttribute("aria-hidden") === "true") hiddenDialogs.push(el.outerHTML.slice(0, 200));
  }
};
const dialogWatch = new MutationObserver(noteHiddenDialogs);

beforeEach(() => {
  hiddenDialogs = [];
  dialogWatch.observe(document.documentElement, { subtree: true, attributes: true, attributeFilter: ["aria-hidden"] });
});

afterEach(() => {
  noteHiddenDialogs(dialogWatch.takeRecords());
  dialogWatch.disconnect();
  const problems: string[] = [];
  const missing = [...new Set(takeUnmockedCalls())];
  if (missing.length) problems.push(`request(s) to unmocked route(s): ${missing.join(", ")} (mock them, or mock an explicit 404)`);
  const drift = [...new Set(takeContractViolations())];
  if (drift.length) problems.push(`request(s) outside the OpenAPI contract:\n  ${drift.join("\n  ")}`);
  const unnamed = unnamedControls(document.body);
  if (unnamed.length) problems.push(`control(s) with no accessible name: ${unnamed.slice(0, 5).join(", ")}`);
  if (hiddenDialogs.length) problems.push(`a dialog was made aria-hidden while mounted: ${hiddenDialogs[0]}`);
  for (const d of document.querySelectorAll(DIALOG)) {
    const focus = document.activeElement;
    if (d.querySelector("button:not([disabled])") && (focus === d || !d.contains(focus)))
      problems.push(`focus did not move into an open dialog (it is on <${focus?.tagName.toLowerCase()}>)`);
  }
  cleanup();
  // Anything left after cleanup() was rendered outside RTL's containers (a leaked
  // portal) or is an aria-hidden left on the document; either bleeds into the next test.
  const leftover = document.body.firstElementChild;
  if (leftover) problems.push(`${document.body.children.length} element(s) left in <body> after cleanup, first: <${leftover.tagName.toLowerCase()} class="${leftover.className}">`);
  const leftHidden = document.querySelectorAll('[aria-hidden="true"]').length;
  if (leftHidden) problems.push(`${leftHidden} aria-hidden element(s) left in the document after cleanup`);
  document.body.replaceChildren();
  document.body.removeAttribute("aria-hidden");
  window.sessionStorage.clear();
  window.localStorage.clear();
  hydrateAuth(); // reset in-memory credentials between tests
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  if (problems.length) throw new Error(`Test harness guard:\n- ${problems.join("\n- ")}`);
});
