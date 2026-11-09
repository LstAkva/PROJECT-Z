---
version: 1.0.0
name: ZakoWhat
description: Distinctive Uzbek-language intellectual competition platform inspired by Zakovat traditions, classical typography, and contemplative game aesthetics.
colors:
  ground: "#0A080C"
  ground-void: "#070509"
  surface: "#100D14"
  surface-elevated: "#17131C"
  ink: "#FAF7F2"
  ink-muted: "#BDB4AE"
  ink-dim: "#7C727A"
  burgundy-deep: "#3B0A12"
  burgundy-accent: "#570F1C"
  burgundy-hover: "#7A1627"
  brass-accent: "#C5A059"
  brass-muted: "#7A602B"
  border-subtle: "rgba(197, 160, 89, 0.14)"
  border-medium: "rgba(197, 160, 89, 0.28)"
typography:
  display:
    fontFamily: "'Cinzel', 'Trajan Pro', Georgia, serif"
    fontWeight: 700
  body:
    fontFamily: "'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif"
    fontWeight: 400
  mono:
    fontFamily: "'JetBrains Mono', 'SF Mono', Consolas, monospace"
    fontWeight: 500
rounded:
  xs: "2px"
  sm: "4px"
  md: "6px"
  lg: "8px"
  pill: "9999px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "16px"
  lg: "24px"
  xl: "32px"
  xxl: "48px"
---

# ZakoWhat Design System Specification

## 1. Overview & Cultural Foundation

ZakoWhat is a dedicated, intellectual competition platform designed specifically for Uzbek players, thinkers, and Zakovat enthusiasts. It is not an enterprise analytics SaaS dashboard, a tech-startup landing page, or a casual gamified trivia app.

The aesthetic draws inspiration from:
- Classical intellectual assemblies and the austere, focused atmosphere of tournament halls.
- Architectural heritage of Samarkand and Bukhara: geometric order, proportion, restrained materials (obsidian stone, rich deep burgundy velvet/felt, and subtle hand-tooled brass accents).
- High-legibility typography optimized for complex Uzbek Latin texts (apostrophes, `o‘`, `g‘`, `sh`, `ch`).

---

## 2. Color Palette & Roles

The palette is strictly restrained to four tonal layers:

| Token | Hex / Value | Role | Contrast / Usage |
|---|---|---|---|
| `--bg-void` | `#070509` | Deepest base canvas | Root backdrop |
| `--bg-app` | `#0A080C` | Dominant ground | Main application background |
| `--bg-surface` | `#100D14` | Primary content surface | Screen panels, catalog list containers, modals |
| `--bg-surface-elevated` | `#17131C` | Active/elevated elements | Item hover, focused inputs, selection states |
| `--text-main` | `#FAF7F2` | Alabaster ink | Headings, primary question text, active buttons (> 14:1) |
| `--text-muted` | `#BDB4AE` | Parchment silver | Descriptions, metadata, secondary copy (> 7:1) |
| `--text-dim` | `#7C727A` | Slate ash | Structural labels, timestamps, counters (> 4.5:1) |
| `--accent-burgundy` | `#570F1C` | Intellectual signal | Primary action buttons, active tabs, round badges |
| `--accent-brass` | `#C5A059` | Cultural heritage accent | Girih insignia, subtle borders, brand emblems |
| `--border-subtle` | `rgba(197, 160, 89, 0.14)` | Fine division lines | Card separators, section dividers |
| `--border-medium` | `rgba(197, 160, 89, 0.28)` | Defined perimeter | Interactive borders, focus states |

---

## 3. Typography System

1. **Display Face — Cinzel / Serif**:
   - Reserved strictly for top-level brand marks, primary tournament/pack titles, and ceremonial headers.
   - Never used for body paragraphs, buttons, or form controls.
2. **Interface & Reading Face — Plus Jakarta Sans**:
   - Highly legible, neutral, contemporary sans-serif with excellent diacritic and punctuation support for Uzbek Latin.
   - Comfortable line-height (`1.5` to `1.6`) for reading challenging intellectual questions without fatigue.
