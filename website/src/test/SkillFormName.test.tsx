import { readFileSync } from 'node:fs'
import path from 'node:path'

import { render, screen, fireEvent } from '@testing-library/react'

import SkillForm, {
  sanitizeSkillName,
  skillNameProblem,
  type SkillFormData,
} from '../components/SkillForm'

const EMPTY: SkillFormData = {
  name: '',
  category: '',
  description: '',
  triggers: '',
  tags: '',
  always: false,
  body: '',
}

/** Names with no character in `[a-z0-9-/]`, one per script the issue names.
 *
 *  Written as code-point escapes because the repo forbids CJK literals in
 *  source. That is safe HERE in a way it would not be in a locale catalog: the
 *  sanitizer only cares that no character is in the allowed set, so a mistyped
 *  code point still exercises the same branch, whereas a mistyped translation
 *  ships a non-word. */
const NON_LATIN_NAMES: Record<string, string> = {
  japanese: '\u30b9\u30ad\u30eb',
  hindi: '\u0915\u094c\u0936\u0932',
  bengali: '\u09a6\u0995\u09cd\u09b7\u09a4\u09be',
  korean: '\uc2a4\ud0ac',
  chinese: '\u6280\u80fd',
}

/** Render the create-shaped form (identity fields visible) and return setters
 *  for the Name and Category fields, so each case reads as "type this, assert
 *  the hint". */
function renderForm() {
  let data = EMPTY
  const onChange = (d: SkillFormData) => {
    data = d
    rerender(<SkillForm data={d} onChange={onChange} />)
  }
  const { rerender } = render(<SkillForm data={data} onChange={onChange} />)
  return {
    typeName: (name: string) =>
      fireEvent.change(screen.getByPlaceholderText('e.g. my-tool'), { target: { value: name } }),
    typeCategory: (cat: string) =>
      fireEvent.change(screen.getByPlaceholderText('e.g. utils, code'), { target: { value: cat } }),
  }
}

describe('sanitizeSkillName', () => {
  // The server's own rule, mirrored: lowercase, then every character outside
  // [a-z0-9-/] becomes a hyphen, then edge hyphens THEN edge slashes are
  // stripped, then slash runs collapse to a single separator.
  it.each([
    ['My Skill', 'my-skill'],
    ['My Skill!', 'my-skill'],
    ['ALLCAPS', 'allcaps'],
    ['already-fine', 'already-fine'],
    ['keeps9digits', 'keeps9digits'],
    // '/' is preserved for nesting -- the load-bearing difference from prompts.
    ['utils/code', 'utils/code'],
    ['a/b/c', 'a/b/c'],
    // Slash runs collapse to one.
    ['a//b', 'a/b'],
    ['a///b', 'a/b'],
    // Leading/trailing slashes are stripped.
    ['/a/', 'a'],
    ['///a///', 'a'],
    // Leading/trailing hyphens are stripped too.
    ['---leading-and-trailing---', 'leading-and-trailing'],
  ])('reduces %j to %j', (raw, want) => {
    expect(sanitizeSkillName(raw)).toBe(want)
  })

  // The case the issue is about: a name with nothing in the allowed set leaves
  // no filename at all, which is what the server answers 400 invalid_name for.
  it.each(Object.keys(NON_LATIN_NAMES))('leaves nothing to save for a %s name', script => {
    expect(sanitizeSkillName(NON_LATIN_NAMES[script])).toBe('')
  })

  it.each([['!!!'], ['   '], ['-'], ['---'], ['/'], ['///'], ['-/'], ['']])(
    'leaves nothing to save for %j',
    raw => {
      expect(sanitizeSkillName(raw)).toBe('')
    },
  )

  // Interior non-slash punctuation becomes ONE hyphen per character, exactly as
  // the server's re.sub does for the hyphen class -- it never collapses hyphen
  // runs, only slash runs.
  it('does not collapse a run of replaced characters into one hyphen', () => {
    expect(sanitizeSkillName('a  b')).toBe('a--b')
  })

  // The `u` flag is what makes this true. Matching UTF-16 code units instead
  // would write two hyphens for one astral character and the preview would
  // disagree with the saved filename.
  it('counts an astral character as one replacement, as the server does', () => {
    const astral = String.fromCodePoint(0x1f389) // one code point, two UTF-16 units
    expect(astral).toHaveLength(2)
    expect(sanitizeSkillName(`a${astral}b`)).toBe('a-b')
  })

  /* ── Drift guard ────────────────────────────────────────────────────────
   *
   *  Every case above restates the Python rule in TypeScript, which pins the
   *  mirror to what we BELIEVE the server does. That is the wrong half to pin
   *  alone: if the handler's expression changes, all of them still pass and the
   *  preview goes on confidently showing a filename the server will not use --
   *  green and wrong, which is worse than having no preview.
   *
   *  So the source expression itself is pinned here, next to the mirror that
   *  has to follow it. A failure here does NOT mean this file is wrong: it
   *  means the server rule moved and `sanitizeSkillName` has to move with it. */
  it('still mirrors the expression the create handler actually runs', () => {
    // `__dirname`, not `import.meta.url`: under vitest the module URL is an
    // http:// one, so fileURLToPath refuses it.
    const handler = path.resolve(
      __dirname,
      '../../../src/kiro_crew/dashboard/handlers/prompts.py',
    )
    const source = readFileSync(handler, 'utf-8')
    expect(source).toContain(
      'safe_name = re.sub(r"[^a-z0-9\\-/]", "-", name.lower()).strip("-").strip("/")',
    )
    expect(source).toContain('safe_name = re.sub(r"/+", "/", safe_name)')
  })
})

