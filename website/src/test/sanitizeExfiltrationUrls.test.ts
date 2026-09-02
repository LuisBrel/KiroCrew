/** `sanitizeExfiltrationUrls` is the browser-side mirror of the backend's
 *  per-URL exfil classifier (`_exfil_url_warning` in security.py). It splits
 *  the checks into an UNCONDITIONAL tier (heavy percent-encoding + hard
 *  credential markers, applied to every host) and a HEURISTIC tier (base64-blob
 *  + aggregate query length) that is skipped ONLY for a code-owned exact-host
 *  allowlist — the two confirmed-benign hosts from issue #7820.
 *
 *  These tests pin the narrowing: legitimate long/query-heavy links at the
 *  exempt hosts render unchanged, arbitrary hosts still redact, and the exempt
 *  hosts still redact when the unconditional tier fires.
 */
import { describe, it, expect } from 'vitest'
import { sanitizeExfiltrationUrls } from '../utils/sanitize'

// A benign, prefilled GitHub issue link whose query is >=200 chars but carries
// no credential markers and no 20+ consecutive percent-encodings.
// No literal spaces: URL_RE's path group stops at whitespace, so a realistic
// prefilled link uses %20 for spaces. Still no 20+ CONSECUTIVE %-octets and no
// 40+ consecutive base64 run, so only the length heuristic can flag it.
const LONG_BENIGN_QUERY =
  'title=' + 'Bug%20report%20'.repeat(8) +
  '&body=' + 'Steps%20to%20reproduce%20and%20expected%20behavior%20go%20here.%20'.repeat(4) +
  '&labels=bug,triage,needs-repro'
const GITHUB_ISSUE_URL =
  `https://github.com/kirodotdev/KiroCrew/issues/new?${LONG_BENIGN_QUERY}`
const MONITORPORTAL_URL =
  `https://monitorportal.amazon.com/dashboard?${LONG_BENIGN_QUERY}`

describe('sanitizeExfiltrationUrls: exempt-host narrowing', () => {
  it('leaves a long benign github.com issue link unchanged', () => {
    expect(LONG_BENIGN_QUERY.length).toBeGreaterThanOrEqual(200)
    const text = `see ${GITHUB_ISSUE_URL} for details`
    expect(sanitizeExfiltrationUrls(text)).toBe(text)
  })

  it('leaves a long benign monitorportal.amazon.com link unchanged', () => {
    const text = `open ${MONITORPORTAL_URL} now`
    expect(sanitizeExfiltrationUrls(text)).toBe(text)
  })

  it('still redacts an arbitrary host with a >=200-char query', () => {
    const url = `https://evil.example/collect?${LONG_BENIGN_QUERY}`
    const out = sanitizeExfiltrationUrls(`leak: ${url}`)
    expect(out).not.toContain(url)
    expect(out).toContain('evil.example')
  })

  it('still redacts an arbitrary host with a 40+ base64 blob', () => {
    const url = 'https://evil.example/c?d=' + 'A'.repeat(48)
    const out = sanitizeExfiltrationUrls(`leak: ${url}`)
    expect(out).not.toContain(url)
  })

  it('does NOT treat a suffix look-alike host as exempt', () => {
    const url = `https://github.com.evil.example/x?${LONG_BENIGN_QUERY}`
    const out = sanitizeExfiltrationUrls(`leak: ${url}`)
    expect(out).not.toContain(url)
    expect(out).toContain('github.com.evil.example')
  })

  it('matches the exempt host case-insensitively', () => {
    // RFC 4343: DNS host case is not significant. A mixed-case `GitHub.com`
    // must resolve to the exempt entry so the long benign query renders
    // unchanged, not redacted.
    const url = `https://GitHub.com/kirodotdev/KiroCrew/issues/new?${LONG_BENIGN_QUERY}`
    const text = `see ${url} for details`
    expect(sanitizeExfiltrationUrls(text)).toBe(text)
  })
})

describe('sanitizeExfiltrationUrls: unconditional tier applies to exempt hosts', () => {
  it('redacts a github.com URL whose query carries an AWS key id', () => {
    const url = 'https://github.com/x/y/issues/new?body=AKIAIOSFODNN7EXAMPLE'
    const out = sanitizeExfiltrationUrls(`link ${url} end`)
    expect(out).not.toContain(url)
  })

  it('redacts a github.com URL whose query carries an ssh/PEM marker', () => {
    const url = 'https://github.com/x?body=ssh-ed25519%20AAAA'
    const out = sanitizeExfiltrationUrls(`link ${url} end`)
    expect(out).not.toContain(url)
  })

  it('redacts a github.com URL whose query carries a Slack token', () => {
    const url = 'https://github.com/x?body=xoxb-123456789012-abcdefghijkl'
    const out = sanitizeExfiltrationUrls(`link ${url} end`)
    expect(out).not.toContain(url)
  })

  it('redacts a monitorportal URL with 20+ consecutive percent-encodings', () => {
    const url = 'https://monitorportal.amazon.com/d?x=' + '%41'.repeat(21)
    const out = sanitizeExfiltrationUrls(`link ${url} end`)
    expect(out).not.toContain(url)
  })

  it('does NOT redact a github.com URL with fewer than 20 percent-encodings', () => {
    // Short percent runs are benign; the heuristic tier is skipped for github.com
    // and the unconditional percent detector needs 20+ consecutive octets.
    const url = 'https://github.com/x?q=' + '%41'.repeat(5)
    const text = `link ${url} end`
    expect(sanitizeExfiltrationUrls(text)).toBe(text)
  })

  it('does NOT redact a sub-200-char base64 blob at an exempt host (accepted residual)', () => {
    // ACCEPTED-RESIDUAL PIN. This is the deliberate tradeoff, not a bug: for the
    // exempt hosts the base64-blob and aggregate-length heuristics are skipped,
    // so a query under 200 chars carrying a 40+ char base64 blob (but no literal
    // AKIA/ssh/PEM/Slack credential marker and fewer than 20 consecutive %XX
    // octets) rides through UNCHANGED. The guard is display-only (the agent can
    // already fetch any URL via tools), so this is an accepted residual. Pinned
    // here so a future re-tightening of the exempt tier fails loudly instead of
    // silently changing this documented behavior.
    const blob = 'A'.repeat(48) // 48-char base64 run; whole query stays < 200 chars
    const url = `https://github.com/x/y/issues/new?body=${blob}`
    const query = url.slice(url.indexOf('?') + 1)
    expect(query.length).toBeLessThan(200)
    const text = `see ${url} for details`
    expect(sanitizeExfiltrationUrls(text)).toBe(text)
  })
})
