import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterEach, beforeEach, test } from 'node:test'

const scanner = fileURLToPath(new URL('./check-secrets.mjs', import.meta.url))
const githubToken = `ghp_${'a'.repeat(36)}`
const openaiKeys = [
  ['legacy', `sk-${'a'.repeat(40)}`],
  ['project', `sk-proj-${'aB1_-'.repeat(10)}`],
  ['service account', `sk-svcacct-${'aB1_-'.repeat(10)}`],
]
let directory

beforeEach(() => {
  directory = mkdtempSync(join(tmpdir(), 'check-secrets-test-'))
})

afterEach(() => {
  rmSync(directory, { recursive: true, force: true })
})

function scan(path, content) {
  const file = join(directory, path)
  mkdirSync(dirname(file), { recursive: true })
  writeFileSync(file, content)
  const result = spawnSync(process.execPath, [scanner, file], { encoding: 'utf8' })
  assert.ifError(result.error)
  assert.equal(result.signal, null)
  return result
}

for (const path of ['.env.example', 'app/.env.example']) {
  test(`rejects credentials in ${path}`, () => {
    const result = scan(path, `TOKEN=${githubToken}\n`)
    assert.equal(result.status, 1)
    assert.ok(result.stderr.includes(`${path}:1: looks like a GitHub personal token`))
  })
}

test('accepts example environment files with placeholders', () => {
  const result = scan('app/.env.example', 'OPENAI_API_KEY=your-api-key\nTOKEN=\n')
  assert.equal(result.status, 0)
  assert.equal(result.stderr, '')
})

test('allows an explicitly ignored fixture in an example environment file', () => {
  const result = scan('app/.env.example', `TOKEN=${githubToken} # secret-scan:ignore\n`)
  assert.equal(result.status, 0)
  assert.equal(result.stderr, '')
})

for (const [format, key] of openaiKeys) {
  test(`rejects an OpenAI ${format} key`, () => {
    const result = scan('credentials.txt', `OPENAI_API_KEY=${key}\n`)
    assert.equal(result.status, 1)
    assert.match(result.stderr, /:1: looks like a OpenAI-style API key/)
  })

  test(`allows an explicitly ignored OpenAI ${format} fixture`, () => {
    const result = scan('credentials.txt', `OPENAI_API_KEY=${key} # secret-scan:ignore\n`)
    assert.equal(result.status, 0)
    assert.equal(result.stderr, '')
  })
}

test('an ignored fixture does not hide a later credential', () => {
  const result = scan(
    'app/.env.example',
    `TOKEN=${githubToken} # secret-scan:ignore\nTOKEN=${githubToken}\n`,
  )
  assert.equal(result.status, 1)
  assert.ok(result.stderr.includes('.env.example:2: looks like a GitHub personal token'))
  assert.ok(!result.stderr.includes('.env.example:1:'))
})
