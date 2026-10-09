// Bidi override/isolate controls (U+202A–U+202E, U+2066–U+2069) let a feedback
// note visually reorder surrounding text ("Trojan Source"). Strip them from any
// untrusted note before it is rendered.
const BIDI_CONTROLS = /[‪-‮⁦-⁩]/g;

export function stripBidi(text: string): string {
  return text.replace(BIDI_CONTROLS, "");
}
