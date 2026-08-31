// PublishHub -- the publish NOTICE discriminator, and the consent copy's promise.
//
// Both defects pinned here shipped as strings that asserted something the code
// knew was false:
//
//  1. The backend emits more than one kind of notice, but both render sites
//     printed one fixed line -- "Still rolling out - reachable in a few minutes."
//     For a DISABLED distribution the link never resolves until a human
//     re-enables it, so that line sent the user off to wait for something that
//     would not happen, and the one action that fixes it was never shown.
//  2. The consent copy promised the user could "make it private or unpublish
//     it". Neither control exists anywhere in the dashboard -- the API functions
//     are defined and called from zero components -- so the copy named a way out
//     the product does not have.
import { describe, it, expect } from 'vitest'
import { publishNoticeKey } from '../components/PublishHub'
import en from '../i18n/locales/en.manual.json'

const KEYS = {
  rolling_out: 'rolling_out.key',
  distribution_disabled: 'distribution_disabled.key',
  notice_generic: 'notice_generic.key',
}

describe('publishNoticeKey', () => {
  it('gives a disabled distribution its own copy, not the rolling-out line', () => {
    expect(publishNoticeKey(KEYS, 'distribution_disabled')).toBe(KEYS.distribution_disabled)
    // The regression: this used to resolve to the rolling-out line for every notice.
    expect(publishNoticeKey(KEYS, 'distribution_disabled')).not.toBe(KEYS.rolling_out)
  })

  it('keeps the rolling-out line for the case that is genuinely rolling out', () => {
    expect(publishNoticeKey(KEYS, 'rolling_out')).toBe(KEYS.rolling_out)
  })

  it('never promises a time for a code it does not recognise', () => {
    // 'unknown' is emitted when the rollout state could not be read at all, and a
    // newer backend may introduce a code this build has never heard of. Neither may
    // inherit the time promise.
    for (const code of ['unknown', '', undefined, 'some_future_code']) {
      expect(publishNoticeKey(KEYS, code)).toBe(KEYS.notice_generic)
      expect(publishNoticeKey(KEYS, code)).not.toBe(KEYS.rolling_out)
    }
  })
})

describe('publish notice copy', () => {
  const hub = en.components.publishHub
  const detail = en.pages.artifactDetailPage
  // The consent copy belongs to the acknowledgment modal, not the panel.
  const ack = en.components.publicPublishAckModal

  it('states the console remedy for a disabled distribution and promises no time', () => {
    for (const s of [hub.published_distribution_disabled, detail.publication_distribution_disabled]) {
      expect(s).toMatch(/AWS console/i)
      // The whole point of the discriminator: no "few minutes" anywhere in this case.
      expect(s).not.toMatch(/few minutes/i)
    }
  })

  it('the generic notice promises no time either', () => {
    for (const s of [hub.published_notice_generic, detail.publication_notice_generic]) {
      expect(s).not.toMatch(/few minutes/i)
    }
  })

  it('the consent copy names an exit that exists in the dashboard', () => {
    const s = ack.exposure_window_persistent_withdrawable
    // Deleting the artifact is reachable from the library and the detail page.
    expect(s).toMatch(/delete/i)
    // "make it private" and "unpublish" are NOT: their API calls have no callers.
    expect(s).not.toMatch(/unpublish/i)
    expect(s).not.toMatch(/make it private/i)
  })
})
