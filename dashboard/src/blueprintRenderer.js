import { createElement, useEffect, useId, useState } from 'react'

let mermaidConfigured = false
let mermaidPromise = null

async function loadMermaid() {
  if (!mermaidPromise) {
    mermaidPromise = import('mermaid').then(module => module.default || module)
  }
  const mermaid = await mermaidPromise
  if (!mermaidConfigured) {
    mermaid.initialize({
      startOnLoad: false,
      securityLevel: 'strict',
      suppressErrorRendering: true,
      maxTextSize: 50_000,
      maxEdges: 500,
      theme: 'neutral',
      htmlLabels: false,
      flowchart: { htmlLabels: false, useMaxWidth: true },
    })
    mermaidConfigured = true
  }
  return mermaid
}

export function isSafeMermaidSvg(svg) {
  const source = String(svg || '')
  if (!source.startsWith('<svg')) return false
  return !/(?:<script\b|<foreignObject\b|\bon\w+\s*=|\b(?:href|xlink:href)\s*=\s*["']\s*(?:https?:|data:|javascript:)|url\(\s*["']?\s*(?:https?:|data:|javascript:))/i.test(source)
}

function MermaidDiagram({ source }) {
  const reactId = useId().replaceAll(':', '')
  const [state, setState] = useState({ status: 'loading', image: '', error: '' })

  useEffect(() => {
    let cancelled = false
    async function render() {
      try {
        const mermaid = await loadMermaid()
        const result = await mermaid.render(`cortex-blueprint-${reactId}`, source)
        if (!isSafeMermaidSvg(result.svg)) {
          throw new Error('Rendered diagram did not pass the local SVG safety check.')
        }
        if (!cancelled) {
          setState({
            status: 'ready',
            image: `data:image/svg+xml;charset=utf-8,${encodeURIComponent(result.svg)}`,
            error: '',
          })
        }
      } catch (error) {
        if (!cancelled) {
          setState({
            status: 'error',
            image: '',
            error: error instanceof Error ? error.message : 'Diagram rendering failed.',
          })
        }
      }
    }
    render()
    return () => { cancelled = true }
  }, [reactId, source])

  return createElement(
    'section',
    { className: 'blueprint-mermaid-block' },
    createElement('small', { className: 'blueprint-mermaid-label' }, 'Mermaid workflow'),
    state.status === 'ready'
      ? createElement('img', {
          className: 'blueprint-mermaid-image',
          src: state.image,
          alt: 'Blueprint Mermaid workflow diagram',
        })
      : state.status === 'loading'
        ? createElement('p', { className: 'blueprint-mermaid-loading' }, 'Rendering local diagram…')
        : createElement('p', { className: 'blueprint-mermaid-error' }, state.error),
    createElement(
      'details',
      { className: 'blueprint-mermaid-source' },
      createElement('summary', null, 'View Mermaid source'),
      createElement('pre', { className: 'blueprint-mermaid-diagram' }, createElement('code', null, source)),
    ),
  )
}

export function parseBlueprintMarkdown(markdown) {
  const source = String(markdown || '')
  const lines = source.replace(/\r\n/g, '\n').split('\n')
  const blocks = []
  let codeLanguage = null
  const codeLines = []
  let paragraph = []
  let list = null

  function flushParagraph() {
    if (paragraph.length === 0) return
    blocks.push({ type: 'paragraph', text: paragraph.join('\n') })
    paragraph = []
  }

  function flushList() {
    if (!list) return
    blocks.push({
      type: 'list',
      ordered: list.ordered,
      items: list.items,
    })
    list = null
  }

  function flushCode(language) {
    const text = codeLines.join('\n').trim()
    if (text) {
      if ((language || '').toLowerCase() === 'mermaid') {
        blocks.push({ type: 'mermaid', text })
      } else {
        blocks.push({ type: 'code', language: language || 'text', text })
      }
    }
    codeLines.length = 0
  }

  for (const rawLine of lines) {
    const line = rawLine.replace(/\u0000/g, '')
    const fenceMatch = line.match(/^```(\S*)\s*$/)
    if (fenceMatch) {
      if (codeLanguage === null) {
        flushParagraph()
        flushList()
        codeLanguage = fenceMatch[1] || 'text'
        continue
      }
      flushCode(codeLanguage)
      codeLanguage = null
      continue
    }

    if (codeLanguage !== null) {
      codeLines.push(line)
      continue
    }

    const headingMatch = line.match(/^(#{1,6})\s+(.*)$/)
    if (headingMatch) {
      flushParagraph()
      flushList()
      blocks.push({
        type: 'heading',
        level: headingMatch[1].length,
        text: headingMatch[2].trim(),
      })
      continue
    }

    if (!line.trim()) {
      flushParagraph()
      flushList()
      continue
    }

    const unorderedMatch = line.match(/^[\-\+*]\s+(.*)$/)
    if (unorderedMatch) {
      if (!list || list.ordered) {
        flushParagraph()
        flushList()
        list = { ordered: false, items: [] }
      }
      list.items.push(unorderedMatch[1].trim())
      continue
    }

    const orderedMatch = line.match(/^\d+\.\s+(.*)$/)
    if (orderedMatch) {
      if (!list || !list.ordered) {
        flushParagraph()
        flushList()
        list = { ordered: true, items: [] }
      }
      list.items.push(orderedMatch[1].trim())
      continue
    }

    if (list) flushList()
    paragraph.push(line.trim())
  }

  flushParagraph()
  flushList()
  if (codeLanguage !== null) flushCode(codeLanguage)

  return blocks
}

function parseInlineMarkup(text) {
  const nodes = []
  const re = /\*\*([^*\n]+)\*\*|\*([^*\n]+)\*|`([^`\n]+)`|\[([^\]]+)\]\(([^)\s]+)(?:\s+"[^"]+")?\)/g
  let lastIndex = 0
  let match
  while ((match = re.exec(text)) !== null) {
    if (match.index > lastIndex) {
      nodes.push({ kind: 'text', value: text.slice(lastIndex, match.index) })
    }
    if (match[1] !== undefined) nodes.push({ kind: 'strong', value: match[1] })
    else if (match[2] !== undefined) nodes.push({ kind: 'em', value: match[2] })
    else if (match[3] !== undefined) nodes.push({ kind: 'code', value: match[3] })
    else {
      const href = match[4] || ''
      const lowerHref = String(href).trim().toLowerCase()
      if (lowerHref.startsWith('javascript:') || lowerHref.startsWith('data:')) {
        nodes.push({ kind: 'text', value: match[0] })
      } else {
        nodes.push({ kind: 'link', label: match[3] || '', href })
      }
    }
    lastIndex = match.index + match[0].length
  }
  if (lastIndex < text.length) nodes.push({ kind: 'text', value: text.slice(lastIndex) })
  return nodes
}

function inlineToElements(text, keyPrefix) {
  const parts = parseInlineMarkup(String(text || ''))
  return parts.map((part, index) => {
    const key = `${keyPrefix}-${index}`
    switch (part.kind) {
      case 'strong':
        return createElement('strong', { key }, part.value)
      case 'em':
        return createElement('em', { key }, part.value)
      case 'code':
        return createElement('code', { key }, part.value)
      case 'link':
        return createElement('a', { href: part.href, key, target: '_blank', rel: 'noreferrer noopener' }, part.label)
      default:
        return createElement('span', { key }, part.value)
    }
  })
}

function renderParagraph(text, key) {
  const lines = String(text || '').split('\n')
  const children = []
  lines.forEach((line, index) => {
    if (index > 0) children.push(createElement('br', { key: `br-${index}` }))
    children.push(...inlineToElements(line, `p-${key}-${index}`))
  })
  return createElement('p', { key, className: 'blueprint-markdown-paragraph' }, ...children)
}

export function renderBlueprintMarkdown(markdown) {
  return parseBlueprintMarkdown(markdown).map((block, index) => {
    const key = `blueprint-block-${index}`
    if (block.type === 'heading') {
      const tag = `h${Math.min(6, Math.max(1, block.level || 1))}`
      return createElement(
        tag,
        { key, className: `blueprint-markdown-heading level-${block.level}` },
        ...inlineToElements(block.text, key),
      )
    }
    if (block.type === 'list') {
      const ListTag = block.ordered ? 'ol' : 'ul'
      return createElement(
        ListTag,
        { key, className: block.ordered ? 'blueprint-markdown-list ordered' : 'blueprint-markdown-list' },
        block.items.map((item, itemIndex) =>
          createElement('li', { key: `item-${itemIndex}` }, ...inlineToElements(item, `${key}-${itemIndex}`)),
        ),
      )
    }
    if (block.type === 'code') {
      return createElement(
        'pre',
        { key, className: 'blueprint-code-block', 'data-language': block.language || 'text' },
        createElement('code', { className: 'blueprint-code-content' }, block.text),
      )
    }
    if (block.type === 'mermaid') {
      return createElement(MermaidDiagram, { key, source: block.text })
    }
    return renderParagraph(block.text, key)
  })
}

function planBasisEntryNodes(value, prefix = 'basis', level = 0, label = null) {
  const nodeLabel = label === null ? prefix : `${prefix}.${label}`
  if (Array.isArray(value)) {
    if (!value.length) {
      return createElement('li', { key: nodeLabel }, `${nodeLabel}: `, createElement('em', { className: 'blueprint-plan-empty' }, 'empty list'))
    }
    return createElement(
      'li',
      { key: nodeLabel },
      createElement('span', { className: 'blueprint-plan-key' }, `${label || nodeLabel}:`),
      createElement(
        'ul',
        { className: 'blueprint-plan-list' },
        value.map((entry, index) => planBasisEntryNodes(entry, `${nodeLabel}[${index}]`, index + 1, `item ${index + 1}`)),
      ),
    )
  }
  if (value && typeof value === 'object') {
    const entries = Object.entries(value)
    if (!entries.length) {
      return createElement('li', { key: nodeLabel }, createElement('span', { className: 'blueprint-plan-key' }, `${label || nodeLabel}:`), createElement('span', { className: 'blueprint-plan-empty' }, 'empty'))
    }
    return createElement(
      'li',
      { key: nodeLabel },
      createElement('span', { className: 'blueprint-plan-key' }, `${label || nodeLabel}:`),
      createElement(
        'ul',
        { className: 'blueprint-plan-list' },
        entries.map(([entryKey, entryValue]) => planBasisEntryNodes(entryValue, `${nodeLabel}-${entryKey}`, level + 1, entryKey)),
      ),
    )
  }
  return createElement('li', { key: nodeLabel },
    createElement('span', { className: 'blueprint-plan-key' }, `${label || nodeLabel}:`),
    String(value)
  )
}

export function renderBlueprintPlanBasis(planBasis) {
  const normalized = planBasis ?? {}
  const entries = Object.entries(normalized)
  if (!entries.length) {
    return createElement('div', { className: 'blueprint-design-plan-basis' }, createElement('small', null, 'Plan basis provenance'), createElement('p', { className: 'blueprint-plan-empty' }, 'No provenance details recorded.'))
  }
  return createElement(
    'label',
    { className: 'blueprint-design-plan-basis' },
    createElement('small', null, 'Plan basis provenance'),
    createElement(
      'ul',
      { className: 'blueprint-plan-basis-list' },
      entries.map(([key, value]) => planBasisEntryNodes(value, `basis-${key}`, 1, key)),
    ),
  )
}
