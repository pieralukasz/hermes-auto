// Hermes Auto: one sidebar page listing the sessions the automation created (sorter, Todoist,
// briefs, questions). Plain ESM, no build step. Sessions are found by their `☀ ` title prefix,
// which hermes-auto gives every conversation it opens; the sorter marks them hidden so they live
// here instead of the main Sessions list. Opening a row opens the normal chat.

import {
  Codicon,
  host,
  PALETTE_AREA,
  PanelEmpty,
  relativeTime,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  Tip,
  useQuery,
  queryClient
} from '@hermes/plugin-sdk'
import { useEffect, useMemo, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'hermes-auto'
const ROUTE = '/auto'
const QK = ['hermes-auto', 'sessions']
const PREFIX = '☀ '

const GROUPS = [
  { key: 'questions', label: 'Pytania', icon: 'question' },
  { key: 'mail', label: 'Poczta', icon: 'mail' },
  { key: 'todoist', label: 'Todoist', icon: 'checklist' },
  { key: 'brief', label: 'Briefy', icon: 'note' }
]

// ctx.storage is bound in register(); module scope resets on every reload.
let storage = null

function parse(session) {
  const raw = String(session.title || '')
  const body = raw.slice(PREFIX.length)
  const match = body.match(/^(.*) · ([a-z]+) · #(\d+)$/)
  const source = match ? match[2] : ''
  const group = source === 'gmail' || source === 'proton' ? 'mail' : source || 'todoist'
  return { ...session, label: match ? match[1] : body, source, group }
}

async function fetchSessions() {
  const result = await host.request('session.list', { include_hidden: true, limit: 400 })
  return (result?.sessions || []).filter(s => String(s.title || '').startsWith(PREFIX)).map(parse)
}

function useSeen() {
  const [seen, setSeen] = useState(() => storage?.get('seen') || {})
  const mark = (id, count) => {
    const next = { ...(storage?.get('seen') || {}), [id]: count }
    storage?.set('seen', next)
    setSeen(next)
  }
  return [seen, mark]
}

function Row({ session, unread, onOpen, onArchive }) {
  return jsxs('div', {
    className: 'group/row flex items-center gap-2 rounded-md px-2 py-1.5 hover:bg-(--ui-row-hover-background)',
    children: [
      jsx('span', {
        'aria-hidden': 'true',
        className: 'size-1.5 shrink-0 rounded-full',
        style: { background: unread ? 'var(--ui-accent)' : 'transparent' }
      }),
      jsxs('button', {
        type: 'button',
        className: 'flex min-w-0 flex-1 flex-col items-start text-left',
        onClick: onOpen,
        children: [
          jsx('span', {
            className: unread ? 'w-full truncate text-[0.8rem] font-medium text-foreground' : 'w-full truncate text-[0.8rem] text-(--ui-text-secondary)',
            children: session.label
          }),
          session.preview
            ? jsx('span', { className: 'w-full truncate text-[0.72rem] text-(--ui-text-tertiary)', children: session.preview })
            : null
        ]
      }),
      jsx('span', {
        className: 'shrink-0 text-[0.7rem] text-(--ui-text-quaternary)',
        children: session.started_at ? relativeTime(session.started_at * 1000) : ''
      }),
      jsx(Tip, {
        label: 'Zarchiwizuj (Todoist: zadanie zrobione)',
        children: jsx('button', {
          type: 'button',
          'aria-label': 'Zarchiwizuj',
          className: 'grid size-6 shrink-0 place-items-center rounded opacity-0 group-hover/row:opacity-100 hover:bg-(--chrome-action-hover)',
          onClick: onArchive,
          children: jsx(Codicon, { name: 'archive', size: '0.8rem' })
        })
      })
    ]
  })
}

function AutoPage() {
  const { data, isLoading, error, refetch } = useQuery({ queryKey: QK, queryFn: fetchSessions, refetchInterval: 30_000 })
  const [seen, markSeen] = useSeen()
  const sessions = data || []
  const grouped = useMemo(
    () => GROUPS.map(group => ({ ...group, rows: sessions.filter(s => s.group === group.key) })).filter(g => g.rows.length),
    [sessions]
  )

  useEffect(() => {
    // First visit: everything already there counts as read, only later activity lights up.
    if (data && storage && !storage.get('initialized')) {
      const initial = Object.fromEntries(data.map(s => [s.id, s.message_count || 0]))
      storage.set('seen', initial)
      storage.set('initialized', true)
    }
  }, [data])

  const open = session => {
    markSeen(session.id, session.message_count || 0)
    void host.openSession(session.id)
  }

  const archive = async session => {
    try {
      await host.request('session.archive', { session_id: session.id, archived: true })
      void queryClient.invalidateQueries({ queryKey: QK })
    } catch (err) {
      host.notify({ kind: 'error', message: String(err?.message || err) })
    }
  }

  let body
  if (error) {
    body = jsx(PanelEmpty, { icon: 'error', title: 'Nie udało się wczytać sesji', description: String(error?.message || error) })
  } else if (isLoading) {
    body = jsx('div', { className: 'p-4 text-sm text-(--ui-text-tertiary)', children: 'Wczytuję…' })
  } else if (!sessions.length) {
    body = jsx(PanelEmpty, {
      icon: 'inbox',
      title: 'Na razie pusto',
      description: 'Tu trafiają sesje z automatu: ważne maile, zadania z Todoista, briefy i pytania sortownika.'
    })
  } else {
    body = jsx('div', {
      className: 'flex flex-col gap-4 p-3',
      children: grouped.map(group =>
        jsxs(
          'section',
          {
            className: 'flex flex-col gap-0.5',
            children: [
              jsxs('div', {
                className: 'flex items-center gap-1.5 px-2 pb-1 text-[0.7rem] font-medium uppercase tracking-wide text-(--ui-text-tertiary)',
                children: [jsx(Codicon, { name: group.icon, size: '0.8rem' }), `${group.label} (${group.rows.length})`]
              }),
              ...group.rows.map(session =>
                jsx(
                  Row,
                  {
                    session,
                    unread: (session.message_count || 0) > (seen[session.id] ?? -1),
                    onOpen: () => open(session),
                    onArchive: () => void archive(session)
                  },
                  session.id
                )
              )
            ]
          },
          group.key
        )
      )
    })
  }

  return jsxs('div', {
    className: 'flex h-full min-h-0 flex-col',
    children: [
      jsxs('div', {
        className: 'flex items-center justify-between border-b border-(--ui-stroke-secondary) px-4 py-2.5',
        children: [
          jsx('div', { className: 'text-sm font-medium', children: 'Auto' }),
          jsx(Tip, {
            label: 'Odśwież',
            children: jsx('button', {
              type: 'button',
              'aria-label': 'Odśwież',
              className: 'grid size-6 place-items-center rounded hover:bg-(--chrome-action-hover)',
              onClick: () => void refetch(),
              children: jsx(Codicon, { name: 'refresh', size: '0.8rem' })
            })
          })
        ]
      }),
      jsx('div', { className: 'min-h-0 flex-1 overflow-y-auto', children: body })
    ]
  })
}

export default {
  id: ID,
  name: 'Hermes Auto',
  register(ctx) {
    storage = ctx.storage
    ctx.onDispose?.(() => {
      storage = null
    })
    ctx.registerMany([
      { id: 'page', area: ROUTES_AREA, data: { path: ROUTE }, render: () => jsx(AutoPage, {}) },
      { id: 'nav', area: SIDEBAR_NAV_AREA, order: 5, data: { path: ROUTE, label: 'Auto', codicon: 'sparkle' } },
      {
        id: 'open',
        area: PALETTE_AREA,
        data: {
          id: 'hermes-auto.open',
          label: 'Otwórz Auto (sesje automatu)',
          keywords: ['auto', 'hermes-auto', 'sortownik', 'brief', 'todoist', 'mail'],
          run: () => host.navigate(ROUTE)
        }
      }
    ])
  }
}
