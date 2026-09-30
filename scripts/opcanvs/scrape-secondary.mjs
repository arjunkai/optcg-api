// OPCanvs Secondary Cards — vision scrape.
// For every non-Don card, ask Claude vision which roster characters are DEPICTED
// IN THE ART other than the card's own title character(s), and emit idempotent
// INSERT OR IGNORE SQL for card_characters role='secondary' rows.
//
// Accuracy guardrails (owner rule: never mis-tag; art depiction only):
//   - the model must name a character with HIGH CONFIDENCE; unsure => omit
//   - detected names are resolved to the roster (normalize + alias); unmapped dropped
//   - the card's OWN primary character(s) are always excluded
//   - a confidence floor (default 0.85) drops weak hits
//   - writes a review JSON; SPOT-CHECK a sample before applying the SQL
//   - resumable via checkpoint; INSERT OR IGNORE keeps re-runs safe
//
// Inputs (regenerate with wrangler d1 execute --json, see APPLY notes below):
//   scrape_input/characters.json   [{id,name,name_normalized,fandom_title}]
//   scrape_input/cards.json        [{id,category}]  (non-Don)
//   scrape_input/card_primary.json [{card_id,character_id}]  (role != secondary)
//
// Usage:
//   npm i @anthropic-ai/sdk
//   ANTHROPIC_API_KEY=sk-... node scripts/opcanvs/scrape-secondary.mjs \
//     [--limit N] [--concurrency 6] [--model claude-sonnet-5] \
//     [--min-confidence 0.85] [--dry-run] [--reset]
//   # spot-check scrape_input/secondary_candidates.json, then:
//   npx wrangler d1 execute optcg-cards --remote \
//     --file=scripts/opcanvs/opcanvs_batches/secondary_scrape.sql
//
import { readFile, writeFile, mkdir } from 'node:fs/promises'
import { existsSync } from 'node:fs'

const HERE = new URL('./', import.meta.url)
const IN = new URL('./scrape_input/', HERE)
const OUT = new URL('./opcanvs_batches/', HERE)
const CANDIDATES = new URL('./scrape_input/secondary_candidates.json', HERE)
const CHECKPOINT = new URL('./scrape_input/secondary_checkpoint.json', HERE)

// ---- args ----
const argv = process.argv.slice(2)
const flag = (name, def) => {
  const i = argv.indexOf(`--${name}`)
  if (i === -1) return def
  const v = argv[i + 1]
  return v && !v.startsWith('--') ? v : true
}
const LIMIT = Number(flag('limit', 0)) || 0
const CONCURRENCY = Number(flag('concurrency', 6)) || 6
const MODEL = String(flag('model', 'claude-sonnet-5'))
const MIN_CONF = Number(flag('min-confidence', 0.85))
const DRY_RUN = !!flag('dry-run', false)
const RESET = !!flag('reset', false)

// ---- name normalization + alias (kept in sync with resolve-characters.mjs) ----
const norm = (s) => !s ? '' : s.normalize('NFKD').replace(/[̀-ͯ]/g, '')
  .replace(/[.・]/g, ' ').replace(/[^\p{L}\p{N}\s]/gu, ' ').replace(/\s+/g, ' ').trim().toLowerCase()
const ALIAS = {
  akainu: 'Sakazuki', kizaru: 'Borsalino', aokiji: 'Kuzan', fujitora: 'Issho', ryokugyu: 'Aramaki',
  sogeking: 'Usopp', 'god usopp': 'Usopp', nika: 'Monkey D. Luffy', 'sun god nika': 'Monkey D. Luffy',
  joker: 'Donquixote Doflamingo', corazon: 'Donquixote Rosinante', whitebeard: 'Edward Newgate',
  blackbeard: 'Marshall D. Teach', 'big mom': 'Charlotte Linlin', hakuba: 'Cavendish',
  'dark king': 'Silvers Rayleigh', kyoshiro: 'Denjiro', komurasaki: 'Kozuki Hiyori',
}

