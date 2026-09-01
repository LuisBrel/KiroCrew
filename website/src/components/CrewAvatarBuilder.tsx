/**
 * Game-style ghost avatar builder — the "捏脸" tier of per-crew custom avatars.
 *
 * A nested dialog opened from the crew editor. Left column: large live
 * preview, blush/mirror switches, a randomize button. Right column: one
 * category tab per trait axis with a thumbnail grid, each thumbnail rendering
 * the CURRENT draft face with only that axis varied — so a tile shows exactly
 * what picking it does. The draft starts from the face the crew already wears
 * (the pinned traits, or the name-derived ones), never from a blank.
 *
 * Composition goes through `compose()` from the style module — the same and
 * only path the roster uses — so the preview cannot drift from the saved
 * result. The trait VOCABULARY also stays in the style module: this file only
 * enumerates `Object.keys(...)` of the exported maps, so a new hat appears
 * here without edits.
 */
import { useEffect, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Dices } from 'lucide-react'
import { Dialog, DialogBody, DialogContent, DialogFooter, DialogHeader, DialogTitle } from './ui/dialog'
import { Btn, Toggle } from './ui'
import SegmentedControl from './SegmentedControl'
import {
  ACCESSORIES,
  BROWS,
  BRAND_PURPLE,
  EYES,
  MOUTHS,
  PROPS,
  TILES,
  type KiroGhostTraits,
} from '../lib/kiroGhostAvatar'
import { ghostDataUri, seededTraits, type CrewAvatarOverride } from './CrewAvatar'

/** Trait axes shown as category tabs, in mockup order. `blush` is a two-option
 *  axis (off/on) and `tile` is the color — both special-cased below. */
type Axis = 'eyes' | 'brows' | 'mouth' | 'accessory' | 'prop' | 'blush' | 'tile'

/**
 * Options per pickable axis, read off the style module. `accessory` has no
 * 'none' key in its map (absence is a probability there, not an entry), but
 * `compose` resolves an unknown key to nothing — so a literal 'none' option
 * is both honest and renderable.
 */
const AXIS_OPTIONS: Record<Exclude<Axis, 'tile' | 'blush'>, string[]> = {
  eyes: Object.keys(EYES),
  brows: Object.keys(BROWS),
  mouth: Object.keys(MOUTHS),
  accessory: ['none', ...Object.keys(ACCESSORIES)],
  prop: Object.keys(PROPS),
}

const AXES: Axis[] = ['eyes', 'brows', 'mouth', 'accessory', 'prop', 'blush', 'tile']

/**
 * Literal catalog keys, indexed rather than assembled: a key built at runtime
 * (`t(\`…opt_${k}\`)`) is invisible to the extractor and the dead-key gate
 * (see dynamicKeys.test.ts — AboutPanel's UPDATE_ERROR_KEYS is the pattern).
 */
const AXIS_LABEL_KEYS: Record<Axis, string> = {
  eyes: 'components.avatarBuilder.axis_eyes',
  brows: 'components.avatarBuilder.axis_brows',
  mouth: 'components.avatarBuilder.axis_mouth',
  accessory: 'components.avatarBuilder.axis_accessory',
  prop: 'components.avatarBuilder.axis_prop',
  blush: 'components.avatarBuilder.axis_blush',
  tile: 'components.avatarBuilder.axis_tile',
}

const OPT_LABEL_KEYS: Record<string, string> = {
  none: 'components.avatarBuilder.opt_none',
  blush: 'components.avatarBuilder.opt_blush',
  canon: 'components.avatarBuilder.opt_canon',
  closed: 'components.avatarBuilder.opt_closed',
  sleepy: 'components.avatarBuilder.opt_sleepy',
  wink: 'components.avatarBuilder.opt_wink',
  wide: 'components.avatarBuilder.opt_wide',
  sparkle: 'components.avatarBuilder.opt_sparkle',
  visor: 'components.avatarBuilder.opt_visor',
  glasses: 'components.avatarBuilder.opt_glasses',
  cross: 'components.avatarBuilder.opt_cross',
  squint: 'components.avatarBuilder.opt_squint',
  swirl: 'components.avatarBuilder.opt_swirl',
  heart: 'components.avatarBuilder.opt_heart',
  cyclops: 'components.avatarBuilder.opt_cyclops',
  raised: 'components.avatarBuilder.opt_raised',
  angry: 'components.avatarBuilder.opt_angry',
  flat: 'components.avatarBuilder.opt_flat',
  smile: 'components.avatarBuilder.opt_smile',
  open: 'components.avatarBuilder.opt_open',
  cat: 'components.avatarBuilder.opt_cat',
  oh: 'components.avatarBuilder.opt_oh',
  grin: 'components.avatarBuilder.opt_grin',
  tongue: 'components.avatarBuilder.opt_tongue',
  wobble: 'components.avatarBuilder.opt_wobble',
  smirk: 'components.avatarBuilder.opt_smirk',
  antenna: 'components.avatarBuilder.opt_antenna',
  halo: 'components.avatarBuilder.opt_halo',
  cap: 'components.avatarBuilder.opt_cap',
  phones: 'components.avatarBuilder.opt_phones',
  bow: 'components.avatarBuilder.opt_bow',
  crown: 'components.avatarBuilder.opt_crown',
  beanie: 'components.avatarBuilder.opt_beanie',
  party: 'components.avatarBuilder.opt_party',
  flower: 'components.avatarBuilder.opt_flower',
  bandana: 'components.avatarBuilder.opt_bandana',
  hardhat: 'components.avatarBuilder.opt_hardhat',
  mug: 'components.avatarBuilder.opt_mug',
  glass: 'components.avatarBuilder.opt_glass',
  wrench: 'components.avatarBuilder.opt_wrench',
  bolt: 'components.avatarBuilder.opt_bolt',
  star: 'components.avatarBuilder.opt_star',
  term: 'components.avatarBuilder.opt_term',
}

