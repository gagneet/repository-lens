# TypeScript & JavaScript: lessons learnt

**Scope.** Language traps: dates, sorting, numbers, truthiness, types that lie.

| id | severity | lesson |
|---|---|---|
| TS-001 | high | A wrong prop name in `.jsx` gives the child `undefined`, and gated UI disappears |
| TS-002 | high | `// @ts-nocheck` hid a ReferenceError |
| TS-003 | high | `any`, `as` and `JSON.parse` defeat the type system |
| TS-004 | critical | Floating-point money |
| TS-005 | high | `new Date('YYYY-MM-DD')` parses as UTC, but `'YYYY-MM-DDTHH:mm'` parses as local |
| TS-006 | medium | `Array.prototype.sort()` is lexicographic and mutates in place |
| TS-007 | high | `async` inside `forEach` is not awaited; floating promises crash Node |
| TS-008 | medium | `||` versus `??`, and `==` |
| TS-009 | low | Shallow spread copies, and a lossy `JSON` round-trip |
| TS-010 | medium | Integers above 2^53 lose precision in JSON |
| TS-011 | high | `Number.isFinite(Number(x))` accepts null, '' and [] as zero |
| TS-012 | high | A bounds check that is always true makes the rest of the condition dead |

## TS-001 — A wrong prop name in `.jsx` gives the child `undefined`, and gated UI disappears

*Severity:* **high** · *Stacks:* jsx, typescript, allowJs

**Symptom.** Every admin block rendered nothing: the caller passed `canEdit` where the prop is `canAdmin`.

**Root cause.** `.jsx` callers are not type-checked against the props a `.tsx` component declares.

**Resolution.** Fixed the prop, and converted the callers to `.tsx`.

**Prevention.** Enable `checkJs`, or convert callers of typed components to TypeScript.

**How it is checked.**

- `ast`: JSX attributes on a component with a TS prop type that are not declared props

**Evidence.** d6f2bb915 (2026-09-03)

## TS-002 — `// @ts-nocheck` hid a ReferenceError

*Severity:* **high** · *Stacks:* typescript

**Symptom.** Both export buttons threw at runtime. JSX referenced state that was never declared.

**Root cause.** The file opted out of type checking, so undeclared identifiers compiled.

**Resolution.** Removed `@ts-nocheck` and declared the state.

**Prevention.** Ratchet the count of `@ts-nocheck`/`@ts-ignore`, and run ESLint `no-undef` on JS/JSX.

**How it is checked.**

- `regex`: `@ts-nocheck|@ts-ignore`

**Evidence.** 5eb0efa57; 4170d67c7 (2026-06-09)

## TS-003 — `any`, `as` and `JSON.parse` defeat the type system

*Severity:* **high** · *Stacks:* typescript

**Symptom.** Type-correct code crashes on data of the wrong shape.

**Root cause.** `JSON.parse`, `res.json()` and axios data are `any` (or a lie via a generic), and `as` asserts without checking.

**Resolution.** Validate at the boundary (zod/valibot) and narrow from `unknown`.

**Prevention.** `strict`, `noUncheckedIndexedAccess`, typescript-eslint `no-explicit-any` and `no-unsafe-*`.

**How it is checked.**

- `regex`: `:\s*any\b|as any|as unknown as`
- `config`: tsconfig strict: true

**Sources.** <https://typescript-eslint.io/rules/no-unsafe-assignment/> <https://www.typescriptlang.org/tsconfig/#noUncheckedIndexedAccess>

## TS-004 — Floating-point money

*Severity:* **critical** · *Stacks:* javascript

**Symptom.** `0.1+0.2 !== 0.3`; `(1.005).toFixed(2) === '1.00'`; sums of dollar floats drift by cents.

**Root cause.** IEEE-754 binary floats cannot represent most decimal fractions exactly.

**Resolution.** Carry integer cents (or a decimal library) end to end, and format only at display.

**Prevention.** Name money variables `*_cents` and convert once at the boundary.

**How it is checked.**

- `regex`: `parseFloat\(.*(amount|price|total|balance|salary|cost|income)`
- `ast`: arithmetic on a money-typed binding, not a formatting call on one

**Evidence.** retirement_calculator_au (2026-09-20): every one of 20 'toFixed(2)' matches was display formatting; none was money arithmetic

**Sources.** <https://developer.mozilla.org/en-US/docs/Web/JavaScript/Reference/Global_Objects/Number/toFixed>

## TS-005 — `new Date('YYYY-MM-DD')` parses as UTC, but `'YYYY-MM-DDTHH:mm'` parses as local

*Severity:* **high** · *Stacks:* javascript, time

**Symptom.** Due dates showed the previous day for users in negative-offset time zones.

**Root cause.** Per the ECMAScript spec, a date-only ISO string is UTC midnight while a date-time string without an offset is local time.

**Resolution.** Treat date-only values as strings or construct them with `new Date(y, m-1, d)`, and always pass `timeZone` to `Intl.DateTimeFormat`.

**Prevention.** Keep business dates as plain `date` values end to end.

**How it is checked.**