3. **Monospace Face — JetBrains Mono**:
   - Dedicated strictly to numerical data, timers (`01:30`), question counters (`Savol 04 / 12`), and scores.
   - Never used for decorative all-caps labels or UI eyebrows.

---

## 4. Layout & Spacing Rules

- **Rhythm**: Modular 8px spacing scale (`4px`, `8px`, `16px`, `24px`, `32px`, `48px`).
- **Surface Integrity**: Avoid nested "card-inside-card-inside-card" wrappers. The page is treated as an intentional architectural sheet with calm, fine hairline dividers (`--border-subtle`).
- **Corner Radii**: Restrained architectural corners (`4px` to `8px`). Never use oversized rounded pill corners (`rounded-2xl`, `rounded-3xl`) for large containers.
- **Elevation**: Surfaces do not float with exaggerated blurry shadows or colorful ambient glows. Depth is created through tonal background value shifts (`#0A080C` -> `#100D14` -> `#17131C`) and crisp 1px borders.

---

## 5. Component Guidelines

### 5.1 Arena Catalog (Curated Tournament Library)
- **Library Concept**: The catalog is presented as an authoritative index of intellectual competitions, not an e-commerce grid or casual card stack.
- **Pack Entry Structure**:
  1. **Header / Identity**: Pack title in prominent, crisp type, accompanied by clean status badges (Turkum / Format).
  2. **Editorial Summary**: Concise synopsis of the pack's subject matter.
  3. **Technical Specs**: Clean metadata row showing structured facts: *2 tur • 24 savol • ~30 daqiqa* without cartoonish emojis.
  4. **Peshqadam (Tournament Leader)**: Displayed as an understated record attribution, integrated into the pack entry rather than as a floating badge.
  5. **Actions**: Clear visual priority. Primary action is *O‘ynash* (or *Davom etish* when active session exists); secondary actions are *Batafsil* and *Reyting*.

### 5.2 Forms & Modals (Auth, Settings, Details)
- High contrast, dark-table modal backdrops (`rgba(0, 0, 0, 0.85)`).
- Clean, focused inputs with subtle 1px brass focus borders.
- Action buttons:
  - `.btn-primary`: Deep burgundy background with alabaster text; subtle hover brightness.
  - `.btn-secondary`: Subtle transparent/slate background with fine 1px brass border.

---

## 6. Anti-AI-Slop Rules & Refused Defaults

This design system deliberately prohibits:

1. **No Gratuitous Emojis**: Do not use smartphone emojis (`🏛️`, `⭐`, `📋`, `👑`, `🎯`, `❓`, `⏱️`, `🔒`, `🛡️`, `✨`, `👤`) as primary UI icons or section headings. Use clean text labels or crisp SVGs.
2. **No Purple/Indigo Gradients & Rainbow Glows**: No `linear-gradient(135deg, #6366f1, #a855f7)` or blurred backdrop blobs.
3. **No Repetitive 3-Card Feature Slop**: Screens are laid out specifically for their functional task, not forced into standard 3-column card grids.
4. **No Artificial Top-Card Glow Bars**: Avoid pseudo-elements with `linear-gradient(90deg, transparent, brass, transparent)` stuck on top of cards.
5. **No Tracked-Out All-Caps Eyebrow Spam**: Do not prepend every title with `UPPERCASE TRACKED EYEBROWS`. Use sentence case, informative headings.
6. **No "Puffy" Nested Cards**: Avoid stacking rounded containers inside rounded containers with soft drop-shadows.
7. **No Decorative "01 / 02 / 03"**: Do not number items unless they represent an actual sequential process.
8. **No Generic One-Word Headline Accents**: Do not highlight a single random word with italics or color gradients.

---

## 7. Responsive Behavior

- **Mobile (< 640px)**:
  - Single unified column layout.
  - Generous tap targets (minimum 44px height for interactive controls).
  - Compact horizontal metadata rows that wrap naturally without overflow.
  - Sticky or accessible navigation without obstructing question text.
- **Tablet / Desktop (641px - 1024px, 1025px+)**:
  - Max container width constrained to `1120px` for optimal reading line-length.
  - Distinct side-by-side or clean architectural ledger layouts for catalog items.
