---
name: init-deep
description: Use when generating hierarchical AGENTS.md knowledge base.
version: 1.1.0
author: Yuki
license: MIT
metadata:
  hermes:
    tags: [agents-md, documentation, project-knowledge, structure, code-map]
    category: software-development
    related_skills: [plan]
---

# /init-deep — Hierarchical AGENTS.md Generator

Generate AGENTS.md berjenjang: root (full treatment) + subdirectory yang complexity-scored.
Discovery jalan paralel (`delegate_task` + codegraph), bukan sequential.

## When to Use

- User bilang "init-deep", "generate AGENTS.md", "bikin knowledge base project".
- Project baru mau dibikin AGENTS.md berjenjang.
- Restructure dokumentasi project yang udah ada (mode update).

Jangan pakai untuk: ngerapihin AGENTS.md yang udah ada doang, atau bikin 1 file doang. Untuk
perapihan tanpa regenerate, edit file yang ada langsung pakai `patch`.

## Modes

```
Mode update       # Default: baca existing → modify + create baru yang warranted
Mode create-new   # --create-new: baca existing dulu (preserve context) → baru regenerate
```

## Prerequisites

- `delegate_task` tersedia (max **3 concurrent** — batas user ini; kalau butuh >3 explore task,
  jalanin batch ≤3 berurutan).
- `todo_list` dipakai buat tracking phase.
- CodeGraph MCP aktif (optional, SUPPLEMENT buat semantic search / blast radius). Kalau gak ada,
  fallback ke `search_files` + `read_file` manual.
- Repo target: cek dulu kalau `codegraph init` udah jalan (folder `.codegraph/`).
- Plugin `agents-md-injector` (optional): kalau enabled, AGENTS.md terdekat otomatis di-inject ke
  hasil `read_file` / `search_files` / `terminal`. File hasil generate langsung kepakai tanpa
  setup tambahan.

## Workflow

### Phase 1: Discovery + Analysis (PARALEL)

**1. Mark todo: `discovery` in_progress** via `todo_list`.

**2. Fire explore agents SEGERA** (jangan nunggu analisis manual) — batch ≤3 via `delegate_task`,
masing-masing fokus:

```
Batch 1:
- "Explore project structure: PREDICT standard patterns utk bahasa yang dideteksi → REPORT cuma deviations"
- "Find entry points: FIND main files → REPORT non-standard organization"
- "Find conventions: FIND config files (.eslintrc, pyproject.toml, .editorconfig, .config) → REPORT project-specific rules"

Batch 2 (kalau perlu):
- "Find anti-patterns: FIND 'DO NOT', 'NEVER', 'ALWAYS', 'DEPRECATED' comments → LIST forbidden patterns"
- "Explore build/CI: FIND .github/workflows, Makefile, build scripts → REPORT non-standard patterns"
- "Find test patterns: FIND test configs, test structure → REPORT unique conventions"
```

Kasih `context` yang self-contained: path repo absolut, bahasa utama, batasan (jangan sentuh
vendored dir), output format.

**3. Dynamic spawning** — ukur skala project dulu, spawn tambahan sesuai:

```bash
total_files=$(find . -type f -not -path '*/.git/*' -not -path '*/.venv/*' -not -path '*/node_modules/*' -not -path '*/build/*' -not -path '*/dist/*' -not -path '*/tmp/*' | wc -l)
total_lines=$(find . -type f \( -name "*.py" -o -name "*.ts" -o -name "*.js" -o -name "*.go" \) -not -path '*/.git/*' -not -path '*/.venv/*' -not -path '*/node_modules/*' -exec wc -l {} + 2>/dev/null | tail -1 | awk '{print $1}')
large_files=$(find . -type f \( -name "*.py" -o -name "*.ts" -o -name "*.js" \) -not -path '*/.git/*' -not -path '*/.venv/*' -exec wc -l {} + 2>/dev/null | awk '$1 > 500 {count++} END {print count+0}')
max_depth=$(find . -type d -not -path '*/.git/*' -not -path '*/.venv/*' -not -path '*/node_modules/*' -not -path '*/tmp/*' | awk -F/ '{print NF}' | sort -rn | head -1)
```

