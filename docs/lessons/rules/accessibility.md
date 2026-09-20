# Accessibility (WCAG): lessons learnt

**Scope.** ARIA roles that promise more than the markup delivers, live regions, contrast across themes, keyboard navigation.

| id | severity | lesson |
|---|---|---|
| AX-001 | high | A composite ARIA role without the children the role requires |
| AX-002 | high | A toast injected into the DOM with no live region |
| AX-003 | medium | Contrast verified in the default theme only |
| AX-004 | medium | A long form with no skip link, no landmark, and headings buried inside buttons |

## AX-001 — A composite ARIA role without the children the role requires

*Severity:* **high** · *Stacks:* accessibility, html

**Symptom.** `role="tablist"` on a div whose children were plain buttons with no `role="tab"`, `aria-selected` or `aria-controls`, and whose panels were plain divs. A screen reader announced a tab list containing no tabs and never said which view was showing. On another page the same role wrapped a single button that controlled nothing.

**Root cause.** The container role was added for the name it gives, without the owned elements it requires. `tablist`, `radiogroup`, `menu`, `listbox`, `tree` and `grid` all require specific child roles; a container without them is worse than no role, because it replaces a working generic with a broken widget.

**Resolution.** Apply the roles, ids and selected state from one function at init so the buttons, the panels and the selection cannot drift apart, and add a roving tabindex with arrow/Home/End keys. Where the thing is not really a tab list, delete the role: a set of toggles is `role="group"` with `aria-pressed`, and a single label is a heading.

**Prevention.** A container role is a promise about its children. Adding one means adding all of them.

**How it is checked.**

- `regex`: `role=[\"'](tablist|radiogroup|menu|menubar|listbox|tree|grid)[\"']`
- `review`: per container role, confirm every required child role is present and kept in sync

**Evidence.** retirement_calculator_au (2026-09-20 audit): two pages carried role=tablist with no role=tab children; WCAG 2.0 SC 4.1.2

**Sources.** <https://www.w3.org/WAI/ARIA/apg/patterns/tabs/>

## AX-002 — A toast injected into the DOM with no live region

*Severity:* **high** · *Stacks:* accessibility, javascript

**Symptom.** A shared notification helper created a styled div, appended it to `body` and removed it after five seconds. Screen-reader users got no feedback for any action on any page — including "this action could not be completed".

**Root cause.** The banner conveys its meaning through position and colour. Nothing tells assistive technology that the DOM changed, and the element is never focused, so nothing reads it.

**Resolution.** `role="alert"` with `aria-live="assertive"` for errors and warnings, `role="status"` with `aria-live="polite"` for confirmations, plus `aria-atomic="true"`. Put it in the shared helper, not at the call sites — one edit then covers every page.

**Prevention.** Anything that appears in response to an action, and does not take focus, needs a live role. An error conveyed only visually also fails SC 3.3.1.

**How it is checked.**

- `regex`: `className\s*=\s*[`'\"]?(notification|toast|snackbar|flash)`
- `review`: per element appended in response to an action, a live role or a focus move

**Evidence.** retirement_calculator_au (2026-09-20 audit): utils.js showNotification appended an unannounced div for success and error alike

**Sources.** <https://www.w3.org/WAI/WCAG21/Understanding/error-identification.html>

## AX-003 — Contrast verified in the default theme only

*Severity:* **medium** · *Stacks:* accessibility, css

**Symptom.** A text token used for small labels measured 4.12:1 in the light theme (large text only) and 2.81:1 in the dark theme — below even the 3:1 large-text floor. A third theme failed at 3.34:1. The shipped default theme passed, so nobody saw any of it.

**Root cause.** Contrast was checked once, against one background. A token redefined in each theme block needs checking against THAT theme's background, and against the card surface as well as the page background, which are rarely the same colour.

**Resolution.** Compute the ratio for every (token, theme background) pair and require 4.5:1 for normal text and 3:1 for large. Darken or lighten the token in place — one value per theme fixes every usage at once.

**Prevention.** Make the check a test beside the tokens, and fail when a new theme block introduces a token the table does not cover, so a theme cannot be added without being measured.

**How it is checked.**

- `regex`: `\[data-theme=|\[data-contrast=|prefers-color-scheme`
- `test`: relative-luminance ratio per token against each theme background and surface

**Evidence.** retirement_calculator_au (2026-09-20 audit): --ink-4 failed AA in 2 of 5 themes; WCAG 2.0 SC 1.4.3

**Sources.** <https://www.w3.org/WAI/WCAG21/Understanding/contrast-minimum.html>

## AX-004 — A long form with no skip link, no landmark, and headings buried inside buttons

*Severity:* **medium** · *Stacks:* accessibility, html

**Symptom.** A twenty-section form where every keyboard user tabbed through the whole top bar and tier selector on each load. No `<main>`, no skip link, and each accordion's `<h3>` sat INSIDE its `<button>` rather than wrapping it, so the heading list screen-reader users navigate by did not reliably offer the sections.

**Root cause.** The controls were keyboard-operable, so the page passed a shallow check. Operability is not navigability: on a page this long, the cost is reaching the part you want.

**Resolution.** A skip link as the first focusable element, one `<main>` as its target, and the APG accordion shape — heading wraps button, button carries `aria-expanded` and `aria-controls`. Assign the ids from the init function rather than by hand across twenty sections.

**Prevention.** Tab through the page before shipping. The first Tab should offer to skip to the content.

**How it is checked.**

- `regex`: `<button[^>]*>(\s*<[^/][^>]*>)*\s*<h[1-6]`
- `test`: assert a skip link precedes every other focusable element and its href resolves to a landmark

**Evidence.** retirement_calculator_au (2026-09-20 audit): retirement.html and reverse.html; WCAG 2.0 SC 2.4.1

**Sources.** <https://www.w3.org/WAI/ARIA/apg/patterns/accordion/>
