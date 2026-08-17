import assert from 'node:assert/strict'
import test from 'node:test'

import {
  parseBlueprintMarkdown,
  renderBlueprintMarkdown,
  renderBlueprintPlanBasis,
  isSafeMermaidSvg,
} from './blueprintRenderer.js'

test('parses headings, bullets, and diagram blocks', () => {
  const markdown = `# Project blueprint

Intro paragraph.

- First milestone
- Second milestone

\`\`\`mermaid
flowchart TD
A --> B
\`\`\`

\`\`\`sql
SELECT 1;
\`\`\`
`
  const blocks = parseBlueprintMarkdown(markdown)
  assert.equal(blocks[0].type, 'heading')
  assert.equal(blocks[0].level, 1)
  assert.equal(blocks[1].type, 'paragraph')
  assert.equal(blocks[2].type, 'list')
  assert.equal(blocks[2].ordered, false)
  assert.equal(blocks[2].items.length, 2)
  assert.equal(blocks[3].type, 'mermaid')
  assert.equal(blocks[3].text.includes('flowchart TD'), true)
  assert.equal(blocks[4].type, 'code')
  assert.equal(blocks[4].language, 'sql')
})

test('renders markdown blocks as React elements', () => {
  const markdown = `## Scope

\`exact preview\` and **safe rendering** are required.
`
  const rendered = renderBlueprintMarkdown(markdown)
  assert.equal(Array.isArray(rendered), true)
  assert.equal(rendered.length, 2)
  assert.equal(rendered[0].type, 'h2')
  assert.equal(rendered[0].props.className.includes('blueprint-markdown-heading'), true)
  assert.equal(rendered[1].type, 'p')
  const children = rendered[1].props.children
  assert.equal(Array.isArray(children), true)
  assert.ok(children.some(node => node.type === 'code'))
  assert.ok(children.some(node => node.type === 'strong'))
})

test('renders plan-basis provenance trees without throwing', () => {
  const planBasis = {
    source: { name: 'readme.md', contacted: true },
    accepted: true,
    findings: ['owner approved', 'provider denied'],
  }
  const rendered = renderBlueprintPlanBasis(planBasis)
  assert.equal(rendered.type, 'label')
  assert.equal(rendered.props.children[0].type, 'small')
  assert.ok(Array.isArray(rendered.props.children[1].props.children))
})

test('rejects executable or remote-loading Mermaid SVG output', () => {
  assert.equal(isSafeMermaidSvg('<svg><path d="M0 0" /></svg>'), true)
  assert.equal(isSafeMermaidSvg('<svg><script>alert(1)</script></svg>'), false)
  assert.equal(isSafeMermaidSvg('<svg><foreignObject>unsafe</foreignObject></svg>'), false)
  assert.equal(isSafeMermaidSvg('<svg><image href="https://example.com/a.png" /></svg>'), false)
  assert.equal(isSafeMermaidSvg('<svg><path onclick="alert(1)" /></svg>'), false)
})