| Faktor | Threshold | Explore task tambahan |
|---|---|---|
| total_files > 100 | +1 per 100 files | "Large file analysis: FIND files >500 lines, REPORT complexity hotspots" |
| total_lines > 10k | +1 per 10k | "Deep modules: FIND hidden patterns di depth dalam" |
| max_depth ≥ 4 | +2 | "Cross-cutting: FIND shared utilities antar dir" |
| Monorepo | +1 per package/workspace | "Per-package conventions" |

**4. Main session (sambil nunggu delegate):**

- **Structural bash**:
  ```bash
  find . -type d -not -path '*/.git/*' -not -path '*/.venv/*' -not -path '*/node_modules/*' -not -path '*/tmp/*' | awk -F/ '{print NF-1}' | sort -n | uniq -c
  find . -type f -not -path '*/.git/*' -not -path '*/.venv/*' -not -path '*/node_modules/*' -not -path '*/build/*' -not -path '*/dist/*' | sed 's|/[^/]*$||' | sort | uniq -c | sort -rn | head -30
  ```
- **Baca existing**: `find . -type f \( -name "AGENTS.md" -o -name "CLAUDE.md" \)` → `read_file`
  masing-masing → simpan ke `EXISTING_AGENTS` map (key insights, conventions, anti-patterns).
  Mode `--create-new`: baca SEMUA dulu, terus hapus, baru regenerate.
- **Codegraph explore** (parallel): entry points (`main.py`, `app.py`, `src/index.ts`), key symbols
  (`class`, `interface`, `function`), centrality top exports — pakai
  `mcp__codegraph__codegraph_explore(query=..., projectPath=<repo absolut>)`.

**5. Collect hasil delegate_task** (balik sendiri). Merge: bash + codegraph + existing + explore
findings. **Mark `discovery` completed.**

### Phase 2: Scoring & Location Decision

**Mark `scoring` in_progress.**

| Faktor | Weight | High threshold | Source |
|---|---|---|---|
| File count | 3x | >20 | bash |
| Subdir count | 2x | >5 | bash |
| Code ratio | 2x | >70% | bash |
| Unique patterns | 1x | punya config sendiri | explore |
| Module boundary | 2x | ada index.ts/__init__.py | bash |
| Symbol density | 2x | >30 symbols | codegraph |
| Export count | 2x | >10 exports | codegraph |
| Reference centrality | 3x | >20 refs | codegraph |

Decision:

| Score | Aksi |
|---|---|
| Root (.) | **ALWAYS** create |
| >15 | Create AGENTS.md |
| 8-15 | Create kalau domain distinct |
| <8 | Skip (parent covers) |

Output: `AGENTS_LOCATIONS = [{path: ".", type: "root"}, {path: "src/hooks", score: 18, reason: "high complexity"}, ...]`. **Mark `scoring` completed.**

### Phase 3: Generate

**Mark `generate` in_progress.**

**⚠️ File Writing Rule**: AGENTS.md udah ada di target → `read_file` + `patch`, JANGAN `write_file`
nimpa existing. File baru → `write_file`.

**Root AGENTS.md (full treatment)** — template:

```markdown
# {PROJECT} KNOWLEDGE BASE

**Generated:** {TIMESTAMP}
**Commit:** {SHORT_SHA}
**Branch:** {BRANCH}

## OVERVIEW
{1-2 kalimat: apa + stack inti}

## STRUCTURE
{root}/
├── {dir}/    # {purpose non-obvious aja}
└── {entry}

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|

## CODE MAP
{skip kalau project <10 files}

| Symbol | Type | Location | Refs | Role |
|--------|------|----------|------|------|

## CONVENTIONS
{ONLY deviations dari standar bahasa/framework}

## ANTI-PATTERNS (THIS PROJECT)
{Explicitly forbidden di sini}

## UNIQUE STYLES
{Project-specific}

## COMMANDS
```bash
{dev/test/build}
```

## NOTES
{Gotchas}
```

**Quality gates root**: 50-150 lines, no generic advice, no obvious info, semua angka/commit real
(jangan fabricated).