/** Every pickable tile, brand purple first — the "no hue" spelling. */
const TILE_OPTIONS = [BRAND_PURPLE, ...TILES]

const pickRandom = <T,>(arr: T[]): T => arr[Math.floor(Math.random() * arr.length)]

export default function CrewAvatarBuilder({
  open,
  name,
  value,
  onCancel,
  onSave,
}: {
  open: boolean
  /** Crew name — the dialog subtitle and the seed of the default face. */
  name: string
  /** The pinned override currently held by the editor, or null for default. */
  value: CrewAvatarOverride | null
  onCancel: () => void
  /** null = reset to the name-derived face. */
  onSave: (next: CrewAvatarOverride | null) => void
}) {
  const { t } = useTranslation()
  /** Name-derived traits — the pre-fill when nothing is pinned, and the face
   *  the reset link previews. */
  const defaults = useMemo(() => seededTraits(name), [name])
  /** null draft = "no override": preview the default face; the first pick
   *  branches the draft off it. */
  const [draft, setDraft] = useState<KiroGhostTraits | null>(value?.traits ?? null)
  const [axis, setAxis] = useState<Axis>('eyes')
  // Re-arm when the dialog (re)opens: it stays mounted while closed (Radix
  // layer-stack requirement, see WorkspaceModal), so state must not leak from
  // the previous opening.
  useEffect(() => {
    if (open) {
      setDraft(value?.traits ?? null)
      setAxis('eyes')
    }
  }, [open, value])

  const shown = draft ?? defaults

  const setTrait = (patch: Partial<KiroGhostTraits>) => setDraft({ ...shown, ...patch })

  const randomize = () =>
    setDraft({
      eyes: pickRandom(AXIS_OPTIONS.eyes),
      brows: pickRandom(AXIS_OPTIONS.brows),
      mouth: pickRandom(AXIS_OPTIONS.mouth),
      accessory: pickRandom(AXIS_OPTIONS.accessory),
      prop: pickRandom(AXIS_OPTIONS.prop),
      blush: Math.random() < 0.5,
      flip: Math.random() < 0.5,
      tile: pickRandom(TILE_OPTIONS),
    })

  /** Thumbnails for the active axis: the draft face with one axis varied. */
  const thumbs = useMemo(() => {
    if (axis === 'tile') {
      return TILE_OPTIONS.map(tile => ({ key: tile, uri: ghostDataUri({ ...shown, tile }) }))
    }
    if (axis === 'blush') {
      // A two-option axis: off first (parallel to every other tab's "None").
      return [
        { key: 'none', uri: ghostDataUri({ ...shown, blush: false }) },
        { key: 'blush', uri: ghostDataUri({ ...shown, blush: true }) },
      ]
    }
    return AXIS_OPTIONS[axis].map(key => ({
      key,
      uri: ghostDataUri({ ...shown, [axis]: key }),
    }))
  }, [axis, shown])

  const optLabel = (key: string) => { const k = OPT_LABEL_KEYS[key]; return k ? t(k) : key }

  const segments = AXES.map(a => ({ key: a, label: t(AXIS_LABEL_KEYS[a]) }))

  return (
    <Dialog open={open} onOpenChange={next => { if (!next) onCancel() }}>
      {/* z-[110]: same stacking reason as WorkspaceModal — the editor's own
          content sits at z-[101], and an equal z-index would render this
          behind its opener. */}
      <DialogContent maxWidth={760} className="z-[110]" aria-label={t('components.avatarBuilder.title')}>
        <DialogHeader>
          <DialogTitle>{t('components.avatarBuilder.title_named', { name })}</DialogTitle>
        </DialogHeader>
        <DialogBody>
          <div className="flex flex-col gap-4 md:flex-row">
            {/* Left: live preview + the whole-face view switch. Flip stays a
                toggle rather than a tab: it is a view transform of the same
                face, not a trait with its own vocabulary. Narrow-first: the
                columns stack below md so a phone gets the full width for the
                tab strip and the thumbnail grid. */}
            <div className="flex w-full flex-col items-center gap-3 md:w-[200px] md:flex-none">
              <img
                src={ghostDataUri(shown)}
                alt=""
                aria-hidden="true"
                width={176}
                height={176}
                className="rounded-xl border border-border"
                data-testid="avatar-builder-preview"
              />
              <div className="flex w-full flex-col gap-2 text-[12px]">
                <div className="flex items-center justify-between">
                  <span>{t('components.avatarBuilder.mirror')}</span>
                  <Toggle
                    checked={shown.flip}
                    onChange={v => setTrait({ flip: v })}
                    label={t('components.avatarBuilder.mirror')}
                  />
                </div>
              </div>
              <Btn onClick={randomize} className="w-full" data-testid="avatar-builder-randomize">
                <Dices className="lucide-inline" aria-hidden="true" />
                {t('components.avatarBuilder.randomize')}
              </Btn>
            </div>
            {/* Right: category tabs + the thumbnail grid. */}
            <div className="flex min-w-0 flex-1 flex-col gap-3">
              <SegmentedControl segments={segments} value={axis} onChange={setAxis} />
              <div className="grid max-h-[380px] grid-cols-[repeat(auto-fill,minmax(84px,1fr))] gap-2 overflow-y-auto pr-1" role="listbox" aria-label={t(AXIS_LABEL_KEYS[axis])}>
                {thumbs.map(({ key, uri }) => {
                  const selected =
                    axis === 'tile'
                      ? shown.tile === key
                      : axis === 'blush'
                        ? shown.blush === (key === 'blush')
                        : shown[axis] === key
                  return (
                    <button
                      key={key}
                      type="button"
                      role="option"
                      aria-selected={selected}
                      aria-label={axis === 'tile' ? key : optLabel(key)}
                      onClick={() =>
                        setTrait(
                          axis === 'tile'
                            ? { tile: key }
                            : axis === 'blush'
                              ? { blush: key === 'blush' }
                              : { [axis]: key },
                        )
                      }
                      className={`flex flex-col items-center gap-1 rounded-lg border-2 p-1.5 pb-1 transition-colors ${
                        selected ? 'border-ring bg-accent-subtle' : 'border-transparent hover:bg-bg-hover'
                      }`}
                      data-testid={`avatar-opt-${key.replace('#', '')}`}
                    >
                      <img src={uri} alt="" aria-hidden="true" width={72} height={72} className="rounded-lg" />
                      <span className={`text-[10.5px] leading-tight ${selected ? 'text-text-strong' : 'text-muted'}`}>
                        {/* Tile options carry no text label: the swatch IS the
                            meaning, and a raw hex code is noise to a person
                            (first-run review, Medium #7). The hex still reaches
                            AT users via the aria-label. */}
                        {axis === 'tile' ? '\u00a0' : optLabel(key)}
                      </span>
                    </button>
                  )
                })}
              </div>
            </div>
          </div>
        </DialogBody>
        {/* flex-wrap: at phone width the reset link + hint block and the
            action buttons cannot share one row; wrapping keeps Apply on
            screen instead of pushing it past the dialog's overflow-hidden
            edge. */}
        <DialogFooter className="flex-wrap">
          {/* Reset previews immediately (draft -> null shows the name-derived
              face); Save is what commits either outcome to the editor. The
              hint under the link is High finding #2 from the first-run review:
              without it, Apply reads as the final save and the editor's own
              Save changes step is a silent second commit the user can miss. */}
          <div className="mr-auto flex min-w-0 flex-col gap-0.5">
            <button
              type="button"
              onClick={() => setDraft(null)}
              className="self-start text-[12px] text-muted underline underline-offset-2 hover:text-text"
              data-testid="avatar-builder-reset"
            >
              {t('components.avatarBuilder.reset_default')}
            </button>
            <span className="text-[11px] text-muted">{t('components.avatarBuilder.apply_hint')}</span>
          </div>
          <Btn onClick={onCancel}>{t('components.avatarBuilder.cancel')}</Btn>
          <Btn primary onClick={() => onSave(draft ? { kind: 'ghost', traits: draft } : null)} data-testid="avatar-builder-save">
            {t('components.avatarBuilder.apply')}
          </Btn>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
