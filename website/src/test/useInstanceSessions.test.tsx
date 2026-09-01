/**
 * Test: remote-instance live sessions become mergeable rows, and the preview flag
 * gates the WIRE rather than the render.
 *
 * The load-bearing properties are about blast radius and units, not shape:
 *  - flag OFF issues NO request at all. This hook runs inside `ChatSidebar`, which
 *    every dashboard user mounts, so a version that fetched and discarded rows
 *    would put every user's instances on the wire for an opt-in preview.
 *  - a DISCONNECTED instance is never queried (the proxy would 503 it).
 *  - one unreachable instance contributes no rows and lands in `failed`, while
 *    every other instance's rows still arrive — one dead tunnel cannot empty the
 *    list, which is the objection that sank an earlier fully-merged design.
 *  - the peer's ISO ladder (`last_turn_ts` / `last_ts` / `created`) is passed
 *    through VERBATIM, because `lastActivityEpoch` short-circuits on `modified`
 *    and would skip the ladder. `last_message` is NOT a timestamp — it is an
 *    80-char message preview — so synthesizing `modified` from it put a string
 *    where a number belongs and NaN-poisoned the whole merged list's sort.
 *  - the row carries `instance_id` / `instance_name`, which is what makes the
 *    sidebar's existing badge and remote-activation path fire. #7104's own spec
 *    asserts the badge's classes, so this one asserts the data that reaches it.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

const { listInstancesMock, instanceChatSlotsMock } = vi.hoisted(() => ({
  listInstancesMock: vi.fn(),
  instanceChatSlotsMock: vi.fn(),
}))

vi.mock('../api/client', () => ({
  api: { listInstances: listInstancesMock, instanceChatSlots: instanceChatSlotsMock },
}))

import { useInstanceSessions } from '../hooks/useInstanceSessions'

const CONNECTED = { id: 'astro', name: 'astro', status: { state: 'connected' } }
const OFFLINE = { id: 'chick', name: 'chick', status: { state: 'disconnected' } }

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

beforeEach(() => vi.clearAllMocks())

describe('useInstanceSessions', () => {
  it('issues NO request while the preview flag is off', async () => {
    const { result } = renderHook(() => useInstanceSessions(false), { wrapper })
    await waitFor(() => expect(result.current.rows).toHaveLength(0))
    // The gate is the wire, not the render.
    expect(listInstancesMock).not.toHaveBeenCalled()
    expect(instanceChatSlotsMock).not.toHaveBeenCalled()
  })

  it('maps a connected instance’s slots onto rows the sessions list can render', async () => {
    listInstancesMock.mockResolvedValue({ instances: [CONNECTED] })
    instanceChatSlotsMock.mockResolvedValue([
      { key: 'chat-1', title: 'deploy checklist', agent: 'kirocrew', last_turn_ts: '2026-08-31T14:35:00Z', last_ts: '2026-08-31T14:36:00Z', created: '2026-08-20T09:00:00Z', running: true },
    ])
    const { result } = renderHook(() => useInstanceSessions(true), { wrapper })

    await waitFor(() => expect(result.current.rows).toHaveLength(1))
    const row = result.current.rows[0]
    expect(row.key).toBe('chat-1')
    expect(row.title).toBe('deploy checklist')
    expect(row.instance_id).toBe('astro')
    expect(row.instance_name).toBe('astro')
    expect(row.running).toBe(true)
    // SECONDS carried straight through — the unit the local list sorts on.
    // The ladder is passed through verbatim AND collapsed into `modified`, which
    // is what ranking, the date-segment header and the row label all read. Leaving
    // it absent made the row sort by last activity but segment by `created`, which
    // emitted a duplicate date header at every flip.
    expect(row.last_turn_ts).toBe('2026-08-31T14:35:00Z')
    expect(row.last_ts).toBe('2026-08-31T14:36:00Z')
    expect(row.created).toBe('2026-08-20T09:00:00Z')
    // last_turn_ts wins the ladder, not last_ts (which advances on tool calls).
    expect(row.modified).toBe(Date.parse('2026-08-31T14:35:00Z') / 1000)
    expect(result.current.failed).toEqual([])
  })

  it('preserves row identity across unrelated rerenders after query data settles', async () => {
    listInstancesMock.mockResolvedValue({ instances: [CONNECTED] })
    instanceChatSlotsMock.mockResolvedValue([
      { key: 'chat-1', title: 'stable row', last_turn_ts: '2026-08-31T14:00:00Z' },
    ])
    const { result, rerender } = renderHook(
      ({ tick }: { tick: number }) => {
        void tick
        return useInstanceSessions(true)
      },
      { wrapper, initialProps: { tick: 0 } },
    )

    await waitFor(() => expect(result.current.rows).toHaveLength(1))
    const firstRows = result.current.rows
    rerender({ tick: 1 })
    expect(result.current.rows).toBe(firstRows)
  })

  it('never queries a disconnected instance', async () => {
    listInstancesMock.mockResolvedValue({ instances: [OFFLINE] })
    const { result } = renderHook(() => useInstanceSessions(true), { wrapper })

    await waitFor(() => expect(listInstancesMock).toHaveBeenCalled())
    expect(instanceChatSlotsMock).not.toHaveBeenCalled()
    expect(result.current.rows).toHaveLength(0)
  })

  it('contains one unreachable instance without losing the others', async () => {
    listInstancesMock.mockResolvedValue({
      instances: [CONNECTED, { id: 'baymax', name: 'baymax', status: { state: 'connected' } }],
    })
    instanceChatSlotsMock.mockImplementation((id: string) =>
      id === 'baymax'
        ? Promise.reject(new Error('proxy_peer_not_connected'))
        : Promise.resolve([{ key: 'chat-1', title: 'still here', last_turn_ts: '2026-08-31T14:00:00Z' }]),
    )
    const { result } = renderHook(() => useInstanceSessions(true), { wrapper })

    await waitFor(() => expect(result.current.failed).toEqual(['baymax']))
    // The healthy instance's row survives the other's failure.
    expect(result.current.rows.map(r => r.title)).toEqual(['still here'])
  })

  it('drops malformed slots rather than emitting keyless rows', async () => {
    listInstancesMock.mockResolvedValue({ instances: [CONNECTED] })
    instanceChatSlotsMock.mockResolvedValue([
      { key: 'chat-1', title: 'ok' },
      { title: 'no key at all' },
      null,
    ])
    const { result } = renderHook(() => useInstanceSessions(true), { wrapper })

    await waitFor(() => expect(result.current.rows).toHaveLength(1))
    expect(result.current.rows[0].key).toBe('chat-1')
  })
})