**Subdirectory AGENTS.md** — generate per lokasi (30-80 lines):

```markdown
# {DIR} KNOWLEDGE BASE

## OVERVIEW
{1 line}

## STRUCTURE
{hanya kalau >5 subdir}

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|

## CONVENTIONS
{hanya kalau beda dari parent}

## ANTI-PATTERNS
{hanya kalau ada}
```

**JANGAN pernah ngulang konten parent** — subdir cuma beda-nya. **Mark `generate` completed.**

### Phase 4: Review & Dedup

**Mark `review` in_progress.**

Per file generated:
- Buang generic advice (yang berlaku di semua project)
- Buang duplikat parent
- Trim ke batas ukuran
- Verifikasi gaya telegraphic (padat, bukan prosa)
- Cek cross-ref: kalau root nyebut `see services/AGENTS.md`, file itu harus beneran ada

**Mark `review` completed.**

## Ramah agents-md-injector

Kalau plugin `agents-md-injector` enabled, tiap file yang di-generate bakal di-inject ke konteks
saat agent baca file di bawah direktori itu (nearest first, sekali per session). Konsekuensinya:

- **Root jangan lebih dari ~150 lines.** Injector punya cap default 32 KB per file dan 64 KB total
  per tool result; file kegedean ke-truncate dan aturan penting bisa hilang di tail.
- **Tulis aturan yang berdiri sendiri per direktori.** Subdir file tetap kebaca walau root nggak
  ke-inject (misal target di luar repo root walk).
- **Jangan taruh secret** (`.env`, key, token) di AGENTS.md — file ini masuk konteks model apa adanya.
- Nama file default `AGENTS.md` (bisa `agents.md`); kalau butuh `CLAUDE.md`, set
  `AGENTS_MD_INJECTOR_FILENAMES=AGENTS.md,CLAUDE.md`.

## Final Report

```
=== init-deep Complete ===

Mode: update | create-new

Files:
  [OK] ./AGENTS.md (root, N lines)
  [OK] ./src/hooks/AGENTS.md (N lines)

Dirs Analyzed: N
AGENTS.md Created: N
AGENTS.md Updated: N

Hierarchy:
  ./AGENTS.md
  └── src/hooks/AGENTS.md
```

## Common Pitfalls

1. **Static agent count** — jumlah explore agent harus ngikut skala project (dynamic spawning), jangan selalu 6.
2. **Sequential execution** — explore agents + codegraph + bash WAJIB paralel. Jangan nunggu satu-satu.
3. **Nimpuk existing file** — selalu cek dulu (`find`), existing → `patch`, bukan `write_file`.
4. **Over-documenting** — gak semua dir butuh AGENTS.md. Score <8 → skip.
5. **Redundancy** — child gak boleh ngulang parent.
6. **Generic content** — "be careful", "use best practices" = sampah. Buang.
7. **Fabricated numbers** — jangan nulis commit SHA / line count / symbol yang gak dicek beneran. Cek via git + codegraph dulu.
8. **Lupa existing context di mode create-new** — baca existing DULU, baru delete & regenerate.
9. **delegate_task max 3 concurrent** — kalau butuh >3 explore task, batch ≤3, tunggu hasil, lanjut batch berikutnya.
10. **Codegraph query salah project** — kasih `projectPath` eksplisit kalau lagi di repo lain.

## Verification Checklist

- [ ] `todo_list` dipakai: discovery → scoring → generate → review, semua marked completed
- [ ] Explore agents di-fire duluan (paralel), hasilnya ke-merge
- [ ] Existing AGENTS.md ke-baca sebelum ditulis (mode update & create-new)
- [ ] Scoring matrix dipakai buat mutusin lokasi (bukan asal pilih)
- [ ] Root: 50-150 lines, semua section sesuai template, angka real
- [ ] Subdir: 30-80 lines, gak ngulang parent
- [ ] Gak ada generic advice / no-op prose
- [ ] Cross-ref antar AGENTS.md valid (file target beneran ada)
- [ ] Final report format `=== init-deep Complete ===` dikasih ke user