// ---- load inputs ----
const load = async (name) => JSON.parse(await readFile(new URL(name, IN), 'utf8'))
const characters = await load('characters.json')
const cards = await load('cards.json')
const cardPrimary = await load('card_primary.json')

// name -> id resolver (canonical name, precomputed name_normalized, fandom title, alias)
const nameToId = new Map()
const idToName = new Map()
for (const ch of characters) {
  idToName.set(ch.id, ch.name)
  for (const key of [norm(ch.name), ch.name_normalized, norm(ch.fandom_title)]) {
    if (key && !nameToId.has(key)) nameToId.set(key, ch.id)
  }
}
// Token sets per roster name, sorted MOST-specific first, so a model variant like
// "Trafalgar D. Water Law" still resolves to canonical "Trafalgar Law" (its tokens
// {trafalgar,law} are a subset), and "Portgas D. Ace" wins over bare "Ace".
const rosterTokens = characters
  .map((ch) => ({ id: ch.id, toks: new Set(norm(ch.name).split(' ').filter(Boolean)) }))
  .filter((e) => e.toks.size > 0)
  .sort((a, b) => b.toks.size - a.toks.size)
const resolve = (raw) => {
  const n = norm(raw)
  if (nameToId.has(n)) return nameToId.get(n)
  const aliased = ALIAS[n]
  if (aliased && nameToId.has(norm(aliased))) return nameToId.get(norm(aliased))
  // token-subset: every token of a roster name present in the detected name.
  const dt = new Set(n.split(' ').filter(Boolean))
  for (const e of rosterTokens) {
    let all = true
    for (const t of e.toks) if (!dt.has(t)) { all = false; break }
    if (all) return e.id
  }
  return null
}

const primaryByCard = new Map()
for (const { card_id, character_id } of cardPrimary) {
  if (!primaryByCard.has(card_id)) primaryByCard.set(card_id, new Set())
  primaryByCard.get(card_id).add(character_id)
}

const imageUrl = (id) =>
  `https://wsrv.nl/?url=optcg-api.arjunbansal-ai.workers.dev/images/${encodeURIComponent(id)}&output=jpg&w=620&q=90`

// ---- checkpoint (resume) ----
let done = new Set()
let candidates = []
if (!RESET && existsSync(CHECKPOINT)) {
  const cp = JSON.parse(await readFile(CHECKPOINT, 'utf8'))
  done = new Set(cp.done || [])
  candidates = cp.candidates || []
  console.log(`resuming: ${done.size} cards already scanned, ${candidates.length} candidates so far`)
}

const queue = cards.filter((c) => !done.has(c.id))
const work = LIMIT ? queue.slice(0, LIMIT) : queue
console.log(`scanning ${work.length} cards (model=${MODEL}, conc=${CONCURRENCY}, minConf=${MIN_CONF}${DRY_RUN ? ', DRY-RUN' : ''})`)

if (DRY_RUN) {
  console.log('dry-run: resolver + input sanity only, no API calls.')
  const sample = work.slice(0, 5).map((c) => ({ id: c.id, category: c.category, primary: [...(primaryByCard.get(c.id) || [])].map((i) => idToName.get(i)), img: imageUrl(c.id) }))
  console.log(JSON.stringify(sample, null, 2))
  process.exit(0)
}

// ---- Anthropic vision ----
const { default: Anthropic } = await import('@anthropic-ai/sdk')
const client = new Anthropic()   // reads ANTHROPIC_API_KEY

const SYSTEM = 'You identify One Piece characters depicted in official One Piece Card Game artwork. You know the One Piece cast thoroughly. Be strict: only name a character you can identify with high confidence from what is actually drawn.'
const promptFor = (primaryNames) =>
  `This card's title character is: ${primaryNames.length ? primaryNames.join(', ') : '(unknown)'}. Do NOT list the title character.\n` +
  `List every OTHER named One Piece character CLEARLY DEPICTED in this card's artwork (foreground or background). ` +
  `Do not list characters who are merely referenced, implied, or not actually visible. ` +
  `Only include a character if you can name them with high confidence; if unsure, omit them.\n` +
  `Respond with JSON ONLY: {"characters":[{"name":"<full canonical English name>","confidence":<0-1>,"where":"foreground|background"}]}. ` +
  `Return {"characters":[]} if no other characters are clearly depicted.`

