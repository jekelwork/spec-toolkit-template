#!/usr/bin/env node
// Lightweight secret gate for staged files, run by the pre-commit hook. Node builtins only, so it works
// before `npm ci` and has no build-time dependencies.
import { readFileSync, statSync } from 'node:fs'

const PATTERNS = [
  [/-----BEGIN (RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----/, 'private key'],
  [/AKIA[0-9A-Z]{16}/, 'AWS access key id'],
  [/ghp_[A-Za-z0-9]{36}/, 'GitHub personal token'],
  [/gho_[A-Za-z0-9]{36}/, 'GitHub OAuth token'],
  [/ghs_[A-Za-z0-9]{36}/, 'GitHub App installation token'],
  [/github_pat_[A-Za-z0-9_]{60,}/, 'GitHub fine-grained token'],
  [/xox[baprs]-[0-9A-Za-z-]{10,}/, 'Slack token'],
  [
    /https:\/\/hooks\.slack\.com\/(services|workflows|triggers)\/[A-Za-z0-9/_-]{20,}/,
    'Slack webhook URL',
  ],
  [/sk-ant-[A-Za-z0-9_-]{20,}/, 'Anthropic API key'],
  [/sk-(?:(?:proj|svcacct)-)?[A-Za-z0-9_-]{32,}/, 'OpenAI-style API key'],
  [/eyJ[A-Za-z0-9_-]{20,}\.eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}/, 'JWT'],
  [/(client_secret|CLIENT_SECRET)\s*[=:]\s*['"]?[A-Za-z0-9~._-]{20,}/, 'client secret assignment'],
  [/AccountKey=[A-Za-z0-9+/=]{60,}/, 'Azure storage account key'],
]

// Every size: a secret in a large file is as leaked as one in a small file. Binary files are
// skipped below, by content.
const files = process.argv.slice(2).filter((f) => {
  try {
    return statSync(f).isFile()
  } catch {
    return false
  }
})

let failed = false
for (const file of files) {
  if (/(^|\/)check-secrets\.mjs$/.test(file)) continue
  const buf = readFileSync(file)
  if (buf.includes(0)) continue // binary
  const text = buf.toString('utf8')
  for (const [re, label] of PATTERNS) {
    // Every match, not the first: a fixture marked `secret-scan:ignore` early in a file must not hide a
    // real key further down it.
    for (const m of text.matchAll(new RegExp(re.source, 'g'))) {
      const before = text.slice(0, m.index)
      const line = before.split('\n').length
      const lineText = text.slice(before.lastIndexOf('\n') + 1).split('\n')[0] ?? ''
      // Deliberate fixtures (tests, docs) opt out per line with this marker.
      if (lineText.includes('secret-scan:ignore')) continue
      console.error(`✖ ${file}:${line}: looks like a ${label}`)
      failed = true
    }
  }
}
if (failed) {
  console.error('\nRefusing to commit. Move the value to .env (git-ignored) or a Secret manifest.')
  process.exit(1)
}
