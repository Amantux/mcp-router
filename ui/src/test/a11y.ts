import { computeAccessibleName } from "dom-accessibility-api";

const CONTROLS = [
  "button",
  "input:not([type=hidden])",
  "select",
  "textarea",
  '[role="button"]',
  '[role="switch"]',
  '[role="checkbox"]',
  '[role="radio"]',
  '[role="combobox"]',
  '[role="slider"]',
  '[role="spinbutton"]',
  '[role="textbox"]',
  '[role="tab"]',
  '[role="columnheader"]',
].join(", ");

function hidden(el: Element): boolean {
  for (let e: Element | null = el; e; e = e.parentElement) {
    if (e.getAttribute("aria-hidden") === "true" || (e as HTMLElement).hidden) return true;
    if (window.getComputedStyle(e).display === "none") return true;
  }
  return false;
}

/**
 * Every visible control (and column header) must have an accessible name: a screen
 * reader user can't tell "Approve" from "Approve", or an icon button from nothing.
 * Returns a short description of each offender.
 */
export function unnamedControls(root: ParentNode): string[] {
  const out: string[] = [];
  for (const el of root.querySelectorAll(CONTROLS)) {
    if (hidden(el)) continue;
    if (computeAccessibleName(el).trim()) continue;
    const tag = el.tagName.toLowerCase();
    const role = el.getAttribute("role");
    out.push(`<${tag}${role ? ` role="${role}"` : ""}${el.className ? ` class="${String(el.className).slice(0, 40)}"` : ""}>`);
  }
  return out;
}