async function scanCard(card) {
  const primIds = primaryByCard.get(card.id) || new Set()
  const primaryNames = [...primIds].map((i) => idToName.get(i)).filter(Boolean)
  let lastErr
  for (let attempt = 0; attempt < 4; attempt++) {
    try {
      const msg = await client.messages.create({
        model: MODEL,
        max_tokens: 500,
        temperature: 0,
        system: SYSTEM,
        messages: [{
          role: 'user',
          content: [
            { type: 'image', source: { type: 'url', url: imageUrl(card.id) } },
            { type: 'text', text: promptFor(primaryNames) },
          ],
        }],
      })
      const text = msg.content.map((b) => (b.type === 'text' ? b.text : '')).join('')
      const m = text.match(/\{[\s\S]*\}/)
      const parsed = m ? JSON.parse(m[0]) : { characters: [] }
      const rows = []
      for (const d of parsed.characters || []) {
        if (typeof d?.confidence === 'number' && d.confidence < MIN_CONF) continue
        const cid = resolve(d.name)
        if (!cid) continue                 // not in roster
        if (primIds.has(cid)) continue     // that's the title character
        rows.push({ card_id: card.id, character_id: cid, name: idToName.get(cid), detected: d.name, confidence: d.confidence ?? null, where: d.where || null })
      }
      return rows
    } catch (e) {
      lastErr = e
      const status = e?.status || e?.response?.status
      if (status === 429 || (status >= 500 && status < 600)) { await sleep(1500 * (attempt + 1)); continue }
      throw e
    }
  }
  throw lastErr
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

// ---- run with a concurrency pool + periodic checkpoint ----
let idx = 0, processed = 0
async function worker() {
  while (idx < work.length) {
    const card = work[idx++]
    try {
      const rows = await scanCard(card)
      candidates.push(...rows)
      done.add(card.id)
      if (rows.length) console.log(`  ${card.id}: + ${rows.map((r) => r.name).join(', ')}`)
    } catch (e) {
      console.log(`  ${card.id}: ERROR ${e?.status || ''} ${e?.message || e}`)
    }
    if (++processed % 25 === 0) { await checkpoint(); console.log(`... ${processed}/${work.length} (${candidates.length} candidates)`) }
  }
}
async function checkpoint() {
  await mkdir(new URL('./', CHECKPOINT), { recursive: true })
  await writeFile(CHECKPOINT, JSON.stringify({ done: [...done], candidates }, null, 0))
}

await Promise.all(Array.from({ length: CONCURRENCY }, worker))
await checkpoint()

// ---- outputs ----
await writeFile(CANDIDATES, JSON.stringify(candidates, null, 2))
const uniq = new Map()
for (const r of candidates) uniq.set(`${r.card_id}|${r.character_id}`, r)
const sql = [
  '-- OPCanvs Secondary Cards — generated by scrape-secondary.mjs.',
  '-- SPOT-CHECK scrape_input/secondary_candidates.json before applying.',
  ...[...uniq.values()].sort((a, b) => a.card_id.localeCompare(b.card_id))
    .map((r) => `INSERT OR IGNORE INTO card_characters (card_id,character_id,role,match_method,confidence) VALUES ('${r.card_id.replace(/'/g, "''")}',${r.character_id},'secondary','vision',${r.confidence ?? 'NULL'});  -- ${r.name}`),
].join('\n')
await mkdir(OUT, { recursive: true })
await writeFile(new URL('secondary_scrape.sql', OUT), sql + '\n')

console.log(`\ndone. ${done.size} cards scanned, ${uniq.size} unique secondary pairs across ${new Set([...uniq.values()].map((r) => r.card_id)).size} cards.`)
console.log(`review: scripts/opcanvs/scrape_input/secondary_candidates.json`)
console.log(`SQL:    scripts/opcanvs/opcanvs_batches/secondary_scrape.sql`)