- `regex`: `new Date\(['\"`]?\d{4}-\d{2}-\d{2}['\"`]?\)|new Date\(\w+\.(due_date|date)\)`

**Sources.** <https://developer.mozilla.org/en-US/docs/Web/JavaScript/Reference/Global_Objects/Date/Date#date_time_string_format>

## TS-006 — `Array.prototype.sort()` is lexicographic and mutates in place

*Severity:* **medium** · *Stacks:* javascript

**Symptom.** `[10,9,1].sort()` returns `[1,10,9]`, and `'TH10'` sorts before `'TH2'`.

**Root cause.** The default comparator compares strings.

**Resolution.** Use `(a,b)=>a-b` or `Intl.Collator(undefined,{numeric:true}).compare`, and `toSorted()` to avoid mutating the input.

**Prevention.** Mirror of the database lesson MG-003.

**How it is checked.**

- `regex`: `\.sort\(\)`

**Evidence.** retirement_calculator_au (2026-09-20): Object.keys(versions).sort().reverse() ranked '2026.1' above '2.4.0'

**Sources.** <https://developer.mozilla.org/en-US/docs/Web/JavaScript/Reference/Global_Objects/Array/sort>

## TS-007 — `async` inside `forEach` is not awaited; floating promises crash Node

*Severity:* **high** · *Stacks:* javascript, node

**Symptom.** Work ran out of order, and errors surfaced as unhandled rejections that killed the process.

**Root cause.** `forEach` ignores returned promises. Since Node 15 an unhandled rejection throws by default.

**Resolution.** Use `for…of` with `await`, or `await Promise.all(arr.map(f))` / `allSettled`.

**Prevention.** typescript-eslint `no-misused-promises` and `no-floating-promises`.

**How it is checked.**

- `regex`: `forEach\(async`
- `lint`: @typescript-eslint/no-floating-promises: error

**Sources.** <https://typescript-eslint.io/rules/no-misused-promises/> <https://nodejs.org/api/cli.html#--unhandled-rejectionsmode>

## TS-008 — `||` versus `??`, and `==`

*Severity:* **medium** · *Stacks:* javascript

**Symptom.** A genuine zero balance was replaced by the default value.

**Root cause.** `x || d` also replaces `0`, `''` and `false`. `==` coerces.

**Resolution.** Use `??` for defaults and `===` everywhere.

**Prevention.** ESLint `eqeqeq` and `prefer-nullish-coalescing`.

**How it is checked.**

- `regex`: `(amount|balance|count|total|cents)\s*\|\|`

**Sources.** <https://eslint.org/docs/latest/rules/eqeqeq>

## TS-009 — Shallow spread copies, and a lossy `JSON` round-trip

*Severity:* **low** · *Stacks:* javascript

**Symptom.** Nested state mutated through a 'copy'. Dates became strings.

**Root cause.** Spread is shallow. `JSON.parse(JSON.stringify(x))` drops `undefined`, stringifies dates, loses Map/Set and throws on BigInt.

**Resolution.** Use `structuredClone()`.

**Prevention.** Grep for the JSON round-trip idiom.

**How it is checked.**

- `regex`: `JSON\.parse\(JSON\.stringify`

**Sources.** <https://developer.mozilla.org/en-US/docs/Web/API/Window/structuredClone>

## TS-010 — Integers above 2^53 lose precision in JSON

*Severity:* **medium** · *Stacks:* javascript, api-contract

**Symptom.** A `bigint` identifier or amount from the database came back altered in the browser.

**Root cause.** JSON numbers become IEEE doubles, and `Number.MAX_SAFE_INTEGER` is 2^53-1.

**Resolution.** Serialise big integers as strings in the API.

**Prevention.** Review response models whose int fields map to BIGINT columns.

**How it is checked.**

- `review`: int fields mapped to BIGINT in API responses

**Sources.** <https://developer.mozilla.org/en-US/docs/Web/JavaScript/Reference/Global_Objects/Number/MAX_SAFE_INTEGER>

## TS-011 — `Number.isFinite(Number(x))` accepts null, '' and [] as zero

*Severity:* **high** · *Stacks:* javascript, typescript

**Symptom.** A confidence that had not been measured yet rendered as a measured `0%` — the worst possible reading of a missing value. The guard looked like a validity check.

**Root cause.** `Number(null)`, `Number('')`, `Number(' ')`, `Number([])` and `Number(false)` are all `0`, and `0` is finite. `Number.isFinite` answers "is this a finite number", never "did the caller supply one".

**Resolution.** Test the input rather than its coercion: `typeof x === 'number' && Number.isFinite(x)`, or reject nullish and empty explicitly before coercing. Render a missing value as "not assessed", not as zero.

**Prevention.** "Has a value been supplied" and "is the value in range" are two questions; a parse helper that answers only the second needs a nullish guard in front of it.

**How it is checked.**

- `regex`: `Number\.isFinite\(\s*(Number|parseFloat|parseInt)\(`
- `review`: a coerce-then-range-check helper reached by a value that may be null

**Evidence.** retirement_calculator_au (2026-09-20 audit): parseConfidence(null) returned 0, which reached the comparison table as '0%'

**Sources.** <https://developer.mozilla.org/en-US/docs/Web/JavaScript/Reference/Global_Objects/Number/isFinite>

## TS-012 — A bounds check that is always true makes the rest of the condition dead

*Severity:* **high** · *Stacks:* javascript, typescript

**Symptom.** `const passes = target <= 1 || (measured !== undefined ? measured >= target : true)`. A probability is always `<= 1`, so the right-hand side never ran and the user's confidence requirement was ignored on every code path. It had shipped, with tests — all of which asserted the `true` result.

**Root cause.** The guard was written against the wrong bound. The author meant "no target was set" (`<= 0`) and wrote "this is a probability" (`<= 1`).

**Resolution.** Correct the bound, and separate "the condition does not apply" from "the condition passed" so the caller can tell which happened.

**Prevention.** For every short-circuit guard, write the test that makes the right-hand side matter. A branch no test can make false is dead code, and a suite that only asserts the passing result cannot see it.

**How it is checked.**

- `regex`: `<=\s*1(\.0)?\s*\|\||>=\s*0(\.0)?\s*\|\|`
- `test`: per short-circuit guard, one case making the left operand false and asserting on the right

**Evidence.** retirement_calculator_au (2026-09-20 audit): evaluateEngineGoal's passesConfidence returned true for a 99% target against a measured 1%