describe('skillNameProblem', () => {
  it('reports nothing for an empty field, which is not yet a problem', () => {
    expect(skillNameProblem('')).toBeNull()
    expect(skillNameProblem('   ')).toBeNull()
  })

  it('reports no-stem when nothing survives sanitizing', () => {
    expect(skillNameProblem(NON_LATIN_NAMES.japanese)).toBe('no-stem')
    expect(skillNameProblem('!!!')).toBe('no-stem')
    expect(skillNameProblem('/')).toBe('no-stem')
  })

  it('reports null for a name that keeps a stem, including a nested one', () => {
    expect(skillNameProblem('My Skill!')).toBeNull()
    expect(skillNameProblem('utils/code')).toBeNull()
  })
})

describe('SkillForm name hint', () => {
  it('states the rule generically before anything is typed', () => {
    render(<SkillForm data={EMPTY} onChange={() => {}} />)
    // The literal placeholder, rendered through the SAME string the preview
    // uses -- there is no second catalog entry for the empty state.
    expect(screen.getByText(/Saved as <name>/)).toBeInTheDocument()
  })

  it('previews the sanitized filename as the user types', () => {
    const { typeName } = renderForm()
    typeName('My Skill!')
    expect(screen.getByText(/Saved as my-skill/)).toBeInTheDocument()
  })

  it('previews the COMBINED category/name, as the server sanitizes it', () => {
    const { typeName, typeCategory } = renderForm()
    typeName('My Skill')
    typeCategory('Utils Code')
    // category-then-name, both sanitized and joined by the surviving slash.
    expect(screen.getByText(/Saved as utils-code\/my-skill/)).toBeInTheDocument()
  })

  it('says why a name that sanitizes away cannot be saved', () => {
    const { typeName } = renderForm()
    typeName(NON_LATIN_NAMES.japanese)
    expect(screen.getByText(/has none of them/)).toBeInTheDocument()
    // Not the preview: there is no filename to preview.
    expect(screen.queryByText(/Saved as/)).not.toBeInTheDocument()
  })

  it('describes the Name input with the hint, so the filename is not sighted-only', () => {
    const { typeName } = renderForm()
    typeName('My Skill')
    const input = screen.getByPlaceholderText('e.g. my-tool')
    const hintId = input.getAttribute('aria-describedby')
    expect(hintId).toBeTruthy()
    expect(document.getElementById(hintId as string)).toHaveTextContent(/Saved as my-skill/)
  })

  it('hides the name field entirely when editing an existing skill', () => {
    render(<SkillForm data={{ ...EMPTY, name: 'fixed' }} onChange={() => {}} hideIdentity />)
    expect(screen.queryByPlaceholderText('e.g. my-tool')).not.toBeInTheDocument()
    expect(screen.queryByText(/Saved as/)).not.toBeInTheDocument()
  })
})
